"""The Showcase screen: the demo videos, the pitch deck as a slide carousel, and the project docs.

GET /showcase                 the page (app/static/showcase.html), open to everyone
GET /showcase/doc/{name}      one project doc as Markdown (a fixed list, never a path from the request)
/showcase-media/...           videos, slide images and manifest.json from SHOWCASE_DIR (default: ./showcase).
                              The media are deployed with the app but not committed; without them the page
                              shows the docs only.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "static" / "showcase.html"
DOCS = {
    "readme": ("README.md", "README"),
    "story": ("docs/SUBMISSION.md", "The story"),
    "how-it-works": ("docs/how-it-works.md", "How it works"),
    "testing": ("docs/testing.md", "Testing"),
    "evals": ("docs/evals.md", "Evals and harness"),
    "built": ("docs/built-so-far.md", "What's built"),
    "spec": ("docs/spec-v2.md", "BarSpec v2"),
    "privacy": ("docs/privacy.md", "What we store"),
    "terms": ("docs/terms.md", "Terms"),
}

router = APIRouter()


def media_dir() -> Path:
    return Path(os.environ.get("SHOWCASE_DIR") or ROOT / "showcase")


def mount_media(app) -> None:
    folder = media_dir()
    folder.mkdir(parents=True, exist_ok=True)  # an empty folder: the page falls back to docs only
    app.mount("/showcase-media", StaticFiles(directory=str(folder)), name="showcase-media")


@router.get("/showcase", response_class=HTMLResponse)
def showcase_page():
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/showcase/docs")
def showcase_docs():
    return [{"name": k, "title": t} for k, (path, t) in DOCS.items() if (ROOT / path).exists()]


@router.get("/showcase/doc/{name}", response_class=PlainTextResponse)
def showcase_doc(name: str):
    entry = DOCS.get(name)
    if entry is None or not (ROOT / entry[0]).exists():
        raise HTTPException(404, "No such doc.")
    return PlainTextResponse((ROOT / entry[0]).read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
