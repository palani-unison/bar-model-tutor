"""Eval harness. Usage:

  python evals/run_evals.py                       # text suite (default)
  python evals/run_evals.py --suite photo --workers 4
  python evals/run_evals.py --suite all           # text, lesson, tutor, photo, voice in order
  python evals/run_evals.py --data private --only '^h' --workers 8   # one private file, by id

Suites (see evals/suites.py): text, photo, lesson, tutor, voice. The lesson and tutor
suites reuse the verified specs saved by the newest text run. Each suite writes one
JSON file into evals/results/ (never overwritten). Datasets: evals/dataset/*.json:
  {"id": str, "question": str, "answer": number, "problem_type": str, "may_decline": bool}

The numbers that must stay at 0: WRONG answers shown (text, photo) and UNSAFE tutor
replies shown (tutor).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import write_result  # noqa: E402
from suites import SUITES  # noqa: E402

ORDER = ["text", "lesson", "tutor", "photo", "voice"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="text", choices=[*SUITES, "all"])
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--data", default="public", choices=["public", "private"],
                    help="private = school papers in evals/private/dataset (results stay in evals/private/results)")
    ap.add_argument("--only", default="",
                    help="regex over item ids, e.g. '^h' (school-by-heuristic) or '^\\d' (school-sample-60)")
    args = ap.parse_args()
    if args.data == "private":
        import harness

        harness.DATA_DIR = harness.ROOT / "evals" / "private" / "dataset"
    if args.only:
        import re

        import suites

        every, wanted = suites.load_items, re.compile(args.only)
        suites.load_items = lambda: [item for item in every() if wanted.search(str(item["id"]))]

    failed = False
    for name in ORDER if args.suite == "all" else [args.suite]:
        summary, lines = SUITES[name](args.limit, args.workers)
        out = write_result(name, summary)
        print(f"[{name}] {lines[0]} -> {out.name}")
        for line in lines[1:]:
            print(line)
        failed |= bool(summary.get("wrong") or summary.get("unsafe_replies_shown"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
