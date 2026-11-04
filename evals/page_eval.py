"""Photo test on real scanned exam pages (private data): can the app read question N off a full page?

  python evals/page_eval.py [--limit 80] [--workers 8]

Reads evals/private/pages/index.json (page image + question number + the expected question text),
asks the vision step for that one question, and checks that the numbers it read match the
expected text exactly. The pages and results stay in evals/private (gitignored, not ours to publish).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from agent.extract import extract_from_image  # noqa: E402
from agent.verify import _numbers  # noqa: E402
from harness import percentile  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    index = json.loads((ROOT / "evals" / "private" / "pages" / "index.json").read_text(encoding="utf-8"))[: args.limit]

    def one(item):
        started = time.perf_counter()
        try:
            read = extract_from_image((ROOT / item["page"]).read_bytes(), str(item["qno"]).rstrip("."))
        except Exception as exc:  # noqa: BLE001
            return {**item, "ok": False, "error": str(exc)[:200], "seconds": 0}
        got, want = sorted(_numbers(read.get("question", ""))), sorted(_numbers(item["question"]))
        return {**item, "read": read.get("question", ""), "ok": got == want, "missing": [n for n in want if n not in got],
                "extra": [n for n in got if n not in want], "seconds": round(time.perf_counter() - started, 1)}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, index))
    ok = sum(r["ok"] for r in rows)
    out = ROOT / "evals" / "private" / "results" / f"pages-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"n": len(rows), "numbers_exact": ok, "rows": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"[pages] {ok}/{len(rows)} questions read with every number exact | p50 "
          f"{percentile([r['seconds'] for r in rows], 50)} s -> {out.name}")
    for r in rows:
        if not r["ok"]:
            print(f"  {r['grade']} q{r['qno']}: missing {r.get('missing')} extra {r.get('extra')} {r.get('error', '')[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
