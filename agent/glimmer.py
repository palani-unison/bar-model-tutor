"""Fine-tuned Muse Glimmer 30B as a fast first planner rung (Fireworks Serverless Training sampler).

Glimmer was LoRA-tuned only on BarSpec v2 plans that passed the full gate (verifier, layout
check, independent second solver) AND matched the answer key. Its output is never trusted on its
own: plan.py runs it through the same gate and falls back to the Spark ladder on any miss.

Serving: Fireworks does not serve LoRA models serverlessly through chat completions, so this
module samples the tuned checkpoint per token through the Serverless Training API (no
deployment). It needs `fireworks-ai[training]` and `transformers==5.10.4` (not in the app's
requirements; the module reports unavailable without them).

Config (environment / .env):
  FIREWORKS_API_KEY        the Fireworks key (never printed or logged)
  GLIMMER_MODEL_PATH       sampler checkpoint, e.g. <account>/<run-id>/<checkpoint> (evals/private/glimmer/run.json)
  GLIMMER_BASE=1           sample the untuned base model instead
  GLIMMER_BUDGET_USD       hard cap on the sampling spend logged in GLIMMER_LEDGER (default 20)
  GLIMMER_LEDGER           JSONL of token counts per call (default evals/private/glimmer/ledger.jsonl)

The request format is shared with the training data (evals/synth/label.py, train_glimmer.py).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import client as _client  # noqa: F401 — loads .env

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "glimmer.md"
BASE_MODEL = "accounts/fireworks/models/muse-glimmer-30b"
TOKENIZER = "meta-models/Muse-Glimmer-30B"
TOKENIZER_REV = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"
SERVERLESS_URL = "https://api.fireworks.ai/training/v1/serverless"
# Published Serverless Training list prices for Glimmer 30B, USD per 1M tokens.
PRICE_PREFILL, PRICE_SAMPLE = 1.96, 4.88
MAX_TOKENS = 3000  # trained answers are <= ~1.3k tokens; longer means it is rambling

_lock = threading.Lock()
_state: dict = {}


class BudgetExceeded(RuntimeError):
    pass


def system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def request_messages(question: str) -> list[dict]:
    """System + user messages for one (plain-text) question; identical at training and inference."""
    from .plan import v2_family

    req = {"question": question}
    family = v2_family(question)
    if family:
        req["looks_like"] = family
    return [{"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(req, ensure_ascii=False)}]


def ledger_path() -> Path:
    return Path(os.environ.get("GLIMMER_LEDGER") or ROOT / "evals" / "private" / "glimmer" / "ledger.jsonl")


def spent_usd() -> float:
    path = ledger_path()
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            total += json.loads(line).get("usd", 0.0)
    return total


def budget_usd() -> float:
    return float(os.environ.get("GLIMMER_BUDGET_USD", "20"))


def available() -> bool:
    if not os.environ.get("FIREWORKS_API_KEY", "").strip():
        return False
    if not (os.environ.get("GLIMMER_MODEL_PATH", "").strip() or os.environ.get("GLIMMER_BASE") == "1"):
        return False
    try:
        import fireworks.training.sdk  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def _sampler(base: bool | None = None):
    """(tokenizer, sampler) for the tuned checkpoint, or the base model when base is True."""
    base = os.environ.get("GLIMMER_BASE") == "1" if base is None else base
    key = "base" if base else os.environ.get("GLIMMER_MODEL_PATH", "").strip()
    with _lock:
        if "tok" not in _state:
            from fireworks.training.sdk import FiretitanServiceClient
            from transformers import AutoTokenizer

            _state["tok"] = AutoTokenizer.from_pretrained(TOKENIZER, revision=TOKENIZER_REV)
            _state["service"] = FiretitanServiceClient(api_key=os.environ["FIREWORKS_API_KEY"].strip(), base_url=SERVERLESS_URL)
        if key not in _state:
            service, tok = _state["service"], _state["tok"]
            if base:
                _state[key] = service.create_sampling_client(base_model=BASE_MODEL, tokenizer=tok)
            else:
                _state[key] = service.create_sampling_client(model_path=key, tokenizer=tok)
        return _state["tok"], _state[key]


def render_prompt(tok, messages: list[dict]) -> str:
    text = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, reasoning_strength="low")
    return text + " to=user<|message|>"


def _log(prompt_tokens: int, sampled_tokens: int, seconds: float, tag: str) -> float:
    usd = (prompt_tokens * PRICE_PREFILL + sampled_tokens * PRICE_SAMPLE) / 1e6
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "tag": tag, "prompt_tokens": prompt_tokens,
                             "sampled_tokens": sampled_tokens, "usd": round(usd, 6), "seconds": round(seconds, 2)}) + "\n")
    return usd


def sample_text(messages: list[dict], max_tokens: int = MAX_TOKENS, base: bool | None = None) -> tuple[str, dict]:
    """One greedy completion of the opened assistant turn, and its usage. Enforces the spend cap first."""
    if spent_usd() + (2000 * PRICE_PREFILL + max_tokens * PRICE_SAMPLE) / 1e6 > budget_usd():
        raise BudgetExceeded(f"Glimmer sampling budget ${budget_usd():.2f} would be exceeded")
    from fireworks.training import sdk

    tok, sampler = _sampler(base)
    ids = tok.encode(render_prompt(tok, messages), add_special_tokens=False)
    params = sdk.SamplingParams(max_tokens=max_tokens, temperature=0.0, stop=["<|eot|>"])
    started = time.perf_counter()
    out = sampler.sample(prompt=sdk.ModelInput.from_ints(ids), num_samples=1, sampling_params=params).result()
    seq = out.sequences[0]
    tokens = list(getattr(seq, "tokens", None) or getattr(seq, "_tokens_list", []))
    seconds = time.perf_counter() - started
    usd = _log(len(ids), len(tokens), seconds, os.environ.get("GLIMMER_TAG", "plan"))
    text = tok.decode(tokens, skip_special_tokens=False).split("<|eot|>")[0].strip()
    stop = getattr(seq, "stop_reason", None)
    return text, {"prompt_tokens": len(ids), "sampled_tokens": len(tokens), "usd": usd, "seconds": round(seconds, 2),
                  "stop_reason": str(getattr(stop, "value", stop))}


def parse_spec(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    spec = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
    if not isinstance(spec, dict):
        raise json.JSONDecodeError("not an object", text, 0)
    return spec


def glimmer_spec(problem: dict) -> dict:
    """One BarSpec v2 from the tuned model. Raises on transport, budget or JSON errors."""
    from .plan import plain_question

    text, _usage = sample_text(request_messages(plain_question(problem.get("question", ""))))
    return parse_spec(text)
