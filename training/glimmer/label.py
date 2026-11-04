"""Step 2, label, and step 3, build: verified plans for the training split, then SFT chat JSONL.

label: runs agent.plan.plan_problem on every `train` row of <run>/pool.jsonl, exactly as the
text eval does (problem_from_text), appending one row per question to <run>/labels.jsonl.
Resumable: rows already labelled are skipped, so re-running after a crash continues. Test rows
are never read.

build: keeps a pair only if the plan passed the full gate (verifier + layout check), the
independent second solver agreed, the spec is BarSpec v2, AND its answer equals the bank's key
with the evals' exact tolerance (harness.close; a cm/m slip is a mismatch). Disputed rows are
dropped and counted. Validation rows are chosen by paper (stable hash), so validation never
shares a paper with training. Writes <run>/train.jsonl, <run>/val.jsonl, <run>/yield.json.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .common import ROOT, SEED, load_manifest, now, read_jsonl, run_dir, sha256_file, stable_fraction, update_manifest, write_jsonl

VAL_FRAC = 0.10


def planner_info() -> dict:
    """What labelled the data: model ids, the attempt ladder, and prompt/code hashes."""
    from agent import client, plan

    return {
        "planner": "agent.plan.plan_problem",
        "models": {"default": client.MODEL_DEFAULT, "escalation": client.MODEL_ESCALATION, "check": client.MODEL_CHECK},
        "waves": [[list(a) for a in wave] for wave in plan.V2_WAVES],
        "prompt_sha256": {p: sha256_file(ROOT / "agent" / "prompts" / p) for p in ("plan.md", "plan_v2.md")},
        "code_sha256": {p: sha256_file(ROOT / "agent" / p) for p in ("plan.py", "verify_v2.py", "verify.py", "check.py")},
    }


def label_one(row: dict) -> dict:
    sys.path.insert(0, str(ROOT / "evals"))
    from agent.extract import problem_from_text
    from agent.plan import plan_problem
    from harness import close

    started = time.perf_counter()
    out = {"id": row["id"], "paper": row["paper"], "level": row["level"], "family": row.get("family"), "key": row["answer"]}
    log: list[str] = []
    try:
        spec, result, log = plan_problem(problem_from_text(row["question"]), log=log)
        answer = float(spec["answer"] if spec.get("version") == 2 else spec["model"]["answer"])
        agreed = result.check == "agreed"
        out.update(answer=answer, check=result.check, version=spec.get("version", 1),
                   heuristic=spec.get("heuristic") or spec.get("problem_type"), spec=spec,
                   outcome="keep" if (agreed and close(answer, row["answer"])) else ("disputed" if agreed else "unchecked"))
    except Exception as exc:  # noqa: BLE001 — declined is an outcome
        out.update(outcome="declined", error=f"{type(exc).__name__}: {exc}"[:200])
    out["attempts"] = sum(line.startswith("attempt") for line in log)
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


def run_label(run_id: str, workers: int = 10, limit: int | None = None) -> None:
    d = run_dir(run_id)
    labels = d / "labels.jsonl"
    done = {r["id"] for r in read_jsonl(labels)}
    todo = sorted((r for r in read_jsonl(d / "pool.jsonl") if r["split"] == "train" and r["id"] not in done),
                  key=lambda r: r["id"])
    if limit:
        todo = todo[:limit]
    print(f"{len(done)} already labelled, {len(todo)} to go", flush=True)
    started, lock, tally = now(), threading.Lock(), Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool, open(labels, "a", encoding="utf-8") as fh:
        for i, res in enumerate(pool.map(label_one, todo), 1):
            with lock:
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                fh.flush()
                tally[res["outcome"]] += 1
                if i % 25 == 0 or i == len(todo):
                    print(f"[{i}/{len(todo)}] {dict(tally)}", flush=True)
    m = load_manifest(run_id)
    prev = m.get("label", {})
    update_manifest(run_id, "label", {
        "started": prev.get("started", started), "finished": now(), "workers": workers,
        "labelled": len(read_jsonl(labels)), **planner_info(),
    })


def is_val(paper: str) -> bool:
    return stable_fraction(paper, f"val:{SEED}") < VAL_FRAC


def run_build(run_id: str) -> dict:
    from agent.plan import plain_question
    from agent.glimmer import request_messages

    d = run_dir(run_id)
    pool = {r["id"]: r for r in read_jsonl(d / "pool.jsonl")}
    labels = {r["id"]: r for r in read_jsonl(d / "labels.jsonl")}  # a re-labelled row wins
    train, val, kept = [], [], {"train": [], "val": []}
    by = defaultdict(Counter)
    for rid in sorted(labels):
        lab, row = labels[rid], pool.get(rid)
        if not row or row["split"] != "train":  # never train on a test row
            continue
        outcome = lab["outcome"] if (lab["outcome"] != "keep" or lab.get("version") == 2) else "v1 spec"
        for key in (f"level:{row['level']}", f"family:{row['family']}"):
            by[key][outcome] += 1
        if outcome != "keep":
            continue
        assistant = json.dumps(lab["spec"], ensure_ascii=False, separators=(",", ":"))
        msg = {"messages": request_messages(plain_question(row["question"])) + [{"role": "assistant", "content": assistant}]}
        part = "val" if is_val(row["paper"]) else "train"
        (val if part == "val" else train).append(msg)
        kept[part].append(row)
    write_jsonl(d / "train.jsonl", train)
    write_jsonl(d / "val.jsonl", val)

    def tally(rows):
        return {"n": len(rows), "by_level": dict(sorted(Counter(r["level"] or "?" for r in rows).items())),
                "by_family": dict(sorted(Counter(r["family"] for r in rows).items()))}

    report = {
        "labelled": len(labels), "outcomes": dict(sorted(Counter(l["outcome"] for l in labels.values()).items())),
        "yield": {k: dict(v) for k, v in sorted(by.items())},
        "train": tally(kept["train"]), "val": tally(kept["val"]), "val_frac": VAL_FRAC,
        "system_prompt_sha256": sha256_file(ROOT / "agent" / "prompts" / "glimmer.md"),
        "files": {n: sha256_file(d / n) for n in ("train.jsonl", "val.jsonl", "labels.jsonl", "pool.jsonl")},
    }
    (d / "yield.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    update_manifest(run_id, "build", {"finished": now(), **report})
    print(json.dumps({k: report[k] for k in ("labelled", "outcomes", "train", "val")}, indent=1))
    return report
