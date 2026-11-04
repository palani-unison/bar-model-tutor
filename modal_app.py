"""Deploy the whole app (FastAPI + live-lesson WebSocket + video renderer) to Modal.

One-time setup (needs a Modal account):
  pip install modal==1.6.3
  modal token new                      # opens a browser to sign in
  modal secret create bar-model-tutor META_API_KEY=... SESSION_SECRET=... GOOGLE_CLIENT_ID=...
  modal secret create bar-model-tutor-fireworks FIREWORKS_API_KEY=...   # Glimmer planner

Deploy:  modal deploy modal_app.py      (prints the https://<workspace>--bar-model-tutor-web.modal.run URL)
Try it:  modal serve modal_app.py       (temporary URL, reloads on save)

One warm container serves everything: lessons keep their state in memory and videos
are written to local disk, so a second container would not see them.

Personalisation (parents, child profiles, mastery; app/db.py) lives in SQLite. The container works on a
local copy (DB_LOCAL_COPY) and snapshots it to the "bar-model-tutor-data" Volume at /data/app.db a few
seconds after each write (debounced) and at shutdown, then calls `data_volume.commit()`. A Volume only
persists what is committed (Modal also commits in the background and when the container stops), so an
explicit commit after each snapshot keeps redeploys from losing recent writes; the snapshot is written to a
temp file and renamed, so a commit never sees a half-written database. A new container restores from it.
"""
from __future__ import annotations

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent
# This file is imported again inside the container, where requirements.txt isn't present;
# the image is only built from here on the deploying machine.
PINNED = [line.strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
          if line.strip() and not line.startswith("#")] if modal.is_local() else []

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("ffmpeg")
    .uv_pip_install(*PINNED)
    # The Piper voice (~63 MB) is gitignored locally, so bake it into the image.
    .run_commands(
        "python -m piper.download_voices en_US-lessac-medium --download-dir /voice",
    )
    # The fine-tuned Glimmer planner (agent/glimmer.py) samples through the Fireworks training SDK.
    # The "training-sdk" extra needs no torch; transformers only renders the chat template.
    .uv_pip_install("fireworks-ai[training-sdk]==1.2.30", "transformers==5.10.4", "tokenizers==0.22.2", "Jinja2==3.1.6")
    .env({"HF_HOME": "/hf"})
    .run_commands(
        "python -c \"from transformers import AutoTokenizer; "
        "AutoTokenizer.from_pretrained('meta-models/Muse-Glimmer-30B', "
        "revision='a4e59da52a7bc87ae7251dd5545c0dd437c44b68')\"",
    )
    .add_local_dir(
        ROOT, remote_path="/root/app",
        # Never upload the private school-paper data, secrets or working notes.
        ignore=[".git", ".venv", ".env", "out", "**/__pycache__", "renderer/voice.onnx*", "evals/results",
                "evals/private", "evals/private/**", ".claude", "claude_handoff", "*.mp4", "*.wav", "data",
                "hackathon_files", "docs/private", "showcase"],
    )
)
if modal.is_local() and (ROOT / "showcase").is_dir():
    # The Showcase screen's videos and slides (app/showcase.py): deployed with the app, never committed.
    image = image.add_local_dir(ROOT / "showcase", remote_path="/root/app/showcase")

app = modal.App("bar-model-tutor")
data_volume = modal.Volume.from_name("bar-model-tutor-data", create_if_missing=True)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("bar-model-tutor"), modal.Secret.from_name("bar-model-tutor-fireworks")],
    volumes={"/data": data_volume},  # app/db.py snapshots the SQLite database here
    min_containers=1,       # no cold start during a demo
    max_containers=1,       # lessons and videos live in this container's memory and disk
    timeout=60 * 60,        # a WebSocket lesson is one long-running input
    cpu=2.0,
    memory=2048,
)
@modal.concurrent(max_inputs=50)
@modal.asgi_app(label="barmodel")  # https://<workspace>--barmodel.modal.run
def web():
    import os
    import shutil
    import sys

    os.chdir("/root/app")
    os.environ.setdefault("DB_PATH", "/data/app.db")  # the durable snapshot on the Volume
    os.environ.setdefault("DB_LOCAL_COPY", "/root/db/app.db")  # SQLite runs on local disk
    # Fine-tuned Glimmer can race the Spark planner (agent/plan.py), but it made the median plan slower and no
    # more accurate (docs/testing.md), so it is off; PLANNER_MODEL=glimmer turns it back on.
    os.environ.setdefault("PLANNER_MODEL", "spark")
    # The owner's Google account opens the admin dashboard (app/admin.py); the address is checked at sign-in, never stored.
    os.environ.setdefault("ADMIN_EMAILS", "owner@example.com")
    os.environ.setdefault("PUBLIC_DEPLOY", "1")  # sign-in is the front door; the AI needs a signed-in family
    os.environ.pop("APP_PASSWORD", None)  # no admin password any more (an old secret may still hold one)
    os.environ.setdefault("GLIMMER_MODEL_PATH", "<your-fireworks-model-path>")
    os.environ.setdefault("GLIMMER_LEDGER", "/data/glimmer-ledger.jsonl")  # spend log survives redeploys
    os.environ.setdefault("GLIMMER_BUDGET_USD", "5")  # this ledger's cap; $9.52 already spent locally of $20
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # the tokenizer is baked into the image
    sys.path.insert(0, "/root/app")
    voice = Path("/root/app/renderer/voice.onnx")
    if not voice.exists():
        shutil.copy("/voice/en_US-lessac-medium.onnx", voice)
        shutil.copy("/voice/en_US-lessac-medium.onnx.json", str(voice) + ".json")

    from app import db
    from app.main import app as fastapi_app

    db.set_persist_hook(data_volume.commit)
    _warm_glimmer()

    return fastapi_app


def _warm_glimmer() -> None:
    """Open Glimmer's sampler in the background at start-up, so the first lesson doesn't pay ~20 s for it.

    Loads the tokenizer and opens the Fireworks session only: no tokens are sampled, so it costs nothing.
    """
    import threading

    def warm():
        try:
            from agent import glimmer

            if glimmer.available():
                glimmer._sampler()
        except Exception as exc:  # noqa: BLE001 — a cold first plan is the only cost
            print(f"glimmer warm-up skipped: {type(exc).__name__}")

    threading.Thread(target=warm, daemon=True).start()
