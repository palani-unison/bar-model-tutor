"""Live lesson: a verified BarSpec turned into steps the teacher shows and questions the child answers.

The child does the thinking. At each checkpoint the board stops, the teacher asks
("How many units are there altogether?"), and code — never a model — checks the
child's number against the verified spec. Two hints, then the teacher shows it.
Everything here is deterministic, so a lesson can never state an unverified number.

v1 specs (a `model` field) and v2 specs (`"version": 2`, docs/spec-v2.md §6) both work.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import sympy as sp

from .spec_v2 import Solution, SpecError, bars_of, parse_expr, solve_spec
from .verify import _close, unit_value_of

HINTS_BEFORE_REVEAL = 2

_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?|[a-z]+", re.I)


def numbers_in(text: str) -> list[float]:
    """Numbers a child said or typed: digits ("90", "$1,200") and simple words ("ninety", "twenty four", "a hundred")."""
    out: list[float] = []
    current: float | None = None
    for tok in _TOKEN.findall(text.lower()):
        if tok[0] in "$0123456789":
            if current is not None:
                out.append(current)
                current = None
            out.append(float(tok.lstrip("$").replace(",", "")))
        elif tok in _WORDS:
            value = _WORDS[tok]
            # "twenty four" -> 24, "a hundred and twenty" -> 120; "four twenty" stays two numbers.
            if current is not None and ((current >= 100 and current % 100 == 0 and value < 100)
                                        or (current % 100 >= 20 and current % 10 == 0 and value < 10)):
                current += value
            else:
                if current is not None:
                    out.append(current)
                current = float(value)
        elif tok == "hundred":
            current = (current or 1) * 100
        elif tok == "and" and current is not None and current >= 100:
            continue  # "a hundred and twenty"
        elif current is not None and tok != "a":
            out.append(current)
            current = None
    if current is not None:
        out.append(current)
    return out


@dataclass
class Checkpoint:
    key: str  # "units" | "unit" | "answer" | "step1".. ; board (agent/board_task.py): "build" | "mark" | "move"
    ask: str
    expected: float | dict  # a board checkpoint's is a dict: {"kavya_bar": 4, "lily_bar": 1}, {"bar", "segments"} ...
    hints: list[str]
    praise: str
    reveal: str
    kind: str = "number"  # "number" | "board"
    task: dict | None = None  # board: what the browser's builder shows ({"op": "units"|"mark"|"move", "rows": [...]})


@dataclass
class Verdict:
    result: str  # "correct" | "try_again" | "revealed" | "no_number" | "show_board" (a board move it couldn't read)
    say: str
    heard: float | None = None
    board: dict | None = None  # a board checkpoint: the move it checked (or, revealed, the right one) for the builder


def _fmt(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".") if x != int(x) else f"{int(x):,}"


def _noun(spec: dict) -> str:
    """'marbles' from the answer step ('Ali has 90 marbles.'), else 'of them'."""
    for step in spec["timeline"]:
        if step.get("do") == "answer":
            m = re.search(r"\d[\d,.]*\s+([a-z]+)", step.get("text", ""), re.I)
            if m:
                return m.group(1)
    return ""


def shows(step: dict, value: float | None) -> bool:
    """Does this step put `value` on the board as a result ("… = 30", a unit label, the answer)?"""
    if value is None or step.get("do") not in ("write", "fill_cell", "mark_point", "label_unit", "answer"):
        return False
    results = [numbers_in(step.get("text", ""))[-1:], numbers_in(step.get("say", ""))[-1:]]
    if step.get("do") == "answer":
        results.append(numbers_in(step.get("text", "")))
    return any(_close(n, value) for found in results for n in found)


def is_v2(spec: dict) -> bool:
    return isinstance(spec, dict) and spec.get("version") == 2


def checkpoints(spec: dict, solution: Solution | None = None) -> dict[int, Checkpoint]:
    """Checkpoints keyed by the timeline index they stop before."""
    if is_v2(spec):
        return _checkpoints_v2(spec, solution or solve_spec(spec))
    model = spec["model"]
    bars = {b["id"]: b for b in model["bars"]}
    unit, _ = unit_value_of(model)
    asked = bars[model["asked"]]
    noun = _noun(spec)
    timeline = spec["timeline"]
    answer_value = float(model["answer"])

    # Each question stops the board just before the first step that would show its answer.
    first_write = next((i for i, s in enumerate(timeline) if s.get("do") == "write"), None)
    unit_write = next((i for i, s in enumerate(timeline) if shows(s, unit)), None)
    answer_at = next((i for i, s in enumerate(timeline)
                      if i > (unit_write if unit_write is not None else -1) and shows(s, answer_value)), None)
    if unit is not None and _close(answer_value, unit) and asked["units"] == 1 and not asked.get("extra"):
        answer_at = None  # finding 1 unit already answers the question; don't ask the same thing twice
    if first_write is not None and unit_write is not None and first_write > unit_write:
        first_write = None  # the unit value comes first; counting units would be asked too late to help

    out: dict[int, Checkpoint] = {}
    if first_write is not None and first_write != unit_write:
        diff = model.get("difference")
        if diff:
            more, less = bars[diff["more"]], bars[diff["less"]]
            n = more["units"] - less["units"]
            out[first_write] = Checkpoint(
                "units", f"Look at the bars. How many more units does {more['label']} have than {less['label']}?", n,
                [f"Count {more['label']}'s units, then take away {less['label']}'s units.",
                 f"{more['label']} has {more['units']} units and {less['label']} has {less['units']}. What is {more['units']} take away {less['units']}?"],
                f"Yes! {_fmt(n)} units. Those {_fmt(n)} units are the difference.",
                f"Let's count together. {more['units']} take away {less['units']} is {_fmt(n)} units.")
        else:
            n = sum(b["units"] for b in bars.values())
            parts = " plus ".join(str(b["units"]) for b in bars.values())
            out[first_write] = Checkpoint(
                "units", "Look at the bars. How many units are there altogether?", n,
                ["Count every small box in all the bars.", f"Add the units in each bar: {parts}."],
                f"Yes! {_fmt(n)} units altogether.",
                f"Let's count together. {parts} makes {_fmt(n)} units.")
    if unit_write is not None and unit is not None:
        diff = model.get("difference")
        if diff:
            n = bars[diff["more"]]["units"] - bars[diff["less"]]["units"]
            whole = float(diff["value"])
        else:
            n = sum(b["units"] for b in bars.values())
            whole = float(model["total"]) - sum(float(b.get("extra") or 0) for b in bars.values())
        out[unit_write] = Checkpoint(
            "unit", "So what is 1 unit worth?", unit,
            [f"{_fmt(n)} units are worth {_fmt(whole)}. How can we share {_fmt(whole)} into {_fmt(n)} equal parts?",
             f"Try {_fmt(whole)} divided by {_fmt(n)}."],
            f"Well done! 1 unit is {_fmt(unit)}.",
            f"{_fmt(whole)} divided by {_fmt(n)} is {_fmt(unit)}. So 1 unit is {_fmt(unit)}.")
    if answer_at is not None and unit is not None:
        ans = float(model["answer"])
        extra = float(asked.get("extra") or 0)
        how = f"How many {noun} does {asked['label']} have?" if noun else f"What is {asked['label']}'s amount?"
        build = f"{asked['label']} has {asked['units']} unit{'s' if asked['units'] != 1 else ''}" + (f" and {_fmt(extra)} more" if extra else "")
        out[answer_at] = Checkpoint(
            "answer", f"Now you try the last step. {how}", ans,
            [f"Look at {asked['label']}'s bar. {build}.",
             f"Each unit is {_fmt(unit)}. What is {asked['units']} times {_fmt(unit)}" + (f", plus {_fmt(extra)}?" if extra else "?")],
            f"Excellent! {asked['label']} has {_fmt(ans)}" + (f" {noun}." if noun else "."),
            f"{asked['units']} times {_fmt(unit)}" + (f" plus {_fmt(extra)}" if extra else "") + f" is {_fmt(ans)}.")
    return out


# ---------------------------------------------------------------- v2 (docs/spec-v2.md §6)
MAX_CHECKPOINTS = 4
# Steps whose text is a line of working: "1 unit = 24 ÷ 4 = 6", a table cell "18 ÷ 3 = 6", a number line's "3 × 70 = 210 km".
WORKED = ("write", "fill_cell", "mark_point")
BLANK = ("", "?")
_OP_WORDS = {"+": "plus", "-": "take away", "×": "times", "÷": "divided by"}
_EXPR_TOKEN = re.compile(r"\d+(?:\.\d+)?|[-+×÷()]")
_THE_NOUNS = {"difference", "number", "total", "cost", "amount", "sum", "price", "change", "value", "mass",
              "volume", "length", "area", "perimeter", "remainder", "average", "time", "distance", "speed", "share"}


@dataclass
class OpLine:
    """A working line with arithmetic, e.g. '1 unit = 24 ÷ 4 = 6'."""

    lhs: str  # "1 unit" (empty when the line is only "24 ÷ 4 = 6")
    tokens: list[str]  # ["24", "÷", "4"]
    result: float

    @property
    def binary(self) -> tuple[float, str, float] | None:
        if len(self.tokens) == 3 and self.tokens[1] in _OP_WORDS:
            return float(self.tokens[0]), self.tokens[1], float(self.tokens[2])
        return None

    @property
    def words(self) -> str:
        """'24 divided by 4', for the teacher to say."""
        out = " ".join(_fmt(float(t)) if t[0].isdigit() else _OP_WORDS.get(t, t) for t in self.tokens)
        return out.replace("( ", "(").replace(" )", ")")


def op_line(text: str) -> OpLine | None:
    """The arithmetic in a working line, if it has an operator and adds up ('4 units = 24' has none)."""
    parts = [p.strip() for p in str(text or "").split("=")]
    if len(parts) < 2:
        return None
    result = numbers_in(parts[-1])
    if len(result) != 1:
        return None
    expr = re.sub(r"(?<=\d),(?=\d)", "", parts[-2]).replace("$", "").replace("%", "")
    expr = expr.replace("*", "×").replace("/", "÷").replace("−", "-").replace("–", "-")
    expr = re.sub(r"(?<=[\d\s)])[xX](?=[\s\d(])", "×", expr).strip()
    if not expr or re.search(r"[^\d\s.+\-×÷()]", expr) or not re.search(r"[\d)]\s*[-+×÷]\s*[\d(]", expr):
        return None
    try:
        value = float(parse_expr(expr, {}))
    except (SpecError, TypeError, ValueError):
        return None
    if not _close(value, result[0]):
        return None
    return OpLine(parts[-3] if len(parts) >= 3 else "", _EXPR_TOKEN.findall(expr), result[0])


def _op_hints(op: OpLine) -> tuple[str, str]:
    """(a gentle hint, a more direct one) for working out `op` by hand."""
    b = op.binary
    if b is None:
        brackets = "(" in op.tokens
        return ("Work it out one step at a time.",
                "Do the part in brackets first." if brackets else "Do times and divide first, then plus and take away, from left to right.")
    x, sign, y = b
    fx, fy = _fmt(x), _fmt(y)
    if sign == "÷":
        return f"Share {fx} into {fy} equal groups. How many are in each group?", f"{fy} times what number makes {fx}?"
    if sign == "×":
        n, m = (x, y) if x == int(x) and x <= y else (y, x)
        group = f"Think of {_fmt(n)} groups of {_fmt(m)}."
        if n == int(n) and 2 <= n <= 5:
            return group, "What is " + " plus ".join([_fmt(m)] * int(n)) + "?"
        return group, f"Count in {_fmt(m)}s, {_fmt(n)} times: {_fmt(m)}, {_fmt(2 * m)}, and so on."
    if sign == "-":
        return f"Start at {fx} and take away {fy}.", f"What number plus {fy} makes {fx}?"
    tens = int(y // 10 * 10)
    if x == int(x) and y == int(y) and x % 10 == 0 and y % 10 == 0 and x and y:
        return f"Put {fx} and {fy} together.", f"{_fmt(x / 10)} tens and {_fmt(y / 10)} tens make how many tens?"
    if x == int(x) and y == int(y) and y > 10 and tens != y:
        return f"Put {fx} and {fy} together.", f"{fx} plus {tens} is {_fmt(x + tens)}. Now add {_fmt(y - tens)} more."
    return f"Put {fx} and {fy} together.", f"Start at {fx} and count on {fy}."


def _phrase(lhs: str, question: str = "") -> str:
    """'Difference in shortage' -> 'the difference in shortage'; '1 unit' and names ('Amy's beads') stay as they are.

    A first word is a name when the question writes it capitalised and never in lower case.
    """
    lhs = lhs.strip().rstrip(":").strip()
    if not lhs or lhs[0].isdigit() or lhs[0] == "$":
        return lhs
    first = re.sub(r"'s$", "", lhs.split()[0])
    low = first.lower()
    if low in {"the", "each", "every", "a", "an", "one", "all", "both"}:
        return lhs[0].lower() + lhs[1:]
    named = re.search(rf"\b{re.escape(first)}\b", question) and not re.search(rf"\b{re.escape(low)}\b", question)
    if named and low not in _THE_NOUNS:
        return lhs
    return "the " + lhs[0].lower() + lhs[1:]


def _final_question(question: str) -> str:
    """The sentence that asks: 'How many beads did Amy have at first?'."""
    asks = [s.strip() for s in re.split(r"(?<=[.!?])\s+", question.strip()) if s.strip().endswith("?")]
    return asks[-1] if asks else ""


def _unit_parts(expr: str, units: list[str]) -> tuple[dict[str, float], float]:
    """'3*u + 20' -> ({'u': 3}, 20): how many of each unit a segment holds, and its plain amount."""
    syms = {n: sp.Symbol(n, real=True) for n in units}
    try:
        e = sp.expand(parse_expr(expr, syms))
    except SpecError:
        return {}, 0.0
    coeffs = {n: float(e.coeff(s)) for n, s in syms.items()}
    return {n: c for n, c in coeffs.items() if c}, float(e.subs({s: 0 for s in syms.values()}))


def _units_text(k: float) -> str:
    return f"{_fmt(k)} unit{'' if k == 1 else 's'}"


def _first_show(timeline: list[dict], value: float, given: list[float], start: int = 0) -> int | None:
    """The first step from `start` that shows `value` as a result, or says it at all (narration like
    "so 4 units make $96" gives it away just as much), unless the question itself gives that number."""
    said = not any(_close(value, g) for g in given)
    for i, step in enumerate(timeline):
        if i < start or step.get("do") == "write_question":
            continue
        if shows(step, value) or (said and any(_close(value, n) for n in numbers_in(f"{step.get('text', '')} {step.get('say', '')}"))):
            return i
    return None


def _known_part(spec: dict, sol: Solution, unit: str, before: int) -> tuple[float, float, str] | None:
    """A part of a bar the board already gives a value for: (units in it, its value, how to point at it)."""
    units = [u.get("name") for u in spec.get("units") or []]
    u = sol.units.get(unit)
    bars = {b["id"]: b for b in bars_of(spec)}
    timeline = spec["timeline"][:before]
    drawn = list(dict.fromkeys(s.get("bar") for s in timeline if s.get("do") == "draw_bar"))  # in order: hints stay deterministic
    for step in timeline:  # a brace over some segments, labelled with a number
        bar, seg, nums = bars.get(step.get("bar")), step.get("segments"), numbers_in(step.get("text", ""))
        if step.get("do") != "bracket" or bar is None or not seg or len(nums) != 1:
            continue
        parts = [_unit_parts(s.get("expr", ""), units) for s in (bar.get("segments") or [])[seg[0]: seg[1] + 1]]
        k, const = sum(p[0].get(unit, 0) for p in parts), sum(p[1] for p in parts)
        if k > 0 and not const and u and _close(k * u, nums[0]):
            return k, nums[0], f"the part marked {step['text']}"
    for bid in drawn:  # a segment whose own label is its value ("gave 20", "+24")
        bar = bars.get(bid) or {}
        for i, s in enumerate(bar.get("segments") or []):
            nums = numbers_in(s.get("label", ""))
            coeffs, const = _unit_parts(s.get("expr", ""), units)
            k = coeffs.get(unit, 0)
            values = sol.segments.get(bid) or []
            value = values[i] if i < len(values) else None
            if len(nums) == 1 and k > 0 and not const and value is not None and _close(value, nums[0]):
                return k, nums[0], f"the part marked “{s['label']}”"
    return None


def _asked_bar_hint(spec: dict, unit: str | None) -> tuple[str, float, float] | None:
    """('the bar for Amy (at first)', units, plain amount) when the question asks for one bar's amount."""
    asked = str((spec.get("asked") or {}).get("expr", "")).strip()
    units = [u.get("name") for u in spec.get("units") or []]
    labels = [b.get("label") for b in bars_of(spec)]
    for stage in spec.get("stages") or []:
        for bar in stage.get("bars") or []:
            if bar.get("quantity") != asked:
                continue
            k = const = 0.0
            for s in bar.get("segments") or []:
                if s.get("style", "normal") != "removed":
                    coeffs, c = _unit_parts(s.get("expr", ""), units)
                    k, const = k + coeffs.get(unit, 0), const + c
            where = f" ({stage.get('label', '').lower()})" if labels.count(bar.get("label")) > 1 and stage.get("label") else ""
            return f"the bar for {bar.get('label')}{where}", k, const
    return None


@dataclass
class _Candidate:
    kind: str  # "unit" | "answer" | "op"
    value: float
    op: OpLine | None = None


def _checkpoints_v2(spec: dict, sol: Solution) -> dict[int, Checkpoint]:
    """The value of 1 unit, each working line's arithmetic, and the answer (spec-v2 §6).

    Each stops the board just before the first step that would show its value; like v1, the answer is looked
    for from the unit's step on, so an early line that happens to equal the answer doesn't ask the last step
    first. Candidates that stop at the same step merge into one question: the unit question first (finding
    1 unit may already answer the question), then the answer, then the arithmetic. The arithmetic of the line
    the board stopped before gives the hints.
    """
    timeline = spec["timeline"]
    given = numbers_in(spec.get("question", ""))
    unit = next((u.get("name") for u in spec.get("units") or [] if u.get("name") in sol.units), None)
    stops: list[tuple[int | None, _Candidate]] = []
    unit_at = _first_show(timeline, sol.units[unit], given) if unit is not None else None
    if unit is not None:
        stops.append((unit_at, _Candidate("unit", sol.units[unit])))
    for step in timeline:
        op = op_line(step.get("text", "")) if step.get("do") in WORKED else None
        if op is not None:
            stops.append((_first_show(timeline, op.result, given), _Candidate("op", op.result, op)))
        elif step.get("do") in ("fill_cell", "mark_point") and (found := numbers_in(str(step.get("text", "")))):
            stops.append((_first_show(timeline, found[-1], given), _Candidate("cell", found[-1])))
    answer = float(spec["answer"])
    stops.append((_first_show(timeline, answer, given, start=unit_at or 0), _Candidate("answer", answer)))

    groups: dict[int, list[_Candidate]] = {}
    for at, c in stops:
        if at is not None:
            groups.setdefault(at, []).append(c)
    rank = {"unit": 0, "answer": 1, "op": 2, "cell": 2}
    chosen: dict[int, tuple[_Candidate, OpLine | None]] = {}
    for at, group in groups.items():
        lead = min(group, key=lambda c: rank[c.kind])
        here = op_line(timeline[at].get("text", "")) if timeline[at].get("do") in WORKED else None
        if here is None or not _close(here.result, lead.value):
            here = next((c.op for c in group if c.op is not None and _close(c.value, lead.value)), None)
        chosen[at] = (lead, here)
    if len(chosen) > MAX_CHECKPOINTS:  # keep the unit and the answer, then the earliest working lines
        keep = sorted(chosen, key=lambda at: (rank[chosen[at][0].kind], at))
        chosen = {at: chosen[at] for at in keep[:MAX_CHECKPOINTS]}

    out: dict[int, Checkpoint] = {}
    n_ops = 0
    for at in sorted(chosen):
        lead, op = chosen[at]
        if lead.kind == "unit":
            out[at] = _unit_checkpoint(spec, sol, unit, at, op)
        elif lead.kind == "answer":
            out[at] = _answer_checkpoint(spec, sol, unit, op)
        else:
            n_ops += 1
            cell = _cell_phrase(spec, timeline[at])
            if op is None:
                out[at] = _cell_checkpoint(lead.value, f"step{n_ops}", cell or "this box of the table")
            else:
                out[at] = _op_checkpoint(op, f"step{n_ops}", spec.get("question", ""), cell)
    return out


def _cell_phrase(spec: dict, step: dict) -> str | None:
    """'the Legs for Assume' for a fill_cell step (its column header and the first cell of its row), and
    'the number line at 3 h' for a mark_point step."""
    if step.get("do") == "mark_point":
        line = next((s for s in spec["timeline"] if s.get("do") == "number_line" and s.get("id") == step.get("line")), {})
        mark = next((m for m in line.get("marks") or [] if isinstance(m, dict) and m.get("at") == step.get("at")), {})
        label = str(mark.get("label", "")).strip()
        return f"the number line at {label}" if label else "the number line"
    if step.get("do") != "fill_cell":
        return None
    table = next((s for s in spec["timeline"] if s.get("do") == "table" and s.get("id") == step.get("table")), None)
    try:
        header = str(table["columns"][step["col"]]).strip()
        row = str(table["rows"][step["row"]][0]).strip() if step["col"] else ""
    except (TypeError, KeyError, IndexError):
        return None
    if not header:
        return None
    return f"the {header}" + (f" for {row}" if row and row not in BLANK else "")


def _cell_checkpoint(value: float, key: str, cell: str) -> Checkpoint:
    v = _fmt(value)
    return Checkpoint(key, f"What goes in {cell}?", value,
                      ["Look at the working and the table. Which number do we know now?",
                       "It is a number we have just worked out on the board."],
                      f"Yes! {v} goes in {cell}.", f"{v} goes in {cell}.")


def _unit_checkpoint(spec: dict, sol: Solution, unit: str, at: int, op: OpLine | None) -> Checkpoint:
    u = sol.units[unit]
    known = _known_part(spec, sol, unit, at)
    if known and known[0] != 1:
        k, n, _ = known
        hint1 = f"{_units_text(k)} are worth {_fmt(n)}. How can we share {_fmt(n)} into {_fmt(k)} equal parts?"
    elif known:
        hint1 = f"Look at {known[2]}. How many units is it?"
    elif op is not None:
        hint1 = _op_hints(op)[0]
    else:
        hint1 = "Look at the bars. Find a part we know the value of, and count its units."
    if op is not None:
        hint2 = f"Try {op.words}."
    elif known and known[0] != 1:
        hint2 = f"Try {_fmt(known[1])} divided by {_fmt(known[0])}."
    elif known:
        hint2 = f"That part is just 1 unit, and it is {_fmt(known[1])}."
    else:
        hint2 = "Share the amount we know equally between its units."
    lead = f"{op.words} is {_fmt(u)}. " if op is not None else ""
    return Checkpoint("unit", "So what is 1 unit worth?", u, [hint1, hint2],
                      f"Well done! 1 unit is {_fmt(u)}.", f"{lead}So 1 unit is {_fmt(u)}.")


def _answer_checkpoint(spec: dict, sol: Solution, unit: str | None, op: OpLine | None) -> Checkpoint:
    ans = float(spec["answer"])
    said = next((s.get("text", "") for s in spec["timeline"] if s.get("do") == "answer" and s.get("text")), "")
    question = _final_question(spec.get("question", "")) or f"What is {(spec.get('asked') or {}).get('label', 'the answer')}?"
    bar = _asked_bar_hint(spec, unit) if unit else None
    if bar and bar[1] > 0:
        hint1 = f"Look at {bar[0]}. It has {_units_text(bar[1])}" + (f" and {_fmt(bar[2])} more." if bar[2] else ".")
    elif op is not None:
        hint1 = _op_hints(op)[0]
    else:
        hint1 = "Look at the working on the board. Which number does the question ask for?"
    if op is not None:
        hint2 = f"Try {op.words}."
    elif bar and bar[1] > 0 and unit:
        u = sol.units[unit]
        hint2 = f"Each unit is {_fmt(u)}. What is {_fmt(bar[1])} times {_fmt(u)}" + (f", plus {_fmt(bar[2])}?" if bar[2] else "?")
    else:
        hint2 = "Use the numbers we just worked out on the board."
    lead = f"{op.words} is {_fmt(ans)}. " if op is not None else ""
    return Checkpoint("answer", f"Now you try the last step. {question}", ans, [hint1, hint2],
                      f"Excellent! {said}" if said else f"Excellent! The answer is {_fmt(ans)}.",
                      lead + (said or f"The answer is {_fmt(ans)}."))


def _op_checkpoint(op: OpLine, key: str, question: str, cell: str | None = None) -> Checkpoint:
    phrase = _phrase(op.lhs, question)
    if cell and not phrase:
        ask = f"To fill in {cell}, what is {op.words}?"
    else:
        ask = f"To find {phrase}, what is {op.words}?" if phrase else f"Your turn. What is {op.words}?"
    hint1, hint2 = _op_hints(op)
    r = _fmt(op.result)
    return Checkpoint(key, ask, op.result, [hint1, hint2], f"Yes! {op.words} is {r}.",
                      f"{op.words} is {r}." + (f" So {phrase} is {r}." if phrase else ""))


@dataclass
class Lesson:
    """Server-side state for one live lesson. `steps` are the timeline steps the board draws.

    `pacing` (agent/learner.py, only for a signed-in child) changes the flow; None is the classic lesson:
      mode "challenge_first": after the question is written, ask for the final answer straight away. Right ->
          a recap (every remaining step in one event); wrong -> the full guided lesson.
      checkpoints "key": only the 1-unit checkpoint and the answer (or the last working line if there's no unit).
    With any pacing, the final answer given early (at an earlier checkpoint, or while the board is drawing)
    also switches to the recap.
    """

    spec: dict
    position: int = 0
    hints_used: int = 0
    waiting: Checkpoint | None = None
    stops: dict[int, Checkpoint] = field(default_factory=dict)
    score: dict[str, str] = field(default_factory=dict)  # checkpoint key -> "first try" | "with hints" | "shown" (| "early" | "missed")
    solution: Solution | None = None  # v2: the solved spec (solve_spec), so it isn't solved twice
    pacing: dict | None = None
    seconds: dict[str, float] = field(default_factory=dict)  # checkpoint key -> seconds from asking to settling
    board: bool = False  # "build it yourself" board checkpoints (agent/board_task.py); only a client that draws them

    def __post_init__(self):
        if is_v2(self.spec) and self.solution is None:
            self.solution = solve_spec(self.spec)
        self.stops = checkpoints(self.spec, self.solution)
        if self.board and is_v2(self.spec):
            from .board_task import board_checkpoints

            for at, cp in board_checkpoints(self.spec, self.solution).items():
                self.stops.setdefault(at, cp)  # a number question at the same step keeps it
        # The video's "pause and think" steps are replaced by real questions in a live lesson.
        self.steps = [s for s in self.spec["timeline"]]
        self.final = final_answer(self.spec)
        self.all_stops = dict(self.stops)
        self.challenge: Checkpoint | None = None
        self.recap_pending = self.recapped = False
        self.asked_at: float | None = None
        pacing = self.pacing or {}
        if pacing.get("checkpoints") == "key":
            self.stops = key_checkpoints(self.stops)
        if pacing.get("mode") == "challenge_first" and self.final is not None:
            self.challenge = challenge_checkpoint(self.spec, self.final)

    @property
    def done(self) -> bool:
        return self.position >= len(self.steps) and self.waiting is None and not self.recap_pending

    @property
    def checkpoint_count(self) -> int:
        return len(self.stops) + (1 if self.challenge is not None else 0)

    def _ask(self, cp: Checkpoint, index: int) -> dict:
        self.waiting, self.hints_used, self.asked_at = cp, 0, time.monotonic()
        event = {"type": "ask", "key": cp.key, "say": cp.ask, "index": index}
        if cp.kind == "board":
            event["board"] = cp.task
        return event

    def _settle(self, key: str, outcome: str) -> None:
        self.score[key] = outcome
        if self.asked_at is not None:
            self.seconds[key] = round(time.monotonic() - self.asked_at, 1)
        self.asked_at = None
        self.waiting = None

    def next_event(self) -> dict | None:
        """The next thing to show: {"type": "step"|"ask"|"recap", ...}. None when the lesson is over."""
        if self.waiting is not None:
            return None
        if self.recap_pending:
            return self._recap()
        if self.challenge is not None and self.position > self._question_at():
            cp, self.challenge = self.challenge, None
            return self._ask(cp, self.position)
        while self.position < len(self.steps):
            i = self.position
            cp = self.stops.pop(i, None)
            if cp is not None:
                return self._ask(cp, i)
            self.position += 1
            step = self.steps[i]
            if step.get("do") == "pause" and any(k > i for k in self.stops):
                continue  # a checkpoint asks the child for real instead
            return {"type": "step", "index": i, "step": step, "say": step.get("say", "")}
        return None

    def skip_read(self, highlights: bool) -> int:
        """A prepared lesson (app/lesson_ws.py "prepare"): the question is already on the board, and its key facts
        too when `highlights`, so start at beat 2 (Draw). Skips only the leading read steps; returns how many."""
        read = ("write_question", "highlight") if highlights else ("write_question",)
        start = self.position
        while (self.position < len(self.steps) and self.position not in self.stops
               and self.steps[self.position].get("do") in read):
            self.position += 1
        return self.position - start

    def _question_at(self) -> int:
        """The challenge is asked once the question is on the board (just after write_question)."""
        return next((i for i, s in enumerate(self.steps) if s.get("do") == "write_question"), -1)

    def _recap(self) -> dict:
        """Every remaining step in one event: the board draws them all at once, with one short narration."""
        rest = [{"index": i, "step": s} for i, s in enumerate(self.steps)
                if i >= self.position and s.get("do") != "pause"]
        self.position, self.stops, self.recap_pending, self.recapped = len(self.steps), {}, False, True
        answer = next((s.get("text") for s in self.steps if s.get("do") == "answer" and s.get("text")), "")
        say = "Here is all the working in one go." + (f" {answer}" if answer else "")
        return {"type": "recap", "steps": rest, "say": say}

    def is_final_answer(self, text: str) -> bool:
        """Did the child just say the final answer before the board has shown it?"""
        if self.pacing is None or self.final is None or self.recapped or self.recap_pending:
            return False  # anonymous lessons stay exactly as before
        said = numbers_in(text)
        if not said or not _close(said[-1], self.final):
            return False
        if any(_close(self.final, g) for g in numbers_in(self.spec.get("question", ""))):
            return False  # the question gives that number, so saying it proves nothing
        shown_at = next((i for i, s in enumerate(self.steps) if shows(s, self.final)), None)
        return shown_at is None or self.position <= shown_at

    def early_answer(self) -> Verdict:
        """The final answer, right, before it was asked: skip to the recap."""
        if self.waiting is not None:
            self._settle(self.waiting.key, "early")
        self.score.setdefault("answer", "early")
        self.recap_pending = True
        return Verdict("correct", f"Wow, {_fmt(self.final)} is the answer! You worked it out already.", self.final)

    def answer(self, text: str) -> Verdict:
        """Check the child's reply to the open checkpoint. Code decides; nothing is model-judged."""
        cp = self.waiting
        if cp is None:
            raise RuntimeError("no question is waiting for an answer")
        if cp.kind == "board":
            return self._board_said(text)
        said = numbers_in(text)
        if cp.key == "challenge":
            if said and _close(said[-1], cp.expected):
                self._settle("challenge", "first try")
                self.recap_pending = True
                return Verdict("correct", cp.praise, said[-1])
            return self._challenge_missed(said[-1] if said else None)
        if not said:
            return self._hint(None, "That's okay. Here's a clue. ")
        heard = said[-1]  # the number they settle on: "3, no, 4" -> 4
        if _close(heard, cp.expected):
            self._settle(cp.key, "first try" if self.hints_used == 0 else "with hints")
            return Verdict("correct", cp.praise, heard)
        if cp.key != "answer" and self.is_final_answer(text):
            return self.early_answer()
        return self._hint(heard, f"Hmm, {_fmt(heard)} is not quite it. ")

    def answer_board(self, value) -> Verdict:
        """Check a board move from the builder against the verified spec: the same ladder as a number (right, or
        two hints, then the teacher shows it), the same score outcomes. Right or shown, the real draw step follows."""
        from .board_task import check

        cp = self.waiting
        if cp is None or cp.kind != "board":
            raise RuntimeError("no board task is waiting")
        result, lead = check(cp, value)
        if result == "unusable":
            return Verdict("show_board", lead)  # not an attempt: no clue spent
        if result == "correct":
            self._settle(cp.key, "first try" if self.hints_used == 0 else "with hints")
            return Verdict("correct", f"Yes! {lead}{cp.praise.removeprefix('Yes! ')}" if lead else cp.praise, board=value)
        if self.hints_used < HINTS_BEFORE_REVEAL:
            hint = cp.hints[self.hints_used]
            self.hints_used += 1
            return Verdict("try_again", lead + hint, board=value)
        self._settle(cp.key, "shown")
        return Verdict("revealed", cp.reveal, board=cp.expected)

    def _board_said(self, text: str) -> Verdict:
        """A spoken or typed board move ("Kavya has four units, Lily has one"), read by a tiny grammar; never a model."""
        from .board_task import SHOW_ON_BOARD, parse_said

        value = parse_said(self.waiting, text)
        if value is None:
            return Verdict("show_board", SHOW_ON_BOARD)
        return self.answer_board(value)

    def _challenge_missed(self, heard: float | None) -> Verdict:
        """A wrong (or no) challenge answer: no hints, straight into the full guided lesson."""
        self._settle("challenge", "missed")
        self.stops = {i: cp for i, cp in self.all_stops.items() if i >= self.position}
        lead = f"{_fmt(heard)} is not quite it. " if heard is not None else ""
        return Verdict("guided", lead + "Let's draw the bar model together, step by step.", heard)

    def skip(self) -> Verdict:
        """The child asked to be shown. Counts as revealed."""
        cp = self.waiting
        if cp is not None and cp.key == "challenge":
            return self._challenge_missed(None)
        self.hints_used = HINTS_BEFORE_REVEAL
        return self._hint(None, "") if cp else Verdict("no_number", "")

    def _hint(self, heard: float | None, lead: str) -> Verdict:
        cp = self.waiting
        if self.hints_used < HINTS_BEFORE_REVEAL:
            hint = cp.hints[self.hints_used]
            self.hints_used += 1
            return Verdict("try_again" if heard is not None else "no_number", lead + hint, heard)
        self._settle(cp.key, "shown")
        return Verdict("revealed", cp.reveal, heard, board=cp.expected if cp.kind == "board" else None)

    def pending_answers(self) -> list[float]:
        """Numbers the child has not worked out yet. A tutor reply must not give these away."""
        out = [cp.expected for cp in self.stops.values() if cp.kind == "number"]
        if self.waiting is not None and self.waiting.kind == "number":
            out.append(self.waiting.expected)
        return out

    def results(self) -> list[tuple[str, str, float | None]]:
        """(checkpoint key, outcome, seconds) for the learner model."""
        return [(k, v, self.seconds.get(k)) for k, v in self.score.items()]


# ---------------------------------------------------------------- adaptive pacing helpers
def final_answer(spec: dict) -> float | None:
    try:
        return float(spec["answer"]) if is_v2(spec) else float(spec["model"]["answer"])
    except (KeyError, TypeError, ValueError):
        return None


def key_checkpoints(stops: dict[int, Checkpoint]) -> dict[int, Checkpoint]:
    """Strong learners: the 1-unit checkpoint and the answer; without a unit, the last working line too."""
    keep = {i: cp for i, cp in stops.items() if cp.key in ("unit", "answer")}
    if not any(cp.key == "unit" for cp in keep.values()):
        steps = [i for i, cp in stops.items() if cp.key not in ("unit", "answer", "units") and cp.kind == "number"]
        if steps:
            keep[max(steps)] = stops[max(steps)]
    return keep or dict(stops)


def challenge_checkpoint(spec: dict, final: float) -> Checkpoint:
    question = _final_question(spec.get("question", "")) or "What is the answer?"
    said = next((s.get("text", "") for s in spec["timeline"] if s.get("do") == "answer" and s.get("text")), "")
    return Checkpoint("challenge", f"Challenge! Can you work out the answer straight away? {question}", final, [],
                      f"Brilliant! {said}" if said else f"Brilliant! The answer is {_fmt(final)}.", "")
