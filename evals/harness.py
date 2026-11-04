"""Shared pieces for the eval suites: dataset loading, percentiles, result files."""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "evals" / "results"


DATA_DIR = ROOT / "evals" / "dataset"  # --data private switches to evals/private/dataset (never committed)


def load_items() -> list[dict]:
    items: list[dict] = []
    for f in sorted(DATA_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        for i, item in enumerate(data if isinstance(data, list) else [data]):
            items.append({"id": item.get("id", f"{f.stem}-{i}"), **item})
    return items


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))]


def mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 2) if values else None


def close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 1e-6


def write_result(suite: str, summary: dict) -> Path:
    if DATA_DIR != ROOT / "evals" / "dataset":
        suite = f"private-{suite}"
        out_dir = ROOT / "evals" / "private" / "results"  # rows quote copyrighted questions: keep them out of git
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{suite}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        out.write_text(json.dumps({"suite": suite, "ran_at": datetime.now(timezone.utc).isoformat(), **summary},
                                  indent=2, ensure_ascii=False), encoding="utf-8")
        return out
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{suite}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    summary = {"suite": suite, "ran_at": datetime.now(timezone.utc).isoformat(), **summary}
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def latest_specs() -> list[dict]:
    """Verified specs from the newest text-suite run that saved them (lesson/tutor suites reuse them)."""
    runs = sorted(RESULTS.glob("text-*.json"), reverse=True)
    for run in runs:
        rows = json.loads(run.read_text(encoding="utf-8")).get("rows", [])
        specs = [{"id": r["id"], "spec": r["spec"]} for r in rows if r.get("spec") and r["outcome"] == "correct"]
        if specs:
            return specs
    raise SystemExit("No saved specs yet: run `python evals/run_evals.py --suite text` first.")
