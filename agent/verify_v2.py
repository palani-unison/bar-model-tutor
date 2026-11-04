"""BarSpec v2 verification (docs/spec-v2.md §4, checks 1-7). No LLM calls.

solve_spec (agent/spec_v2.py) fixes every number from the question's relations. This module checks
that the rest of the spec agrees with that solution and with the question:

  1. the JSON schema;
  2. the relations solve, and `answer` is the solved `asked`;
  3. every `from` is quoted from the question, and every number a relation uses is in it;
  4. the bars draw real quantities, and the template has the right number of stages;
  5. the timeline names real bars, stages and segments, and keeps the teaching rules;
  6. each line of working is right and, in order, uses only numbers already known;
  7. every number the board shows or the teacher says is known.

Layout (8) and the independent second solve (9) stay in plan.py. Each error is one short sentence
naming the relation or timeline step, because the list goes back to the planning model for a retry.
"""
from __future__ import annotations

import json
import math
import operator
import re
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import sympy as sp

from .spec_v2 import (COUNTED_STYLES, Solution, SpecError, bars_of, expr_numbers, parse_expr, parse_relation,
                      solve_spec)
from .verify import _UNITS, _WORD_NUMBERS, VerifyResult, _close, _eval_expr, _numbers

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "specs" / "barspec.v2.schema.json"

MAX_SAY_WORDS = 20
MAX_SCHEMA_ERRORS = 5
# Numbers a relation may use although the question does not say them (§4.3).
RELATION_STRUCTURAL = (0, 1, 2, 10, 100)
# Small counts the board and the teacher use for units, bars and stages, plus 100 for "100%" (§4.7).
SMALL_COUNTS = tuple(range(13)) + (100,)
# Families whose relations need counts the question leaves implicit: 3 children averaged, 4 legs on a rabbit.
IMPLICIT_COUNTS = {"average", "assumption"}
# A table (or a number line) is the picture of a working-template board: it sits where the bars would be.
CHART_ACTIONS = {"table", "fill_cell", "number_line", "mark_point"}
WORKING_ACTIONS = {"write_question", "highlight", "pause", "write", "answer"} | CHART_ACTIONS
MAX_TABLE_COLS, MAX_TABLE_ROWS, MAX_CELL_CHARS = 4, 6, 18
MAX_MARKS, MAX_HOPS, MAX_LINE_LABEL = 8, 6, 18
BLANK_CELLS = ("", "?")
ONE_BAR_ACTIONS = {"draw_bar", "divide_units", "label_unit", "question_mark"}

# Conversion factors a rate or measurement question relies on without stating them.
_CONVERSIONS = (
    (re.compile(r"\b(?:h|hrs?|hours?|mins?|minutes?|secs?|seconds?)\b", re.I), (60, 3600)),
    (re.compile(r"\b(?:kg|g|km|m|ml|l|m?ℓ|grams?|kilograms?|(?:kilo|milli)?(?:metres?|meters?|litres?|liters?))\b"
                r"|\bm?ℓ", re.I), (1000,)),  # papers often print litres as "ℓ" and "mℓ"
    (re.compile(r"\b(?:cm|centi(?:metres?|meters?))\b", re.I), (100,)),
    (re.compile(r"\b(?:mm|milli(?:metres?|meters?))\b", re.I), (10,)),
    (re.compile(r"\b(?:days?|weeks?)\b", re.I), (7, 24)),
    (re.compile(r"\b(?:months?|years?)\b", re.I), (12,)),
    (re.compile(r"¢|\bcents?\b", re.I), (100,)),  # 20¢ coins and dollars
)
# Clock times ("8 a.m.", "8.05 a.m.", "2:30 p.m.", "noon"): the minutes and hours between them are the question's.
_CLOCK = re.compile(r"\b(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?\s?m\b\.?|\b(noon|midnight)\b", re.I)


def _clock_gaps(question: str) -> list[float]:
    times = []
    for m in _CLOCK.finditer(question):
        if m.group(4):
            times.append(12 * 60 if m.group(4).lower() == "noon" else 0)
            continue
        h, mins = int(m.group(1)) % 12, int(m.group(2) or 0)
        times.append((h + (12 if m.group(3).lower() == "p" else 0)) * 60 + mins)
    gaps = []
    for a, b in combinations(sorted(set(times)), 2):
        gaps += [float(b - a), (b - a) / 60]
    return gaps


_ONES = {"one": 1, **_WORD_NUMBERS}
_WORDS = {**_ONES, "zero": 0, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "hundred": 100, "thousand": 1000,
          "twice": 2, "thrice": 3, "double": 2, "triple": 3, "dozen": 12}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_FRACTION_WORDS = {"half": 2, "halves": 2, "third": 3, "thirds": 3, "quarter": 4, "quarters": 4, "fourth": 4,
                   "fourths": 4, "fifth": 5, "fifths": 5, "sixth": 6, "sixths": 6, "seventh": 7, "sevenths": 7,
                   "eighth": 8, "eighths": 8, "ninth": 9, "ninths": 9, "tenth": 10, "tenths": 10}

_FRACTION = re.compile(r"(?<![\d.])(\d+)\s*/\s*(\d+)(?!\d)")
_MIXED = re.compile(r"(?<![\d./])(\d+)\s+(\d+)\s*/\s*(\d+)(?!\d)")
_PERCENT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%")
_RATIO = re.compile(r"(?<![\d.])(\d+)\s*:\s*(\d+)(?:\s*:\s*(\d+))?(?!\d)")
_AND_A_HALF = re.compile(r"(?<![\d.])(\d+)\s+and\s+a\s+half\b", re.I)

# Working lines: "4 parts", "3u", a trailing "beads", a leading "Ali:".
_PARTS = re.compile(r"^(\d+(?:\.\d+)?)\s+parts?(?:\s+[a-zA-Z]+)*$", re.I)
_TRAILING_WORDS = re.compile(r"(?:\s+[A-Za-z][A-Za-z']*)+\s*$")
_LEADING_LABEL = re.compile(r"^[A-Za-z][A-Za-z' ]*:\s*")
_OPERATION = re.compile(r"\d\s*\)*\s*[-+*/]\s*\(*\s*\d")
_PLAIN_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_COUNT_OF = re.compile(r"(\d+(?:\.\d+)?)\s+([A-Za-z]+)(?:\s+[A-Za-z']+)*")

# Narration: "3 times 6 is 18", "24 divided by 4, which is 6"; and "Ali has 3 units", "the 4 extra units".
_SAID_NUMBER = r"\$?(\d[\d,]*(?:\.\d+)?)"
_SPOKEN_SUM = re.compile(rf"(?<![\w.]){_SAID_NUMBER}\s+(times|multiplied by|divided by|plus|add|minus|take away)\s+"
                         rf"{_SAID_NUMBER}[\s,]+(?:which\s+)?(?:is|are|makes|equals|gives|leaves)\s+(?:about\s+)?"
                         rf"{_SAID_NUMBER}", re.I)
_CHAIN_BEFORE = re.compile(rf"(?<![\w.]){_SAID_NUMBER}[\s,]+(plus|add|minus|take away)[\s,]+$", re.I)
_SPOKEN_OPS = {"times": operator.mul, "multiplied by": operator.mul, "divided by": operator.truediv,
               "plus": operator.add, "add": operator.add, "minus": operator.sub, "take away": operator.sub}
_SPOKEN_UNITS = re.compile(r"(?<![\w.$])(\d+(?:\.\d+)?)\s+(?:equal\s+|more\s+|fewer\s+|extra\s+)?(?:units?|parts?)\b",
                           re.I)


class VerifyResultV2(VerifyResult):
    """A VerifyResult whose str() needs no total_units: a v2 spec has no single unit total."""

    def __str__(self) -> str:
        if self.ok:
            return "OK" if self.unit_value is None else f"OK (1 unit = {self.unit_value:g})"
        return "FAILED: " + "; ".join(self.errors)


def _fmt(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.4f}".rstrip("0").rstrip(".")


def _among(n: float, pool: list[float]) -> bool:
    return any(_close(n, k) for k in pool)


@lru_cache(maxsize=1)
def _validator():
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.validators.validator_for(schema)(schema)


def _where(path) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "spec"


def solution_or_none(spec: dict) -> Solution | None:
    """The solved spec, or None when it can't be solved (verify_spec_v2 says why)."""
    try:
        return solve_spec(spec)
    except Exception:  # noqa: BLE001 — SpecError, or SymPy failing on an unverified spec
        return None


# --- Numbers the question gives ---------------------------------------------------------------

def _word_values(text: str) -> list[float]:
    """Numbers written as words: 'twenty-four' -> 24, 'twice' -> 2, 'two-thirds' -> 2, 3 and 2/3."""
    words = re.findall(r"[a-z]+", text.lower())
    values: list[float] = []
    for k, word in enumerate(words):
        prev = words[k - 1] if k else ""
        if word in _TENS:
            values.append(_TENS[word])
            nxt = words[k + 1] if k + 1 < len(words) else ""
            if _ONES.get(nxt, 10) < 10:
                values.append(_TENS[word] + _ONES[nxt])
        elif word in _WORDS:
            values.append(_WORDS[word])
        elif word in _FRACTION_WORDS:
            d = _FRACTION_WORDS[word]
            n = 1 if prev in ("a", "an") else _ONES.get(prev)
            values += [d, 1 / d] + ([n / d] if n else [])
            if word == "half" and prev == "a" and k >= 3 and words[k - 2] == "and" and words[k - 3] in _WORDS:
                values.append(_WORDS[words[k - 3]] + 0.5)  # "one and a half"
    return values


def question_numbers(question: str) -> list[float]:
    """Every number the question gives, written any way a P3-P6 paper writes it.

    '1,250' and '$4.50' read as numbers. '35%' also gives 0.35 and the 65% left or 135% after;
    '2/5' gives 2, 5, 0.4 and the 3/5 that remains; '3 : 5' gives 3, 5 and the 8 units in all;
    'twice', 'half' and 'three' count; and "minutes" or "kg" allow the factors 60 or 1000.
    """
    values = _numbers(question) + _word_values(question)
    for m in _FRACTION.finditer(question):
        a, b = float(m.group(1)), float(m.group(2))
        if b:
            values.append(a / b)
            if a < b:
                values += [b - a, (b - a) / b]
    for m in _MIXED.finditer(question):
        whole, a, b = map(float, m.groups())
        if b:
            values.append(whole + a / b)
    for m in _PERCENT.finditer(question):
        p = float(m.group(1))
        values += [p / 100, 100 - p, (100 - p) / 100, 100 + p, (100 + p) / 100]
    for m in _RATIO.finditer(question):
        parts = [float(g) for g in m.groups() if g]
        values += [sum(c) for r in range(2, len(parts) + 1) for c in combinations(parts, r)]
        values += [a / b for a in parts for b in parts if b]
    values += [float(m.group(1)) + 0.5 for m in _AND_A_HALF.finditer(question)]
    for pattern, factors in _CONVERSIONS:
        if pattern.search(question):
            values += factors
    return values + _clock_gaps(question)


def _norm_quote(text: str) -> str:
    text = text.lower().translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                                 "–": "-", "—": "-", "−": "-"}))
    return re.sub(r"\s+", "", text)


def _quoted(phrase: str, question: str) -> bool:
    """Is `phrase` copied from the question, ignoring case, spaces and end punctuation ('...' may skip words)?"""
    pieces = [_norm_quote(p).strip(".,;:!?\"'") for p in re.split(r"\.\.\.|…", phrase)]
    pieces = [p for p in pieces if p]
    q, at = _norm_quote(question), 0
    for piece in pieces:
        at = q.find(piece, at)
        if at < 0:
            return False
        at += len(piece)
    return bool(pieces)


def _stated(text: str, value: float) -> bool:
    """Does the text state `value` ('$1,250', '2/3', '1 1/2' or 'six' all count)?"""
    found = _numbers(text) + _word_values(text)
    found += [float(a) / float(b) for a, b in _FRACTION.findall(text) if float(b)]
    found += [float(w) + float(a) / float(b) for w, a, b in _MIXED.findall(text) if float(b)]
    return _among(value, found)


# --- Checks 1-5: structure ----------------------------------------------------------------------

def _check_schema(spec: dict) -> list[str]:
    try:
        validator = _validator()
    except ImportError:
        return ["jsonschema package not installed; run pip install -r requirements.txt"]
    return [f"schema: {_where(e.absolute_path)}: {e.message}"
            for e in list(validator.iter_errors(spec))[:MAX_SCHEMA_ERRORS]]


def _check_relations_parse(spec: dict) -> list[str]:
    """Name the relation that can't be read; solve_spec's own message doesn't say which one."""
    names = [q["id"] for q in spec["quantities"]] + [u["name"] for u in spec["units"]]
    symbols = {n: sp.Symbol(n, real=True) for n in names}
    errors = []
    for i, rel in enumerate(spec["relations"]):
        try:
            parse_relation(rel["eq"], symbols)
        except SpecError as exc:
            errors.append(f"relations[{i}] {rel['eq']!r}: {exc}")
    return errors


def _check_grounding(spec: dict) -> list[str]:
    errors = []
    question = spec["question"]
    allowed = question_numbers(question) + list(RELATION_STRUCTURAL)
    if spec["heuristic"] in IMPLICIT_COUNTS:
        allowed += list(range(13))
    for i, rel in enumerate(spec["relations"]):
        if not _quoted(rel["from"], question):
            errors.append(f"relations[{i}] quotes {rel['from']!r}, which is not in the question; "
                          "copy the phrase exactly")
        for n in expr_numbers(rel["eq"]):
            if not _among(n, allowed):
                errors.append(f"relations[{i}] {rel['eq']!r} uses {_fmt(n)}, which is not a number in the question")
    return errors


def _check_bars(spec: dict) -> list[str]:
    errors = []
    stages, template = spec["stages"], spec["template"]
    if template == "bar" and len(stages) != 1:
        errors.append(f"template 'bar' needs exactly 1 stage, found {len(stages)}; "
                      "use 'bar-stages' for before and after")
    elif template == "bar-stages" and not 2 <= len(stages) <= 3:
        errors.append(f"template 'bar-stages' needs 2 or 3 stages, found {len(stages)}; use 'bar' for one stage")
    elif template == "working" and stages:
        errors.append(f"template 'working' draws no bars, so stages must be [] (found {len(stages)})")
    quantities = {q["id"] for q in spec["quantities"]}
    for kind, ids in (("stage", [s["id"] for s in stages]), ("bar", [b["id"] for b in bars_of(spec)])):
        for dup in sorted({x for x in ids if ids.count(x) > 1}):
            errors.append(f"{kind} id {dup!r} is used twice; {kind} ids must be unique")
    for bar in bars_of(spec):
        if bar["quantity"] not in quantities:
            errors.append(f"bar {bar['id']!r} draws quantity {bar['quantity']!r}, which is not in quantities")
    return errors


def _segment_range(step: dict, i: int, bar: dict) -> str | None:
    pair = step.get("segments")
    if pair is None:
        return None
    n = len(bar["segments"])
    if not (0 <= pair[0] <= pair[1] < n):
        return (f"timeline[{i}] {step['do']} segments {pair} are out of range: "
                f"bar {bar['id']!r} has segments 0 to {n - 1}")
    return None


def _cell_text(cell) -> str:
    return _fmt(cell) if isinstance(cell, (int, float)) and not isinstance(cell, bool) else str(cell).strip()


def _table_problems(i: int, step: dict, tables: dict[str, dict]) -> list[str]:
    """A table step: an id, 2-4 short column headers, 1-6 rows of one cell per column."""
    tid, columns, rows = step.get("id"), step.get("columns"), step.get("rows")
    if not isinstance(tid, str) or not tid:
        return [f"timeline[{i}] table needs an id, so fill_cell steps can name it"]
    if tid in tables:
        return [f"timeline[{i}] table id {tid!r} is used twice"]
    if tables:
        return [f"timeline[{i}] the board has room for one table or number line; put everything in "
                f"{next(iter(tables))!r}"]
    if not isinstance(columns, list) or not 2 <= len(columns) <= MAX_TABLE_COLS:
        return [f"timeline[{i}] table needs 2 to {MAX_TABLE_COLS} columns (headers like \"Item\", \"Legs each\")"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_TABLE_ROWS:
        return [f"timeline[{i}] table needs 1 to {MAX_TABLE_ROWS} rows"]
    errors = []
    for r, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            errors.append(f"timeline[{i}] table row {r} needs exactly {len(columns)} cells, one per column")
    for text in [_cell_text(c) for c in columns] + [_cell_text(c) for row in rows if isinstance(row, list) for c in row]:
        if len(text) > MAX_CELL_CHARS:
            errors.append(f"timeline[{i}] table text {text!r} is too long; keep cells to {MAX_CELL_CHARS} characters")
    if not errors:
        tables[tid] = step
    return errors


def _fill_problems(i: int, step: dict, tables: dict[str, dict], filled: set) -> list[str]:
    """fill_cell writes one blank cell ("" or "?") of a table drawn earlier."""
    tid, r, c, text = step.get("table"), step.get("row"), step.get("col"), _cell_text(step.get("text", ""))
    table = tables.get(tid)
    if table is None or table.get("do") != "table":
        return [f"timeline[{i}] fill_cell names table {tid!r}, which no earlier table step draws"]
    rows = table["rows"]
    if not isinstance(r, int) or not isinstance(c, int) or not (0 <= r < len(rows) and 0 <= c < len(table["columns"])):
        return [f"timeline[{i}] fill_cell row {r!r}, col {c!r} is outside table {tid!r} "
                f"(rows 0 to {len(rows) - 1}, cols 0 to {len(table['columns']) - 1})"]
    if _cell_text(rows[r][c]) not in BLANK_CELLS:
        return [f"timeline[{i}] fill_cell row {r}, col {c} already shows {_cell_text(rows[r][c])!r}; "
                "leave the cell \"?\" in the table step and fill it once"]
    if (tid, r, c) in filled:
        return [f"timeline[{i}] fill_cell row {r}, col {c} is filled twice"]
    if not text or not re.search(r"\d", text):
        return [f"timeline[{i}] fill_cell needs a text with the cell's number"]
    if len(text) > MAX_CELL_CHARS:
        return [f"timeline[{i}] fill_cell text {text!r} is too long for a cell; write the sum in a write step"]
    filled.add((tid, r, c))
    return []


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _line_problems(i: int, step: dict, charts: dict[str, dict]) -> list[str]:
    """A number line: start < end, 2-8 marks inside it (a label under the tick, a value above it, "?" to fill
    later), and 0-6 jumps (curved hops) between two points, each with a label."""
    lid, start, end = step.get("id"), _num(step.get("start")), _num(step.get("end"))
    if not isinstance(lid, str) or not lid:
        return [f"timeline[{i}] number_line needs an id, so mark_point steps can name it"]
    if charts:
        return [f"timeline[{i}] the board has room for one table or number line, not two"]
    if start is None or end is None or not start < end:
        return [f"timeline[{i}] number_line needs numbers start < end (e.g. start 0, end 300 for 300 km)"]
    marks, hops = step.get("marks"), step.get("jumps") or []
    if not isinstance(marks, list) or not 2 <= len(marks) <= MAX_MARKS or not all(isinstance(m, dict) for m in marks):
        return [f"timeline[{i}] number_line needs 2 to {MAX_MARKS} marks like {{\"at\": 0, \"label\": \"P\"}}"]
    if not isinstance(hops, list) or len(hops) > MAX_HOPS or not all(isinstance(h, dict) for h in hops):
        return [f"timeline[{i}] number_line jumps: at most {MAX_HOPS}, like {{\"from\": 0, \"to\": 175, \"label\": \"car\"}}"]
    errors = []
    ats = [_num(m.get("at")) for m in marks]
    if any(a is None or not start <= a <= end for a in ats):
        errors.append(f"timeline[{i}] every number_line mark needs an \"at\" from {_fmt(start)} to {_fmt(end)}")
    elif len(set(ats)) != len(ats):
        errors.append(f"timeline[{i}] two number_line marks are at the same place")
    for h in hops:
        a, b = _num(h.get("from")), _num(h.get("to"))
        if a is None or b is None or a == b or not (start <= min(a, b) and max(a, b) <= end):
            errors.append(f"timeline[{i}] a number_line jump needs two different points from {_fmt(start)} "
                          f"to {_fmt(end)}")
            break
    texts = [m.get(k) for m in marks for k in ("label", "value")] + [h.get("label") for h in hops]
    for text in [_cell_text(t) for t in texts if t is not None]:
        if len(text) > MAX_LINE_LABEL:
            errors.append(f"timeline[{i}] number_line text {text!r} is too long; keep it to {MAX_LINE_LABEL} characters")
    if not errors:
        charts[lid] = step
    return errors


def _mark_problems(i: int, step: dict, charts: dict[str, dict], filled: set) -> list[str]:
    """mark_point writes the value above a mark whose value is "?"."""
    lid, at, text = step.get("line"), _num(step.get("at")), _cell_text(step.get("text", ""))
    line = charts.get(lid)
    if line is None or line.get("do") != "number_line":
        return [f"timeline[{i}] mark_point names number line {lid!r}, which no earlier number_line step draws"]
    marks = [m for m in line["marks"] if at is not None and _close(_num(m.get("at")), at)]
    if not marks:
        return [f"timeline[{i}] mark_point at {step.get('at')!r}: number line {lid!r} has no mark there"]
    if _cell_text(marks[0].get("value", "")) != "?":
        return [f"timeline[{i}] mark_point at {_fmt(at)}: give that mark \"value\": \"?\" in the number_line step, "
                "then fill it once"]
    if (lid, at) in filled:
        return [f"timeline[{i}] mark_point at {_fmt(at)} is filled twice"]
    if not text or not re.search(r"\d", text):
        return [f"timeline[{i}] mark_point needs a text with the number at that mark"]
    if len(text) > MAX_LINE_LABEL:
        return [f"timeline[{i}] mark_point text {text!r} is too long; write the sum in a write step"]
    filled.add((lid, at))
    return []


def _check_timeline(spec: dict) -> list[str]:
    errors: list[str] = []
    timeline, template, question = spec["timeline"], spec["template"], spec["question"]
    bars = {b["id"]: b for b in bars_of(spec)}
    stage_ids = [s["id"] for s in spec["stages"]]
    known_bars = ", ".join(bars) or "none"
    drawn: set[str] = set()

    def bar_ref(i: int, step: dict, bid) -> dict | None:
        if bid is None:
            errors.append(f"timeline[{i}] {step['do']} needs the id of the bar it is drawn on")
        elif bid not in bars:
            errors.append(f"timeline[{i}] {step['do']} names bar {bid!r}, "
                          f"which is not a bar in the stages ({known_bars})")
        elif step["do"] != "draw_bar" and bid not in drawn:
            errors.append(f"timeline[{i}] {step['do']} uses bar {bid!r} before a draw_bar step draws it")
        else:
            return bars[bid]
        return None

    tables: dict[str, dict] = {}
    filled: set[tuple[str, int, int]] = set()
    for i, step in enumerate(timeline):
        do = step["do"]
        if template == "working" and do not in WORKING_ACTIONS:
            errors.append(f"timeline[{i}] {do} needs bars, but the working template has none; use write steps")
            continue
        if do in CHART_ACTIONS and template != "working":
            errors.append(f"timeline[{i}] {do}: a table is drawn where the bars go, so it needs template 'working' "
                          "and stages []")
            continue
        if do == "table":
            errors += _table_problems(i, step, tables)
            continue
        if do == "fill_cell":
            errors += _fill_problems(i, step, tables, filled)
            continue
        if do == "number_line":
            errors += _line_problems(i, step, tables)
            continue
        if do == "mark_point":
            errors += _mark_problems(i, step, tables, filled)
            continue
        if do in ("highlight", "write", "answer", "label_unit") and not step.get("text"):
            errors.append(f"timeline[{i}] {do} needs a text")
        if do == "highlight":
            if step.get("text") and step["text"] not in question:
                errors.append(f"timeline[{i}] highlight text {step['text']!r} is not in the question; "
                              "copy the words exactly")
        elif do == "draw_stage" and step.get("stage") not in stage_ids:
            errors.append(f"timeline[{i}] draw_stage names stage {step.get('stage')!r}, "
                          f"which is not one of {stage_ids}")
        elif do in ONE_BAR_ACTIONS:
            bar = bar_ref(i, step, step.get("bar"))
            if do == "draw_bar" and bar:
                drawn.add(bar["id"])
            if do == "question_mark" and bar and (problem := _segment_range(step, i, bar)):
                errors.append(problem)
        elif do == "bracket":
            if step.get("bars"):
                for bid in step["bars"]:
                    bar_ref(i, step, bid)
                if step.get("side", "right") != "right":
                    errors.append(f"timeline[{i}] bracket over several bars must use side 'right'")
            elif step.get("bar") is not None:
                bar = bar_ref(i, step, step["bar"])
                if bar and (problem := _segment_range(step, i, bar)):
                    errors.append(problem)
                if step.get("side", "top") not in ("top", "bottom"):
                    errors.append(f"timeline[{i}] bracket on one bar must use side 'top' or 'bottom'")
            else:
                errors.append(f"timeline[{i}] bracket needs a bar (with segments) or a list of bars")
        elif do == "move_part":
            if any(step.get(k) is None for k in ("from", "from_segment", "to", "to_segment")):
                errors.append(f"timeline[{i}] move_part needs from, from_segment, to and to_segment")
                continue
            for end, seg_key, style in (("from", "from_segment", "removed"), ("to", "to_segment", "added")):
                need = f"start at a '{style}'" if end == "from" else f"end at an '{style}'"
                bar = bar_ref(i, step, step[end])
                if bar is None:
                    continue
                k = step[seg_key]
                if k >= len(bar["segments"]):
                    errors.append(f"timeline[{i}] move_part {seg_key} {k} is out of range: bar {bar['id']!r} "
                                  f"has segments 0 to {len(bar['segments']) - 1}")
                elif (got := bar["segments"][k].get("style", "normal")) != style:
                    errors.append(f"timeline[{i}] move_part must {need} segment, "
                                  f"but bar {bar['id']!r} segment {k} is '{got}'")

    if template != "working":
        for bid in bars:
            if bid not in drawn:
                errors.append(f"bar {bid!r} is never drawn; add a draw_bar step for it")

    dos = [s["do"] for s in timeline]
    if "pause" not in dos:
        errors.append("timeline needs at least one 'pause' step, so the child can think before the reveal")
    answers = [i for i, do in enumerate(dos) if do == "answer"]
    if len(answers) != 1:
        errors.append(f"timeline needs exactly one 'answer' step, found {len(answers)}")
    answer = float(spec["answer"])
    for i in answers:
        text = timeline[i].get("text", "")
        if text and not _stated(text, answer):
            errors.append(f"timeline[{i}] answer text {text!r} does not state the answer {_fmt(answer)}")
        if not _stated(timeline[i]["say"], answer):
            errors.append(f"timeline[{i}] answer say does not state the answer {_fmt(answer)}")
    for i, step in enumerate(timeline):
        words = len(step["say"].split())
        if step["do"] != "write_question" and words > MAX_SAY_WORDS:
            errors.append(f"timeline[{i}] say is {words} words; keep it to {MAX_SAY_WORDS} or fewer")
    return errors


# --- Check 5b: the board's shape (docs/board-templates.md §3) -----------------------------------

# Boards that are a picture of bars, and boards that are a table, a timeline or working only.
BAR_BOARDS = {"part_whole", "comparison", "before_after", "constant_difference", "remainder", "percentage",
              "ratio_change"}
BOARDS = BAR_BOARDS | {"units_value_table", "two_scenarios", "assume_adjust", "average_level", "rate_speed",
                       "working"}
_QUESTION_FRACTION = re.compile(r"(?<![\d./])(\d+)\s*/\s*(\d+)(?![\d/])")


def _counted_values(bar: dict, solution: Solution) -> list[float]:
    values = solution.segments.get(bar["id"], [])
    return [v for v, s in zip(values, bar["segments"]) if s.get("style", "normal") in COUNTED_STYLES]


def _unit_total(bar: dict, seg_units: dict) -> float | None:
    """How many units a bar's counted part is, when it is made only of whole units ('u', '3*u'), else None."""
    total = 0.0
    for seg, counts in zip(bar["segments"], seg_units.get(bar["id"], [])):
        if seg.get("style", "normal") not in COUNTED_STYLES:
            continue
        if not counts:
            return None
        total += sum(counts.values())
    return total


def _check_board(spec: dict, solution: Solution | None) -> list[str]:
    """The picture a `board` names must have that board's shape, so it looks like a teacher's picture."""
    board = spec.get("board")
    if board is None:
        return []
    stages, template = spec["stages"], spec["template"]
    if board == "working":
        return [] if template == "working" else ["board 'working' draws no bars: use template 'working' and stages []"]
    if board in BAR_BOARDS and template == "working":
        return [f"board {board!r} is a bar picture, but template is 'working'; draw the bars, or use board 'working'"]
    if not stages or solution is None:
        return []
    labels = [sorted(b["label"].strip().lower() for b in st["bars"]) for st in stages]

    if board in ("before_after", "constant_difference", "ratio_change"):
        if len(stages) < 2:
            return [f"board {board!r} needs 2 stages (at first / in the end); found {len(stages)}"]
        if board != "ratio_change" and labels[0] != labels[-1]:
            return [f"board {board!r}: the first and last stages must draw the same people "
                    f"(bar labels {labels[0]} vs {labels[-1]})"]

    if board == "constant_difference":
        # The difference never changes, so it is drawn as the same piece at the end of the longer bar in every row.
        diffs = []
        for st in stages:
            if len(st["bars"]) < 2:
                return [f"board 'constant_difference': stage {st['id']!r} needs both people's bars"]
            totals = sorted(((sum(_counted_values(b, solution)), b) for b in st["bars"]),
                              key=lambda t: t[0])  # bars may tie; dicts don't compare
            (small, _), (big, long_bar) = totals[0], totals[-1]
            diff = big - small
            diffs.append(diff)
            tail = _counted_values(long_bar, solution)
            if not any(_close(sum(tail[k:]), diff) for k in range(1, len(tail))):
                return [f"board 'constant_difference': in stage {st['id']!r}, end bar {long_bar['id']!r} with the "
                        f"difference ({_fmt(diff)}) as its own segment, so the same piece shows in every row"]
        if any(not _close(d, diffs[0]) for d in diffs):
            return ["board 'constant_difference': the difference between the bars must be the same in every stage"]

    if board == "ratio_change":
        # Whoever does not change is drawn with the same units before and after (e.g. Beth: 5 units both times).
        by_quantity: dict[str, list[dict]] = {}
        for st in stages:
            for b in st["bars"]:
                by_quantity.setdefault(b["quantity"], []).append(b)
        for q, drawn in by_quantity.items():
            shapes = {tuple(s["expr"].replace(" ", "") for s in b["segments"]) for b in drawn}
            if len(shapes) > 1:
                return [f"board 'ratio_change': {q!r} does not change, so draw it with the same segments in "
                        f"every stage (bars {', '.join(b['id'] for b in drawn)})"]

    seg_units = _segment_units(spec)
    if board == "remainder":
        marked = [b for b in bars_of(spec) if any(s.get("style") == "marked" for s in b["segments"])]
        if not marked:
            return ["board 'remainder' needs one bar for the whole, with each spent part 'marked' and labelled"]
        fractions = [(int(a), int(b)) for a, b in _QUESTION_FRACTION.findall(spec["question"]) if 0 < int(a) < int(b)]
        for bar in marked:
            values = solution.segments.get(bar["id"], [])
            whole = sum(_counted_values(bar, solution))
            left, k, ok = whole, 0, True
            for v, seg in zip(values, bar["segments"]):
                if seg.get("style") != "marked" or k >= len(fractions):
                    continue
                a, b = fractions[k]
                if not (_close(v, a / b * left) or _close(v, a / b * whole)):
                    ok = False
                    break
                left, k = left - v, k + 1
            if ok:
                return []
        a, b = fractions[k] if fractions and k < len(fractions) else (0, 0)
        hint = f"; the question's {a}/{b} must be one marked part" if a else ""
        return ["board 'remainder': the marked parts must follow the question's fractions in order (each of the "
                f"whole or of what is left), in units that split evenly (e.g. 2/5 then 1/3 of the rest: 5 units){hint}"]

    if board == "percentage":
        percents = [float(p) for p in _PERCENT.findall(spec["question"])]
        for bar in bars_of(spec):
            n = _unit_total(bar, seg_units)
            if n and all(_close(p * n / 100, round(p * n / 100)) for p in percents):
                return []
        example = f" (e.g. {_fmt(percents[0])}% -> {_fmt(100 / math.gcd(int(percents[0]), 100))} units)" \
            if percents and float(percents[0]).is_integer() and percents[0] > 0 else ""
        return ["board 'percentage' needs a bar for 100% drawn only in units, so each percentage in the question "
                f"is a whole number of units{example}, or use board 'working'"]
    return []


# --- Checks 6-7: working and narration numbers ------------------------------------------------

def _segment_units(spec: dict) -> dict[str, list[dict[str, float]]]:
    """bar id -> how many of each unit every segment holds ('3*u' -> {'u': 3.0}, '20' -> {})."""
    symbols = {u["name"]: sp.Symbol(u["name"], real=True) for u in spec["units"]}
    out: dict[str, list[dict[str, float]]] = {}
    for bar in bars_of(spec):
        counts = []
        for seg in bar["segments"]:
            try:
                expr = sp.expand(parse_expr(seg["expr"], symbols))
                counts.append({n: float(expr.coeff(s)) for n, s in symbols.items() if expr.coeff(s) != 0})
            except (SpecError, TypeError):
                counts.append({})
        out[bar["id"]] = counts
    return out


def _spans(xs: list[float]) -> list[float]:
    return [sum(xs[i:j + 1]) for i in range(len(xs)) for j in range(i, len(xs))]


def _pairs(xs: list[float]) -> list[float]:
    return [v for a, b in combinations(xs, 2) for v in (a + b, abs(a - b))]


def _counted(segments: list[dict], xs: list[float]) -> float:
    """Sum of xs over the segments that count towards a bar's value (not the 'removed' ones)."""
    return sum(x for x, seg in zip(xs, segments) if seg.get("style", "normal") in COUNTED_STYLES)


def _board(spec: dict, solution: Solution, seg_units: dict) -> tuple[list[float], list[float]]:
    """(unit counts, amounts) on the board: segments, runs of segments, bars, stages, and the sums and
    differences of bars ('Amy gained 4 units', 'together they have 8 units')."""
    counts_out: list[float] = []
    values_out: list[float] = []
    all_counts: list[float] = []
    for stage in spec["stages"]:
        stage_counts, stage_values = [], []
        for bar in stage["bars"]:
            counts = [sum(c.values()) for c in seg_units.get(bar["id"], [])]
            values = solution.segments.get(bar["id"], [])
            counts_out += _spans(counts)
            values_out += _spans(values)
            stage_counts.append(_counted(bar["segments"], counts))
            stage_values.append(_counted(bar["segments"], values))
        counts_out += stage_counts + _pairs(stage_counts) + [sum(stage_counts)]
        values_out += stage_values + _pairs(stage_values) + [sum(stage_values)]
        all_counts += stage_counts
    # Across stages only a change means something ("Amy gained 4 units"), not a sum.
    return counts_out + [abs(a - b) for a, b in combinations(all_counts, 2)], values_out


def _spoken_sum_problem(say: str) -> str | None:
    """'24 divided by 4, which is 6' must be true; a decimal answer may be rounded to the places it shows."""
    for m in _SPOKEN_SUM.finditer(say):
        a, op, b, said = float(m[1].replace(",", "")), m[2].lower(), float(m[3].replace(",", "")), m[4]
        if op == "divided by" and b == 0:
            continue
        value = _SPOKEN_OPS[op](a, b)
        # "106 plus 52 plus 53 is 211": the match starts mid-chain, so add in what came before it, left to right.
        head, chain = say[:m.start()], []
        while c := _CHAIN_BEFORE.search(head):
            chain.insert(0, (float(c[1].replace(",", "")), c[2].lower()))
            head = head[:c.start()]
        if chain:
            total = chain[0][0]
            for (_, o), (n, _) in zip(chain, chain[1:] + [(a, None)]):
                total = _SPOKEN_OPS[o](total, n)
            value = _SPOKEN_OPS[op](total, b)
        lead = "".join(f"{_fmt(n)} {o} " for n, o in chain)
        stated = float(said.replace(",", ""))
        places = len(said.split(".")[1]) if "." in said else 0
        if not _close(value, stated) and not (places and abs(value - stated) <= 0.5 * 10 ** -places + 1e-9):
            return f"{lead}{_fmt(a)} {op} {_fmt(b)} is {_fmt(value)}, not {said}"
    return None


def _unit_name(tag: str, names: list[str]) -> str:
    """'units' -> u and 'parts' -> p (or the only unit there is); a unit letter names itself."""
    tag = tag.lower()
    for word, default in (("unit", "u"), ("part", "p")):
        if tag.startswith(word):
            return default if default in names or len(names) != 1 else names[0]
    return tag


def _arith(expr: str) -> tuple[float | None, bool]:
    """(value, has an operation) of a plain arithmetic part, after dropping trailing words like 'beads'."""
    expr = _TRAILING_WORDS.sub("", expr).replace("×", "*").replace("÷", "/")
    expr = re.sub(r"(?<=[\d\s)])[xX](?=[\s\d(])", "*", expr)
    return _eval_expr(expr), bool(_OPERATION.search(expr))


def _term(part: str, names: list[str], tags: re.Pattern, unit_values: dict[str, float], first: bool = False):
    """One side of '=' -> (dimension, value, worked out here?), or None for a label like 'Difference'.

    The dimension is '' for an amount, a unit name for a count of units ('4 units' -> u, 4), '%', or
    '#noun' for a count of things whose worth is not known ('1 fifth = 250', '3 boxes = 3 × 12').
    """
    text = part.strip().replace("−", "-").replace("–", "-").rstrip(".,;:!?✓✔ ").strip()
    text = _LEADING_LABEL.sub("", re.sub(r"(?<=\d),(?=\d)", "", text))
    money = "$" in text
    text = re.sub(r"(?<=[\d)%])\s+of\s+(?=[\d(])", " * ", text.replace("$", ""))  # "2/5 of 150", "35% of 200"
    if text in names:
        return text, 1.0, False
    for pattern, tag in ((_UNITS, "units"), (_PARTS, "parts")):
        if m := pattern.match(text):
            return _unit_name(tag, names), float(m.group(1)), False
    # Before the first '=', "N noun" counts things ("1 fifth = 250"); after it, "6 pupils" is an amount.
    if first and not money and (m := _COUNT_OF.fullmatch(text)):
        return f"#{m.group(2).lower().rstrip('s')}", float(m.group(1)), False
    if not re.search(r"\d", text):
        return None
    numbers = _PLAIN_NUMBER.findall(text)
    tagged = list(tags.finditer(text))
    if tagged and len(tagged) == len(numbers):  # "7 units - 3 units"
        dims = {_unit_name(m.group(2), names) for m in tagged}
        value, worked = _arith(tags.sub(r"\1", text))
        return (dims.pop(), value, worked) if len(dims) == 1 and value is not None else None
    if "%" in text and len(re.findall(r"\d\s*%", text)) == len(numbers):  # "100% - 35%"
        value, worked = _arith(text.replace("%", ""))
        return None if value is None else ("%", value, worked)
    # Mixed: known units and percentages become amounts ("3 units + 20" with u = 6 is 38; "35% * 200" is 70).
    for m in tagged:
        if _unit_name(m.group(2), names) not in unit_values:
            return None
    text = tags.sub(lambda m: f"({m.group(1)}*{unit_values[_unit_name(m.group(2), names)]!r})", text)
    value, worked = _arith(re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", text))
    return None if value is None else ("", value, worked)


def _written_units(names: list[str]) -> re.Pattern:
    """A unit count in working: '4 units', '3 parts', '2u' (but not the 'x' in '3 x 6')."""
    letters = "".join(f"|{n}" for n in names)
    return re.compile(rf"(\d+(?:\.\d+)?)\s*(units?|parts?{letters})\b(?!\s*[\d(])", re.I)


def _show(value: float, dim: str) -> str:
    """12, 65% or 4u."""
    return f"{_fmt(value)}{dim}"


def _check_line(text: str, names: list[str], unit_values: dict[str, float]) -> tuple[str | None, list[float]]:
    """Check one working line. Returns (problem, the values it works out).

    Like verify._check_working, but with several unit symbols ('N units' is N*u, 'N parts' is N*p),
    unit arithmetic ('7 units - 3 units = 4 units'), percentages, and a unit that may be unsolved.
    """
    tags = _written_units(names)
    parts = text.split("=")
    terms = [t for k, part in enumerate(parts)
             if (t := _term(part, names, tags, unit_values, first=k == 0 and len(parts) > 1)) is not None]
    chains: dict[str, list[float]] = {}
    for dim, value, _ in terms:
        chains.setdefault(dim, []).append(value)
    for dim, values in chains.items():
        for a, b in zip(values, values[1:]):
            if not _close(a, b):
                return f"{_show(a, dim)} does not equal {_show(b, dim)}", []
    worked = [v for _, v, w in terms if w]
    if "%" in chains:
        worked += [v * 100 for dim, v, w in terms if w and dim == ""]  # "2 × 35% = 70%"
    plain = chains.get("", [])
    worths = []
    for dim, counts in chains.items():
        if dim in ("", "%") or dim not in unit_values:
            continue  # a percentage's whole, or an unsolved unit, can't be checked
        for count in counts:
            worth = count * unit_values[dim]
            worths.append(worth)
            # "1 + 3 = 4 units" counts units; "4 units = 120" states what they are worth.
            if plain and not _close(plain[-1], count) and not _close(plain[-1], worth):
                return f"{_show(count, dim)} is worth {_fmt(worth)}, not {_fmt(plain[-1])}", []
    # "3 units = 5 parts" links two unit symbols: both sides must be worth the same.
    if not plain and any(not _close(worths[0], w) for w in worths):
        return f"the two sides are worth {_fmt(worths[0])} and {_fmt(worths[-1])}", []
    return None, worked + worths


_UNKNOWN = "which is not in the question or the solved model"


def _working_items(spec: dict) -> list[tuple[tuple[int, int], str, str]]:
    """Everything the board writes that holds numbers, in order: working lines, the table's headers and given
    cells, and filled cells. Each is ((timeline index, k), text, what it is for an error message)."""
    items = []
    for i, step in enumerate(spec["timeline"]):
        if step["do"] == "write":
            items.append(((i, 0), step.get("text", ""), "working"))
        elif step["do"] == "fill_cell":
            items.append(((i, 0), _cell_text(step.get("text", "")), "fill_cell"))
        elif step["do"] == "table":
            cells = list(step.get("columns") or []) + [c for row in step.get("rows") or [] if isinstance(row, list)
                                                       for c in row]
            items += [((i, k), _cell_text(c), "table cell") for k, c in enumerate(cells)
                      if _cell_text(c) not in BLANK_CELLS]
        elif step["do"] == "mark_point":
            items.append(((i, 0), _cell_text(step.get("text", "")), "mark_point"))
        elif step["do"] == "number_line":
            texts = [m.get(k) for m in step.get("marks") or [] if isinstance(m, dict) for k in ("label", "value")]
            texts += [h.get("label") for h in step.get("jumps") or [] if isinstance(h, dict)]
            items += [((i, k), _cell_text(t), "number_line label") for k, t in enumerate(texts)
                      if t is not None and _cell_text(t) not in BLANK_CELLS]
    return items


def _check_working(spec: dict, solution: Solution, known: list[float]) -> tuple[list[str], dict[int, list[float]]]:
    """Check 6: working lines and table cells, in order. Returns (errors, {timeline index: numbers a correct line
    shows}). A table cell or a filled cell is checked like a line: true arithmetic, and only numbers already known."""
    errors: list[str] = []
    names = [u["name"] for u in spec["units"]]
    items = _working_items(spec)
    checked = {key: _check_line(text, names, solution.units) for key, text, _ in items}
    so_far = list(known)
    for key, text, what in items:
        i = key[0]
        problem, worked = checked[key]
        if problem:
            errors.append(f'timeline[{i}] {what} "{text}" is wrong: {problem}')
            continue
        # Each operand must be known before its line; the line's results are known after it.
        for n in _numbers(text):
            if _among(n, worked) or _among(n, so_far):
                continue
            later = next((j for (j, _), (p, w) in checked.items() if j > i and not p and _among(n, w)), None)
            if later is not None:
                errors.append(f'timeline[{i}] {what} "{text}" uses {_fmt(n)} before timeline[{later}] '
                              "works it out; put that line first")
            else:
                errors.append(f'timeline[{i}] {what} "{text}" uses {_fmt(n)}, {_UNKNOWN}')
            break
        so_far += _numbers(text) + worked
    shown: dict[int, list[float]] = {}
    for key, text, _ in items:
        problem, worked = checked[key]
        if not problem:
            shown.setdefault(key[0], []).extend(_numbers(text) + worked)
    return errors, shown


def _check_narration(spec: dict, solution: Solution, seg_units: dict, told: list[float],
                     unit_counts: list[float] | None) -> list[str]:
    """Check 7: every number said or shown is known; spoken sums are right; spoken unit counts are on the board."""
    errors: list[str] = []
    bars = {b["id"]: b for b in bars_of(spec)}

    def unknown(text: str) -> float | None:
        return next((n for n in _numbers(text) if not _among(n, told)), None)

    for i, step in enumerate(spec["timeline"]):
        do, text, say = step["do"], step.get("text", ""), step["say"]
        if do != "write_question":  # reading the question aloud says only its own numbers
            if (n := unknown(say)) is not None:
                errors.append(f"timeline[{i}] says {_fmt(n)}, {_UNKNOWN}")
            elif problem := _spoken_sum_problem(say):
                errors.append(f"timeline[{i}] says it wrong: {problem}")
            elif unit_counts and (m := next((m for m in _SPOKEN_UNITS.finditer(say)
                                             if not _among(float(m[1]), unit_counts)), None)):
                errors.append(f"timeline[{i}] says {m[0]!r}, but no bar or working line shows that many")
        if do in ("write", "highlight", "fill_cell", "mark_point") or not text:
            continue  # working and cells are checked above; a highlight is the question's own text
        if do == "label_unit" and step.get("bar") in bars:
            used = set().union(*seg_units[step["bar"]]) or set(solution.units)
            values = [solution.units[u] for u in sorted(used) if u in solution.units]
            if not values:
                errors.append(f"timeline[{i}] label_unit is on bar {step['bar']!r}, which has no solved unit")
            elif not any(_among(n, values) for n in _numbers(text)):
                errors.append(f"timeline[{i}] label_unit text {text!r} should show the value of 1 unit, "
                              f"{_fmt(values[0])}")
            continue
        if do == "bracket" and _numbers(text):
            spans = _bracket_spans(step, bars, solution, seg_units)
            if spans and not any(_among(n, spans) for n in _numbers(text)):
                errors.append(f"timeline[{i}] bracket text {text!r} does not match what it brackets "
                              f"({_fmt(spans[0])}, or {_fmt(spans[-1])} units)")
                continue
        if (n := unknown(text)) is not None:
            errors.append(f"timeline[{i}] text {text!r} shows {_fmt(n)}, {_UNKNOWN}")

    labels = [(f"stage {s['id']!r}", s["label"]) for s in spec["stages"]]
    for bar in bars.values():
        labels.append((f"bar {bar['id']!r}", bar["label"]))
        labels += [(f"bar {bar['id']!r} segment {k}", seg.get("label", "")) for k, seg in enumerate(bar["segments"])]
    for where, label in labels:
        if (n := unknown(label)) is not None:
            errors.append(f"{where} label {label!r} shows {_fmt(n)}, {_UNKNOWN}")
    return errors


def _check_numbers(spec: dict, solution: Solution) -> list[str]:
    """Checks 6-7. Known: the question's numbers, small counts, solved values and what the board shows."""
    seg_units = _segment_units(spec)
    board_counts, board_values = _board(spec, solution, seg_units)
    known = (question_numbers(spec["question"]) + list(SMALL_COUNTS) + [float(spec["answer"]), solution.asked]
             + list(solution.quantities.values()) + list(solution.units.values())
             + [v for values in solution.segments.values() for v in values] + board_counts + board_values)
    errors, worked = _check_working(spec, solution, known)
    # Narration may use any correct line's numbers, wherever it is; "3 units" must match the bars or
    # a unit count written in correct working, not merely be a small number.
    told = known + [n for numbers in worked.values() for n in numbers]
    tags = _written_units([u["name"] for u in spec["units"]])
    written = [float(m[1]) for i in worked for m in tags.finditer(spec["timeline"][i].get("text", ""))]
    unit_counts = (board_counts + written + [1.0]) if spec["stages"] else None
    return errors + _check_narration(spec, solution, seg_units, told, unit_counts)


def _bracket_spans(step: dict, bars: dict, solution: Solution, seg_units: dict) -> list[float]:
    """What a bracket may be labelled with: the amount it spans (all drawn, or only counted), then its units."""
    if step.get("bars"):
        chosen = [bars[b] for b in step["bars"] if b in bars]
        if len(chosen) != len(step["bars"]):
            return []
        value = sum(_counted(b["segments"], solution.segments.get(b["id"], [])) for b in chosen)
        count = sum(_counted(b["segments"], [sum(c.values()) for c in seg_units[b["id"]]]) for b in chosen)
        return [value, count]
    bar = bars.get(step.get("bar"))
    if bar is None:
        return []
    values = solution.segments.get(bar["id"], [])
    counts = [sum(c.values()) for c in seg_units[bar["id"]]]
    lo, hi = step.get("segments") or (0, len(values) - 1)
    if not 0 <= lo <= hi < len(values):
        return []
    segs, values, counts = bar["segments"][lo:hi + 1], values[lo:hi + 1], counts[lo:hi + 1]
    return [sum(values), _counted(segs, values), _counted(segs, counts), sum(counts)]


def verify_spec_v2(spec: dict) -> VerifyResult:
    """Run checks 1-7 of docs/spec-v2.md §4. unit_value is the solved u (or None); total_units is None."""
    # 1) Schema: everything below may assume the shape.
    errors = _check_schema(spec)
    if errors:
        return VerifyResultV2(ok=False, errors=errors)

    # 2) The relations solve, and the answer is what they give.
    solution: Solution | None = None
    errors = _check_relations_parse(spec)
    if not errors:
        try:
            solution = solve_spec(spec)
        except SpecError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001 — SymPy can fail on odd input; report it, don't crash
            errors.append(f"the relations could not be solved ({type(exc).__name__}); write them more simply")
    if solution is not None and not _close(float(spec["answer"]), solution.asked):
        errors.append(f"answer is {_fmt(spec['answer'])}, but the relations give {_fmt(solution.asked)} "
                      f"for {spec['asked']['expr']}; fix the answer or the relations")

    # 3-5) Grounding, bars and template, timeline references and teaching rules.
    errors += _check_grounding(spec) + _check_bars(spec) + _check_timeline(spec)
    if not errors:
        errors += _check_board(spec, solution)

    # 6-7) Numbers need the solved model; without it they would only add noise to the retry.
    if solution is not None:
        errors += _check_numbers(spec, solution)

    errors = list(dict.fromkeys(errors))
    unit_value = solution.units.get("u") if solution else None
    return VerifyResultV2(ok=not errors, errors=errors, unit_value=unit_value, total_units=None)
