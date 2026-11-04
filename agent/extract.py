"""Scan → ProblemJSON.

Handles all three initial input modes:
  - camera scan  (JPEG/PNG captured in the browser)
  - photo upload (any image file)
  - PDF upload   (worksheet PDF; text layer used when present, else pages rendered to images)

Without META_API_KEY the vision call is skipped and the caller gets a clear
error so the UI can offer manual text entry (demo mode). PDF text extraction
and problem_from_text() work fully offline.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from . import client as _client

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract.md"

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PDF_MIME = "application/pdf"
MAX_PDF_PAGES = 5

ProblemJSON = dict


def _prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return "Read the maths word problem in the image. Return only ProblemJSON."


def is_pdf(content_type: str | None, filename: str | None) -> bool:
    if content_type and "pdf" in content_type:
        return True
    return bool(filename and filename.lower().endswith(".pdf"))


def is_image(content_type: str | None, filename: str | None) -> bool:
    if content_type and content_type in IMAGE_MIMES:
        return True
    if content_type and content_type.startswith("image/"):
        return True
    return bool(filename and filename.lower().rsplit(".", 1)[-1] in {"jpg", "jpeg", "png", "webp", "gif"})


# ---------------------------------------------------------------- PDF helpers
def pdf_page_count(pdf_bytes: bytes) -> int:
    import pymupdf

    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return len(doc)


def pdf_extract_text(pdf_bytes: bytes) -> str:
    """Embedded text layer of a PDF (empty string if scanned-only)."""
    import pymupdf

    parts: list[str] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts).strip()


def pdf_to_images(pdf_bytes: bytes, max_pages: int = MAX_PDF_PAGES, zoom: float = 2.0) -> list[bytes]:
    """Render PDF pages to PNG bytes for the vision model."""
    import pymupdf

    images: list[bytes] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in list(doc)[:max_pages]:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            images.append(pix.tobytes("png"))
    return images


# ---------------------------------------------------------------- offline parse
_NUM = r"(\d+(?:\.\d+)?)"


def problem_from_text(text: str) -> ProblemJSON:
    """Best-effort ProblemJSON from plain text (typed input or PDF text layer).

    Deterministic heuristics — good enough for demo mode and unit tests.
    The vision/LLM path remains primary for messy handwriting.
    """
    clean = " ".join(text.split())
    problem_type = "part-whole"
    if re.search(r"times as many", clean, re.I):
        problem_type = "multiples"
    elif re.search(r"\bmore than\b|\bless than\b|\bdifference\b", clean, re.I):
        problem_type = "comparison"
    elif re.search(r"\bratio\b|:", clean):
        problem_type = "ratio"
    elif re.search(r"fraction|/\d|\bhalf\b|\bquarter", clean, re.I):
        problem_type = "fractions"

    numbers = [
        {"value": float(m) if "." in m else int(m), "unit": "", "role": "mentioned"}
        for m in re.findall(_NUM, clean)
    ]
    # Names: capitalised words that are not sentence starts is unreliable;
    # grab "X has ..." / "as Y" patterns common in bar-model problems.
    entities: list[dict] = []
    for name in re.findall(r"\b([A-Z][a-z]+)\s+has\b", clean):
        entities.append({"name": name, "role": "person"})
    for name in re.findall(r"\bas\s+([A-Z][a-z]+)\b", clean):
        if not any(e["name"] == name for e in entities):
            entities.append({"name": name, "role": "person"})

    return {
        "question": clean,
        "level": "P3-P4",
        "problem_type": problem_type,
        "entities": entities,
        "numbers": numbers,
        "asked": "",
        "raw_text_confidence": 0.5,
        "source": "text",
    }


# ---------------------------------------------------------------- vision path
def _image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/png"


LIST_PROMPT = """This is a page from a Singapore primary maths worksheet or exam paper.
List every numbered question on it. Return JSON only:
{"questions": [{"number": "<as printed, e.g. 12 or 12a>", "start": "<the first 8 words of the question>",
                "has_figure": <true if it needs a diagram, table or graph to solve>}]}"""


def list_questions(image: bytes) -> list[dict]:
    """The numbered questions on a worksheet page, so the child can pick one."""
    b64 = base64.b64encode(image).decode()
    resp = _client.chat(
        [{"role": "user", "content": [{"type": "text", "text": LIST_PROMPT},
                                      {"type": "image_url", "image_url": {"url": f"data:{_image_mime(image)};base64,{b64}"}}]}],
        response_format={"type": "json_object"}, reasoning_effort="low",
    )
    found = json.loads(resp.choices[0].message.content or "{}").get("questions") or []
    return [q for q in found if isinstance(q, dict) and str(q.get("number", "")).strip()]


def _vision_problem(images_png_or_jpg: list[bytes], question_number: str | None = None) -> ProblemJSON:
    text = _prompt()
    if question_number:
        text += f"\n\nRead ONLY question {question_number} on this page. Ignore every other question."
    content: list[dict] = [{"type": "text", "text": text}]
    for img in images_png_or_jpg:
        b64 = base64.b64encode(img).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{_image_mime(img)};base64,{b64}"}})
    resp = _client.chat(
        [{"role": "user", "content": content}],
        model=_client.MODEL_DEFAULT,
        response_format={"type": "json_object"},
        reasoning_effort="low",  # reading a printed question needs little thought; default effort is ~4x slower
    )
    raw = resp.choices[0].message.content or "{}"
    problem = json.loads(raw)
    problem.setdefault("source", "vision")
    return problem


def extract_problem(file_bytes: bytes, content_type: str | None, filename: str | None,
                    question_number: str | None = None) -> ProblemJSON:
    """Entry point for uploads. Dispatches on image vs PDF. `question_number` picks one question on a full page."""
    if is_pdf(content_type, filename):
        return extract_from_pdf(file_bytes)
    if is_image(content_type, filename):
        return extract_from_image(file_bytes, question_number)
    raise ValueError(f"Unsupported file type '{content_type or filename}'. Upload an image or a PDF.")


def extract_from_image(image_bytes: bytes, question_number: str | None = None) -> ProblemJSON:
    if not _client.has_key():
        raise _client.MissingKeyError(
            "No META_API_KEY set, so the photo cannot be read automatically. "
            "Type or paste the problem text instead, or set the key and retry."
        )
    return _vision_problem([image_bytes], question_number)


def extract_from_pdf(pdf_bytes: bytes) -> ProblemJSON:
    # Prefer the embedded text layer — fast, free, exact for printed worksheets.
    try:
        text = pdf_extract_text(pdf_bytes)
    except Exception:
        text = ""
    if len(text) >= 20:
        problem = problem_from_text(text)
        problem["source"] = "pdf-text"
        return problem

    # Scanned PDF: render pages and read them with the vision model.
    if not _client.has_key():
        raise _client.MissingKeyError(
            "This PDF is a scanned image with no text layer, and no META_API_KEY is set. "
            "Type the problem text instead, or set the key and retry."
        )
    images = pdf_to_images(pdf_bytes)
    if not images:
        raise ValueError("PDF has no pages.")
    problem = _vision_problem(images)
    problem["source"] = "pdf-vision"
    return problem
