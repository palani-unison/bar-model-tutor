"""Bar Model Tutor — FastAPI app.

Initial version input modes (all three land here):
  1. Camera scan  — browser captures a photo (JPEG blob) → POST /api/solve
  2. Photo upload — image file → POST /api/solve
  3. PDF upload   — worksheet PDF → POST /api/solve (text layer or vision)

Plus POST /api/solve/text for typed/pasted problems (demo + accessibility).
Uploads are processed in memory and never written to disk (child safety/PDPA).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
OUT_DIR = ROOT / "out"
MARBLES = ROOT / "specs" / "examples" / "marbles.json"
VIDEO_TTL_S = 60 * 60

logger = logging.getLogger("bar_model_tutor.app")

import sys as _sys

_sys.path.insert(0, str(ROOT))

from agent import client as agent_client  # noqa: E402
from agent.extract import extract_problem, problem_from_text  # noqa: E402
from agent.plan import plan_problem  # noqa: E402
from agent.verify import verify_spec  # noqa: E402
from app.lesson_ws import router as lesson_router  # noqa: E402

from app.accounts import router as accounts_router  # noqa: E402
from app.practice import router as practice_router  # noqa: E402
from app.lessons import router as lessons_router  # noqa: E402
from app import access  # noqa: E402
from app.admin import router as admin_router  # noqa: E402
from app.admin import record_plan  # noqa: E402
from app import showcase  # noqa: E402

app = FastAPI(title="Bar Model Tutor")
app.include_router(lesson_router)
app.include_router(showcase.router)  # videos, slides and docs (app/showcase.py)
app.include_router(admin_router)  # the owner's dashboard and "Report a problem" (app/admin.py)
app.include_router(accounts_router)  # parent sign-in, child profiles, progress (app/accounts.py)
app.include_router(lessons_router)  # "My lessons": the signed-in child's own history and replays (app/lessons.py)
app.include_router(practice_router)  # "Try one like this" twins, mastery levels and method badges (app/practice.py)


ADMIN_PATHS = ("/admin", "/api/models", "/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def require_admin(request: Request, call_next):
    """The admin pages are only for the owner's Google account (ADMIN_EMAILS, app/admin.py); there is no password.

    Everyone else starts at the sign-in page; the AI endpoints check for a signed-in family themselves.
    """
    path = request.url.path
    if not access.public() or not path.startswith(ADMIN_PATHS) or access.is_admin(request.cookies):
        return await call_next(request)
    return HTMLResponse("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        "<title>Admin · Bar Model Tutor</title><body style='font:17px system-ui;margin:32px'>"
                        "<p>This page is for the Bar Model Tutor admin. Please <a href='/'>sign in with Google</a> "
                        "using the admin account, then open it again.</p>", 401)
OUT_DIR.mkdir(exist_ok=True)
app.mount("/out", StaticFiles(directory=str(OUT_DIR)), name="out")
app.mount("/fonts", StaticFiles(directory=str(ROOT / "renderer" / "fonts")), name="fonts")
app.mount("/pwa", StaticFiles(directory=str(STATIC_DIR / "pwa")), name="pwa")  # icons for "Add to Home Screen"
app.mount("/ui", StaticFiles(directory=str(STATIC_DIR / "ui")), name="ui")
showcase.mount_media(app)  # /showcase-media: videos and slide images (not committed; deployed with the app)  # the page's stylesheet (skins) and stage machine


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "pwa" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Served from the root so it may control every page; never cached, so a deploy updates it.
    return FileResponse(STATIC_DIR / "pwa" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


class TextProblem(BaseModel):
    text: str


class RenderRequest(BaseModel):
    spec: dict


def _delete_old_videos() -> None:
    cutoff = time.time() - VIDEO_TTL_S
    for path in OUT_DIR.glob("solution-*.mp4"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _solve(problem: dict, timings: dict | None = None) -> dict:
    timings = dict(timings or {})
    started = time.perf_counter()
    spec, result, log = plan_problem(problem)
    timings["plan_s"] = round(time.perf_counter() - started, 1)
    v2 = spec.get("version") == 2
    body = {
        "timings": timings,
        "problem": problem,
        "spec": spec,
        "verified": result.ok,
        "check": getattr(result, "check", None),
        "planner": getattr(result, "planner", None) or "spark",
        "verify": {"ok": result.ok, "errors": result.errors, "unit_value": getattr(result, "unit_value", None),
                   "total_units": getattr(result, "total_units", None)},
        "attempts": log,
        "answer": spec["answer"] if v2 else spec["model"]["answer"],
    }
    if v2:  # exact solved values, so the board draws true to scale without its own solver
        from agent.spec_v2 import solve_spec

        body["solved"] = solve_spec(spec).as_json()
    return body


def _solve_recorded(cookies, problem: dict, timings: dict | None, source: str) -> dict:
    """_solve for the HTTP endpoints, with the outcome recorded for the admin dashboard (app/admin.py)."""
    started = time.perf_counter()
    question = str(problem.get("question", ""))
    try:
        body = _solve(problem, timings)
    except (agent_client.MissingKeyError, agent_client.ModelUnavailableError) as exc:
        record_plan(cookies, "error", source=source, seconds=time.perf_counter() - started, question=question, detail=str(exc))
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        record_plan(cookies, "declined", source=source, seconds=time.perf_counter() - started, question=question, detail=str(exc))
        raise HTTPException(422, str(exc)) from exc
    record_plan(cookies, "ok", source=source, body=body, seconds=time.perf_counter() - started)
    return body


@app.get("/api/health")
def health():
    from voice.tts import available as voice_available

    return {
        "ok": True,
        "api_key_set": agent_client.has_key(),
        "voice_model_present": voice_available(),
        "modes": ["camera", "photo", "pdf", "text"],
    }


@app.get("/api/models")
def models():
    """Diagnostics (behind the password): which models this server's key can see from where it runs."""
    try:
        return {"models": sorted(m.id for m in agent_client.get_client().models.list())}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _example_files() -> dict[str, Path]:
    """Example specs by file stem: specs/examples/*.json (v1) and specs/examples/v2/*.json. v1 wins a shared name."""
    examples = MARBLES.parent
    return {p.stem: p for p in sorted((examples / "v2").glob("*.json")) + sorted(examples.glob("*.json"))}


@app.get("/api/example")
def example(name: str = "marbles"):
    """A ready-made lesson (default: marbles). `name` must be one of the example files, so no path is ever built from it.

    Every reply lists the examples for the "Try an example" picker.
    """
    from agent.verify_v2 import solution_or_none, verify_spec_v2

    files = _example_files()
    if name not in files:
        raise HTTPException(404, "No example with that name.")
    menu = []
    for stem, path in files.items():
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        menu.append({"name": stem, "title": info.get("title", stem), "level": info.get("level", ""), "version": info.get("version", 1)})
    menu.sort(key=lambda e: (e["name"] != "marbles", e["version"], e["level"], e["name"]))
    spec = json.loads(files[name].read_text(encoding="utf-8"))
    if spec.get("version") == 2:
        result, solution = verify_spec_v2(spec), solution_or_none(spec)
        return {"spec": spec, "verified": result.ok, "verify": str(result),
                "solved": solution.as_json() if solution else None, "examples": menu}
    result = verify_spec(spec)
    return {"spec": spec, "verified": result.ok, "verify": str(result), "examples": menu}


async def _extract_upload(file: UploadFile, question_number: str) -> tuple[dict, dict]:
    """(ProblemJSON, timings) for a camera scan, photo or PDF; HTTP errors as /api/solve gives them."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 20 MB).")
    started = time.perf_counter()
    try:
        qno = question_number.strip().rstrip(".")[:6] or None
        problem = await run_in_threadpool(extract_problem, data, file.content_type, file.filename, qno)
    except (agent_client.MissingKeyError, agent_client.ModelUnavailableError) as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return problem, {"read_s": round(time.perf_counter() - started, 1)}


@app.post("/api/extract")
async def extract_upload(file: UploadFile = File(...), question_number: str = Form(""),
                         _who: str | None = Depends(access.require_member)):
    """Read the problem only (a few seconds), so the page can start the lesson's "prepare" phase (app/lesson_ws.py)
    while it is planned. The image is read in memory, as for /api/solve, and never saved."""
    problem, timings = await _extract_upload(file, question_number)
    return {"problem": problem, "timings": timings}


@app.post("/api/questions")
async def questions_upload(file: UploadFile = File(...), _who: str | None = Depends(access.require_member)):
    """The numbered questions on a worksheet photo (agent/extract.list_questions), so the child can pick one before
    the lesson starts. Read in memory, never saved; an empty list when there is nothing to pick from."""
    data = await file.read()
    if not data or len(data) > 20 * 1024 * 1024 or not (file.content_type or "").startswith("image/"):
        return {"questions": []}
    from agent.extract import list_questions
    try:
        found = await run_in_threadpool(list_questions, data)
    except Exception as exc:  # noqa: BLE001 — optional: the child can still type a number or just start
        logger.info("list_questions failed: %s", exc)
        found = []
    return {"questions": [{"number": str(q.get("number", "")).strip()[:6], "start": str(q.get("start", ""))[:80]}
                          for q in found][:30]}


@app.post("/api/solve")
async def solve_upload(request: Request, file: UploadFile = File(...), question_number: str = Form("")):
    """Camera scan, photo upload, or PDF upload — one endpoint, dispatched by type.

    `question_number` picks one question when the photo is a whole worksheet page.
    """
    access.spend_plan(request)
    problem, timings = await _extract_upload(file, question_number)
    return await run_in_threadpool(_solve_recorded, request.cookies, problem, timings, "photo")


@app.post("/api/solve/text")
def solve_text(body: TextProblem, request: Request):
    access.spend_plan(request)
    text = body.text.strip()
    if len(text) < 10:
        raise HTTPException(400, "Please enter the full word problem.")
    return _solve_recorded(request.cookies, problem_from_text(text), None, "text")


@app.post("/api/check-drawing")
async def check_drawing_upload(file: UploadFile = File(...), spec: str = Form(...),
                               _who: str | None = Depends(access.require_member)):
    """Check my bar model: the child's photo of their own drawing, compared step by step with the verified spec."""
    from agent.review import check_drawing
    from voice import tts

    try:
        spec_obj = json.loads(spec)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "The lesson's bar model was not sent.") from exc
    if not verify_spec(spec_obj).ok:
        raise HTTPException(422, "This bar model could not be checked.")
    image = await file.read()  # in memory only, never saved
    try:
        result = await run_in_threadpool(check_drawing, image, spec_obj)
    except (agent_client.MissingKeyError, agent_client.ModelUnavailableError) as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        result["audio"] = base64.b64encode(await run_in_threadpool(tts.speak_wav, result["say"])).decode()
    except Exception:  # noqa: BLE001 — the written hint is enough
        pass
    result.pop("read", None)
    return result


@app.post("/api/render")
def render(body: RenderRequest, _who: str | None = Depends(access.require_member)):
    """Render a verified BarSpec to MP4. Slow (~3 min) until the Oct 30 speedup."""
    if body.spec.get("version") == 2:
        from agent.verify_v2 import verify_spec_v2

        result = verify_spec_v2(body.spec)
    else:
        result = verify_spec(body.spec)
    if not result.ok:
        raise HTTPException(422, f"Spec failed verification: {result}")
    from voice.tts import available

    if not available():
        raise HTTPException(
            503,
            "Piper voice model (renderer/voice.onnx) is not installed yet, so video cannot be rendered. "
            "The verified bar spec above is still correct. See README.md for the download command.",
        )
    _delete_old_videos()
    # A unique name per render: concurrent students never share a file, and browsers never replay a cached video.
    out_path = OUT_DIR / f"solution-{uuid.uuid4().hex}.mp4"
    # The spec holds the child's question, so it only exists on disk during the render.
    with tempfile.NamedTemporaryFile("w", suffix=".json") as tf:
        json.dump(body.spec, tf)
        tf.flush()
        proc = subprocess.run(
            [sys.executable, str(ROOT / "renderer" / "render.py"), tf.name, str(out_path)],
            capture_output=True, text=True, timeout=900,
        )
    out_path.with_suffix(".wav").unlink(missing_ok=True)
    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        logger.error("renderer failed: %s", proc.stderr[-800:])
        raise HTTPException(500, "Sorry, the video could not be drawn. The bar model above is still correct.")
    return {"video_url": f"/out/{out_path.name}", "log": proc.stdout}


def _doc_page(name: str, title: str) -> HTMLResponse:
    """docs/<name>.md as a plain, readable page (headings, lists, bold, tables, links): the parent-facing terms."""
    import html
    import re

    def inline(text: str) -> str:
        text = html.escape(text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        return re.sub(r"\[(.+?)\]\((privacy|terms)\.md\)", r'<a href="/\2">\1</a>', text)

    # Group lines into blocks first (paragraphs, list items with their wrapped lines), then format each.
    blocks: list[tuple[str, str]] = []
    for line in (ROOT / "docs" / f"{name}.md").read_text(encoding="utf-8").splitlines():
        item = re.match(r"^(\s*)(-|\d+\.)\s+(.*)", line)
        if item:
            blocks.append(("ol" if item.group(2)[0].isdigit() else "ul", item.group(3)))
        elif line.startswith("#"):
            blocks.append((f"h{min(len(line) - len(line.lstrip('#')), 3)}", line.lstrip("#").strip()))
        elif line.startswith("|"):
            if not set(line) <= set("|-: "):
                blocks.append(("tr", line.strip().strip("|")))
        elif not line.strip():
            blocks.append(("", ""))
        elif blocks and blocks[-1][0] in ("p", "ul", "ol"):
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + " " + line.strip())  # a wrapped line
        else:
            blocks.append(("p", line.strip()))
    out, open_list = [], ""
    for kind, text in blocks:
        if kind not in ("ul", "ol") and open_list:
            out.append(f"</{open_list}>")
            open_list = ""
        if kind in ("ul", "ol"):
            if open_list != kind:
                if open_list:
                    out.append(f"</{open_list}>")
                out.append(f"<{kind}>")
                open_list = kind
            out.append(f"<li>{inline(text)}</li>")
        elif kind == "tr":
            out.append("<table><tr>" + "".join(f"<td>{inline(c.strip())}</td>" for c in text.split("|")) + "</tr></table>")
        elif kind:
            out.append(f"<{kind}>{inline(text)}</{kind}>")
    if open_list:
        out.append(f"</{open_list}>")
    return HTMLResponse(f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Bar Model Tutor</title>
<body style="font:17px/1.55 system-ui,sans-serif;color:#1f2a44;background:#fbfaf5;margin:0">
<main style="max-width:720px;margin:0 auto;padding:24px 16px 48px">{''.join(out)}
<p><a href="/">Back to Bar Model Tutor</a></p></main></body></html>""")


@app.get("/terms", response_class=HTMLResponse)
def terms():
    return _doc_page("terms", "Terms and parent consent")


@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return _doc_page("privacy", "What we store")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
