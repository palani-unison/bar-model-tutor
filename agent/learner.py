"""Learner model and adaptive pacing: how well and how fast a child does on each problem type.

Per child and heuristic (before_after, remainder, ratio ...) we keep an Elo-like rating and a
response-time average. Each checkpoint is a little "match" between the child and the checkpoint:

  difficulty D = 1000 + 100 × (problem level − 3) + a per-checkpoint offset
                 (counting units −50, a working line 0, 1 unit 0, the last step +50,
                  the whole answer straight away (challenge, or said early) +150,
                  a board task (build the bars, place the "?", drag a transfer) −25)
  expected   E = 1 / (1 + 10^((D − R) / 400))
  score      S = 1 first try (or challenge/early right), 0.5 with hints, 0 shown or challenge missed
  rating     R ← R + K × (S − E),   K = 48 for the first 12 checkpoints, then 24
  speed      avg_seconds ← 0.3 × seconds + 0.7 × avg_seconds   (an EMA; the first one is taken as is)

A new child starts at their own school level's par: R = 1000 + 100 × (child level − 3).

Pacing (see `pacing`) is a handful of plain rules over the rating margin (R minus the problem's D),
the speed average and how the last lesson went, so a parent can be told why.
Nothing here stores question text: only the heuristic, the level and the outcomes.
"""
from __future__ import annotations

import re

BASE = 1000.0
PER_LEVEL = 100.0
OFFSETS = {"units": -50.0, "unit": 0.0, "answer": 50.0, "challenge": 150.0,
           "build": -25.0, "mark": -25.0, "move": -25.0, "grow": -25.0}  # board checkpoints: building the model is easier than a sum
STEP_OFFSET = 0.0
EARLY_OFFSET = 150.0
SCORES = {"first try": 1.0, "early": 1.0, "with hints": 0.5, "shown": 0.0, "missed": 0.0}
K_NEW, K_SETTLED, SETTLE_N = 48.0, 24.0, 12
EMA = 0.3

# Pacing thresholds
STRONG_MARGIN = 75.0     # rating this far above the problem: challenge first
STRUGGLE_MARGIN = -75.0  # this far below: guided, every checkpoint, normal speed
MIN_N = 3                # checkpoints seen on this heuristic before the rating is trusted
FAST_S, OK_S, SLOW_S = 10.0, 15.0, 25.0  # average seconds from question to settled answer
VERY_STRONG = 175.0


def level_number(level: str | None, top: bool = False) -> float | None:
    """'P5' -> 5; 'P3-P4' -> 3.5 (or 4 with top=True, the hardest level it is meant for)."""
    nums = [int(n) for n in re.findall(r"[1-6]", str(level or ""))]
    if not nums:
        return None
    return float(max(nums)) if top else sum(nums) / len(nums)


def heuristic_of(spec: dict) -> str:
    """v2: its `heuristic`. v1: from `problem_type` ("part-whole with multiples" -> part_whole)."""
    if spec.get("heuristic"):
        return str(spec["heuristic"])
    kind = str(spec.get("problem_type", "")).lower()
    if "differ" in kind or "more than" in kind or "compar" in kind:
        return "comparison"
    if "part" in kind and "whole" in kind:
        return "part_whole"
    if "multiple" in kind or "times" in kind:
        return "multiples"
    return "part_whole"


def par_rating(level: str | None) -> float:
    n = level_number(level) or 4.0
    return BASE + PER_LEVEL * (n - 3)


def difficulty(level: str | None, key: str, outcome: str | None = None) -> float:
    if outcome == "early":
        offset = EARLY_OFFSET
    else:
        offset = OFFSETS.get(key, STEP_OFFSET)
    return par_rating(level) + offset


def expected(rating: float, diff: float) -> float:
    return 1.0 / (1.0 + 10 ** ((diff - rating) / 400.0))


def update(state: dict | None, child_level: str | None, problem_level: str | None,
           results: list[tuple[str, str, float | None]]) -> dict:
    """Fold one lesson's checkpoint results (key, outcome, seconds) into {rating, n, avg_seconds}."""
    state = dict(state or {})
    rating = float(state.get("rating") if state.get("rating") is not None else par_rating(child_level))
    n = int(state.get("n") or 0)
    avg = state.get("avg_seconds")
    for key, outcome, seconds in results:
        if outcome not in SCORES:
            continue
        k = K_NEW if n < SETTLE_N else K_SETTLED
        rating += k * (SCORES[outcome] - expected(rating, difficulty(problem_level, key, outcome)))
        n += 1
        if seconds is not None and outcome != "missed":  # a missed challenge is a guess, not a working time
            avg = float(seconds) if avg is None else EMA * float(seconds) + (1 - EMA) * float(avg)
    return {"rating": round(rating, 1), "n": n, "avg_seconds": None if avg is None else round(avg, 1)}


def pacing_from(child_level: str | None, problem_level: str | None, state: dict | None,
                last: list[dict] | None = None) -> dict:
    """The rules, from plain numbers. `last` is the most recent lesson's attempts on this heuristic."""
    reasons: list[str] = []
    child_n, prob_top = level_number(child_level), level_number(problem_level, top=True)
    below = child_n is not None and prob_top is not None and prob_top < child_n
    state = state or {}
    n = int(state.get("n") or 0)
    rating = state.get("rating")
    margin = (float(rating) - par_rating(problem_level)) if rating is not None and n else None
    avg = state.get("avg_seconds")
    shown = sum(1 for a in (last or []) if a.get("outcome") in ("shown", "missed"))
    struggled = bool(last) and shown * 3 >= len(last) and shown > 0

    if below:
        reasons.append(f"the problem ({problem_level}) is below {child_level}")
    if margin is not None:
        reasons.append(f"rating {margin:+.0f} against this problem after {n} checkpoints")
    if avg is not None:
        reasons.append(f"about {avg:.0f} s per answer")

    # 1. Struggling wins: every checkpoint, the hints, normal speed.
    if struggled or (margin is not None and n >= MIN_N and margin <= STRUGGLE_MARGIN) or (avg is not None and avg >= SLOW_S):
        if struggled:
            reasons.append("needed to be shown steps last time")
        return {"mode": "guided", "speed": 1.0, "checkpoints": "all", "reasons": reasons}
    strong = margin is not None and n >= MIN_N and margin >= STRONG_MARGIN
    # 2. Below level or strong: try the answer first; a right answer gets the one-step recap.
    if below or strong:
        fast = avg is not None and avg <= FAST_S
        speed = 2.0 if (fast and margin is not None and margin >= VERY_STRONG) else 1.5 if (fast or below) else 1.25
        return {"mode": "challenge_first", "speed": speed, "checkpoints": "key", "reasons": reasons}
    # 3. Doing fine: guided, a little quicker, only the key checkpoints once the rating is above par.
    if margin is not None and n >= MIN_N and margin >= 0:
        speed = 1.25 if avg is not None and avg <= OK_S else 1.0
        return {"mode": "guided", "speed": speed, "checkpoints": "key", "reasons": reasons}
    if not reasons:
        reasons.append("first lesson of this kind")
    return {"mode": "guided", "speed": 1.0, "checkpoints": "all", "reasons": reasons}


# ---------------------------------------------------------------- with the store (app/db.py)
def _last_lesson(rows: list[dict]) -> list[dict]:
    """The newest lesson's attempts: rows written together share a timestamp."""
    if not rows:
        return []
    newest = rows[0]["created"]
    return [r for r in rows if abs(r["created"] - newest) < 1.0]


def pacing(child: dict | None, spec: dict, db=None) -> dict | None:
    """{"mode", "speed", "checkpoints", "reasons"} for this child on this spec; None for an anonymous lesson."""
    if not child:
        return None
    if db is None:
        from app import db  # noqa: PLC0415 — the store lives in the app; the rules above stay pure
    heuristic = heuristic_of(spec)
    state = db.get_mastery(child["id"], heuristic)
    last = _last_lesson(db.attempts(child["id"], heuristic, limit=20))
    out = pacing_from(child.get("level"), spec.get("level"), state, last)
    out["heuristic"] = heuristic
    return out


def record_lesson(child: dict, spec: dict, results: list[tuple[str, str, float | None]], db=None) -> dict:
    """Write the lesson's attempts and fold them into mastery. Returns the new mastery state."""
    if db is None:
        from app import db  # noqa: PLC0415
    heuristic, level = heuristic_of(spec), str(spec.get("level", ""))[:8]
    if results:
        db.add_attempts(child["id"], heuristic, level, results)
    state = update(db.get_mastery(child["id"], heuristic), child.get("level"), spec.get("level"), results)
    db.set_mastery(child["id"], heuristic, state["rating"], state["n"], state["avg_seconds"])
    return state


# ---------------------------------------------------------------- mastery levels (shown to the child and parent)
# The rating above, in words a child can read. Gentle on purpose: one good lesson reaches Growing.
# margin = rating minus the child's own school-level par (a new child starts at 0).
LEVELS = ("Starting", "Growing", "Strong", "Master")
LEVEL_RULES = (  # (min checkpoints, min margin, needs a challenge-first win) to reach LEVELS[1], [2], [3]
    (3, -75.0, False),
    (6, 0.0, False),
    (10, 75.0, True),
)
REFRESH_AFTER_S = 14 * 24 * 3600  # not practised for two weeks: down one level, "time for a refresher"


def mastery_level(state: dict | None, child_level: str | None, challenge_wins: int = 0,
                  now: float | None = None) -> dict:
    """{"level", "index", "next", "progress" (0..1 towards next), "tip", "refresher"} for one heuristic."""
    import time as _time

    state = state or {}
    n = int(state.get("n") or 0)
    rating = state.get("rating")
    margin = (float(rating) - par_rating(child_level)) if rating is not None else 0.0
    index = 0
    for need_n, need_margin, need_win in LEVEL_RULES:
        if n >= need_n and margin >= need_margin and (challenge_wins > 0 or not need_win):
            index += 1
        else:
            break
    updated = state.get("updated")
    refresher = bool(index and updated and (now or _time.time()) - float(updated) > REFRESH_AFTER_S)
    if refresher:
        index -= 1
    out = {"level": LEVELS[index], "index": index, "next": None, "progress": 1.0, "tip": "", "refresher": refresher}
    if index + 1 < len(LEVELS):
        need_n, need_margin, need_win = LEVEL_RULES[index]
        prev_margin = LEVEL_RULES[index - 1][1] if index else need_margin - 75.0
        parts = [min(1.0, n / need_n),
                 max(0.0, min(1.0, (margin - prev_margin) / max(1.0, need_margin - prev_margin)))]
        if need_win:
            parts.append(1.0 if challenge_wins else 0.0)
        out["next"], out["progress"] = LEVELS[index + 1], round(sum(parts) / len(parts), 2)
        if refresher:
            out["tip"] = "Time for a refresher! One problem of this kind brings it back."
        elif n < need_n:
            more = need_n - n
            out["tip"] = f"Work out {more} more step{'s' if more != 1 else ''} of this kind."
        elif margin < need_margin:
            out["tip"] = "Try a few more steps on your own before asking for a hint."
        else:
            out["tip"] = "Solve a challenge-first problem of this kind on your own."
    else:
        out["tip"] = "You know this method well. Try one like it now and then to keep it."
    return out


def challenge_wins(rows: list[dict]) -> int:
    """Challenge-first answers right on the first try (or said early), from attempt rows."""
    return sum(1 for r in rows if (r.get("checkpoint_key") == "challenge" and r.get("outcome") == "first try")
               or r.get("outcome") == "early")
