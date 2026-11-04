"""Who may use what, on a public deploy (PUBLIC_DEPLOY=1).

The first page is parent sign-in / register (Google) and kids' sign-in (hero name + password), so the app
itself has no browser password any more. Instead:
- Anything that spends the Meta API on a NEW problem (reading a photo, planning, twins, checking a drawing,
  rendering a video, opening a room) needs a signed-in parent or child: 401 {"detail": "Sign in first."}.
- Planning is counted per family per day (PLANS_PER_FAMILY_DAY) and for the whole server
  (PLANS_PER_DAY), so one account can't run up the bill.
- Anonymous visitors can still try the built-in example lessons: their specs are pre-verified files, and the
  lesson socket accepts an anonymous "start" only for one of them (app/lesson_ws.py).
- Admin: only a Google sign-in with an address on ADMIN_EMAILS (app/admin.py). It sets an admin cookie that
  counts as signed in with no quota and opens the dashboard (usage, families, plan outcomes, reports).
  There is no admin password.

Locally (PUBLIC_DEPLOY unset) everything stays open, as before, for development and the tests.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request

from app.accounts import family_consented, session_of, sign, unsign

ADMIN_COOKIE = "bmt_admin"
ADMIN_TTL_S = 7 * 24 * 3600  # the owner's Google sign-in: admin for a week, or until sign-out
PLANS_PER_FAMILY_DAY = int(os.environ.get("PLANS_PER_FAMILY_DAY", "40"))
PLANS_PER_DAY = int(os.environ.get("PLANS_PER_DAY", "800"))
SIGN_IN_FIRST = "Sign in first."


def public() -> bool:
    """A public deploy: sign-in is required for the AI, and /admin only for the owner's Google account."""
    return os.environ.get("PUBLIC_DEPLOY", "") == "1"


def is_admin(cookies) -> bool:
    return (unsign(cookies.get(ADMIN_COOKIE)) or {}).get("a") == 1


def member(cookies) -> str | None:
    """'admin', 'f<parent id>' for a signed-in parent or child (one family, one quota), or None."""
    if is_admin(cookies):
        return "admin"
    sess = session_of(cookies)
    family = sess.get("p") if isinstance(sess.get("p"), int) else sess.get("f")
    return f"f{family}" if isinstance(family, int) else None


NEEDS_CONSENT = "A parent needs to agree to the terms first."


def _checked(key: str | None) -> str:
    """401 for no account; 403 until the family's parent has agreed to the current terms (/terms)."""
    if key is None:
        raise HTTPException(401, SIGN_IN_FIRST)
    if key != "admin" and not family_consented(int(key[1:])):
        raise HTTPException(403, NEEDS_CONSENT)
    return key


def require_member(request: Request) -> str | None:
    """The caller's quota key; 401/403 (no account / no consent) on a public deploy. None when not public."""
    if not public():
        return None
    return _checked(member(request.cookies))


class DailyQuota:
    """Plans per family and in total, per UTC day, in memory (one container: max_containers=1)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._day = ""
        self._counts: dict[str, int] = {}

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day:
            self._day, self._counts = today, {}

    def spend(self, key: str | None) -> None:
        """Count one plan for `key`, or raise 429. Admin and local (None) are never limited."""
        if key is None or key == "admin":
            return
        with self._lock:
            self._roll()
            if self._counts.get("*", 0) >= PLANS_PER_DAY:
                raise HTTPException(429, "The tutor is very busy today. Please try again tomorrow, "
                                         "or replay your saved lessons.")
            if self._counts.get(key, 0) >= PLANS_PER_FAMILY_DAY:
                raise HTTPException(429, "That's a lot of new problems today! Replay your saved lessons, "
                                         "or come back tomorrow for more.")
            self._counts[key] = self._counts.get(key, 0) + 1
            self._counts["*"] = self._counts.get("*", 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            return {"day": self._day, "total": self._counts.get("*", 0),
                    "families": len([k for k in self._counts if k != "*"])}


quota = DailyQuota()


def spend_plan(request_or_cookies) -> None:
    """require_member + count one plan. Takes a Request (HTTP) or a cookie mapping (the lesson socket)."""
    cookies = getattr(request_or_cookies, "cookies", request_or_cookies)
    if not public():
        return
    quota.spend(_checked(member(cookies)))


# ---------------------------------------------------------------- admin
def admin_cookie(ttl_s: int = ADMIN_TTL_S) -> str:
    return sign({"a": 1}, ttl_s)


def usage() -> dict:
    """Counts for the admin page: accounts, lessons, today's plans and the Glimmer spend. No personal data."""
    from app import db

    with db.connect() as con:
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 — fixed names
                  for table in ("parents", "children", "lessons", "attempts")}
    spend = 0.0
    ledger = Path(os.environ.get("GLIMMER_LEDGER", ""))
    if ledger.name and ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                spend += json.loads(line).get("usd", 0.0)
    return {**counts, "plans_today": quota.snapshot(), "glimmer_usd": round(spend, 2),
            "limits": {"per_family_day": PLANS_PER_FAMILY_DAY, "per_day": PLANS_PER_DAY}}


_EXAMPLES: set[str] | None = None


def example_question(spec: dict) -> bool:
    """True when `spec` is one of the built-in examples (specs/examples), compared by question text."""
    global _EXAMPLES
    if _EXAMPLES is None:
        root = Path(__file__).resolve().parent.parent / "specs" / "examples"
        found = set()
        for path in list(root.glob("*.json")) + list((root / "v2").glob("*.json")):
            try:
                found.add(" ".join(str(json.loads(path.read_text(encoding="utf-8")).get("question", "")).split()))
            except (OSError, json.JSONDecodeError):
                continue
        _EXAMPLES = found - {""}
    return " ".join(str((spec or {}).get("question", "")).split()) in _EXAMPLES


def lesson_message_allowed(cookies, kind: str, data: dict) -> str | None:
    """None if the lesson socket may act on this message, else the reason to show. Spends a plan on "prepare"."""
    if not public() or kind not in ("start", "prepare"):
        return None
    if kind == "start" and example_question(data.get("spec") or {}):
        return None  # the built-in example lessons are open to everyone
    try:
        if kind == "prepare":
            spend_plan(cookies)
        else:
            _checked(member(cookies))
    except HTTPException as exc:
        return str(exc.detail)
    return None
