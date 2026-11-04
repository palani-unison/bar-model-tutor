"""Build it yourself: board checkpoints, where the child makes the bar model before the teacher draws it.

A board checkpoint stops the lesson just before a draw step (docs/board-templates.md beats 2-3) and asks the
child to do that step on a little builder: set each bar's units (templates A part-whole and B comparison, and
the first row of C/G), put the "?" on the part the question asks for, or drag a transferred piece from one bar
to the other (C). The browser sends back a small JSON fact; code compares it with values taken only from the
verified spec:

  units  {"kavya_bar": 4, "lily_bar": 1}   segment exprs ("3*u" -> 3), as a ratio: 8:2 is right for 4:1
  mark   {"bar": "kavya_bar", "segment": 1} the question_mark step's bar (and segments)
  move   {"from": "ali_a", "to": "ben_a"}    the move_part step's from/to
  grow   {"bars": ["dad_n", "son_n"]}          template D: who gets the "+ years" piece? (everyone in the stage)

No model judges anything. When a spec can't give a clean whole-number answer, no board task is offered and the
number checkpoints run as before. Spoken moves ("Kavya has four units, Lily has one") go through a tiny grammar
over the bar labels and number words (`parse_said`); anything it can't read is "Can you show me on the board?".
"""
from __future__ import annotations

import math
import re

from .spec_v2 import Solution
from .lesson import Checkpoint, _final_question, _fmt, _unit_parts, numbers_in

MAX_UNITS = 12  # per row: more than a dozen boxes doesn't fit a phone, and no P4-P5 model needs it
# Boards whose first picture is bars of whole units. A remainder board's LCM re-split (2:1:2) is a step to
# teach, not one to guess, so it gets the "?" task only.
UNITS_BOARDS = {None, "part_whole", "comparison", "before_after", "ratio_change", "units_value_table", "percentage"}
DRAWING = ("draw_stage", "draw_bar", "divide_units", "highlight")  # beat 2: the picture going up, fact by fact
ASK_UNITS = "Tap + and –, then tap Check."
SHOW_ON_BOARD = "Can you show me on the board?"


def board_checkpoints(spec: dict, sol: Solution) -> dict[int, Checkpoint]:
    """Board checkpoints keyed by the timeline index they stop before: units, then the "?", then a transfer."""
    out: dict[int, Checkpoint] = {}
    for make in (_units_checkpoint, _grow_checkpoint, _move_checkpoint, _mark_checkpoint):
        try:
            found = make(spec, sol)
        except (KeyError, TypeError, ValueError, IndexError):
            found = None  # an odd spec just doesn't get this board task
        if found is not None and found[0] not in out:
            out[found[0]] = found[1]
    return out


# ---------------------------------------------------------------- what the child builds
def _unit_count(expr: str, units: list[str]) -> int | None:
    """'3*u' -> 3; anything with a plain amount, a second unit or a fraction of a unit -> None."""
    coeffs, const = _unit_parts(str(expr or ""), units)
    if const or len(coeffs) != 1:
        return None
    k = next(iter(coeffs.values()))
    return int(k) if k == int(k) and 1 <= k <= MAX_UNITS else None


def _stage_of(spec: dict) -> dict[str, dict]:
    return {bar["id"]: stage for stage in spec.get("stages") or [] for bar in stage.get("bars") or []}


def _bars(spec: dict) -> dict[str, dict]:
    return {bar["id"]: bar for stage in spec.get("stages") or [] for bar in stage.get("bars") or []}


def _units_checkpoint(spec: dict, sol: Solution) -> tuple[int, Checkpoint] | None:
    """Before the first bar is drawn: how many units is each bar (B), or each part of the one bar (A)?"""
    if spec.get("board") not in UNITS_BOARDS:
        return None
    timeline, units = spec["timeline"], [u.get("name") for u in spec.get("units") or []]
    first = next((i for i, s in enumerate(timeline) if s.get("do") == "draw_bar"), None)
    if first is None:
        return None
    stage_of, bars = _stage_of(spec), _bars(spec)
    stage = stage_of.get(timeline[first].get("bar"))
    drawn: list[str] = []
    for step in timeline[first:]:  # the bars of beat 2: drawn one after another before the first mark or line
        if step.get("do") not in DRAWING:
            break
        bid = step.get("bar")
        if step.get("do") == "draw_bar" and stage_of.get(bid) is stage and bid not in drawn:
            drawn.append(bid)
    rows: list[dict] = []
    if len(drawn) == 1:  # A: one bar, split into its parts
        segs = bars[drawn[0]].get("segments") or []
        if len(segs) < 2 or not all(s.get("label") for s in segs):
            return None  # parts with no names can't be told apart
        for i, s in enumerate(segs):
            rows.append({"id": f"{drawn[0]}#{i}", "label": str(s["label"]), "units": _unit_count(s.get("expr"), units),
                         "style": s.get("style", "normal")})
    else:  # B: each bar's length in units
        for bid in drawn:
            segs = bars[bid].get("segments") or []
            counts = [_unit_count(s.get("expr"), units) for s in segs]
            if not segs or any(s.get("style", "normal") in ("removed", "added") for s in segs) or None in counts:
                return None
            rows.append({"id": bid, "label": str(bars[bid].get("label", "")), "units": sum(counts)})
    if len(rows) < 2 or any(r["units"] is None or not 1 <= r["units"] <= MAX_UNITS for r in rows) \
            or len({r["label"].lower() for r in rows}) != len(rows):
        return None
    expected = {r["id"]: r["units"] for r in rows}
    parts = len(drawn) == 1
    name = (lambda r: f"“{r['label']}”") if parts else (lambda r: r["label"])  # a part's label is a word on the bar
    names = _join([name(r) for r in rows])
    if parts:
        ask = f"Show me: split the bar for {bars[drawn[0]].get('label', '')} into {names}. How many units is each part? {ASK_UNITS}"
    elif len(rows) == 2:
        ask = f"Show me: how many units do {names} each have? {ASK_UNITS}"
    else:
        ask = f"Show me: how many units does each bar have? {ASK_UNITS}"
    least = min(rows, key=lambda r: r["units"])
    others = [r for r in rows if r is not least]
    if parts:
        total = sum(expected.values())
        hint1 = "Look at the fraction in the question. How many equal units is the whole bar, and how many go in each part?"
        hint2 = f"The whole bar is {_units(total)}, and {name(least)} is {_units(least['units'])}. How many units is {_join([name(r) for r in others])}?"
    else:
        hint1 = (f"Give {least['label']} 1 unit. How many times as much does {_join([r['label'] for r in others])} have?"
                 if least["units"] == 1 else "Read the question again. Who has more, and how many times as much?")
        hint2 = f"{least['label']} has {_units(least['units'])}. Now set {_join([r['label'] for r in others])}."
    listing = _join([f"{name(r)} {_units(r['units'])}" for r in rows])
    task = {"op": "units", "parts": parts, "max": MAX_UNITS,
            "rows": [{"id": r["id"], "label": r["label"], **({"style": r["style"]} if parts else {})} for r in rows]}
    if parts:
        task["bar"] = str(bars[drawn[0]].get("label", ""))
    return first, Checkpoint("build", ask, expected, [hint1, hint2], f"Yes! {listing}.",
                             f"Here's how: {listing}. Watch me draw it.", kind="board", task=task)


def _drawn_rows(spec: dict, sol: Solution, before: int, only_stage: dict | None = None) -> list[dict]:
    """The bars on the board before step `before`, as the builder draws them: segments sized by solved value."""
    stage_of, bars = _stage_of(spec), _bars(spec)
    units = [u.get("name") for u in spec.get("units") or []]
    order = list(dict.fromkeys(s.get("bar") for s in spec["timeline"][:before] if s.get("do") == "draw_bar"))
    labels = [str(bars[b].get("label", "")) for b in order if b in bars]
    rows = []
    for bid in order:
        bar, stage = bars.get(bid), stage_of.get(bid)
        if bar is None or (only_stage is not None and stage is not only_stage):
            continue
        values = sol.segments.get(bid) or []
        segs = []
        for i, s in enumerate(bar.get("segments") or []):
            v = values[i] if i < len(values) else 0
            segs.append({"w": round(float(v), 4) if v and v > 0 else 1, "label": str(s.get("label", "")),
                         "style": s.get("style", "normal"), "units": _unit_count(s.get("expr", ""), units)})
        label = str(bar.get("label", ""))
        rows.append({"id": bid, "label": label, "stage": str((stage or {}).get("label", "")) if labels.count(label) > 1 else "",
                     "segments": segs})
    return rows


def _bar_name(row: dict) -> str:
    return f"the bar for {row['label']}" + (f" ({row['stage'].lower()})" if row.get("stage") else "")


def _mark_checkpoint(spec: dict, sol: Solution) -> tuple[int, Checkpoint] | None:
    """Before the "?" goes up: which bar, or which part of it, does the question ask for?"""
    timeline = spec["timeline"]
    at = next((i for i, s in enumerate(timeline) if s.get("do") == "question_mark"), None)
    if at is None:
        return None
    step = timeline[at]
    rows = _drawn_rows(spec, sol, at)
    target = next((r for r in rows if r["id"] == step.get("bar")), None)
    if target is None:
        return None
    n = len(target["segments"])
    seg = step.get("segments")
    first, last = (int(seg[0]), int(seg[1])) if isinstance(seg, list) and len(seg) == 2 else (0, n - 1)
    if not 0 <= first <= last < n or sum(len(r["segments"]) for r in rows) - (last - first + 1) < 1:
        return None  # out of range, or nowhere wrong to tap (one bar, the whole of it): nothing to decide
    whole = first == 0 and last == n - 1
    expected = {"bar": target["id"], "segments": [first, last]}
    labels = [target["segments"][i]["label"] for i in range(first, last + 1) if target["segments"][i]["label"]]
    where = _bar_name(target) if whole else (f"the part marked “{labels[0]}” on {_bar_name(target)}"
                                              if len(labels) == 1 and last == first else f"a part of {_bar_name(target)}")
    question = _final_question(spec.get("question", ""))
    ask = (f"{question} " if question else "") + "Show me where the ? goes: tap that part of the bars, then tap Check."
    hint1 = "Read the question's last sentence again. Whose amount does it ask for, and is it the whole bar or one part?"
    hint2 = f"It's on {_bar_name(target)}." + ("" if whole else " Which part of it is the question about?")
    task = {"op": "mark", "rows": rows}
    return at, Checkpoint("mark", ask, expected, [hint1, hint2], "Yes! That's the part we want to find.",
                          f"The ? goes on {where}.", kind="board", task=task)


def _grow_checkpoint(spec: dict, sol: Solution) -> tuple[int, Checkpoint] | None:
    """Template D, before the "later" row is drawn: who gets older? Everyone, by the same years, which is why the
    difference never changes. The verifier's constant_difference rule (same bars in every stage, the same
    difference in every row) is what makes "all of them" the verified answer."""
    stages = spec.get("stages") or []
    if spec.get("board") != "constant_difference" or len(stages) < 2:
        return None
    timeline = spec["timeline"]
    later = stages[1]
    at = next((i for i, s in enumerate(timeline) if s.get("do") == "draw_stage" and s.get("stage") == later.get("id")), None)
    if at is None:
        return None
    rows = _drawn_rows(spec, sol, at, only_stage=stages[0])
    if len(rows) < 2 or sorted(r["label"] for r in rows) != sorted(str(b.get("label", "")) for b in later.get("bars") or []):
        return None
    for r in rows:
        r["stage"] = ""  # one row of bars here, so no "(now)" after the names
    names, every = _join([r["label"] for r in rows]), "both" if len(rows) == 2 else "all"
    ask = "Show me: time passes. Whose bar gets the “+ years” piece? Tap + years on each bar that gets older, then tap Check."
    return at, Checkpoint("grow", ask, {"bars": sorted(r["id"] for r in rows)},
                          ["When years go by, who gets older?",
                           f"Everyone gets older by the same number of years: {names}."],
                          f"Yes! They {every} get the same years added, so the difference between them never changes.",
                          f"{names} {every} get older by the same number of years, so the difference stays the same.",
                          kind="board", task={"op": "grow", "rows": rows})


def _move_checkpoint(spec: dict, sol: Solution) -> tuple[int, Checkpoint] | None:
    """Before a transfer arrow (template C): drag the piece from the bar that gave it to the bar that got it."""
    timeline = spec["timeline"]
    at = next((i for i, s in enumerate(timeline) if s.get("do") == "move_part"), None)
    if at is None:
        return None
    step = timeline[at]
    stage = _stage_of(spec).get(step.get("from"))
    rows = _drawn_rows(spec, sol, at, only_stage=stage)
    ids = [r["id"] for r in rows]
    if step.get("from") not in ids or step.get("to") not in ids or step["from"] == step["to"] or len(rows) < 2:
        return None
    by_id = {r["id"]: r for r in rows}
    giver, getter = by_id[step["from"]], by_id[step["to"]]
    expected = {"from": giver["id"], "to": getter["id"]}
    ask = "Show me the move: drag the piece from the bar that gave it to the bar that got it, then tap Check."
    task = {"op": "move", "rows": rows}
    return at, Checkpoint("move", ask, expected,
                          ["Read the question again. Who gave, and who got?",
                           f"{giver['label']} gave it away. Which bar did it go to?"],
                          f"Yes! It moved from {giver['label']} to {getter['label']}.",
                          f"It moved from {giver['label']} to {getter['label']}. Watch the arrow.", kind="board", task=task)


# ---------------------------------------------------------------- checking
def check(cp: Checkpoint, value) -> tuple[str, str]:
    """('correct' | 'wrong' | 'unusable', what to say first). Only cp.expected (from the verified spec) decides."""
    op = (cp.task or {}).get("op")
    if op == "units":
        return _check_units(cp, value)
    if op == "mark":
        return _check_mark(cp, value)
    if op == "move":
        return _check_move(cp, value)
    if op == "grow":
        return _check_grow(cp, value)
    return "unusable", SHOW_ON_BOARD


def _whole(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v == int(v):
        return int(v)
    return None


def _check_units(cp: Checkpoint, value) -> tuple[str, str]:
    expected: dict[str, int] = cp.expected
    if not isinstance(value, dict):
        return "unusable", SHOW_ON_BOARD
    got = {k: _whole(value.get(k)) for k in expected}
    if any(v is None or not 1 <= v <= MAX_UNITS for v in got.values()):
        return "unusable", "Give every bar at least 1 unit, then tap Check."
    keys = list(expected)
    if _proportional([got[k] for k in keys], [expected[k] for k in keys]):
        if all(got[k] == expected[k] for k in keys):
            return "correct", ""
        said = _join([_fmt(got[k]) for k in keys])
        simplest = _join([_fmt(expected[k]) for k in keys])
        g = math.gcd(*[expected[k] for k in keys])
        return "correct", (f"{said} is the same as {simplest}, and {simplest} is the simplest. " if g == 1
                           else f"{said} is the same as {simplest}. ")
    if sorted(got.values()) == sorted(expected.values()) or \
            _proportional(sorted(got.values()), sorted(expected.values())):
        return "wrong", "Close! You have them the wrong way round. "
    return "wrong", "Not quite. "


def _proportional(a: list[int], b: list[int]) -> bool:
    """6:2 is 3:1. Every pair cross-multiplies to the same."""
    return len(a) == len(b) and all(a[i] * b[j] == a[j] * b[i] for i in range(len(a)) for j in range(len(a)))


def _check_mark(cp: Checkpoint, value) -> tuple[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("bar"), str):
        return "unusable", SHOW_ON_BOARD
    bar, (first, last) = cp.expected["bar"], cp.expected["segments"]
    rows = {r["id"]: r for r in cp.task["rows"]}
    if value["bar"] not in rows:
        return "unusable", SHOW_ON_BOARD
    if value.get("whole") is True:  # the brace under the whole bar
        if value["bar"] != bar:
            return "wrong", "Not that bar. "
        if first == 0 and last == len(rows[bar]["segments"]) - 1:
            return "correct", ""
        return "wrong", "Right bar, but the question is about one part of it, not the whole bar. "
    seg = value.get("segment")
    if seg is None:  # a spoken "on Ben": enough only when the question is about the whole bar
        if value["bar"] != bar:
            return "wrong", "Not that bar. "
        if first == 0 and last == len(rows[bar]["segments"]) - 1:
            return "correct", ""
        return "unusable", f"Yes, it's on {_bar_name(rows[bar])}. Can you tap the part on the board?"
    seg = _whole(seg)
    if seg is None:
        return "unusable", SHOW_ON_BOARD
    if value["bar"] == bar and first <= seg <= last:
        return "correct", ""
    if value["bar"] == bar:
        return "wrong", "Right bar, but not that part. "
    return "wrong", "Not quite. "


def _check_move(cp: Checkpoint, value) -> tuple[str, str]:
    if not isinstance(value, dict):
        return "unusable", SHOW_ON_BOARD
    ids = {r["id"] for r in cp.task["rows"]}
    src, dst = value.get("from"), value.get("to")
    if src not in ids or dst not in ids or src == dst:
        return "unusable", "Drag the piece from one bar onto another bar."
    if src == cp.expected["from"] and dst == cp.expected["to"]:
        return "correct", ""
    if src == cp.expected["to"] and dst == cp.expected["from"]:
        return "wrong", "That's the other way round. "
    return "wrong", "Not quite. "


def _check_grow(cp: Checkpoint, value) -> tuple[str, str]:
    ids = {r["id"] for r in cp.task["rows"]}
    got = value.get("bars") if isinstance(value, dict) else None
    if not isinstance(got, list) or not got or not all(isinstance(b, str) and b in ids for b in got):
        return "unusable", "Tap + years on a bar, then tap Check."
    if sorted(set(got)) == cp.expected["bars"]:
        return "correct", ""
    only = next(r["label"] for r in cp.task["rows"] if r["id"] == got[0])
    return "wrong", (f"Does only {only} get older? " if len(set(got)) == 1 else "Not quite. ")


# ---------------------------------------------------------------- voice: a tiny grammar over the bar labels
_CLAUSE = re.compile(r",|;|\band\b|\bthen\b", re.I)


def _mentions(text: str, rows: list[dict]) -> list[tuple[int, dict]]:
    """(position, row) for each bar label said, in order: 'kavya', "kavya's"."""
    out = []
    low = text.lower()
    for r in rows:
        label = re.escape(r["label"].lower().strip())
        if not label:
            continue
        for m in re.finditer(rf"\b{label}(?:'s|s)?\b", low):
            out.append((m.start(), r))
    return sorted(out, key=lambda t: t[0])


def parse_said(cp: Checkpoint, text: str):
    """What a spoken or typed board move says, as the browser would send it; None if it can't be read."""
    op, rows = (cp.task or {}).get("op"), (cp.task or {}).get("rows") or []
    if op == "units":
        found: dict[str, int] = {}
        for clause in _CLAUSE.split(text):
            said = _mentions(clause, rows)
            nums = numbers_in(clause)
            if not nums and re.search(r"\ba unit\b", clause, re.I):
                nums = [1.0]
            if len({r["id"] for _, r in said}) != 1 or len(nums) != 1 or nums[0] != int(nums[0]):
                continue
            found[said[0][1]["id"]] = int(nums[0])
        return found if len(found) == len(rows) else None
    if op == "mark":
        said = {r["id"]: r for _, r in _mentions(text, rows)}
        if len(said) != 1:
            return None  # no bar, two bars, or a label two stages share: point at it instead
        bid = next(iter(said))
        if sum(1 for r in rows if r["label"].lower() == said[bid]["label"].lower()) > 1:
            return None
        return {"bar": bid, "segment": None}
    if op == "move":
        said = _mentions(text, rows)
        names = list(dict.fromkeys(r["id"] for _, r in said))
        labels = {r["label"].lower() for r in rows}
        if len(names) != 2 or len(labels) != len(rows):
            return None
        a, b = names
        if re.search(r"\bto\b.*\bfrom\b", text, re.I):  # "to Ben from Ali"
            a, b = b, a
        return {"from": a, "to": b}
    if op == "grow":
        if re.search(r"\b(both|all|everyone|everybody|each)\b", text, re.I):
            return {"bars": [r["id"] for r in rows]}
        names = list(dict.fromkeys(r["id"] for _, r in _mentions(text, rows)))
        return {"bars": names} if names else None
    return None


# ---------------------------------------------------------------- words
def _units(k: int) -> str:
    return f"{k} unit{'' if k == 1 else 's'}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


