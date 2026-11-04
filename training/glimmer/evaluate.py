"""Step 5, evaluate: base vs tuned Glimmer on the held-out test split, and the planner trial.

Every sampling call is billable (Serverless Training sampler, per token) and is logged in the
ledger (evals/private/glimmer/ledger.jsonl, token counts only). Before each batch the step
prints an estimate, and refuses to start if spent + estimate would pass GLIMMER_BUDGET_USD.

compare (deterministic graders, no model judges):
  json      the reply parses as one JSON object
  gate      verify_v2 (+ invented-number check) and the whiteboard layout check pass
  correct   the gate passes AND the answer equals the bank key (harness.close)
  wrong_shown  the gate passes but the answer is wrong: what the second solver must still catch
Rows: <run>/eval/<mode>.jsonl (resumable by id). Summary: manifest["eval"][<mode>].

trial: evals/run_evals.py --suite text with PLANNER_MODEL=glimmer (Glimmer first rung, Spark
ladder as fallback) on the public set and on the private 180 (both held out from training).
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .common import DOCS, ROOT, load_env, load_manifest, now, read_jsonl, run_dir, update_manifest

PRICE_PREFILL, PRICE_SAMPLE = 1.96, 4.88  # USD per 1M tokens, Glimmer 30B serverless training sampler


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def grade(spec_text: str, question: str, key: float) -> dict:
    """Deterministic grade of one reply."""
    sys.path.insert(0, str(ROOT / "evals"))
    from agent.glimmer import parse_spec
    from agent.plan import _layout_error, verify_v2
    from harness import close

    out = {"json": False, "gate": False, "correct": False, "wrong_shown": False}
    try:
        spec = parse_spec(spec_text)
    except Exception:  # noqa: BLE001
        return out
    out["json"] = True
    out["version"] = spec.get("version")
    try:
        spec["question"] = question
        result = verify_v2(spec)
        drawn = _layout_error(spec) if result.ok else None
        ok = result.ok and not drawn
        if drawn:
            result.errors = [f"layout: {drawn}"]
        out["gate_errors"] = [str(e)[:200] for e in (result.errors or [])][:5]
    except Exception as exc:  # noqa: BLE001 — a verifier crash is a failed gate
        ok = False
        out["gate_errors"] = [f"verifier crash: {type(exc).__name__}"]
    out["gate"] = bool(ok)
    if ok:
        try:
            answer = float(spec["answer"])
        except (TypeError, ValueError, KeyError):
            return out
        out["answer"] = answer
        out["correct"] = close(answer, key)
        out["wrong_shown"] = not out["correct"]
    return out


def test_rows(run_id: str, subset: str) -> list[dict]:
    rows = [r for r in read_jsonl(run_dir(run_id) / "pool.jsonl") if r["split"] == "test"]
    if subset == "eval":
        rows = [r for r in rows if r.get("eval_id")]
    elif subset == "papers":
        rows = [r for r in rows if not r.get("eval_id")]
    return sorted(rows, key=lambda r: r["id"])


def estimate(n: int, mode: str, run_id: str) -> float:
    """Upper-ish cost estimate: ~1k prompt tokens; tuned writes about the training answers' length,
    base is assumed to run to 80% of the token cap."""
    from agent.glimmer import MAX_TOKENS

    prep = load_manifest(run_id).get("prepare", {})
    mean_total = (prep.get("tokens_min_mean_max") or [0, 2000, 0])[1]
    out_tokens = max(1000, mean_total - 900) * 1.3 if mode == "tuned" else MAX_TOKENS * 0.8
    return n * (1000 * PRICE_PREFILL + out_tokens * PRICE_SAMPLE) / 1e6


def budget_check(est: float) -> None:
    from agent.glimmer import budget_usd, spent_usd

    spent = spent_usd()
    print(f"spent so far ${spent:.2f}; this batch est. ${est:.2f}; cap ${budget_usd():.2f}", flush=True)
    if spent + est > budget_usd():
        raise SystemExit("refusing: this batch would pass the sampling budget (raise GLIMMER_BUDGET_USD only with approval)")


def run_compare(run_id: str, mode: str, subset: str = "all", limit: int | None = None, workers: int = 4) -> dict:
    load_env()
    from agent.glimmer import request_messages, sample_text
    from agent.plan import plain_question

    m = load_manifest(run_id)
    if mode == "tuned":
        ckpt = (m.get("train") or {}).get("sampler_checkpoint")
        if not ckpt:
            raise SystemExit("no tuned checkpoint in the manifest: run the train step first")
        os.environ["GLIMMER_MODEL_PATH"] = ckpt
    out_path = run_dir(run_id) / "eval" / f"{mode}-{subset}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = {r["id"] for r in read_jsonl(out_path)}
    rows = test_rows(run_id, subset)[: limit or None]
    todo = [r for r in rows if r["id"] not in done]
    budget_check(estimate(len(todo), mode, run_id))
    os.environ["GLIMMER_TAG"] = f"{run_id}:{mode}-{subset}"

    def one(row):
        q = plain_question(row["question"])
        try:
            text, usage = sample_text(request_messages(q), base=(mode == "base"))
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "BudgetExceeded":
                raise
            return {"id": row["id"], "level": row["level"], "family": row["family"], "error": type(exc).__name__}
        return {"id": row["id"], "level": row["level"], "family": row["family"], **usage,
                **grade(text, q, row["answer"]), "reply": text}  # private rows only (evals/private)

    with ThreadPoolExecutor(max_workers=workers) as pool, open(out_path, "a", encoding="utf-8") as fh:
        for i, res in enumerate(pool.map(one, todo), 1):
            fh.write(json.dumps(res) + "\n")
            fh.flush()
            if i % 10 == 0 or i == len(todo):
                print(f"[{mode} {i}/{len(todo)}]", flush=True)
    summary = summarise([r for r in read_jsonl(out_path) if r["id"] in {x["id"] for x in rows}])
    ev = load_manifest(run_id).get("eval", {})  # re-read: other steps may have written meanwhile
    ev[f"{mode}-{subset}"] = {"finished": now(), **summary}
    update_manifest(run_id, "eval", ev)
    print(json.dumps(summary, indent=1))
    return summary


def summarise(rows: list[dict]) -> dict:
    n = len(rows)
    secs = [r["seconds"] for r in rows if "seconds" in r]
    k = {f: sum(bool(r.get(f)) for r in rows) for f in ("json", "gate", "correct", "wrong_shown")}
    by_level = {}
    for lv in sorted({r.get("level") or "?" for r in rows}):
        rs = [r for r in rows if (r.get("level") or "?") == lv]
        by_level[lv] = {"n": len(rs), "correct": sum(bool(r.get("correct")) for r in rs)}
    return {"n": n, **k, "errors": sum(1 for r in rows if "error" in r),
            "correct_rate": round(k["correct"] / n, 3) if n else None,
            "p50_seconds": pct(secs, 50), "p90_seconds": pct(secs, 90),
            "usd": round(sum(r.get("usd", 0) for r in rows), 4),
            "mean_sampled_tokens": round(statistics.mean([r["sampled_tokens"] for r in rows if "sampled_tokens" in r]), 1)
            if any("sampled_tokens" in r for r in rows) else None,
            "by_level": by_level}


# ---------------------------------------------------------------- planner trial
def run_trial(run_id: str, data: str, glimmer: bool, workers: int = 4) -> dict:
    load_env()
    m = load_manifest(run_id)
    env = dict(os.environ)
    if glimmer:
        ckpt = (m.get("train") or {}).get("sampler_checkpoint")
        if not ckpt:
            raise SystemExit("no tuned checkpoint in the manifest")
        n = 107 if data == "public" else 180
        budget_check(estimate(n, "tuned", run_id))
        env.update(PLANNER_MODEL="glimmer", GLIMMER_MODEL_PATH=ckpt, GLIMMER_TAG=f"{run_id}:trial-{data}")
    else:
        env.pop("PLANNER_MODEL", None)
    started = time.time()
    cmd = [sys.executable, "evals/run_evals.py", "--suite", "text", "--workers", str(workers), "--limit", "1000"]
    if data == "private":
        cmd += ["--data", "private"]
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    first = (proc.stdout.strip().splitlines() or [""])[0]
    print(first)
    out_dir = ROOT / "evals" / ("private/results" if data == "private" else "results")
    newest = max((p for p in out_dir.glob("*text-*.json") if p.stat().st_mtime >= started), key=lambda p: p.stat().st_mtime,
                 default=None)
    summary = {"finished": now(), "planner": "glimmer first rung + Spark ladder" if glimmer else "Spark ladder",
               "data": data, "exit_code": proc.returncode, "result_file": newest.name if newest else None}
    if newest:
        res = json.loads(newest.read_text(encoding="utf-8"))
        rows = res.get("rows", [])
        summary.update({k: res.get(k) for k in ("n", "correct", "wrong", "declined", "p50_seconds", "p90_seconds",
                                                 "mean_attempts", "checker_agreed")})
        summary["glimmer_first_rung_answered"] = sum(
            1 for r in rows if r.get("outcome") != "declined" and any("glimmer" in l and "OK" in l for l in r.get("log", []))
            and r.get("attempts") == 1)
    trials = load_manifest(run_id).get("trial", {})
    trials[f"{'glimmer' if glimmer else 'spark'}-{data}"] = summary
    update_manifest(run_id, "trial", trials)
    print(json.dumps(summary, indent=1))
    return summary


# ---------------------------------------------------------------- chart + ledger
def write_chart(run_id: str) -> str | None:
    """A small base-vs-tuned bar chart (SVG, no plotting dependency) under docs/private/glimmer/."""
    ev = load_manifest(run_id).get("eval", {})
    pairs = [(k.split("-", 1)[1], ev.get(f"base-{k.split('-', 1)[1]}"), v) for k, v in ev.items() if k.startswith("tuned-")]
    pairs = [(s, b, t) for s, b, t in pairs if b and t]
    if not pairs:
        return None
    subset, base, tuned = pairs[0]
    metrics = [("valid JSON", "json"), ("passes gate", "gate"), ("correct", "correct")]
    w, h, left, top, bar = 560, 260, 110, 30, 16
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="system-ui,sans-serif" font-size="12">',
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             f'<text x="{left}" y="18" font-weight="600">Muse Glimmer 30B, base vs tuned (held-out {subset}, n={tuned["n"]})</text>']
    for i, (label, key) in enumerate(metrics):
        y = top + 20 + i * 60
        parts.append(f'<text x="{left - 8}" y="{y + bar + 4}" text-anchor="end">{label}</text>')
        for j, (name, res, color) in enumerate((("base", base, "#B8B2B3"), ("tuned", tuned, "#E8663D"))):
            rate = res[key] / res["n"] if res["n"] else 0
            bw = rate * (w - left - 80)
            yy = y + j * (bar + 4)
            parts.append(f'<rect x="{left}" y="{yy}" width="{bw:.1f}" height="{bar}" fill="{color}"/>')
            parts.append(f'<text x="{left + bw + 6:.1f}" y="{yy + 12}">{name} {rate:.0%}</text>')
    parts.append("</svg>")
    DOCS.mkdir(parents=True, exist_ok=True)
    path = DOCS / f"{run_id}-base-vs-tuned.svg"
    path.write_text("\n".join(parts), encoding="utf-8")
    return str(path.relative_to(ROOT))


def ledger_summary() -> dict:
    from agent.glimmer import ledger_path

    rows = read_jsonl(ledger_path())
    by: dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r.get("tag", "?"), {"calls": 0, "prompt_tokens": 0, "sampled_tokens": 0, "usd": 0.0})
        b["calls"] += 1
        b["prompt_tokens"] += r.get("prompt_tokens", 0)
        b["sampled_tokens"] += r.get("sampled_tokens", 0)
        b["usd"] = round(b["usd"] + r.get("usd", 0.0), 4)
    return {"total_usd": round(sum(r.get("usd", 0.0) for r in rows), 4), "calls": len(rows), "by_batch": by}
