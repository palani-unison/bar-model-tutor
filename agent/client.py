"""Single Meta Model API client (OpenAI SDK pointed at Meta).

All model calls go through here: retries, cost logging, model IDs in one place.
Never hard-code or print the API key; read META_API_KEY from the environment.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("bar_model_tutor.client")


def _load_dotenv() -> None:
    """Load project-root .env into the environment (existing env wins)."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

BASE_URL = "https://api.meta.ai/v1"
REQUEST_TIMEOUT_S = 60

# Model IDs — use exactly (see MUSE_CODE.md). Never use -contributor models.
MODEL_DEFAULT = "muse-spark-1.3"
MODEL_ESCALATION = "muse-wonder-1.0"
# Independent second solver: an older Spark checkpoint, so it does not share the planner's exact weights.
MODEL_CHECK = "muse-spark-1.2"
MODEL_SEGMENT = "sam-3.1"
MODEL_ASR = "muse-voice-transcribe-1.0"

# Rough relative cost weights for logging (Wonder ~4x Spark). Not billing truth.
_COST_WEIGHT = {MODEL_DEFAULT: 1.0, MODEL_ESCALATION: 4.0, MODEL_CHECK: 1.0, MODEL_SEGMENT: 1.0, MODEL_ASR: 1.0}


class MissingKeyError(RuntimeError):
    pass


class ModelUnavailableError(RuntimeError):
    """The model API could not be reached or refused the request."""


def get_client():
    """Return an OpenAI client pointed at Meta. Raises MissingKeyError if unset."""
    from openai import OpenAI

    key = os.environ.get("META_API_KEY", "").strip()
    if not key:
        raise MissingKeyError(
            "META_API_KEY is not set. Copy .env.example to .env and add your key, "
            "or export META_API_KEY in your shell. The app still runs in demo mode without it."
        )
    return OpenAI(base_url=BASE_URL, api_key=key, timeout=REQUEST_TIMEOUT_S, max_retries=0)


def has_key() -> bool:
    return bool(os.environ.get("META_API_KEY", "").strip())


def chat(messages: list[dict], model: str = MODEL_DEFAULT, retries: int = 2, **kwargs):
    """Chat completion with simple retry + cost logging. Returns the response."""
    from openai import APIStatusError

    client = get_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(model=model, messages=messages, **kwargs)
            usage = getattr(resp, "usage", None)
            tokens = getattr(usage, "total_tokens", None) if usage else None
            logger.info(
                "model=%s tokens=%s cost_weight=%.1f attempt=%d",
                model, tokens, _COST_WEIGHT.get(model, 1.0), attempt,
            )
            return resp
        except Exception as exc:  # network / rate limit / 5xx — retry with backoff
            last_exc = exc
            logger.warning(
                "model=%s attempt=%d failed: %s status=%s %s",
                model, attempt, type(exc).__name__, getattr(exc, "status_code", None), str(exc)[:300],
            )
            permanent = isinstance(exc, APIStatusError) and exc.status_code < 500 and exc.status_code != 429
            if permanent:
                break  # a bad key or bad request will not fix itself
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise ModelUnavailableError("The maths model is not available right now. Please try again in a moment.") from last_exc
