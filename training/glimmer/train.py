"""Step 4, prepare + train: LoRA SFT of Muse Glimmer 30B with Fireworks Serverless Training.

Follows the Fireworks workshop / cookbook starter (examples/sft/muse_glimmer_starter.py): data
stays local and token batches stream to the shared trainer pool. Unlike the starter, NOTHING is
sampled here, so the run only uses the free promotional training meter (Glimmer 30B LoRA SFT,
until 2026-11-30). No deployment is created. The tuned sampler checkpoint is saved and
promoted to a private LoRA model in the account.

Prompt format (agent/glimmer.render_prompt): the model's own HF chat template at reasoning
strength "low", assistant turn opened as " to=user<|message|>"; loss on the spec JSON + <|eot|>.

archive: uploads <run>/train.jsonl and val.jsonl as private Fireworks datasets
(bmt-planner-v2-<run>-train / -val) as an off-Mac copy of the training record. Not used by
training (Serverless Training never reads uploaded datasets).
"""
from __future__ import annotations

import json
import os
import random
import time

from .common import BASE_MODEL, SEED, TOKENIZER, TOKENIZER_REV, load_env, load_manifest, now, read_jsonl, run_dir, \
    sha256_file, update_manifest

SERVERLESS_URL = "https://api.fireworks.ai/training/v1/serverless"
API = "https://api.fireworks.ai/v1"
HYPER = {"lora_rank": 8, "lora_alpha": 32, "epochs": 2, "batch_size": 16, "learning_rate": 1e-4,
         "adam": {"beta1": 0.9, "beta2": 0.999, "eps": 1e-8, "weight_decay": 0.0, "grad_clip_norm": 1.0},
         "lora_targets": {"train_mlp": True, "train_attn": True, "train_unembed": True},
         "max_seq_len": 8192, "reasoning_strength": "low", "loss": "cross_entropy on the last assistant turn"}


def tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(TOKENIZER, revision=TOKENIZER_REV)


def datum_tokens(tok, ex: dict) -> tuple[list[int], list[float]]:
    """Token ids and loss weights (0 on the prompt, 1 on the answer), checked against the template."""
    from agent.glimmer import render_prompt

    msgs = ex["messages"]
    prompt = render_prompt(tok, msgs[:-1])
    full = prompt + msgs[-1]["content"] + "<|eot|>"
    if tok.apply_chat_template(msgs, tokenize=False, reasoning_strength="low") != full:
        raise ValueError("chat template does not match the prompt/answer split")
    p_ids = tok.encode(prompt, add_special_tokens=False)
    ids = tok.encode(full, add_special_tokens=False)
    if ids[:len(p_ids)] != p_ids:
        raise ValueError("prompt tokens are not a prefix of the full example")
    return ids, [0.0] * len(p_ids) + [1.0] * (len(ids) - len(p_ids))


def run_prepare(run_id: str, hyper: dict) -> dict:
    d = run_dir(run_id)
    tok = tokenizer()
    train = read_jsonl(d / "train.jsonl")
    lens, trained = [], 0
    for ex in train:
        ids, w = datum_tokens(tok, ex)
        lens.append(len(ids))
        trained += int(sum(w))
    report = {"train_rows": len(train), "val_rows": len(read_jsonl(d / "val.jsonl")),
              "tokens_min_mean_max": [min(lens), round(sum(lens) / len(lens)), max(lens)] if lens else None,
              "trained_tokens_per_epoch": trained, "over_max_seq_len": sum(n > hyper["max_seq_len"] for n in lens),
              "steps": hyper["epochs"] * -(-len(train) // hyper["batch_size"])}
    print(json.dumps(report, indent=1))
    return report


def run_train(run_id: str, hyper: dict, force: bool = False) -> dict:
    load_env()
    from fireworks.training import sdk
    from fireworks.training.sdk import AdamParams, FiretitanServiceClient, FireworksClient

    d = run_dir(run_id)
    prev = load_manifest(run_id).get("train", {})
    if prev.get("sampler_checkpoint") and not force:
        print(f"already trained: {prev['sampler_checkpoint']} (use --force to retrain)")
        return prev
    tok = tokenizer()
    datums, skipped = [], 0
    for ex in read_jsonl(d / "train.jsonl"):
        ids, w = datum_tokens(tok, ex)
        if len(ids) > hyper["max_seq_len"]:
            skipped += 1
            continue
        datums.append(sdk.Datum(model_input=sdk.ModelInput.from_ints(ids[:-1]),
                                loss_fn_inputs={"target_tokens": ids[1:], "weights": w[1:]}))
    rec = {"started": now(), "surface": "serverless training", "hyper": hyper, "seed": SEED, "train_rows": len(datums),
           "skipped_over_max_seq_len": skipped, "train_sha256": sha256_file(d / "train.jsonl"), "losses": []}
    print(f"{len(datums)} train rows rendered ({skipped} over max_seq_len)", flush=True)
    service = FiretitanServiceClient(api_key=os.environ["FIREWORKS_API_KEY"], base_url=SERVERLESS_URL)
    enable = getattr(service, "_enable_serverless_supervised_409_retry", None)
    if enable:
        enable()
    checkpoint = f"bmt-{run_id}"[:17]
    try:
        for attempt in range(40):  # one serverless run per base model per account: wait for the slot
            try:
                tc = service.create_lora_training_client(base_model=BASE_MODEL, rank=hyper["lora_rank"],
                                                         alpha=hyper["lora_alpha"], **hyper["lora_targets"])
                break
            except Exception as exc:  # noqa: BLE001 — RateLimitError lives in a private SDK module
                if "429" not in str(exc) and "RATE_LIMIT" not in str(exc):
                    raise
                print(f"training slot busy (429); retry {attempt + 1}/40 in 60 s", flush=True)
                time.sleep(60)
        else:
            raise RuntimeError("serverless training slot stayed busy for 40 minutes")
        rec["session"] = getattr(service, "training_session_name", None)
        rec["training_run"] = getattr(tc, "run_id", None)
        adam = AdamParams(learning_rate=hyper["learning_rate"], **hyper["adam"])
        rng, step = random.Random(SEED), 0
        for epoch in range(hyper["epochs"]):
            order = list(range(len(datums)))
            rng.shuffle(order)
            for start in range(0, len(order), hyper["batch_size"]):
                batch = [datums[i] for i in order[start:start + hyper["batch_size"]]]
                fb = tc.forward_backward(batch, "cross_entropy").result()
                tc.optim_step(adam).result()
                step += 1
                m = dict(getattr(fb, "metrics", None) or {})
                n = m.get("response_tokens") or m.get("num_loss_tokens") or 1.0
                row = {"step": step, "epoch": epoch + 1, "loss": round(m.get("loss:sum", 0.0) / max(float(n), 1.0), 4)}
                rec["losses"].append(row)
                print(json.dumps(row), flush=True)
                (d / "train-progress.json").write_text(json.dumps(rec, indent=1), encoding="utf-8")
        rec["steps"] = step
        rec["sampler_checkpoint"] = tc.save_weights_for_sampler(checkpoint).result().path
        print(f"sampler checkpoint: {rec['sampler_checkpoint']}", flush=True)
        fw = FireworksClient(api_key=os.environ["FIREWORKS_API_KEY"])
        rows = [r for r in fw.list_training_session_checkpoints(service.training_session_name)
                if r.get("promotable") and checkpoint in (r.get("checkpointName") or r.get("name") or "")]
        if rows:
            target = max(rows, key=lambda r: r.get("createTime", ""))
            model_id = f"bmt-planner-v2-{run_id}"
            fw.promote_session_checkpoint(name=target["name"], output_model_id=model_id, base_model=BASE_MODEL)
            rec["promoted_from"] = target["name"]
            rec["adapter_model"] = f"{target['name'].split('/trainingSessions')[0]}/models/{model_id}"
            print(f"promoted (private): {rec['adapter_model']}", flush=True)
        rec["cost_usd"] = 0.0  # promotional training meter; no sampling in this step
    finally:
        rec["finished"] = now()
        update_manifest(run_id, "train", rec)
        service.close()
    return rec


# ---------------------------------------------------------------- archive (private datasets)
def _req(method: str, path: str, **kw) -> dict:
    import httpx

    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {os.environ['FIREWORKS_API_KEY'].strip()}"},
                      timeout=300, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def run_archive(run_id: str) -> dict:
    load_env()
    d = run_dir(run_id)
    acct = (_req("GET", "/accounts").get("accounts") or [{}])[0]["name"].split("/", 1)[1]
    out = {}
    for part in ("train", "val"):
        path = d / f"{part}.jsonl"
        ds = f"bmt-planner-v2-{run_id}-{part}".lower()
        n = len(read_jsonl(path))
        try:
            existing = _req("GET", f"/accounts/{acct}/datasets/{ds}")
        except RuntimeError:
            existing = None
        if not existing:
            _req("POST", f"/accounts/{acct}/datasets", json={"datasetId": ds, "dataset": {"userUploaded": {}, "exampleCount": str(n)}})
            existing = {}
        error = None
        if existing.get("state") != "READY":
            for attempt in range(3):
                try:
                    with open(path, "rb") as fh:
                        _req("POST", f"/accounts/{acct}/datasets/{ds}:upload", files={"file": (path.name, fh, "application/jsonl")})
                    error = None
                    break
                except RuntimeError as exc:
                    error = str(exc)[:160]
                    time.sleep(10 * (attempt + 1))
            if error:  # e.g. 403 "dataset uploads are temporarily disabled": drop the empty shell
                _req("DELETE", f"/accounts/{acct}/datasets/{ds}")
                existing = {"state": "NOT UPLOADED"}
            for _ in range(24 if error is None else 0):
                existing = _req("GET", f"/accounts/{acct}/datasets/{ds}")
                if existing.get("state") == "READY":
                    break
                time.sleep(5)
        out[part] = {"dataset": f"accounts/{acct}/datasets/{ds}", "examples": n, "state": existing.get("state"),
                     "sha256": sha256_file(path), "upload_error": error}
        print(f"{out[part]['dataset']}: {n} examples, {out[part]['state']}")
    update_manifest(run_id, "archive", {"finished": now(), "datasets": out,
                                        "note": "private to the account; Fireworks lists no dataset storage fee"})
    return out
