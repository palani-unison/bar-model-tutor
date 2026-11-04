"""Independent second solve: the plain question -> one number, no bar model.

The planner's spec can be self-consistent and still answer the wrong question
(a misread "more than", the wrong person asked). A second model that never sees
the spec solves the question on its own; plan.py shows an answer only when the
two agree. The checker runs in parallel with planning, so it adds no wait.
"""
from __future__ import annotations

import json
import logging

from . import client as _client

logger = logging.getLogger("bar_model_tutor.check")

PROMPT = (
    "Solve this primary school maths word problem. Work it out step by step in your head, "
    'then reply with JSON only: {"answer": <number>}. '
    "The answer is the single number the final question asks for."
)


def independent_answer(question: str) -> float | None:
    """The checker's answer, or None if it could not be reached or replied badly."""
    try:
        resp = _client.chat(
            [{"role": "system", "content": PROMPT}, {"role": "user", "content": question}],
            model=_client.MODEL_CHECK, retries=1, reasoning_effort="low",
            response_format={"type": "json_object"},
        )
        return float(json.loads(resp.choices[0].message.content or "{}")["answer"])
    except Exception as exc:  # noqa: BLE001 — a missing check must never block the answer path
        logger.warning("independent check unavailable: %s", exc)
        return None


def agrees(spec_answer: float, check_answer: float) -> bool:
    return abs(float(spec_answer) - check_answer) <= 0.005
