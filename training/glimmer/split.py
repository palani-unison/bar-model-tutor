"""Step 1, split: the training pool and the held-out test set from the private school bank.

Reads evals/private/responses/*.json (sorted) and writes <run>/pool.jsonl and <run>/split.json.

Pool: maths papers' `open_ended` and `fill_in_blank` questions that are text-only word problems
with one numeric key: no figure, image reference (`![`), table or several parts.

Held out (never trained on):
  - every question in evals/private/dataset/school-sample-60.json and school-by-heuristic.json
    (the 180 the private eval uses), matched by normalised question text;
  - every question of about 15% of maths papers, picked by a stable hash of the paper id, so a
    paper never spans train and test;
  - any repeat (same normalised text) of a held-out question on another paper.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

from .common import PRIVATE, SEED, now, run_dir, sha256_file, stable_fraction, write_jsonl

EVAL_FILES = ["school-sample-60.json", "school-by-heuristic.json"]
TEST_FRAC = 0.15

_NOT_WORD = re.compile(
    r"^\s*(?:find the value of|express|round off|estimate|simplify|write|which of|arrange|convert|"
    r"what is the (?:value|missing|digit)|fill in|complete the|list|name|measure|draw|calculate)\b"
    r"|\bnearest (?:whole number|tenth|hundredth|thousand)\b|\bin (?:its )?simplest form\b|\bas a decimal\b|"
    r"\bas a fraction\b|\bprime\b|\bfactor|\bmultiple of\b|\bangle\b|\bprotractor\b|\bgrid\b|\bnet\b",
    re.I)
_MULTI_KEY = re.compile(r"\(\s*[a-d]\s*\)|\band\b|;|\n|:")
_UNITS = (r"(?:cm²|cm³|m²|m³|cm2|cm3|m2|m3|cm|mm|km|kg|g|ml|l|ℓ|litres?|min(?:utes?)?|h(?:ours?)?|"
          r"s(?:econds?)?|years?|days?|weeks?|°|%|[a-z]+)?")
_NUM_KEY = re.compile(rf"^\s*(-?\d[\d,]*(?:\.\d+)?)(?:\s+(\d+)\s*/\s*(\d+))?\s*(?:/\s*(\d+))?\s*{_UNITS}\.?\s*$", re.I)


def _plain(text: str) -> str:
    from agent.plan import plain_question

    return plain_question(text or "")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _plain(text).lower())


def parse_key(raw) -> float | None:
    """One number from an answer key ('$1700', '84 cm', '48.55%', '3/4', '2 1/2'), else None."""
    s = str(raw or "").strip()
    if not s or _MULTI_KEY.search(s) or "\\pi" in s or "π" in s:
        return None
    s = _plain(s).replace("\\", "").replace("$", "").strip()
    m = _NUM_KEY.match(s)
    if not m:
        return None
    whole, mn, md, den = m.groups()
    try:
        value = float(whole.replace(",", ""))
        if mn and md:
            value += int(mn) / int(md)
        elif den:
            value /= int(den)
    except (ValueError, ZeroDivisionError):
        return None
    return value


def level_of(grade: str) -> str | None:
    m = re.search(r"(\d)", grade or "")
    return f"P{m.group(1)}" if m and m.group(1) in "3456" else None


def skip_reason(text: str) -> str | None:
    """None if the question is a usable text-only word problem, else why it is skipped."""
    from agent.plan import _NEEDS_FIGURE, unsupported_reason

    q = _plain(text)
    if "![" in text or "<img" in text.lower() or re.search(r"\bimg-\d+", text):
        return "image"
    if _NEEDS_FIGURE.search(q) or unsupported_reason(q):
        return "figure/table/multi-part"
    if re.search(r"\(\s*[ab]\s*\)", text):
        return "multi-part"
    if "|" in text:
        return "table"
    if len(q.split()) < 10:
        return "too short"
    if _NOT_WORD.search(q):
        return "not a word problem"
    return None


def family_of(question: str) -> str:
    """The deterministic routing family (plan.v2_family), used to count rows per heuristic."""
    from agent.plan import v2_family

    return v2_family(_plain(question)) or "v1-first"


def held_out_paper(paper_id: str, frac: float = TEST_FRAC) -> bool:
    return stable_fraction(paper_id, SEED) < frac


def counts(rows: list[dict]) -> dict:
    return {"n": len(rows), "by_level": dict(sorted(Counter(r["level"] or "?" for r in rows).items())),
            "by_family": dict(sorted(Counter(r["family"] for r in rows).items()))}


def build_pool(private=PRIVATE, frac: float = TEST_FRAC) -> tuple[list[dict], dict]:
    evals = []
    for name in EVAL_FILES:
        path = private / "dataset" / name
        if path.exists():
            evals += json.loads(path.read_text(encoding="utf-8"))
    eval_norm = {norm(e["question"]): e["id"] for e in evals}

    pool, skipped, papers, test_papers = [], Counter(), [], []
    for f in sorted((private / "responses").glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        paper = data.get("paper") or {}
        if "math" not in str(paper.get("subject", "")).lower():
            continue
        pid = f.stem
        papers.append(pid)
        test_paper = held_out_paper(pid, frac)
        if test_paper:
            test_papers.append(pid)
        level = level_of(paper.get("grade", ""))
        for q in data.get("questions") or []:
            if q.get("type") not in ("open_ended", "fill_in_blank"):
                skipped["type"] += 1
                continue
            text = q.get("question_text") or ""
            why = skip_reason(text)
            key = parse_key(q.get("correct_answer"))
            if why is None and key is None:
                why = "no single numeric key"
            if why:
                skipped[why] += 1
                continue
            eval_id = eval_norm.get(norm(text))
            pool.append({
                "id": f"{pid[:8]}-{q.get('question_number')}", "paper": pid, "qno": q.get("question_number"),
                "level": level, "topic": q.get("topic"), "family": family_of(text), "type": q.get("type"),
                "question": " ".join(text.split()), "key_raw": str(q.get("correct_answer")), "answer": key,
                "split": "test" if (test_paper or eval_id) else "train",
                "test_reason": ("eval" if eval_id else "paper") if (test_paper or eval_id) else None,
                "eval_id": eval_id,
            })
    test_texts = {norm(p["question"]) for p in pool if p["split"] == "test"} | set(eval_norm)
    seen, rows, ids = set(), [], Counter()
    for p in pool:
        n = norm(p["question"])
        if n in test_texts and p["split"] == "train":
            p["split"], p["test_reason"] = "test", "repeat of a test question"
        if (n, p["split"]) in seen:
            skipped["duplicate"] += 1
            continue
        seen.add((n, p["split"]))
        ids[p["id"]] += 1
        if ids[p["id"]] > 1:
            p["id"] = f"{p['id']}-{ids[p['id']]}"
        rows.append(p)
    split = {
        "seed": SEED, "test_frac": frac, "maths_papers": len(papers), "test_papers": sorted(test_papers),
        "eval_question_ids": sorted(eval_norm.values()), "eval_in_pool": sum(1 for r in rows if r["eval_id"]),
        "train": counts([r for r in rows if r["split"] == "train"]),
        "test": counts([r for r in rows if r["split"] == "test"]),
        "skipped": dict(sorted(skipped.items())),
    }
    return rows, split


def run(run_id: str, force: bool = False) -> dict:
    from .common import update_manifest

    d = run_dir(run_id)
    if (d / "pool.jsonl").exists() and (d / "split.json").exists() and not force:
        print(f"split exists for {run_id} (use --force to rebuild)")
        return json.loads((d / "split.json").read_text(encoding="utf-8"))
    started = now()
    rows, split = build_pool()
    write_jsonl(d / "pool.jsonl", rows)
    (d / "split.json").write_text(json.dumps(split, indent=2), encoding="utf-8")
    update_manifest(run_id, "split", {
        "started": started, "finished": now(), "seed": SEED, "test_frac": TEST_FRAC,
        "maths_papers": split["maths_papers"], "test_papers": len(split["test_papers"]),
        "test_paper_ids_sha256": hashlib.sha256("\n".join(split["test_papers"]).encode()).hexdigest(),
        "eval_questions_held_out": len(split["eval_question_ids"]), "eval_in_pool": split["eval_in_pool"],
        "train": split["train"], "test": split["test"], "skipped": split["skipped"],
        "files": {"pool.jsonl": sha256_file(d / "pool.jsonl"), "split.json": sha256_file(d / "split.json")},
    })
    print(json.dumps({k: split[k] for k in ("maths_papers", "eval_in_pool", "train", "test")}, indent=1))
    return split
