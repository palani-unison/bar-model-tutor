"""Paths, run directories, hashing and the per-run manifest for the Glimmer planner fine-tune.

Everything private (data, splits, labels, logs, eval rows, full manifests) lives under
evals/private/glimmer/<run_id>/, a gitignored symlinked folder on the user's Mac. The committed
copy of a manifest (docs/private/glimmer/runs/<run_id>.json) is sanitised: hashes, counts,
ids, hyper-parameters and metrics only; never question text.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "evals" / "private"
GLIMMER = PRIVATE / "glimmer"
DOCS = ROOT / "docs" / "private" / "glimmer"
RUNS_DOC = DOCS / "runs"

BASE_MODEL = "accounts/fireworks/models/muse-glimmer-30b"
TOKENIZER = "meta-models/Muse-Glimmer-30B"
TOKENIZER_REV = "a4e59da52a7bc87ae7251dd5545c0dd437c44b68"
SEED = "glimmer-v1"  # split + shuffles; change only on purpose (it changes the held-out set)

# Keys that may carry private text: never copied into the committed summary.
_PRIVATE_KEYS = {"question", "questions", "key_raw", "explanation", "spec", "messages", "error", "log", "text", "reply",
                 "gate_errors"}


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run_dir(run_id: str, base: Path | None = None) -> Path:
    d = (base or GLIMMER) / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_fraction(key: str, salt: str) -> float:
    """A deterministic number in [0, 1) from a string (paper ids pick the held-out papers)."""
    return int(hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()[:8], 16) / 0x100000000


def git_commit() -> str | None:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:  # noqa: BLE001 — not a git checkout
        return None


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------- manifest
def manifest_path(run_id: str, base: Path | None = None) -> Path:
    return run_dir(run_id, base) / "manifest.json"


def load_manifest(run_id: str, base: Path | None = None) -> dict:
    p = manifest_path(run_id, base)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"run_id": run_id}


def update_manifest(run_id: str, section: str, data: dict, base: Path | None = None, docs: Path | None = None) -> dict:
    """Merge one step's record into the private manifest and refresh the sanitised committed copy.

    Steps may run in parallel (training, trials, evals), so the read-modify-write holds a file lock.
    """
    p = manifest_path(run_id, base)
    with open(p.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        m = load_manifest(run_id, base)
        m.setdefault("run_id", run_id)
        m.setdefault("seed", SEED)
        m.setdefault("base_model", BASE_MODEL)
        m["git_commit"] = git_commit()
        m["updated"] = now()
        m[section] = data
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        out = docs or RUNS_DOC
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{run_id}.json").write_text(json.dumps(sanitise(m), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return m


def sanitise(obj, root: Path = ROOT):
    """Drop private-text keys and turn absolute paths into repo-relative names."""
    if isinstance(obj, dict):
        return {k: sanitise(v, root) for k, v in obj.items() if k not in _PRIVATE_KEYS}
    if isinstance(obj, list):
        return [sanitise(v, root) for v in obj]
    if isinstance(obj, str) and obj.startswith("/"):
        try:
            return str(Path(obj).resolve().relative_to(root.resolve()))
        except ValueError:
            return Path(obj).name
    return obj
