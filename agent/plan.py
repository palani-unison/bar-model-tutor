"""ProblemJSON → BarSpec (v1 or v2), with the verify-retry loop.

Golden rule enforcement point: a spec is returned only after it verifies and the
independent second solver agrees with its answer.

Routing:
  - Questions that need a figure, or have several parts, are refused up front.
  - Before-after, transfer, remainder, percentage, ratio change, "how many more",
    equal-end, constant total/difference, units-and-value, excess-shortage,
    assumption, average and rate questions go straight to BarSpec v2
    (equations + staged bars, docs/spec-v2.md).
  - Everything else tries v1 (units + total/difference, proven on the public set)
    for 2 attempts, then falls through to v2.
Each path climbs the same ladder: Spark low, low, medium, then Wonder.

Without an API key, a deterministic fallback planner covers the common multiples /
part-whole patterns so the demo still runs.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from . import check as _check
from . import client as _client
from .verify import _ASKS_DIFFERENCE, VerifyResult, _final_question, _mentions, verify_spec
from .verify_v2 import verify_spec_v2

logger = logging.getLogger("bar_model_tutor.plan")

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "plan.md"
PROMPT_V2_PATH = Path(__file__).resolve().parent / "prompts" / "plan_v2.md"
SCHEMA_PATH = ROOT / "specs" / "barspec.schema.json"
SCHEMA_V2_PATH = ROOT / "specs" / "barspec.v2.schema.json"

# Low effort plans a typical problem in ~7 s instead of ~28 s at the model default (see V2_WAVES).
CHECK_WAIT_S = 30  # the checker usually answers in ~3 s, well before the first plan

V2_TIMEOUT_S = 150  # a v2 spec is long; at medium effort it can take over a minute



def _prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Plan a Singapore bar model. Return only BarSpec JSON."


def _prompt_v2() -> str:
    return PROMPT_V2_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------- question text
_LATEX_MATH = re.compile(r"\$([^$]*\\[^$]*)\$")  # $\frac{1}{3}$, $5\%$, $2.5\mathrm{kg}$ (a lone "$5" is money)
_LATEX_FRAC = re.compile(r"\\d?frac\s*\{\s*([\d.]+)\s*\}\s*\{\s*([\d.]+)\s*\}")


def plain_question(text: str) -> str:
    """School papers write fractions in LaTeX ($\\frac{2}{5}$). Plan from the plain text a child reads: 2/5."""

    def math(m: re.Match) -> str:
        s = re.sub(r"(\d)\s*(?=\\d?frac)", r"\1 ", m.group(1))  # mixed number 2\frac{1}{2} -> 2 1/2
        s = _LATEX_FRAC.sub(r"\1/\2", s)
        s = re.sub(r"\\(?:mathrm|text)\s*\{([^}]*)\}", r" \1", s)
        s = s.replace("\\%", "%").replace("\\times", "×").replace("\\div", "÷").replace("\\,", " ").replace("\\ ", " ")
        return " ".join(s.split())

    text = _LATEX_MATH.sub(math, text or "")
    text = re.sub(r"(?<![\d.,])(\d{1,3})(?:[ \u00a0\u2009\u202f](\d{3}))+(?:\.\d+)?(?!\d|[.,]\d|\s\d)",
                  lambda m: re.sub(r"[ \u00a0\u2009\u202f]", "", m.group(0)), text)  # "$4 000" -> "$4000"
    text = re.sub(r"\$\s*(\d+(?:\s*:\s*\d+)+)\s*\$", r"\1", text)  # $1:2:3$ is a ratio, not money
    if len(set(_PART_LABEL.findall(text))) == 1:  # one part of a paper question: "b) How many ...?"
        text = _PART_LABEL.sub("", text)
    return " ".join(text.split())


# ---------------------------------------------------------------- fallback planner
def _fallback_spec(problem: dict) -> dict | None:
    """Deterministic BarSpec for 'A has N times as many as B; together T' problems."""
    q = problem.get("question", "")
    m = re.search(
        r"([A-Z][a-z]+)\s+has\s+(\d+)\s+times as many[^.]*?as\s+([A-Z][a-z]+)\b.*?Together they have\s+(\d+)",
        q, re.I | re.S,
    )
    if not m:
        return None
    a_name, mult, b_name, total = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    a_id, b_id = a_name.lower(), b_name.lower()
    total_units = 1 + mult
    if total % total_units != 0:
        return None  # not cleanly divisible — let the LLM handle it
    unit = total // total_units
    final = _final_question(q)
    asks_a, asks_b = _mentions(final, a_name), _mentions(final, b_name)
    if asks_a == asks_b:
        return None  # asks about both or neither (e.g. a difference) — not this pattern
    asked_id, asked_name, asked_units = (a_id, a_name, mult) if asks_a else (b_id, b_name, 1)
    answer = asked_units * unit
    obj = "marbles"
    om = re.search(r"as many\s+(\w+)\s+as", q, re.I)
    if om:
        obj = om.group(1)

    return {
        "title": "Bar Model: Comparison (Multiples)",
        "level": problem.get("level", "P3-P4"),
        "problem_type": "multiples",
        "question": q,
        "model": {
            "bars": [
                {"id": b_id, "label": b_name, "units": 1},
                {"id": a_id, "label": a_name, "units": mult},
            ],
            "total": total,
            "asked": asked_id,
            "answer": answer,
        },
        "timeline": [
            {"do": "write_question", "say": q},
            {"do": "highlight", "text": f"{mult} times as many", "say": f"First, find the key information. {a_name} has {mult} times as many as {b_name}."},
            {"do": "highlight", "text": f"{total} {obj}", "say": f"And together, they have {total} {obj}."},
            {"do": "draw_bar", "id": b_id, "say": f"Let's draw {b_name}'s bar first. {b_name} has 1 unit."},
            {"do": "draw_bar", "id": a_id, "say": f"{a_name} has {mult} times as many, so the bar is {mult} units."},
            {"do": "divide_units", "id": a_id, "say": "Let's mark the units, so we can see they are all equal."},
            {"do": "bracket_total", "text": str(total), "say": f"Both bars together make {total} {obj}."},
            {"do": "question_mark", "id": asked_id, "say": f"We want to find {asked_name}'s {obj}, so we put a question mark here."},
            {"do": "pause", "seconds": 2.0, "say": "Pause and think. How many units are there altogether?"},
            {"do": "write", "text": f"1 + {mult} = {total_units} units", "say": f"Count the units. 1 plus {mult} makes {total_units} units."},
            {"do": "write", "text": f"{total_units} units = {total}", "say": f"{total_units} units are worth {total} {obj}."},
            {"do": "write", "text": f"1 unit = {total} ÷ {total_units} = {unit}", "say": f"So 1 unit is {total} divided by {total_units}, which is {unit}."},
            {"do": "label_unit", "id": b_id, "text": str(unit), "say": f"Each unit is {unit} {obj}."},
            *([{"do": "write", "text": f"{mult} units = {mult} × {unit} = {answer}", "say": f"{a_name} has {mult} units. {mult} times {unit} is {answer}."}] if asks_a else []),
            {"do": "answer", "text": f"{asked_name} has {answer} {obj}.", "say": f"So, {asked_name} has {answer} {obj}. Well done!"},
        ],
    }


# ---------------------------------------------------------------- LLM planner
def _schema_format(path: Path, name: str) -> dict:
    schema = json.loads(path.read_text(encoding="utf-8"))
    schema.pop("$schema", None)
    schema.pop("$id", None)
    return {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": False}}


def _response_format() -> dict:
    """Constrain output to the BarSpec schema. Non-strict: strict mode needs every object closed."""
    return _schema_format(SCHEMA_PATH, "barspec")


def _response_format_v2() -> dict:
    fmt = _schema_format(SCHEMA_V2_PATH, "barspec_v2")
    # The API's constrained decoding emits listed keys in alphabetical order. A step starts with
    # "do", after which the listed keys "bar" and "bars" can no longer be written (the model then
    # invents keys like "bar folks"). So timeline steps are a free object for decoding only;
    # verification still checks every step against the real schema.
    fmt["json_schema"]["schema"]["properties"]["timeline"]["items"] = {"type": "object"}
    return fmt


def _llm_spec(problem: dict, model: str, effort: str, feedback: str = "") -> dict:
    messages = [
        {"role": "system", "content": _prompt()},
        {"role": "user", "content": json.dumps(problem, ensure_ascii=False) + feedback},
    ]
    resp = _client.chat(messages, model=model, reasoning_effort=effort, response_format=_response_format())
    return json.loads(resp.choices[0].message.content or "{}")


def _v2_request(problem: dict, family: str | None) -> str:
    req = {"question": problem.get("question", "")}
    level = str(problem.get("level") or "")
    if re.fullmatch(r"P[3-6](-P[3-6])?", level):
        req["level"] = level
    if family:
        req["looks_like"] = family
    return json.dumps(req, ensure_ascii=False)


def _llm_spec_v2(problem: dict, model: str, effort: str, family: str | None, feedback: str = "") -> dict:
    messages = [
        {"role": "system", "content": _prompt_v2()},
        {"role": "user", "content": _v2_request(problem, family) + feedback},
    ]
    resp = _client.chat(messages, model=model, reasoning_effort=effort, response_format=_response_format_v2(),
                        timeout=V2_TIMEOUT_S)
    spec = json.loads(resp.choices[0].message.content or "{}")
    if not isinstance(spec, dict):
        raise json.JSONDecodeError("not an object", "", 0)
    return spec


def _layout_error(spec: dict) -> str | None:
    """A verified spec must also be drawable, or the video fails after the badge said 'verified'."""
    from renderer.render import layout_error

    return layout_error(spec)


# ---------------------------------------------------------------- refusals and routing
# Questions that need a picture we cannot see, or several answers.
_NEEDS_FIGURE = re.compile(
    r"\b(?:figure|diagram|graph|pie chart|bar chart|line chart)\b"
    r"|\b(?:shown|given|drawn)\s+(?:below|above)\b"
    r"|\b(?:the|this)\s+table\s+(?:below|above|shows)\b|\b(?:in|from)\s+the\s+table\b|\btable\s+(?:below|above)\b"
    r"|!\[[^\]]*\]\(|<img\b|\bimg-\d+\.(?:jpe?g|png)\b",  # an image the question points at
    re.I)
_EACH_OF_GROUP = re.compile(r"\beach of (?:his|her|their|the|its|my|our) (friends|children|pupils|students|classmates|"
                            r"cousins|brothers|sisters|neighbours|teammates|boxes|bags|baskets|jars|shelves|tables)\b",
                            re.I)
_COUNT_WORDS = ("two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "several")
_PART_LABEL = re.compile(r"(?:^|(?<=\s))\(?([a-d])\)\s+(?=[A-Z])")

# Question shapes v1 cannot express: they go straight to v2.
_V2_FAMILIES: list[tuple[str, re.Pattern]] = [
    ("percentage", re.compile(r"%|\bper ?cent|\bpercentage\b|\bdiscount|\bGST\b|\binterest\b", re.I)),
    ("ratio change", re.compile(r"\bratio\b.*\b(?:became|becomes|become|changed?|new ratio|then|after|now|in the end|at first|remaining|left)\b", re.I | re.S)),
    ("remainder", re.compile(r"\bremainder\b|\bthe rest\b|\bof the remaining\b|\bof what was left\b|\bremaining\b", re.I)),
    ("transfer", re.compile(r"\b(?:gave|gives|give|giving|transferred|transfers?|passed|moved|poured)\b[^.?]*\bto\b", re.I)),
    ("before-after", re.compile(
        r"\b(?:at first|in the end|at the end|left with|initially|originally|at the start|at last)\b"
        r"|\bafter\b[^.?]*\b(?:gave|give|spent|spend|sold|sell|bought|buy|received|receive|lost|lose|used|use|added|"
        r"removed|took|take|ate|eaten|donated|threw|withdrew|deposited|saved|collected|baked|made)\b"
        r"|\b(?:had|has|have|were|was|are|is)\b[^.?]*\b(?:left|remained|remaining)\b", re.I)),
    ("equal-end", re.compile(r"\b(?:same number|same amount|equal number|equal amount|the same|equally|as many \w+ as each other)\b", re.I)),
    ("constant difference", re.compile(r"\byears? ago\b|\byears?'? ?time\b|\byears? later\b|\byears? old\b|\b(?:his|her|their) ages?\b|\bage\b", re.I)),
    ("units and value", re.compile(r"\bfor every\b|\beach\b[^.?]*\bcosts?\b|\bcosts?\b[^.?]*\beach\b|\bper (?:kg|box|packet|bottle|item|piece|pen)\b", re.I)),
    ("excess-shortage", re.compile(r"\bshort of\b|\bshortage\b|\bleft over\b|\b\d+ (?:more|extra) \w+ (?:would be )?needed\b", re.I)),
    ("assumption", re.compile(r"\blegs\b|\bheads\b|\bwheels\b|\bdeducted\b|\bif all\b", re.I)),
    ("average", re.compile(r"\baverage\b", re.I)),
    ("rate", re.compile(r"\bspeed\b|\bkm/h\b|\bm/min\b|\bper (?:hour|minute|second|day|week)\b|\brate\b", re.I)),
]


def unsupported_reason(question: str) -> str | None:
    """Why the app will not try this question at all, or None."""
    if _NEEDS_FIGURE.search(question):
        return "questions that need a figure, table or graph"
    if len(set(_PART_LABEL.findall(question))) >= 2:
        return "questions with several parts"
    # Excess-shortage ("If she gives 4 to each of her pupils... If 5 each...") solves for that count, so it is fine.
    if (m := _EACH_OF_GROUP.search(question)) and len(_EACH_OF_GROUP.findall(question)) == 1 \
            and not re.search(r"\bif\b", question, re.I) and not re.search(
            rf"(?:\d+|\b(?:{'|'.join(_COUNT_WORDS)}))\s+(?:\w+\s+)?{m[1]}\b", question, re.I):
        return "questions that seem to be missing a number"  # "gave 204 to each of her friends": how many friends?
    return None


def v2_family(question: str) -> str | None:
    """The problem family that sends this question straight to BarSpec v2, or None (try v1 first)."""
    if _ASKS_DIFFERENCE.search(_final_question(question)):
        return "how many more"
    for name, pattern in _V2_FAMILIES:
        if pattern.search(question):
            return name
    return None


_ROUNDING = re.compile(r"\bround|\bnearest\b|decimal places?|\bcorrect to\b|\bapproximately\b", re.I)


def answer_shape_problem(answer: float, question: str) -> str | None:
    """A P4-P6 answer is a whole number or a short decimal ($187.09, 0.375 kg), unless rounding is asked.

    Round 4 showed an answer of 187.0909... (money): the planner had flipped a "more than" so the
    equations had a positive solution, and a checker matched it. No answer key looks like that.
    """
    if _ROUNDING.search(question):
        return None
    if abs(float(answer) * 1000 - round(float(answer) * 1000)) > 1e-6:
        return (f"the answer {float(answer):.6g} does not come out exact (more than 3 decimal places); "
                "re-read the question: a fact is probably the wrong way round")
    return None


_COMPARE = re.compile(r"^(?P<subject>.*?)\$?(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?:[a-z]+\s+){0,2}?"
                      r"(?P<dir>more|less|fewer|heavier|lighter|longer|shorter|older|younger|taller)\s+than\s+(?P<other>.+)$",
                      re.I)
_BIGGER = {"more", "heavier", "longer", "older", "taller"}


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower().replace("-", ""))
    return {w[:5] for w in words if len(w) > 2 and w not in {"the", "each", "and", "cost", "costs", "than", "had", "has"}}


def comparison_problems(spec: dict) -> list[str]:
    """'A cost $8 more than B' must give A = B + 8, not B = A + 8.

    Checked only when the relation is a plain difference and the quantity ids name the two sides
    clearly; otherwise it says nothing (the second solver still applies).
    """
    problems = []
    for rel in spec.get("relations") or []:
        m = _COMPARE.match(str(rel.get("from", "")).strip())
        eq = str(rel.get("eq", "")).replace(" ", "")
        if not m:
            continue
        n = m.group("n").replace(",", "")
        shape = (re.fullmatch(rf"([a-z][a-z0-9_]*)=([a-z][a-z0-9_]*)([+-]){re.escape(n)}", eq)
                 or re.fullmatch(rf"([a-z][a-z0-9_]*)-([a-z][a-z0-9_]*)()={re.escape(n)}", eq))
        if not shape:
            continue
        left, right, sign = shape.groups()
        bigger, smaller = (left, right) if sign in ("+", "") else (right, left)
        if m.group("dir").lower() not in _BIGGER:
            bigger, smaller = smaller, bigger  # "A is 8 less than B": B is bigger
        subject, other = _stems(m.group("subject")), _stems(m.group("other"))
        big_ids, small_ids = _stems(bigger.replace("_", " ")), _stems(smaller.replace("_", " "))
        if big_ids & other and small_ids & subject and not big_ids & subject and not small_ids & other:
            problems.append(f"relation '{rel['eq']}' has '{rel['from']}' the wrong way round: "
                            f"the phrase makes {smaller} the bigger amount")
    return problems


# "Each pair of shorts cost $8 more than each T-shirt": one item against one item.
_EACH_VS_EACH = re.compile(r"\beach\b[^.?]*\b(?:more|less|fewer|cheaper|dearer|times|than)\b[^.?]*\beach\b", re.I)
# "7 pairs of shorts", "3 T-shirts": how many items were bought (not "2/9 of", "$8 more", "3 times").
_ITEM_COUNT = re.compile(r"(?<![\d./$])(\d+)\s+(?!(?:of|more|less|fewer|times|than|and|or|to|in|at|for|each|per|"
                         r"km|m|cm|kg|g|l|ml|h|min|hours?|minutes?|years?|days?|weeks?|months?)\b)[A-Za-z]", re.I)


def per_item_problems(spec: dict) -> list[str]:
    """'Each pair of shorts cost $8 more than each T-shirt' compares single items. When the question also says how
    many were bought (7 pairs, 3 T-shirts), a relation for that phrase between two totals is a misread: the
    quantities it uses must be tied to a count somewhere (shorts_total = 7*shorts_each), or it must use the counts
    itself (shorts_total/7 = tshirts_total/3 + 8)."""
    question = str(spec.get("question", ""))
    counts = {float(m.group(1)) for m in _ITEM_COUNT.finditer(question)} - {0.0, 1.0}
    if not counts:
        return []
    from .spec_v2 import expr_numbers

    relations = [r for r in spec.get("relations") or [] if isinstance(r, dict)]
    problems = []
    for rel in relations:
        if not _EACH_VS_EACH.search(str(rel.get("from", ""))):
            continue
        eq = str(rel.get("eq", ""))
        if counts & set(expr_numbers(eq)):
            continue
        names = set(re.findall(r"[a-z][a-z0-9_]*", eq))
        tied = {n for r in relations for n in names
                if re.search(rf"\b{re.escape(n)}\b", str(r.get("eq", ""))) and counts & set(expr_numbers(str(r.get("eq", ""))))}
        if names and not tied:
            problems.append(f"relation '{eq}' reads '{rel['from']}' as a fact about totals, but it compares ONE item "
                            "with ONE item; use the price of one item (total = count * price of one)")
    return problems


_COUNTED = re.compile(r"(?<![\d./$])(\d+)\s+(?:pairs?\s+of\s+|boxes\s+of\s+|packets?\s+of\s+)?([A-Za-z][A-Za-z-]*)")
_TIMES_NAME = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*\*\s*([a-z][a-z0-9_]*)|([a-z][a-z0-9_]*)\s*\*\s*(\d+(?:\.\d+)?)(?![\w.])")


def quoted_number_problems(spec: dict) -> list[str]:
    """"on 7 pairs of shorts" written as `3*shorts_each = shorts_total` swaps two of the question's counts.
    Flagged only for a term `k*name` (k > 2) where `name` is the thing the phrase counts (shorts) and `k` is not
    the phrase's count for it."""
    problems = []
    for rel in spec.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        counted = {}
        for m in _COUNTED.finditer(str(rel.get("from", ""))):
            stems = _stems(m.group(2).replace("-", " "))
            if stems and float(m.group(1)) > 1:
                counted[float(m.group(1))] = stems
        if not counted:
            continue
        eq = str(rel.get("eq", "")).replace(" ", "")
        for m in _TIMES_NAME.finditer(eq):
            k, name = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            k, stems = float(k), _stems(name.replace("_", " "))
            if k <= 2:
                continue  # 2 legs, "twice": numbers a question leaves implicit, not a count of items
            wanted = [c for c, noun in counted.items() if noun & stems]
            if wanted and all(abs(k - c) > 1e-9 for c in wanted):
                problems.append(f"relation '{rel['eq']}' multiplies {name} by {k:g}, but its phrase '{rel['from']}' "
                                f"counts {wanted[0]:g}; check which number goes with which item")
                break
    return problems


_ALTOGETHER = re.compile(r"\b(?:altogether|in all|in total|total)\b", re.I)
_AT_FIRST = re.compile(r"\b(?:at first|initially|originally|at the start|in the beginning|before|first)\b", re.I)
_MORE_LATER = re.compile(r"\b(?:another|again|later|then)\b|\bmore\b(?!\s+than)", re.I)  # "more than" compares


def altogether_problems(spec: dict) -> list[str]:
    """"She baked 4 times as many vanilla as banana muffins ... and baked another 65 banana muffins. How many muffins
    did she bake altogether?" counts the 65 too. When the question asks "altogether" without "at first", `asked`
    must not be only the at-first amounts while a relation adds more (`banana_after = banana_before + 65`, from
    "baked another 65")."""
    from .lesson import _final_question

    ask = _final_question(str(spec.get("question", "")))
    if not _ALTOGETHER.search(ask) or _AT_FIRST.search(ask):
        return []
    expr = str((spec.get("asked") or {}).get("expr", ""))
    names = set(re.findall(r"[a-z][a-z0-9_]*", expr))
    if re.search(r"\d", re.sub(r"[a-z][a-z0-9_]*", "", expr)):
        return []  # "vanilla_before + banana_before + 65" already counts what came later
    if not names or not all(re.search(r"(?:_before|_at_first|_first|_start|_initial)$", n) for n in names):
        return []
    added = [r for r in spec.get("relations") or [] if isinstance(r, dict) and _MORE_LATER.search(str(r.get("from", "")))
             and re.search(r"[a-z0-9_]*_before\s*\+", str(r.get("eq", "")))
             and not re.match(r"\s*[a-z0-9_]*_before\s*=", str(r.get("eq", "")))]  # a later amount goes into an "after"
    if not added:
        return []
    return [f"asked '{expr}' counts only what there was at first, but the question asks for the total "
            f"altogether, which includes '{added[0]['from']}'"]


def invented_numbers(spec: dict) -> list[str]:
    """Structural numbers used as facts the question never states.

    The verifier lets 0, 1, 2, 10 and 100 into relations without a source, because "half",
    "percent" and "times 10" need them. Two wrong answers in the school eval came through that
    door: a made-up price `price = 1` (the real one was in a picture) and a `100` with no
    percentage in sight. So the planner also refuses a quantity set straight to a number the
    question does not write, and a 10 or 100 the question gives no reason for.
    """
    from .spec_v2 import expr_numbers
    from .verify import _numbers
    from .verify_v2 import question_numbers

    question = spec.get("question", "")
    given = question_numbers(question)
    low = question.lower()
    reasons = {
        100.0: bool(re.search(r"%|per ?cent|¢|\bcm\b|\bcents?\b|hundred|\b100\b|digit", low)),
        10.0: bool(re.search(r"\bten(?:th)?s?\b|\b10\b|\bmm\b|decimal|digit", low)),
    }
    problems = []
    for rel in spec.get("relations") or []:
        eq = str(rel.get("eq", ""))
        sides = [p.strip() for p in eq.replace("==", "=").split("=")]
        if len(sides) == 2:
            for name, value in (sides, sides[::-1]):
                if re.fullmatch(r"[a-z][a-z0-9_]*", name) and re.fullmatch(r"\d+(?:\.\d+)?", value) and float(value) != 0:
                    digits = "digit" in low and float(value) in (10.0, 100.0, 1000.0)  # "the smallest 3-digit number"
                    if not digits and not any(abs(float(value) - g) < 1e-9 for g in given + _numbers(question)):
                        problems.append(f"relation '{eq}' sets {name} to {value}, a number the question does not give")
        for n in expr_numbers(eq):
            if n in reasons and not reasons[n] and not any(abs(n - g) < 1e-9 for g in given):
                problems.append(f"relation '{eq}' uses {n:g}, but the question gives no reason for it")
    return problems


def verify_v2(spec: dict) -> VerifyResult:
    """The v2 gate: docs/spec-v2.md section 4, items 1-7 (agent/verify_v2.py), plus invented_numbers."""
    result = verify_spec_v2(spec)
    if result.ok:
        extra = (invented_numbers(spec) + comparison_problems(spec) + per_item_problems(spec)
                 + quoted_number_problems(spec) + altogether_problems(spec))
        shape = answer_shape_problem(spec["answer"], spec.get("question", ""))
        extra += [shape] if shape else []
        if extra:
            result.ok, result.errors = False, extra
    return result


def _status(result: VerifyResult) -> str:
    if result.ok:
        unit = getattr(result, "unit_value", None)
        return "OK" + (f" (1 unit = {unit:g})" if isinstance(unit, (int, float)) else "")
    return "FAILED: " + "; ".join(result.errors)


# ---------------------------------------------------------------- the loop
def careful_answer(question: str) -> float | None:
    """A second, slower independent solve (the checker model at medium effort), or None."""
    try:
        resp = _client.chat(
            [{"role": "system", "content": _check.PROMPT}, {"role": "user", "content": question}],
            model=_client.MODEL_CHECK, retries=1, reasoning_effort="medium",
            response_format={"type": "json_object"},
        )
        return float(json.loads(resp.choices[0].message.content or "{}")["answer"])
    except Exception as exc:  # noqa: BLE001 — a missing re-check keeps the first check's answer
        logger.warning("careful re-check unavailable: %s", exc)
        return None


class _SecondSolver:
    """The independent check, started in parallel with planning.

    After the first disagreement, a careful re-solve replaces the quick one for the rest of the
    loop. Without it the planner, told it disagreed, sometimes bent its relations to reach the
    quick checker's wrong number, and the two then "agreed" on a wrong answer.
    """

    def __init__(self, question: str):
        self.question = question
        pool = ThreadPoolExecutor(max_workers=1)
        self._future = pool.submit(_check.independent_answer, question)
        pool.shutdown(wait=False)
        self._done = False
        self._answer: float | None = None
        self.careful = False

    def answer(self) -> float | None:
        if not self._done:
            try:
                self._answer = self._future.result(timeout=CHECK_WAIT_S)
            except Exception:  # noqa: BLE001 — timeout: answer without the second check
                self._answer = None
            self._done = True
        return self._answer

    def recheck(self, log: list[str]) -> bool:
        """Re-solve carefully once. True if this produced a new answer to compare against."""
        if self.careful:
            return False
        self.careful = True
        again = careful_answer(self.question)
        if again is None:
            log.append("careful re-check unavailable")
            return False
        log.append(f"careful re-check ({_client.MODEL_CHECK}, medium): {again:g}")
        self._answer = again
        return True


NO_CHECK = ("Sorry, I could not double-check this answer right now, so I won't show it. "
            "Please try again in a moment.")
DISAGREE_FEEDBACK = ("\n\nAn independent solver got a different answer from yours. Re-read the question: "
                     "which person or quantity does the final question ask for, and is every given number "
                     "used the way the question states? Do not invent numbers the question does not give. "
                     "Return corrected JSON only.")


def _judge(spec_answer: float, checker: _SecondSolver, result: VerifyResult, log: list[str]) -> bool | None:
    """True: show it. None: the checker disagrees (retry). False: no check could be made (decline)."""
    check_answer = checker.answer()
    if check_answer is None:
        # Never show an unchecked answer: round 3 showed a wrong v1 answer this way.
        log.append("independent check unavailable")
        if not checker.recheck(log):
            return False
        check_answer = checker.answer()
    if _check.agrees(spec_answer, check_answer):
        result.check = "agreed"
        log.append(f"independent check ({_client.MODEL_CHECK}) agreed: {check_answer:g}")
        return True
    log.append(f"independent check ({_client.MODEL_CHECK}) disagreed: {check_answer:g}")
    if checker.recheck(log) and _check.agrees(spec_answer, checker.answer()):
        result.check = "agreed"
        log.append(f"careful re-check agreed: {checker.answer():g}")
        return True
    return None


# Waves of attempts. The drafts in a wave run in parallel and the first one that verifies and
# agrees with the independent solver wins; the next wave gets the verifier's feedback. Measured on
# the school eval (round 3): Spark low verifies 75% of v2 first drafts, and every sequential retry
# added ~23 s, so speculating in parallel cuts the slow tail instead of waiting for it.
V2_WAVES = [
    [("v2", _client.MODEL_DEFAULT, "low"), ("v2", _client.MODEL_DEFAULT, "low")],
    [("v2", _client.MODEL_DEFAULT, "low"), ("v2", _client.MODEL_DEFAULT, "medium")],
    [("v2", _client.MODEL_ESCALATION, "medium")],
]
# Questions v1 handles: race one v1 draft against one v2 draft, then continue with the v2 waves.
V1_RACE = [("v1", _client.MODEL_DEFAULT, "low"), ("v2", _client.MODEL_DEFAULT, "low")]


def _draft(kind: str, problem: dict, family: str | None, model: str, effort: str, feedback: str):
    """One model draft -> (spec or None, why it is None)."""
    try:
        if kind == "v1":
            return _llm_spec(problem, model=model, effort=effort, feedback=feedback), ""
        return _llm_spec_v2(problem, model=model, effort=effort, family=family, feedback=feedback), ""
    except _client.ModelUnavailableError:
        return None, "model unavailable"
    except json.JSONDecodeError:
        return None, "invalid JSON"


def _check_draft(kind: str, spec: dict, question: str) -> VerifyResult:
    if kind == "v1":
        result = verify_spec(spec)
        shape = answer_shape_problem(spec["model"]["answer"], question) if result.ok else None
        if shape:
            result.ok, result.errors = False, [shape]
    else:
        spec["version"] = spec.get("version", 2)
        if " ".join(str(spec.get("question", "")).split()) != question:
            spec["question"] = question  # quotes and highlights are checked against the real question
        try:
            result = verify_v2(spec)
            if not result.ok and _trim_extras(spec, result.errors):
                result = verify_v2(spec)
        except Exception as exc:  # noqa: BLE001 — a verifier crash is a failed spec, never a shown answer
            result = VerifyResult(ok=False, errors=[f"verifier error: {type(exc).__name__}: {exc}"])
    if result.ok:
        drawn = _layout_error(spec)
        if drawn and kind == "v2" and _trim_extras(spec, None):
            result = verify_v2(spec)  # dropping a table's cells can take away numbers later lines use
            drawn = _layout_error(spec) if result.ok else None
        if drawn:
            result.ok, result.errors = False, [f"the whiteboard could not lay out this spec ({drawn})"]
    return result


CHART_STEPS = ("table", "fill_cell", "number_line", "mark_point")


def _check_steps(spec: dict) -> list[int]:
    """Timeline indexes of beat 8's check lines ("Check: 60 - 20 = 40 = 20 + 20 ✓")."""
    return [i for i, s in enumerate(spec.get("timeline") or [])
            if isinstance(s, dict) and s.get("do") == "write" and str(s.get("text", "")).lower().startswith("check")]


def _extra_steps(spec: dict) -> list[int]:
    """Check lines and the steps of a table or number line: the board's extras, not its maths."""
    charts = [i for i, s in enumerate(spec.get("timeline") or []) if isinstance(s, dict) and s.get("do") in CHART_STEPS]
    return sorted(set(_check_steps(spec) + charts))


def _trim_extras(spec: dict, errors: list[str] | None) -> bool:
    """Drop the presentation extras (the `board` name, check lines, a table or number line) when they are all
    that fails.

    The maths is verified either way, so a draft whose only problem is its board shape, its check line or its
    table keeps its verified answer and falls back to the plain picture, instead of costing a retry. `errors`
    None means the board didn't lay out: drop the extras to make room. Returns True when it changed the spec;
    the caller verifies again.
    """
    extras = _extra_steps(spec)
    if errors is not None:
        tags = [f"timeline[{i}]" for i in extras]
        rest = [e for e in errors if not e.startswith(("board ", "schema: board"))]
        if any(not any(t in e for t in tags) for e in rest):
            return False
        if len(rest) < len(errors):
            spec.pop("board", None)
        if not rest:
            return True
    if not extras:
        return False
    for i in reversed(extras):
        del spec["timeline"][i]
    return True


def _run_waves(problem: dict, family: str | None, waves: list[list[tuple[str, str, str]]],
               checker: _SecondSolver, log: list[str],
               glimmer_future: Future | None = None) -> tuple[dict, VerifyResult] | None:
    """Run the waves; a Glimmer draft (PLANNER_MODEL=glimmer) races alongside them until it lands."""
    question = problem.get("question", "")
    feedback = {"v1": "", "v2": ""}
    attempt = 0
    for wave in waves:
        pool = ThreadPoolExecutor(max_workers=len(wave))
        futures = {}
        for kind, model, effort in wave:
            futures[pool.submit(_draft, kind, problem, family, model, effort, feedback[kind])] = (attempt, kind, model)
            attempt += 1
        pool.shutdown(wait=False)  # a losing draft finishes in the background and is dropped
        next_feedback = dict(feedback)
        pending = set(futures) | ({glimmer_future} if glimmer_future else set())
        while pending & futures.keys():
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                if fut is glimmer_future:
                    glimmer_future = None
                    found = _take_glimmer(fut, checker, question, log)
                    if found:
                        return found
                    continue
                n, kind, model = futures[fut]
                spec, why = fut.result()
                if spec is None:
                    log.append(f"attempt {n} {kind} model={model} -> {why}")
                    if why == "invalid JSON":
                        next_feedback[kind] = "\n\nYour previous reply was not valid JSON. Return corrected JSON only."
                    continue
                result = _check_draft(kind, spec, question)
                log.append(f"attempt {n} {kind} model={model} -> {_status(result)}")
                if not result.ok:
                    next_feedback[kind] = (f"\n\nYour previous spec failed verification: {_status(result)}. "
                                           "Fix every listed problem and return corrected JSON only.")
                    continue
                verdict = _judge(spec["answer"] if kind == "v2" else spec["model"]["answer"], checker, result, log)
                if verdict:
                    return spec, result
                if verdict is False:
                    raise ValueError(NO_CHECK)
                # Do not reveal the checker's number: the planner should re-read, not copy.
                next_feedback[kind] = DISAGREE_FEEDBACK
        feedback = next_feedback
    if glimmer_future:  # Spark ran out of waves first: Glimmer's draft is the last chance
        return _take_glimmer(glimmer_future, checker, question, log)
    return None


# ---------------------------------------------------------------- PLANNER_MODEL=glimmer
def _start_glimmer(problem: dict) -> Future | None:
    """Start one draft on the fine-tuned Glimmer (agent/glimmer.py) in the background.

    Off unless PLANNER_MODEL=glimmer and Glimmer is available. It races the Spark waves: a hit
    (about 4 s) wins early, and a miss costs no time because Spark is already drafting.
    """
    import os

    if os.environ.get("PLANNER_MODEL", "").strip().lower() != "glimmer":
        return None
    from . import glimmer

    if not glimmer.available():
        return None
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_glimmer_draft, problem)
    pool.shutdown(wait=False)
    return future


def _glimmer_draft(problem: dict):
    from . import glimmer

    try:
        return glimmer.glimmer_spec(problem), ""
    except json.JSONDecodeError:
        return None, "invalid JSON"
    except Exception as exc:  # noqa: BLE001 — transport, budget: Spark carries on
        logger.warning("glimmer draft failed: %s: %s", type(exc).__name__, exc)
        return None, f"error {type(exc).__name__}: {str(exc)[:160]}"


def _take_glimmer(future: Future, checker: _SecondSolver, question: str,
                  log: list[str]) -> tuple[dict, VerifyResult] | None:
    """Glimmer's draft through the same gate as Spark's, accepted only on the quick checker's agreement.

    Never spends the one careful re-check: the Spark ladder may need it, and spending it on a
    Glimmer miss raised declines in the trial (docs/private/glimmer).
    """
    spec, why = future.result()
    if spec is None:
        log.append(f"attempt glimmer -> {why}")
        return None
    kind = "v2" if spec.get("version", 2) == 2 else "v1"
    try:
        result = _check_draft(kind, spec, question)
    except Exception as exc:  # noqa: BLE001 — a malformed spec is a miss
        log.append(f"attempt glimmer -> error {type(exc).__name__}")
        return None
    log.append(f"attempt glimmer {kind} -> {_status(result)}")
    if not result.ok:
        return None
    answer = spec["answer"] if kind == "v2" else spec["model"]["answer"]
    check = checker.answer()
    if check is not None and _check.agrees(answer, check):
        result.check, result.planner = "agreed", "glimmer"
        log.append(f"independent check ({_client.MODEL_CHECK}) agreed with glimmer: {check:g}")
        return spec, result
    log.append("glimmer answer not confirmed by the quick check")
    return None


def plan_problem(problem: dict, log: list[str] | None = None) -> tuple[dict, VerifyResult, list[str]]:
    """Return (spec, verify_result, attempt_log). Raises ValueError on graceful failure.

    The spec is v1 (has `model`) or v2 (`version == 2`); dispatch on spec.get("version").
    Pass `log` to keep the attempt log when planning fails (the evals read it).
    """
    log = [] if log is None else log
    problem = {**problem, "question": plain_question(problem.get("question", ""))}
    question = problem["question"]
    reason = unsupported_reason(question)
    if reason == "questions that seem to be missing a number":
        raise ValueError("This question seems to be missing a number (how many are there?), so I won't guess. "
                         "Please check the question and type it in full.")
    if reason:
        raise ValueError(f"I can't solve {reason} yet, so I won't show an answer. "
                         "Try a word problem with one question and no picture.")

    if not _client.has_key():
        spec = _fallback_spec(problem)
        if spec is None:
            raise _client.MissingKeyError(
                "No META_API_KEY set and the built-in demo planner cannot solve this problem type yet. "
                "Try the marbles-style 'times as many … together' problem, or set the key."
            )
        result = verify_spec(spec)
        log.append(f"fallback planner -> {result}")
        if not result.ok:
            raise ValueError(f"Fallback spec failed verification: {result}")
        return spec, result, log

    checker = _SecondSolver(question)
    glimmer_future = _start_glimmer(problem)  # PLANNER_MODEL=glimmer: races the Spark waves
    family = v2_family(question)
    if family is None:
        log.append("v1 and v2 race")
        waves = [V1_RACE] + V2_WAVES[1:]
    else:
        log.append(f"routed to v2: {family}")
        waves = V2_WAVES
    found = _run_waves(problem, family, waves, checker, log, glimmer_future)
    if found:
        return found[0], found[1], log

    attempts = [line for line in log if line.startswith("attempt")]
    if attempts and all(line.endswith("model unavailable") for line in attempts):
        raise _client.ModelUnavailableError("The maths model is not available right now. Please try again in a moment.")
    logger.warning("no verified spec after retries: %s", log)
    raise ValueError(
        "Sorry, I could not check my working on this problem, so I won't show an answer I'm not sure of. "
        "Try a clearer photo or type the problem."
    )
