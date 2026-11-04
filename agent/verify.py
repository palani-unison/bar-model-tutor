"""Deterministic BarSpec verification. No LLM calls.

Golden rule: no video or tutor answer ships unless this passes.
Mirrors renderer/render.py:verify() but returns a result instead of exiting,
so plan.py can retry and the API can report the failure.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "specs" / "barspec.schema.json"

SUM_TYPES = {"part-whole", "multiples", "comparison", "ratio", "part-whole with multiples"}

# Accepted by the schema, but render.py draws nothing for these yet.
UNDRAWABLE_ACTIONS = {"cut_bar", "shade_part", "move_units"}
# Actions drawn on one bar: the renderer needs to know which.
BAR_ACTIONS = {"draw_bar", "divide_units", "question_mark", "label_unit"}

TOLERANCE = 0.005  # room for rounded working such as 20 / 3 = 6.67

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_UNITS = re.compile(r"^(\d+(?:\.\d+)?)\s+units?(?:\s+[a-zA-Z]+)*$", re.I)
_DIFFERENCE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s*(?:[a-z]+\s+)?(more|fewer|less)\b", re.I)
_MULTIPLE = re.compile(r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+times\b", re.I)
_RATIO = re.compile(r"(?<![\w.])(\d+)\s*:\s*(\d+)(?:\s*:\s*(\d+))?(?!\w|\.\d)")
_ASKS_DIFFERENCE = re.compile(r"\bhow (?:many|much)\b.*\b(?:more|fewer|less|heavier|lighter|longer|shorter|older|younger)\b.*\bthan\b", re.I)
_WORD_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


@dataclass
class VerifyResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    unit_value: float | None = None
    total_units: float | None = None
    check: str | None = None  # independent second solve: agreed / unavailable (set by plan.py)
    planner: str | None = None  # "glimmer" when the fine-tuned Glimmer's draft won (set by plan.py)

    def __str__(self) -> str:
        if self.ok:
            return f"OK ({self.total_units:g} units, 1 unit = {self.unit_value:g})"
        return "FAILED: " + "; ".join(self.errors)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _numbers(text: str) -> list[float]:
    return [float(m.replace(",", "")) for m in _NUMBER.findall(text)]


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE


def _eval_expr(text: str) -> float | None:
    """Value of a plain arithmetic expression, ignoring a '$' or trailing unit word.

    Returns None for anything that is not arithmetic, such as "Ali".
    """
    expr = re.sub(r"(?<=\d),(?=\d)", "", text).replace("$", "").strip()
    expr = re.sub(r"\s*[a-zA-Z]+$", "", expr)
    expr = expr.replace("×", "*").replace("÷", "/")
    expr = re.sub(r"(?<=[\d\s)])[xX](?=[\s\d(])", "*", expr)
    if not re.fullmatch(r"[\d\s.+\-*/()]*\d[\d\s.+\-*/()]*", expr):
        return None

    def walk(node: ast.AST) -> Fraction:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Fraction(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        raise ValueError(expr)

    try:
        return float(walk(ast.parse(expr, mode="eval")))
    except (SyntaxError, ValueError, ZeroDivisionError):
        return None


def _check_working(text: str, unit_value: float) -> str | None:
    """Check one working line such as "1 unit = 120 ÷ 4 = 30". Returns the problem, if any."""
    values: list[float] = []
    unit_counts: list[float] = []
    for part in text.split("="):
        units = _UNITS.match(part.strip())
        if units:
            unit_counts.append(float(units.group(1)))
            continue
        value = _eval_expr(part)
        if value is not None:
            values.append(value)
    for a, b in zip(values, values[1:]):
        if not _close(a, b):
            return f"{a:g} does not equal {b:g}"
    if not values:
        return None
    for count in unit_counts:
        worth = count * unit_value
        # "1 + 3 = 4 units" counts units; "4 units = 120" states what they are worth.
        if not _close(values[-1], count) and not _close(values[-1], worth):
            return f"{count:g} units is worth {worth:g}, not {values[-1]:g}"
    return None


def _extra(bar: dict) -> float:
    return float(bar.get("extra", 0))


def _bar_value(bar: dict, unit_value: float) -> float:
    return bar["units"] * unit_value + _extra(bar)


def unit_value_of(model: dict) -> tuple[float | None, str | None]:
    """Value of 1 unit from the total, or else from the stated difference. Returns (value, problem)."""
    bars = {b["id"]: b for b in model["bars"]}
    total, diff = model.get("total"), model.get("difference")
    if total:
        value = (float(total) - sum(_extra(b) for b in bars.values())) / sum(b["units"] for b in bars.values())
    elif diff:
        more, less = bars.get(diff["more"]), bars.get(diff["less"])
        if more is None or less is None:
            return None, f"model.difference names unknown bars {diff['more']!r} / {diff['less']!r}"
        units = more["units"] - less["units"]
        if units <= 0:
            return None, "the 'more' bar must have more units than the 'less' bar to find 1 unit"
        value = (float(diff["value"]) - _extra(more) + _extra(less)) / units
    else:
        return None, "model needs a non-zero total or a difference"
    if value <= 0:
        return None, f"1 unit would be worth {value:g}; the bars do not fit the numbers"
    if total and diff and diff["more"] in bars and diff["less"] in bars:
        gap = _bar_value(bars[diff["more"]], value) - _bar_value(bars[diff["less"]], value)
        if not _close(gap, float(diff["value"])):
            return None, f"with this total the difference is {gap:g}, not {diff['value']:g}"
    return value, None


def _final_question(question: str) -> str:
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", question.strip()) if s]
    asking = [s for s in sentences if "?" in s]
    return asking[-1] if asking else sentences[-1]


def _mentions(text: str, label: str) -> bool:
    return re.search(rf"\b{re.escape(label)}\b", text, re.I) is not None


def _check_difference(sentence: str, match: re.Match, bars: list[dict], unit_value: float) -> str | None:
    """Check "Ali has 20 more than Ben" against the bars, including which way round it goes."""
    amount = _numbers(match.group(0))[0]
    before, after = sentence[: match.start()], sentence[match.end():]
    subject = max((b for b in bars if _mentions(before, b["label"])),
                  key=lambda b: max(m.start() for m in re.finditer(rf"\b{re.escape(b['label'])}\b", before, re.I)),
                  default=None)
    than = re.search(r"\bthan\b(.*)", after, re.I)
    others = [b for b in bars if than and _mentions(than.group(1), b["label"])]
    if subject is None or not others:
        return None  # cannot tell who is compared; the other checks still apply
    other = min(others, key=lambda b: re.search(rf"\b{re.escape(b['label'])}\b", than.group(1), re.I).start())
    bigger, smaller = (subject, other) if match.group(1).lower() == "more" else (other, subject)
    gap = _bar_value(bigger, unit_value) - _bar_value(smaller, unit_value)
    if not _close(gap, amount):
        return (f"the question says '{match.group(0)}' ({bigger['label']} minus {smaller['label']} is {amount:g}), "
                f"but the bars give {gap:g}")
    return None


def _check_against_question(spec: dict, unit_value: float) -> list[str]:
    """The model must describe the question that was actually asked."""
    errors: list[str] = []
    question = spec["question"]
    model = spec["model"]
    units = [b["units"] for b in model["bars"]]
    asked = next(b for b in model["bars"] if b["id"] == model["asked"])
    numbers = _numbers(question)

    if model.get("total") and not any(_close(n, float(model["total"])) for n in numbers):
        errors.append(f"model.total {model['total']:g} is not a number in the question")
    diff = model.get("difference")
    if diff and not any(_close(n, float(diff["value"])) for n in numbers):
        errors.append(f"model.difference {diff['value']:g} is not a number in the question")
    for b in model["bars"]:
        if b.get("extra") and not any(_close(n, _extra(b)) for n in numbers):
            errors.append(f"bar '{b['id']}' extra {_extra(b):g} is not a number in the question")

    for sentence in re.split(r"(?<=[.?!])\s+", question):
        for match in _DIFFERENCE.finditer(sentence):
            problem = _check_difference(sentence, match, model["bars"], unit_value)
            if problem:
                errors.append(problem)

    ratios = {a / b for a in units for b in units}
    for word in _MULTIPLE.findall(question):
        multiple = int(word) if word.isdigit() else _WORD_NUMBERS[word.lower()]
        if multiple not in ratios:
            errors.append(f"the question says '{word} times' but no two bars have that ratio (units {units})")

    if spec["problem_type"] == "ratio":
        match = _RATIO.search(question)
        if match:
            parts = [int(g) for g in match.groups() if g]
            if [Fraction(n, sum(parts)) for n in sorted(parts)] != [Fraction(n, sum(units)) for n in sorted(units)]:
                errors.append(f"the question gives ratio {match.group(0)} but the bars have units {units}")

    final = _final_question(question)
    if _ASKS_DIFFERENCE.search(final):
        errors.append("the question asks for a difference between bars, which model.asked cannot express yet")
    named = [b for b in model["bars"] if _mentions(final, b["label"])]
    if named and asked not in named:
        errors.append(f"the question asks about {named[0]['label']} but model.asked is {asked['label']}")
    return errors


def _check_timeline_numbers(spec: dict, unit_value: float) -> list[str]:
    """Every number written or spoken must follow from the verified model."""
    errors: list[str] = []
    model = spec["model"]
    timeline = spec["timeline"]
    answer = float(model["answer"])
    question_numbers = _numbers(spec["question"])

    written: list[float] = []
    for step in timeline:
        if step["do"] in {"write", "answer"}:
            written += _numbers(step.get("text", ""))
    known = question_numbers + written + [1.0, answer, unit_value]
    known += [float(b["units"]) for b in model["bars"]] + [_bar_value(b, unit_value) for b in model["bars"]]

    answers = [s for s in timeline if s["do"] == "answer"]
    if len(answers) != 1:
        errors.append(f"timeline must contain exactly one 'answer' step, found {len(answers)}")

    for i, step in enumerate(timeline):
        do, text, say = step["do"], step.get("text", ""), step["say"]
        if do == "write":
            problem = _check_working(text, unit_value)
            if problem:
                errors.append(f"timeline[{i}] working '{text}' is wrong: {problem}")
        elif do == "answer":
            if not any(_close(n, answer) for n in _numbers(text)):
                errors.append(f"timeline[{i}] answer text '{text}' does not state the answer {answer:g}")
            if not any(_close(n, answer) for n in _numbers(say)):
                errors.append(f"timeline[{i}] answer narration does not state the answer {answer:g}")
        elif do == "bracket_total":
            if not model.get("total") or not any(_close(n, float(model["total"])) for n in _numbers(text)):
                errors.append(f"timeline[{i}] bracket text '{text}' is not the model's total")
        elif do == "bracket_difference":
            diff = model.get("difference")
            if not diff or not any(_close(n, float(diff["value"])) for n in _numbers(text)):
                errors.append(f"timeline[{i}] difference text '{text}' is not the model's difference")
        elif do == "label_unit":
            if not any(_close(n, unit_value) for n in _numbers(text)):
                errors.append(f"timeline[{i}] unit label '{text}' is not the unit value {unit_value:g}")

        for n in _numbers(say):
            if not any(_close(n, k) for k in known):
                errors.append(f"timeline[{i}] narration says {n:g}, which does not follow from the model")
    return errors


def verify_spec(spec: dict) -> VerifyResult:
    errors: list[str] = []

    # 1) Schema validation (structure, enums, required fields).
    try:
        import jsonschema

        jsonschema.validate(instance=spec, schema=_load_schema())
    except ImportError:
        errors.append("jsonschema package not installed; run pip install -r requirements.txt")
        return VerifyResult(ok=False, errors=errors)
    except Exception as exc:  # jsonschema.ValidationError and friends
        errors.append(f"schema: {exc.message if hasattr(exc, 'message') else exc}")
        return VerifyResult(ok=False, errors=errors)

    model = spec.get("model", {})
    bars = model.get("bars", [])
    total = model.get("total")
    asked_id = model.get("asked")
    answer = model.get("answer")

    # 2) Referential integrity.
    bar_ids = [b.get("id") for b in bars]
    if len(bar_ids) != len(set(bar_ids)):
        errors.append("duplicate bar ids")
    asked = next((b for b in bars if b.get("id") == asked_id), None)
    if asked is None:
        errors.append(f"asked id '{asked_id}' not found in bars {bar_ids}")

    known_ids = set(bar_ids)
    for i, step in enumerate(spec.get("timeline", [])):
        sid = step.get("id")
        if sid is not None and sid not in known_ids:
            errors.append(f"timeline[{i}] references unknown bar id '{sid}'")
        elif sid is None and step.get("do") in BAR_ACTIONS:
            errors.append(f"timeline[{i}] '{step['do']}' needs the id of the bar it is drawn on")

    # Rendering draws whole units only, and needs an action it can actually draw.
    for b in bars:
        if not isinstance(b.get("units"), int):
            errors.append(f"bar '{b.get('id')}' must have a whole number of units, got {b.get('units')}")
    for i, step in enumerate(spec.get("timeline", [])):
        if step.get("do") in UNDRAWABLE_ACTIONS:
            errors.append(f"timeline[{i}] action '{step['do']}' cannot be drawn yet")

    # 3) Teaching-rule checks.
    dos = [s.get("do") for s in spec.get("timeline", [])]
    if "pause" not in dos:
        errors.append("timeline must contain at least one 'pause' step (Socratic think time)")
    for i, step in enumerate(spec.get("timeline", [])):
        say = step.get("say", "")
        # write_question reads the whole question aloud, however long it is.
        if step.get("do") != "write_question" and len(say.split()) > 20:
            errors.append(f"timeline[{i}] say line over 20 words ({len(say.split())} words)")

    if errors:
        return VerifyResult(ok=False, errors=errors)

    # 4) Only types whose answer can be checked may be called verified.
    if spec.get("problem_type") not in SUM_TYPES:
        errors.append(f"problem type '{spec.get('problem_type')}' cannot be verified yet")
        return VerifyResult(ok=False, errors=errors)

    # 5) Arithmetic check.
    total_units = sum(float(b["units"]) for b in bars)
    unit_value, problem = unit_value_of(model)
    if problem:
        errors.append(problem)
    else:
        expected = _bar_value(asked, unit_value)
        if abs(expected - float(answer)) > TOLERANCE:
            errors.append(
                f"answer mismatch: {asked['label']} = {asked['units']} units x {unit_value:g} + {_extra(asked):g} "
                f"= {expected:g}, but spec answer is {answer}"
            )

        # 6) The model must match the question, and the timeline must match the model.
        errors += _check_against_question(spec, unit_value)
        errors += _check_timeline_numbers(spec, unit_value)

    return VerifyResult(ok=not errors, errors=errors, unit_value=unit_value, total_units=total_units)


def verify_or_raise(spec: dict) -> VerifyResult:
    result = verify_spec(spec)
    if not result.ok:
        raise ValueError(str(result))
    return result
