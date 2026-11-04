"""Method badges: records of a method the child used, earned by evidence in a lesson, never at random.

Why badges at all, and why these: claude_handoff/RESEARCH-interactivity-gamification-2026-11-03.md §2.
Badges are informational (they name the method), not currency: no points, no shop, no leaderboard, no
timer, and nothing to lose. The wording praises the method ("you found 1 unit first"), not the child.

Two kinds:
  - one *method* badge per board family (before-after, remainder, ratio ...), earned in a lesson of that
    family where the child did the key thinking themselves: the challenge-first answer right, or no step
    shown, at least two steps right first try, and the 1-unit step (when there is one) right first try;
  - *process* badges: "Checked my answer" (the end-of-lesson check checkpoint, key "check", once lessons
    have one: until then it simply never fires), "Came back after a mistake" (a twin solved after a miss),
    and "Curious mind" (asked the teacher a question).

Storage (signed-in child only): app/db.py `badges` (child_id, badge, earned_at). Anonymous lessons get the
badge in the "end" event for that lesson only; nothing is stored.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import learner

GOOD = ("first try", "early")
MISSES = ("with hints", "shown", "missed")


@dataclass(frozen=True)
class Badge:
    id: str
    name: str
    kind: str          # "method" | "process"
    why: str           # what the child did, said when it's earned
    heuristics: tuple[str, ...] = ()

    @property
    def icon(self) -> str:
        return f"/badges/{self.id}.svg"

    def as_json(self) -> dict:
        out = asdict(self)
        out["heuristics"] = list(self.heuristics)
        out["icon"] = self.icon
        return out


CATALOG: tuple[Badge, ...] = (
    Badge("before_after_builder", "Before–After Builder", "method",
          "You drew the before and the after, and found what changed.",
          ("before_after", "equal_end", "constant_total")),
    Badge("same_gap_spotter", "Same-Gap Spotter", "method",
          "You saw that the difference stays the same and used it.", ("constant_difference",)),
    Badge("remainder_ranger", "Remainder Ranger", "method",
          "You split the remainder into equal units, step by step.", ("remainder",)),
    Badge("fraction_slicer", "Fraction Slicer", "method",
          "You cut the whole into equal parts to find the fraction.", ("fraction_of",)),
    Badge("ratio_rescaler", "Ratio Re-scaler", "method",
          "You matched the ratio units so they meant the same amount.", ("ratio", "ratio_change")),
    Badge("percentage_pro", "Percentage Pro", "method",
          "You turned the percentage into units of the whole.", ("percentage", "percentage_change")),
    Badge("unit_finder", "Unit Finder", "method",
          "You found what 1 unit is worth, then used it.", ("part_whole", "comparison", "multiples", "how_many_more")),
    Badge("value_matcher", "Value Matcher", "method",
          "You matched each item to its value and added them up.", ("units_and_value",)),
    Badge("step_stacker", "Step Stacker", "method",
          "You worked through every step in order.", ("multi_step",)),
    Badge("fair_sharer", "Fair Sharer", "method",
          "You used the total to find the fair share.", ("average",)),
    Badge("what_if_detective", "What-If Detective", "method",
          "You supposed one thing, then fixed the difference.", ("assumption", "excess_shortage")),
    Badge("rate_reader", "Rate Reader", "method",
          "You used how much per step to find the whole.", ("rate", "speed")),
    Badge("checked_my_answer", "Checked my answer", "process",
          "You checked your answer against the question."),
    Badge("came_back", "Came back after a mistake", "process",
          "You tried one like it after a tricky one, and solved it."),
    Badge("curious_mind", "Curious mind", "process",
          "You asked a question to understand the bars."),
)
BY_ID = {b.id: b for b in CATALOG}
METHOD_FOR = {h: b.id for b in CATALOG for h in b.heuristics}


def method_badge(heuristic: str) -> Badge | None:
    return BY_ID.get(METHOD_FOR.get(heuristic, ""))


def method_shown(score: dict[str, str]) -> bool:
    """The child did the key thinking: the challenge answer right, or a clean lesson with the unit step first try."""
    if score.get("challenge") == "first try" or "early" in score.values():
        return True
    if any(o in ("shown", "missed") for o in score.values()):
        return False
    firsts = sum(1 for o in score.values() if o == "first try")
    return firsts >= 2 and score.get("unit", "first try") == "first try"


def reached_answer(score: dict[str, str]) -> bool:
    """The child got the final answer themselves (challenge, early, or the answer checkpoint)."""
    return (score.get("challenge") in GOOD or "early" in score.values()
            or score.get("answer") in ("first try", "with hints"))


def had_miss(outcomes) -> bool:
    return any(o in MISSES for o in outcomes)


def evaluate(spec: dict, score: dict[str, str], *, twin_after_miss: bool = False,
             asked_question: bool = False) -> list[str]:
    """Badge ids this lesson earned, from its checkpoint outcomes only. Pure: stores nothing."""
    earned = []
    badge = method_badge(learner.heuristic_of(spec))
    if badge is not None and score and method_shown(score):
        earned.append(badge.id)
    if score.get("check") in ("first try", "with hints"):  # the beat-8 check checkpoint, when lessons have it
        earned.append("checked_my_answer")
    if twin_after_miss and reached_answer(score):
        earned.append("came_back")
    if asked_question:
        earned.append("curious_mind")
    return earned


def award(child: dict | None, earned: list[str], db=None) -> list[dict]:
    """The badges to celebrate now. A signed-in child: only the new ones, stored. Anonymous: all, stored nowhere."""
    earned = [b for b in dict.fromkeys(earned) if b in BY_ID]
    if child is None:
        return [BY_ID[b].as_json() for b in earned]
    if db is None:
        from app import db  # noqa: PLC0415
    return [BY_ID[b].as_json() for b in earned if db.add_badge(child["id"], b)]


def child_methods(child: dict, db=None) -> dict:
    """The "My methods" panel: a level per heuristic practised, and every badge (earned or still to earn)."""
    if db is None:
        from app import db  # noqa: PLC0415
    rows = db.attempts(child["id"], limit=2000)
    levels = []
    for m in db.all_mastery(child["id"]):
        wins = learner.challenge_wins([r for r in rows if r.get("heuristic") == m["heuristic"]])
        lvl = learner.mastery_level(m, child.get("level"), wins)
        badge = method_badge(m["heuristic"])
        levels.append({"heuristic": m["heuristic"], "label": m["heuristic"].replace("_", " "), **lvl,
                       "badge": badge.id if badge else None})
    have = {b["badge"]: b["earned_at"] for b in db.list_badges(child["id"])}
    badges = [{**b.as_json(), "earned_at": have.get(b.id)} for b in CATALOG]
    return {"levels": levels, "badges": badges, "levels_names": list(learner.LEVELS)}
