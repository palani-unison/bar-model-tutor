"""Answers the child's own questions during a live lesson ("Why 4 units?").

Spark phrases the reply and may point at the board (go to a step, highlight a bar).
Code keeps it honest: every number in the reply must already be on the board or in
the question, and it must not give away a number the child is still working out.
A reply that breaks either rule is regenerated once, then replaced by a safe one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import client as _client
from .lesson import Lesson, _fmt, _unit_parts, is_v2, numbers_in
from .spec_v2 import bars_of
from .verify import _close

logger = logging.getLogger("bar_model_tutor.tutor")

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "tutor.md"
REASONING_EFFORT = "minimal"
MAX_TOKENS = 1200


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _v2_bars(spec: dict) -> list[dict]:
    """Each v2 bar as the board draws it: unit counts per segment (never a unit's value) and plain amounts."""
    units = [u.get("name") for u in spec.get("units") or []]
    out = []
    for stage in spec.get("stages") or []:
        for bar in stage.get("bars") or []:
            segments, counted = [], 0.0
            for seg in bar.get("segments") or []:
                coeffs, amount = _unit_parts(seg.get("expr", ""), units)
                size = [f"{_fmt(k)} {'unit' if n == 'u' else n}{'' if k == 1 else 's'}" for n, k in coeffs.items()]
                style = seg.get("style", "normal")
                if style != "removed":
                    counted += sum(coeffs.values())
                segments.append({"size": " + ".join(size + ([_fmt(amount)] if amount else [])), "style": style,
                                 **({"label": seg["label"]} if seg.get("label") else {}),
                                 "units": sum(coeffs.values()), "amount": amount})
            out.append({"id": bar.get("id"), "stage": stage.get("label"), "label": bar.get("label"),
                        "units": counted, "segments": segments})
    return out


def allowed_numbers(lesson: Lesson) -> set[float]:
    """Numbers the tutor may say: from the question, the bars, and steps already on the board."""
    spec = lesson.spec
    nums = set(numbers_in(spec["question"]))
    if is_v2(spec):
        # v2 bars appear one by one, so only the ones drawn so far count: unit counts, plain amounts and labels.
        drawn = {s.get("bar") for s in lesson.steps[: lesson.position] if s.get("do") == "draw_bar"}
        for bar in _v2_bars(spec):
            if bar["id"] not in drawn:
                continue
            nums.add(float(bar["units"]))
            for seg in bar["segments"]:
                nums.update(float(x) for x in (seg["units"], seg["amount"]) if x)
                nums.update(numbers_in(seg.get("label", "")))
    else:
        nums.update(float(b["units"]) for b in spec["model"]["bars"])
    nums.update({1.0, 2.0})  # "1 unit", "both bars"
    for step in lesson.steps[: lesson.position]:
        nums.update(numbers_in(step.get("text", "") + " " + step.get("say", "")))
        nums.update(numbers_in(" ".join(_chart_texts(step))))  # a table's given cells, a number line's marks
    return nums


def _chart_texts(step: dict) -> list[str]:
    """What a table or number line step writes on the board besides its say."""
    if step.get("do") == "table":
        cells = list(step.get("columns") or []) + [c for row in step.get("rows") or [] if isinstance(row, list) for c in row]
        return [str(c) for c in cells]
    if step.get("do") == "number_line":
        return [str(m.get("label", "")) for m in step.get("marks") or [] if isinstance(m, dict)] + \
               [str(m.get("value", "")) for m in step.get("marks") or [] if isinstance(m, dict)] + \
               [str(g.get("label", "")) for g in step.get("jumps") or [] if isinstance(g, dict)]
    return []


def check_reply(say: str, lesson: Lesson) -> str | None:
    """Why a tutor reply is unsafe, or None if it is fine."""
    allowed = allowed_numbers(lesson)
    pending = [p for p in lesson.pending_answers() if not any(_close(p, a) for a in allowed)]
    for n in numbers_in(say):
        if any(_close(n, p) for p in pending):
            return f"it gives away {_fmt(n)}, which the child is still working out"
        if not any(_close(n, a) for a in allowed):
            return f"{_fmt(n)} is not on the board or in the question"
    return None


def _context(lesson: Lesson) -> dict:
    spec = lesson.spec
    if is_v2(spec):
        bars = [{"id": b["id"], "stage": b["stage"], "label": b["label"], "units": b["units"],
                 "segments": [{k: v for k, v in s.items() if k in ("size", "style", "label")} for s in b["segments"]]}
                for b in _v2_bars(spec)]
    else:
        bars = [{"id": b["id"], "label": b["label"], "units": b["units"], **({"extra": b["extra"]} if b.get("extra") else {})}
                for b in spec["model"]["bars"]]
    return {
        "question": spec["question"],
        "bars": bars,
        "steps_on_board": [{"n": i, "do": s["do"], "text": s.get("text") or " | ".join(_chart_texts(s)) or None,
                            "said": s.get("say")}
                           for i, s in enumerate(lesson.steps[: lesson.position])],
        "teacher_is_asking": lesson.waiting.ask if lesson.waiting else None,
    }


def answer_question(lesson: Lesson, child_said: str, trace: list | None = None) -> dict:
    """Reply to the child. Returns {"say": str, "go_to_step": int | None, "highlight": str | None}.

    `trace` (evals) collects each rejected draft and whether the safe fallback was used.
    """
    messages = [
        {"role": "system", "content": _prompt()},
        {"role": "user", "content": json.dumps({"lesson": _context(lesson), "child_said": child_said}, ensure_ascii=False)},
    ]
    bar_ids = {b["id"] for b in (bars_of(lesson.spec) if is_v2(lesson.spec) else lesson.spec["model"]["bars"])}
    for attempt in range(2):
        try:
            # "minimal" measured Nov 2: p50 2.9 s vs 5.4 s for "low", same replies, no rejections.
            # The cap bounds a runaway reply (minimal uses ~400 tokens incl. reasoning; too long -> retry/fallback).
            resp = _client.chat(messages, retries=0, reasoning_effort=REASONING_EFFORT, max_tokens=MAX_TOKENS,
                                response_format={"type": "json_object"})
            reply = json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:  # noqa: BLE001 — the lesson goes on without a model reply
            logger.warning("tutor reply failed: %s", exc)
            break
        say = str(reply.get("say", "")).strip()
        problem = check_reply(say, lesson) if say else "empty reply"
        if problem is None:
            step = reply.get("go_to_step")
            highlight = reply.get("highlight")
            return {
                "say": say,
                "go_to_step": step if isinstance(step, int) and 0 <= step < lesson.position else None,
                "highlight": highlight if highlight in bar_ids else None,
            }
        logger.info("tutor reply rejected (%s): %s", problem, say)
        if trace is not None:
            trace.append({"rejected": problem, "draft": say})
        messages.append({"role": "assistant", "content": json.dumps(reply)})
        messages.append({"role": "user", "content": f"Not allowed: {problem}. Reply again without that number."})
    if trace is not None:
        trace.append({"fallback": True})
    hint = f" {lesson.waiting.ask}" if lesson.waiting else ""
    return {"say": "Good question! Look at the bars on the board and think about what each unit stands for." + hint,
            "go_to_step": None, "highlight": None}
