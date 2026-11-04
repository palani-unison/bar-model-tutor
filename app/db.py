"""Personalisation store: parents, their children's profiles, how each child does per problem type, and
the signed-in child's own lesson history ("My lessons").

SQLite from the standard library. What is stored is listed in docs/privacy.md: never photos or audio; a child
is a nickname, a level, an avatar, a hero name and a salted password hash. The only question text kept is a
signed-in child's own checked lessons (the `lessons` table), so they can replay them; deleting the child or the
account deletes them.

Where the file lives:
  DB_PATH          the durable database (default data/app.db).
  DB_LOCAL_COPY    optional (set on Modal): work on this local file instead, restore it from DB_PATH at
                   start-up, and snapshot it back to DB_PATH a few seconds after each write (debounced) and at
                   shutdown. A snapshot is a consistent SQLite backup written to a temp file and renamed into
                   place, then the persist hook runs (Modal: `volume.commit()`), so a Volume commit never
                   catches a half-written database and SQLite never runs its locking on the Volume.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

logger = logging.getLogger("bar_model_tutor.db")

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DELAY_S = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS parents (
    id INTEGER PRIMARY KEY,
    google_sub TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS children (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
    nickname TEXT NOT NULL,
    level TEXT NOT NULL,
    avatar TEXT NOT NULL,
    code_name TEXT NOT NULL UNIQUE,
    created REAL NOT NULL,
    pw_hash TEXT,
    pw_ver INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS children_parent ON children(parent_id);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    heuristic TEXT NOT NULL,
    level TEXT NOT NULL,
    checkpoint_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    seconds REAL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_child ON attempts(child_id, heuristic);
CREATE TABLE IF NOT EXISTS mastery (
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    heuristic TEXT NOT NULL,
    rating REAL NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    avg_seconds REAL,
    updated REAL NOT NULL,
    PRIMARY KEY (child_id, heuristic)
);
CREATE TABLE IF NOT EXISTS badges (
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    badge TEXT NOT NULL,
    earned_at REAL NOT NULL,
    PRIMARY KEY (child_id, badge)
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    child_id INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    spec TEXT NOT NULL,
    solved TEXT,
    title TEXT NOT NULL DEFAULT '',
    heuristic TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT '',
    thumb TEXT NOT NULL DEFAULT '[]',
    steps INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unfinished',
    runs INTEGER NOT NULL DEFAULT 1,
    solved_on INTEGER,
    hints INTEGER NOT NULL DEFAULT 0,
    seconds REAL,
    progress REAL NOT NULL DEFAULT 0,
    kept INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS lessons_child ON lessons(child_id, updated);
-- Admin observability (app/admin.py): one row per new problem planned. The question text is kept only when
-- the tutor could not do it (outcome "declined" or "error"), so the owner can improve the tutor.
CREATE TABLE IF NOT EXISTS plan_events (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    parent_id INTEGER REFERENCES parents(id) ON DELETE CASCADE,
    child_id INTEGER REFERENCES children(id) ON DELETE CASCADE,
    source TEXT NOT NULL DEFAULT 'lesson',
    outcome TEXT NOT NULL,
    heuristic TEXT NOT NULL DEFAULT '',
    planner TEXT NOT NULL DEFAULT '',
    seconds REAL,
    question TEXT,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS plan_events_created ON plan_events(created);
-- "Report a problem": a parent or child flags a question for the owner to look at.
CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY,
    created REAL NOT NULL,
    parent_id INTEGER REFERENCES parents(id) ON DELETE CASCADE,
    child_id INTEGER REFERENCES children(id) ON DELETE CASCADE,
    lesson_id INTEGER,
    reason TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    spec TEXT,
    answer TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    admin_note TEXT NOT NULL DEFAULT '',
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS flags_status ON flags(status, created);
"""

_lock = threading.RLock()
_ready: str | None = None  # the path the schema was created at
_persist_hook: Callable[[], None] | None = None
_timer: threading.Timer | None = None


def durable_path() -> Path:
    return Path(os.environ.get("DB_PATH") or ROOT / "data" / "app.db")


def working_path() -> Path:
    local = os.environ.get("DB_LOCAL_COPY")
    return Path(local) if local else durable_path()


def set_persist_hook(hook: Callable[[], None] | None) -> None:
    """Called after each snapshot reaches DB_PATH (Modal: commit the Volume)."""
    global _persist_hook
    _persist_hook = hook


def _init(path: Path) -> None:
    global _ready
    path.parent.mkdir(parents=True, exist_ok=True)
    durable = durable_path()
    if path != durable and not path.exists() and durable.exists():
        shutil.copyfile(durable, path)  # restore the last snapshot (a new container after a redeploy)
        logger.info("restored database from %s", durable)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        cols = {r[1] for r in con.execute("PRAGMA table_info(children)")}
        for col, decl in (("pw_hash", "TEXT"), ("pw_ver", "INTEGER NOT NULL DEFAULT 0")):  # databases made before kid passwords
            if col not in cols:
                con.execute(f"ALTER TABLE children ADD COLUMN {col} {decl}")
        pcols = {r[1] for r in con.execute("PRAGMA table_info(parents)")}
        for col, decl in (("consent_version", "TEXT"), ("consented", "REAL")):  # databases made before consent
            if col not in pcols:
                con.execute(f"ALTER TABLE parents ADD COLUMN {col} {decl}")
        con.commit()
    finally:
        con.close()
    _ready = str(path)


@contextmanager
def connect(write: bool = False) -> Iterator[sqlite3.Connection]:
    """One short-lived connection; writes are serialised (one container, low traffic)."""
    path = working_path()
    with _lock:
        if _ready != str(path):
            _init(path)
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            if write:
                con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    if write:
        schedule_snapshot()


# ---------------------------------------------------------------- persistence (Modal Volume)
def schedule_snapshot(delay: float = SNAPSHOT_DELAY_S) -> None:
    """Debounced: many writes in a burst become one snapshot and one Volume commit."""
    global _timer
    if working_path() == durable_path():
        return
    with _lock:
        if _timer is not None and _timer.is_alive():
            return
        _timer = threading.Timer(delay, snapshot)
        _timer.daemon = True
        _timer.start()


def snapshot() -> bool:
    """Copy the working database to DB_PATH (consistent backup, atomic rename), then run the persist hook."""
    global _timer
    src, dst = working_path(), durable_path()
    if src == dst or not src.exists():
        return False
    with _lock:
        _timer = None
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        source, target = sqlite3.connect(src), sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
        hook = _persist_hook
    if hook is not None:
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 — Modal also commits in the background and at shutdown
            logger.warning("persist hook failed: %s", exc)
    return True


def flush() -> None:
    """At shutdown: cancel the pending timer and snapshot now."""
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
            _timer = None
    try:
        snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.warning("final snapshot failed: %s", exc)


# ---------------------------------------------------------------- parents
def upsert_parent(google_sub: str, display_name: str) -> dict:
    """The name is only the first one Google gives; after that it is the name the parent chose (onboarding)."""
    with connect(write=True) as con:
        con.execute("INSERT INTO parents (google_sub, display_name, created) VALUES (?, ?, ?) "
                    "ON CONFLICT(google_sub) DO UPDATE SET "
                    "display_name = COALESCE(NULLIF(parents.display_name, ''), excluded.display_name)",
                    (google_sub, display_name[:40], time.time()))
        row = con.execute("SELECT id, display_name FROM parents WHERE google_sub = ?", (google_sub,)).fetchone()
    return dict(row)


def get_parent(parent_id: int) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT id, display_name, consent_version, consented FROM parents WHERE id = ?",
                          (parent_id,)).fetchone()
    return dict(row) if row else None


def set_consent(parent_id: int, version: str) -> dict | None:
    """The parent agreed to the terms (docs/terms.md) of this version, now."""
    with connect(write=True) as con:
        con.execute("UPDATE parents SET consent_version = ?, consented = ? WHERE id = ?",
                    (version[:20], time.time(), parent_id))
    return get_parent(parent_id)


def set_parent_name(parent_id: int, display_name: str) -> dict | None:
    with connect(write=True) as con:
        con.execute("UPDATE parents SET display_name = ? WHERE id = ?", (display_name[:40], parent_id))
    return get_parent(parent_id)


def delete_parent(parent_id: int) -> bool:
    """The parent's account and every child, attempt, mastery and badge row under it (cascade)."""
    with connect(write=True) as con:
        return con.execute("DELETE FROM parents WHERE id = ?", (parent_id,)).rowcount > 0


# ---------------------------------------------------------------- children
_CHILD_COLS = "id, parent_id, nickname, level, avatar, code_name, created, pw_ver, (pw_hash IS NOT NULL) AS has_password"


def add_child(parent_id: int, nickname: str, level: str, avatar: str, code_name: str) -> dict:
    with connect(write=True) as con:
        cur = con.execute("INSERT INTO children (parent_id, nickname, level, avatar, code_name, created) VALUES (?, ?, ?, ?, ?, ?)",
                          (parent_id, nickname, level, avatar, code_name, time.time()))
        row = con.execute(f"SELECT {_CHILD_COLS} FROM children WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def code_name_taken(code_name: str) -> bool:
    with connect() as con:
        return con.execute("SELECT 1 FROM children WHERE code_name = ? COLLATE NOCASE", (code_name,)).fetchone() is not None


def list_children(parent_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute(f"SELECT {_CHILD_COLS} FROM children WHERE parent_id = ? ORDER BY id", (parent_id,)).fetchall()
    return [dict(r) for r in rows]


def get_child(child_id: int, parent_id: int | None = None) -> dict | None:
    """A child, optionally only if it belongs to `parent_id`."""
    sql, args = f"SELECT {_CHILD_COLS} FROM children WHERE id = ?", [child_id]
    if parent_id is not None:
        sql, args = sql + " AND parent_id = ?", args + [parent_id]
    with connect() as con:
        row = con.execute(sql, args).fetchone()
    return dict(row) if row else None


def child_by_code(code_name: str, parent_id: int) -> dict | None:
    """Case-insensitive: "thor987", "Thor987" and the older "brave-otter-42" style all match."""
    with connect() as con:
        row = con.execute(f"SELECT {_CHILD_COLS} FROM children WHERE code_name = ? COLLATE NOCASE AND parent_id = ?",
                          (code_name, parent_id)).fetchone()
    return dict(row) if row else None


def update_child(child_id: int, parent_id: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in ("nickname", "level", "avatar") and v is not None}
    if allowed:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        with connect(write=True) as con:
            con.execute(f"UPDATE children SET {sets} WHERE id = ? AND parent_id = ?", (*allowed.values(), child_id, parent_id))
    return get_child(child_id, parent_id)


def delete_child(child_id: int, parent_id: int) -> bool:
    """The child's profile and all their attempts, mastery and badges (cascade)."""
    with connect(write=True) as con:
        return con.execute("DELETE FROM children WHERE id = ? AND parent_id = ?", (child_id, parent_id)).rowcount > 0


# ---------------------------------------------------------------- kid password (app/accounts.py)
# Only a salted scrypt hash ("scrypt$<salt hex>$<hash hex>") is stored. pw_ver goes up on every set/reset and is
# carried in the child's signed session, so a reset signs the child out everywhere.
def child_password(child_id: int) -> tuple[str | None, int] | None:
    """(pw_hash, pw_ver) for a child, or None if there's no such child."""
    with connect() as con:
        row = con.execute("SELECT pw_hash, pw_ver FROM children WHERE id = ?", (child_id,)).fetchone()
    return (row["pw_hash"], row["pw_ver"]) if row else None


def set_child_password(child_id: int, parent_id: int, pw_hash: str) -> bool:
    with connect(write=True) as con:
        return con.execute("UPDATE children SET pw_hash = ?, pw_ver = pw_ver + 1 WHERE id = ? AND parent_id = ?",
                           (pw_hash, child_id, parent_id)).rowcount > 0


def child_by_hero(code_name: str) -> dict | None:
    """Any family's child by hero name (case-insensitive), for the password sign-in."""
    with connect() as con:
        row = con.execute(f"SELECT {_CHILD_COLS} FROM children WHERE code_name = ? COLLATE NOCASE", (code_name,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- attempts and mastery
def add_attempts(child_id: int, heuristic: str, level: str, rows: list[tuple[str, str, float | None]]) -> None:
    """rows: (checkpoint_key, outcome, seconds)."""
    now = time.time()
    with connect(write=True) as con:
        con.executemany("INSERT INTO attempts (child_id, heuristic, level, checkpoint_key, outcome, seconds, created) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(child_id, heuristic, level, k, o, s, now) for k, o, s in rows])


def attempts(child_id: int, heuristic: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT checkpoint_key, outcome, seconds, heuristic, level, created FROM attempts WHERE child_id = ?", [child_id]
    if heuristic is not None:
        sql, args = sql + " AND heuristic = ?", args + [heuristic]
    with connect() as con:
        rows = con.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def get_mastery(child_id: int, heuristic: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT heuristic, rating, n, avg_seconds, updated FROM mastery WHERE child_id = ? AND heuristic = ?",
                          (child_id, heuristic)).fetchone()
    return dict(row) if row else None


def all_mastery(child_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT heuristic, rating, n, avg_seconds, updated FROM mastery WHERE child_id = ? ORDER BY heuristic",
                           (child_id,)).fetchall()
    return [dict(r) for r in rows]


def set_mastery(child_id: int, heuristic: str, rating: float, n: int, avg_seconds: float | None) -> None:
    with connect(write=True) as con:
        con.execute("INSERT INTO mastery (child_id, heuristic, rating, n, avg_seconds, updated) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(child_id, heuristic) DO UPDATE SET rating = excluded.rating, n = excluded.n, "
                    "avg_seconds = excluded.avg_seconds, updated = excluded.updated",
                    (child_id, heuristic, rating, n, avg_seconds, time.time()))


# ---------------------------------------------------------------- method badges (agent/badges.py)
def add_badge(child_id: int, badge: str) -> bool:
    """Record a badge once. True if it is new for this child."""
    with connect(write=True) as con:
        return con.execute("INSERT OR IGNORE INTO badges (child_id, badge, earned_at) VALUES (?, ?, ?)",
                           (child_id, badge[:40], time.time())).rowcount > 0


def list_badges(child_id: int) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT badge, earned_at FROM badges WHERE child_id = ? ORDER BY earned_at, badge",
                           (child_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- "My lessons" (app/lessons.py, app/lesson_ws.py)
# Every query takes the child id, so a lesson id alone never reaches another child's row.
MAX_LESSONS = 200  # per child; the oldest lessons that aren't kept go first
STATUSES = ("unfinished", "solved", "hints", "redo")
_LESSON_COLS = ("id, title, heuristic, level, thumb, steps, status, runs, solved_on, hints, seconds, progress, kept, "
                "created, updated")


def add_lesson(child_id: int, spec_json: str, solved_json: str | None, title: str, heuristic: str, level: str,
               thumb_json: str, steps: int) -> int:
    now = time.time()
    with connect(write=True) as con:
        cur = con.execute("INSERT INTO lessons (child_id, spec, solved, title, heuristic, level, thumb, steps, created, updated) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (child_id, spec_json, solved_json, title[:80], heuristic[:40], level[:8], thumb_json, steps, now, now))
        con.execute("DELETE FROM lessons WHERE child_id = ? AND kept = 0 AND id NOT IN "
                    "(SELECT id FROM lessons WHERE child_id = ? ORDER BY updated DESC, id DESC LIMIT ?)",
                    (child_id, child_id, MAX_LESSONS))
    return cur.lastrowid


def get_lesson(lesson_id: int, child_id: int, with_spec: bool = False) -> dict | None:
    cols = _LESSON_COLS + (", spec, solved" if with_spec else "")
    with connect() as con:
        row = con.execute(f"SELECT {cols} FROM lessons WHERE id = ? AND child_id = ?", (lesson_id, child_id)).fetchone()
    return dict(row) if row else None


def list_lessons(child_id: int, which: str = "all", heuristic: str | None = None, limit: int = MAX_LESSONS) -> list[dict]:
    """Newest first. which: "all", "kept" or "redo" (unfinished, or a step had to be shown)."""
    sql, args = f"SELECT {_LESSON_COLS} FROM lessons WHERE child_id = ?", [child_id]
    if which == "kept":
        sql += " AND kept = 1"
    elif which == "redo":
        sql += " AND status IN ('unfinished', 'redo')"
    if heuristic:
        sql, args = sql + " AND heuristic = ?", args + [heuristic]
    with connect() as con:
        rows = con.execute(sql + " ORDER BY updated DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def lesson_heuristics(child_id: int) -> list[str]:
    with connect() as con:
        rows = con.execute("SELECT heuristic, MAX(updated) AS t FROM lessons WHERE child_id = ? GROUP BY heuristic "
                           "ORDER BY t DESC", (child_id,)).fetchall()
    return [r["heuristic"] for r in rows]


def count_lessons(child_id: int) -> int:
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM lessons WHERE child_id = ?", (child_id,)).fetchone()[0]


def start_lesson_run(lesson_id: int, child_id: int) -> bool:
    """A replay of a saved lesson: one more run, back to the start. Its status stays until the run ends."""
    with connect(write=True) as con:
        return con.execute("UPDATE lessons SET runs = runs + 1, progress = 0, updated = ? WHERE id = ? AND child_id = ?",
                           (time.time(), lesson_id, child_id)).rowcount > 0


def set_lesson_progress(lesson_id: int, child_id: int, progress: float) -> None:
    with connect(write=True) as con:
        con.execute("UPDATE lessons SET progress = ?, updated = ? WHERE id = ? AND child_id = ?",
                    (max(0.0, min(1.0, progress)), time.time(), lesson_id, child_id))


def finish_lesson_run(lesson_id: int, child_id: int, status: str, hints: int, seconds: float | None) -> None:
    """The run reached the end: its outcome replaces the last one (solved_on is the run it was solved on)."""
    if status not in STATUSES:
        raise ValueError(f"unknown lesson status {status!r}")
    solved = status in ("solved", "hints")
    with connect(write=True) as con:
        con.execute("UPDATE lessons SET status = ?, hints = ?, seconds = ?, progress = 1, updated = ?, "
                    "solved_on = CASE WHEN ? THEN runs ELSE solved_on END WHERE id = ? AND child_id = ?",
                    (status, hints, seconds, time.time(), solved, lesson_id, child_id))


def set_lesson_kept(lesson_id: int, child_id: int, kept: bool) -> bool:
    with connect(write=True) as con:
        return con.execute("UPDATE lessons SET kept = ? WHERE id = ? AND child_id = ?",
                           (1 if kept else 0, lesson_id, child_id)).rowcount > 0


def stars_won(child_id: int) -> int:
    """Checkpoints the child worked out (the stars a lesson lights up), from the attempts above."""
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM attempts WHERE child_id = ? AND outcome IN ('first try', 'with hints', 'early')",
                           (child_id,)).fetchone()[0]


def lesson_times(child_id: int, limit: int = 400) -> list[float]:
    """When the child's recent lessons were last worked on (for the day streak)."""
    with connect() as con:
        rows = con.execute("SELECT updated FROM lessons WHERE child_id = ? ORDER BY updated DESC LIMIT ?",
                           (child_id, limit)).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- admin: plan events and flags (app/admin.py)
def add_plan_event(parent_id: int | None, child_id: int | None, outcome: str, *, source: str = "lesson",
                   heuristic: str = "", planner: str = "", seconds: float | None = None,
                   question: str | None = None, detail: str = "") -> None:
    keep = question if outcome != "ok" else None  # a solved question isn't kept here (a child's lesson keeps it)
    with connect(write=True) as con:
        con.execute("INSERT INTO plan_events (created, parent_id, child_id, source, outcome, heuristic, planner, "
                    "seconds, question, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), parent_id, child_id, source[:12], outcome[:12], heuristic[:40], planner[:20],
                     seconds, (keep or "")[:3000] or None, detail[:400]))


def add_flag(parent_id: int | None, child_id: int | None, lesson_id: int | None, reason: str, note: str,
             question: str, spec_json: str | None, answer: str) -> int:
    now = time.time()
    with connect(write=True) as con:
        cur = con.execute("INSERT INTO flags (created, parent_id, child_id, lesson_id, reason, note, question, spec, "
                          "answer, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (now, parent_id, child_id, lesson_id, reason[:30], note[:1000], question[:3000],
                           spec_json, answer[:60], now))
    return cur.lastrowid


def set_flag_status(flag_id: int, status: str, admin_note: str | None = None) -> bool:
    with connect(write=True) as con:
        if admin_note is None:
            cur = con.execute("UPDATE flags SET status = ?, updated = ? WHERE id = ?", (status, time.time(), flag_id))
        else:
            cur = con.execute("UPDATE flags SET status = ?, admin_note = ?, updated = ? WHERE id = ?",
                              (status, admin_note[:1000], time.time(), flag_id))
    return cur.rowcount > 0


def admin_query(sql: str, args: tuple = ()) -> list[dict]:
    """Read-only queries for the admin dashboard (fixed SQL in app/admin.py, never user text)."""
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
