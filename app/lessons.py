""""My lessons": a signed-in child's own lesson history, and replaying one from its saved spec.

A replay is not a video (videos expire, see VIDEO_TTL_S in app/main.py): the lesson socket runs the live lesson
again from the stored, already verified spec, so the pause-and-predict checkpoints come back and nothing is
re-planned. Strictly private: a child only ever sees their own rows (every query is by child id), a parent may
list their own children's; nobody else's, no likes, followers or comments. Anonymous lessons store nothing.

GET  /api/lessons?which=all|kept|redo&family=<heuristic>&tz=<minutes>
     the child's lessons (newest first, an unfinished one pinned first), topic circles and profile stats.
     A parent may pass ?child_id= for one of their own children.
GET  /api/lessons/{id}          one lesson with its spec and solved values, to replay (404 if not the child's)
POST /api/lessons/{id}/keep     {"kept": bool}
/lessons-assets/lessons.js      the grid and the cinema replay (index.html loads it)
"""
from __future__ import annotations

import json
import logging
import time

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from agent import learner
from app import db
from app.accounts import _parent_id, _public_child, session_of

logger = logging.getLogger("bar_model_tutor.lessons")
router = APIRouter()
STATIC_JS = Path(__file__).resolve().parent / "static" / "lessons.js"
MASTERED = 2  # learner.LEVELS index: "Strong" and up rings the topic circle amber


# ---------------------------------------------------------------- recording (called by app/lesson_ws.py)
def _thumb(spec: dict, solved: dict | None) -> list[float]:
    """Up to four bar lengths (0..1) from the first stage, for the tile's mini sketch: cheap and question-free."""
    try:
        if spec.get("version") == 2:
            stages = spec.get("stages") or []
            bars = (stages[0].get("bars") or []) if stages else []
            values = (solved or {}).get("quantities") or {}
            lengths = [abs(float(values.get(b.get("quantity"), 0) or 0)) for b in bars]
            if not any(lengths):  # a table or number-line board: same-length bars
                lengths = [1.0] * len(bars)
        else:
            lengths = [float(b.get("units") or 1) for b in spec["model"]["bars"]]
        top = max(lengths[:4] or [0]) or 1.0
        return [round(v / top, 3) for v in lengths[:4]]
    except (KeyError, TypeError, ValueError, AttributeError):
        return []


def save_new(child: dict, spec: dict, solved: dict | None) -> int | None:
    """A new row for this lesson (a fresh problem or a twin). None if the store failed; the lesson goes on."""
    try:
        title = str(spec.get("title") or spec.get("problem_type") or "Bar model").strip()
        return db.add_lesson(child["id"], json.dumps(spec, separators=(",", ":")),
                             json.dumps(solved, separators=(",", ":")) if solved is not None else None,
                             title, learner.heuristic_of(spec), str(spec.get("level", "")),
                             json.dumps(_thumb(spec, solved)), len(spec.get("timeline") or []))
    except Exception as exc:  # noqa: BLE001 — a lesson never fails because the store did
        logger.warning("could not save the lesson: %s", exc)
        return None


def replay_of(child: dict, lesson_id) -> int | None:
    """The child's own saved lesson being replayed, or None (unknown id or someone else's: a new row instead)."""
    if not isinstance(lesson_id, int) or isinstance(lesson_id, bool):
        return None
    try:
        return lesson_id if db.start_lesson_run(lesson_id, child["id"]) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not start the replay: %s", exc)
        return None


def outcome(score: dict[str, str], hints: int) -> tuple[str, int]:
    """(status, hints) for a finished run. "redo" when a step had to be shown; hints counts the clues given."""
    values = list(score.values())
    if "shown" in values:
        return "redo", hints
    hints = max(hints, sum(v == "with hints" for v in values))
    if hints or "missed" in values:
        return "hints", hints
    return "solved", 0


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def status_text(row: dict) -> str:
    status = row.get("status")
    if status == "solved":
        n = int(row.get("solved_on") or 1)
        return "Solved first try" if n <= 1 else f"Solved on {_ordinal(n)} try"
    if status == "hints":
        n = int(row.get("hints") or 0)
        return f"Solved with {n} hint{'s' if n != 1 else ''}" if n else "Solved step by step"
    return "To redo"


# ---------------------------------------------------------------- API
def _summary(row: dict) -> dict:
    title = row["title"] or "Bar model"
    label = title.split(":", 1)[-1].strip() if ":" in title else title
    try:
        thumb = json.loads(row.get("thumb") or "[]")
    except ValueError:
        thumb = []
    return {"id": row["id"], "title": title, "label": label[:40], "family": row["heuristic"],
            "family_label": row["heuristic"].replace("_", " ").capitalize(), "level": row["level"],
            "status": row["status"], "status_text": status_text(row), "redo": row["status"] in ("unfinished", "redo"),
            "unfinished": row["status"] == "unfinished", "kept": bool(row["kept"]),
            "progress": round(float(row["progress"] or 0), 2), "steps": row["steps"], "seconds": row["seconds"],
            "runs": row["runs"], "thumb": thumb, "updated": row["updated"]}


def _child(request: Request, child_id: int | None = None) -> dict:
    """The signed-in child, or (with child_id) one of the signed-in parent's own children."""
    if child_id is not None:
        child = db.get_child(child_id, _parent_id(request))
    else:
        sess = session_of(request.cookies)
        if not isinstance(sess.get("c"), int):
            raise HTTPException(401, "Sign in to see your lessons.")
        child = db.get_child(sess["c"], sess.get("f"))
    if child is None:
        raise HTTPException(404, "No such child.")
    return child


def streak(times: list[float], tz_minutes: int = 0, now: float | None = None) -> int:
    """Days in a row, ending today or yesterday, with a lesson. tz_minutes: the browser's getTimezoneOffset()."""
    shift = -tz_minutes * 60
    days = {int((t + shift) // 86400) for t in times}
    today = int(((now or time.time()) + shift) // 86400)
    day = today if today in days else today - 1
    n = 0
    while day in days:
        n, day = n + 1, day - 1
    return n


def _families(child: dict) -> list[dict]:
    out = []
    for h in db.lesson_heuristics(child["id"]):
        try:
            wins = learner.challenge_wins(db.attempts(child["id"], h, limit=500))
            level = learner.mastery_level(db.get_mastery(child["id"], h), child.get("level"), wins)
        except Exception:  # noqa: BLE001
            level = {"level": learner.LEVELS[0], "index": 0}
        out.append({"family": h, "label": h.replace("_", " ").capitalize(), "level": level["level"],
                    "mastered": level["index"] >= MASTERED})
    return out


@router.get("/api/lessons")
def lessons_list(request: Request, which: str = "all", family: str | None = None, tz: int = 0,
                 child_id: int | None = None):
    child = _child(request, child_id)
    which = which if which in ("all", "kept", "redo") else "all"
    rows = [_summary(r) for r in db.list_lessons(child["id"], which, family or None)]
    # The latest unfinished lesson goes first (with its progress bar), as "carry on where you stopped".
    pin = next((i for i, r in enumerate(rows) if r["unfinished"] and r["progress"] > 0), None)
    if pin:
        rows.insert(0, rows.pop(pin))
    stats = {"lessons": db.count_lessons(child["id"]), "stars": db.stars_won(child["id"]),
             "streak": streak(db.lesson_times(child["id"]), max(-900, min(900, tz)))}
    return {"child": _public_child(child), "stats": stats, "families": _families(child), "lessons": rows}


@router.get("/api/lessons/{lesson_id}")
def lessons_get(lesson_id: int, request: Request):
    child = _child(request)
    row = db.get_lesson(lesson_id, child["id"], with_spec=True)
    if row is None:
        raise HTTPException(404, "No such lesson.")
    return {**_summary(row), "spec": json.loads(row["spec"]), "solved": json.loads(row["solved"]) if row["solved"] else None}


class KeepBody(BaseModel):
    kept: bool = True


@router.post("/api/lessons/{lesson_id}/keep")
def lessons_keep(lesson_id: int, body: KeepBody, request: Request):
    child = _child(request)
    if not db.set_lesson_kept(lesson_id, child["id"], body.kept):
        raise HTTPException(404, "No such lesson.")
    return _summary(db.get_lesson(lesson_id, child["id"]))


@router.get("/lessons-assets/lessons.js")
def lessons_js():
    return Response(STATIC_JS.read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})
