"""Check my bar model: the child photographs their own bar model and working.

Spark vision only *reads* the drawing into structured JSON (bars, units, the numbers
written by the braces, where the "?" is, each working line, the final answer).
Code then compares it, step by step, with the verified spec and finds the first
mistake: the bars, the units, the given numbers, the "?", the working, the answer.
The child gets one targeted hint for that step. It never gives the answer away.
"""
from __future__ import annotations

import base64
import json
import logging
import re

from . import client as _client
from .extract import _image_mime
from .lesson import _fmt
from .verify import _check_working, _close, unit_value_of

logger = logging.getLogger("bar_model_tutor.review")

READ_PROMPT = """You are reading a primary school child's own handwritten bar model (Singapore maths) from a photo.
Describe ONLY what is actually drawn and written. Do not fix mistakes, do not solve the problem, do not guess
numbers that are not there. The problem they were solving is given for context only.

Return JSON:
{"legible": true/false,
 "bars": [{"label": "<name written next to the bar>", "units": <number of equal boxes drawn in that bar>,
           "extra": <number written in an extra non-unit part of the bar, or null>}],
 "brace_numbers": [<numbers written next to braces/brackets, e.g. a total or a difference>],
 "question_mark_on": "<label of the bar that has the ? next to it, or null>",
 "working": ["<each line of working exactly as written, e.g. 4 units = 120>"],
 "final_answer": <the number in the child's final answer or answer statement, or null>}"""

# Strict: every object closed and every field required (nullable instead of optional), so the
# reply can't come back with mangled keys.
READ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "legible": {"type": "boolean"},
        "bars": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {
            "label": {"type": "string"}, "units": {"type": "integer"}, "extra": {"type": ["number", "null"]}},
            "required": ["label", "units", "extra"]}},
        "brace_numbers": {"type": "array", "items": {"type": "number"}},
        "question_mark_on": {"type": ["string", "null"]},
        "working": {"type": "array", "items": {"type": "string"}},
        "final_answer": {"type": ["number", "null"]},
    },
    "required": ["legible", "bars", "brace_numbers", "question_mark_on", "working", "final_answer"],
}


def _well_formed(drawing: dict) -> bool:
    bars = drawing.get("bars")
    return isinstance(bars, list) and all(
        isinstance(b, dict) and isinstance(b.get("label"), str) and isinstance(b.get("units"), int) for b in bars)

STEPS = ["bars", "units", "numbers", "question", "working", "answer"]
STEP_NAMES = {"bars": "Bars for each person", "units": "Equal units", "numbers": "Given numbers",
              "question": "Question mark", "working": "Working", "answer": "Answer"}


def read_drawing(image: bytes, spec: dict) -> dict:
    """Vision: the child's drawing as JSON, re-read once if malformed. Raises ModelUnavailableError / ValueError."""
    b64 = base64.b64encode(image).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": READ_PROMPT + "\n\nThe problem: " + spec["question"]},
        {"type": "image_url", "image_url": {"url": f"data:{_image_mime(image)};base64,{b64}"}}]}]
    drawing: dict = {}
    for _ in range(2):
        resp = _client.chat(messages, reasoning_effort="low", response_format={
            "type": "json_schema", "json_schema": {"name": "drawing", "schema": READ_SCHEMA, "strict": True}})
        try:
            drawing = json.loads(resp.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            continue
        if _well_formed(drawing):
            return drawing
        logger.info("malformed drawing read, retrying: %s", str(drawing)[:200])
    raise ValueError("I couldn't read your drawing clearly. Please try another photo.")


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (label or "").lower())


def _match_bars(spec_bars: list[dict], drawn: list[dict]) -> dict[str, dict]:
    """Spec bar id -> the drawn bar with the same label (exact, then prefix/initial)."""
    out, free = {}, list(drawn)
    for rule in (lambda a, b: a == b, lambda a, b: a.startswith(b) or b.startswith(a), lambda a, b: a[:1] == b[:1]):
        for sb in spec_bars:
            if sb["id"] in out:
                continue
            want = _norm(sb["label"])
            hit = next((d for d in free if _norm(d.get("label")) and rule(want, _norm(d.get("label")))), None)
            if hit is not None:
                out[sb["id"]] = hit
                free.remove(hit)
    return out


def diagnose(spec: dict, drawing: dict) -> dict:
    """Compare the child's drawing with the verified spec. Returns checks, the first mistake and its hint."""
    if spec.get("version") == 2:
        return {"legible": True, "checks": [], "first_mistake": None, "all_correct": False,
                "supported": False, "say": NOT_SUPPORTED_V2}
    model = spec["model"]
    bars = model["bars"]
    unit, _ = unit_value_of(model)
    checks: dict[str, dict] = {}

    def mark(step: str, ok: bool | None, say: str = "") -> None:
        checks.setdefault(step, {"step": step, "name": STEP_NAMES[step], "ok": ok, "say": say})

    if not drawing.get("legible", True) or not drawing.get("bars"):
        return {"legible": False, "checks": [], "first_mistake": None, "all_correct": False,
                "say": "I can't see your bar model clearly. Try a closer photo with good light, with the bars and working in view."}

    drawn = _match_bars(bars, drawing["bars"])

    # 1) A bar for everyone in the question.
    missing = [b for b in bars if b["id"] not in drawn]
    if missing:
        mark("bars", False, f"I can't find a bar for {missing[0]['label']}. Who else is in the question? Draw a bar for each one, and write the name next to it.")
    else:
        mark("bars", True, "You drew a bar for everyone in the question.")

    # 2) Units: the right number of equal units (any whole multiple of the spec is still a correct model).
    pairs = [(b, drawn[b["id"]]) for b in bars if b["id"] in drawn]
    scale = None
    for b, d in pairs:
        if b["units"] and d.get("units"):
            scale = d["units"] / b["units"]
            break
    wrong = [(b, d) for b, d in pairs if not scale or not _close(d.get("units", 0), b["units"] * scale)]
    if not pairs:
        mark("units", None)
    elif wrong or (scale and scale != int(scale)):
        b, d = wrong[0] if wrong else pairs[0]
        biggest = max(bars, key=lambda x: x["units"])
        smallest = min(bars, key=lambda x: x["units"])
        mark("units", False,
             f"Look at {b['label']}'s bar. I count {d.get('units')} unit{'s' if d.get('units') != 1 else ''}. "
             f"Read the question again: how does {biggest['label']} compare with {smallest['label']}? "
             f"How many units should {b['label']}'s bar have?")
    else:
        extras = [(b, d) for b, d in pairs if b.get("extra") and not (d.get("extra") and _close(d["extra"], b["extra"]))]
        if extras:
            b, _d = extras[0]
            mark("units", False, f"{b['label']} has some more than the others. Draw that extra part at the end of "
                                 f"{b['label']}'s bar and write how much it is.")
        else:
            mark("units", True, "Your units are equal and the right number.")

    # 3) The given total or difference is on the model.
    given = model.get("total") if model.get("total") else (model.get("difference") or {}).get("value")
    if given is not None:
        if any(_close(n, given) for n in drawing.get("brace_numbers", [])):
            mark("numbers", True, f"You showed {_fmt(float(given))} on your model.")
        else:
            what = "everyone has altogether" if model.get("total") else "the difference is"
            mark("numbers", False, f"Where do we show what {what}? Draw a bracket and write the number from the question.")

    # 4) The question mark is on the bar the question asks about.
    asked = next(b for b in bars if b["id"] == model["asked"])
    qbar = drawing.get("question_mark_on")
    # Match the "?" label against every bar, so "Pen" is never taken for "Pencil".
    if qbar and _norm(qbar) and asked["id"] in _match_bars(bars, [{"label": qbar}]):
        mark("question", True, "Your question mark is on the right bar.")
    else:
        mark("question", False, "Read the last sentence of the question. Whose amount are we finding? Put a question mark on that bar.")

    # 5) Every working line is correct arithmetic.
    bad = next(((line, problem) for line in drawing.get("working", []) if unit is not None
                for problem in [_check_working(line, unit)] if problem), None)
    if bad:
        mark("working", False, f"Check this line: \"{bad[0]}\". Try that step again carefully.")
    elif drawing.get("working"):
        mark("working", True, "Your working is correct.")
    else:
        mark("working", None, "Write your working next to the model, step by step.")

    # 6) The final answer.
    ans = drawing.get("final_answer")
    if ans is None:
        mark("answer", None, "Finish with an answer sentence.")
    elif _close(float(ans), float(model["answer"])):
        mark("answer", True, "Your answer is correct!")
    else:
        mark("answer", False, f"Your answer, {_fmt(float(ans))}, isn't quite right. Look at {asked['label']}'s bar: "
                              f"how many units is it, and what is 1 unit worth?")

    ordered = [checks[s] for s in STEPS if s in checks]
    first = next((c for c in ordered if c["ok"] is False), None)
    all_correct = first is None and checks.get("answer", {}).get("ok") is True
    if all_correct:
        say = "Excellent! Your bar model, working and answer are all correct."
    elif first is not None:
        good = [c["name"].lower() for c in ordered if c["ok"] and STEPS.index(c["step"]) < STEPS.index(first["step"])]
        say = (f"Good, your {', '.join(good)} {'is' if len(good) == 1 else 'are'} right. " if good else "") + first["say"]
    else:
        say = next((c["say"] for c in ordered if c["ok"] is None), "Keep going!")
    return {"legible": True, "checks": ordered, "first_mistake": first["step"] if first else None,
            "all_correct": all_correct, "say": say}


NOT_SUPPORTED_V2 = ("Checking your own bar model is not supported for this problem type yet. "
                    "Watch the lesson, then try the next problem!")


def check_drawing(image: bytes, spec: dict) -> dict:
    if spec.get("version") == 2:  # v2 specs have stages and segments, not spec["model"]
        return {"legible": True, "checks": [], "first_mistake": None, "all_correct": False,
                "supported": False, "say": NOT_SUPPORTED_V2}
    drawing = read_drawing(image, spec)
    result = diagnose(spec, drawing)
    result["read"] = drawing
    return result
