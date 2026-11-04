"""Offline pipeline tests: text/PDF extraction + fallback planner + API input modes."""

import io

import pytest
from fastapi.testclient import TestClient

from agent.extract import is_image, is_pdf, pdf_extract_text, pdf_to_images, problem_from_text
from agent.plan import plan_problem
from app.main import app

MARBLES_Q = "Ali has 3 times as many marbles as Ben. Together they have 120 marbles. How many marbles does Ali have?"

client = TestClient(app)


def test_input_mode_detection():
    assert is_pdf("application/pdf", "worksheet.pdf")
    assert is_pdf(None, "worksheet.PDF")
    assert is_image("image/jpeg", "camera-scan.jpg")
    assert is_image(None, "photo.png")
    assert not is_image("application/pdf", "x.pdf")


def test_problem_from_text_multiples():
    p = problem_from_text(MARBLES_Q)
    assert p["problem_type"] == "multiples"
    assert p["question"] == MARBLES_Q
    assert {e["name"] for e in p["entities"]} == {"Ali", "Ben"}


def test_fallback_plan_solves_marbles_without_api_key(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    spec, result, _log = plan_problem(problem_from_text(MARBLES_Q))
    assert result.ok
    assert spec["model"]["answer"] == 90
    assert spec["model"]["asked"] == "ali"


def _make_pdf_bytes(with_text: bool) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    if with_text:
        page.insert_text((72, 72), MARBLES_Q, fontsize=11)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_pdf_text_layer_extraction():
    pdf = _make_pdf_bytes(with_text=True)
    text = pdf_extract_text(pdf)
    assert "120 marbles" in text
    p = problem_from_text(text)
    assert p["problem_type"] == "multiples"


def test_pdf_renders_to_images():
    pdf = _make_pdf_bytes(with_text=False)
    images = pdf_to_images(pdf)
    assert len(images) == 1
    assert images[0][:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_api_text_mode_end_to_end(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    r = client.post("/api/solve/text", json={"text": MARBLES_Q})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["answer"] == 90


def test_api_pdf_upload_end_to_end(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    pdf = _make_pdf_bytes(with_text=True)
    r = client.post("/api/solve", files={"file": ("worksheet.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == 90
    assert r.json()["problem"]["source"] == "pdf-text"


def test_api_photo_upload_without_key_gives_clear_503(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    png = _tiny_png()
    r = client.post("/api/solve", files={"file": ("photo.png", png, "image/png")})
    assert r.status_code == 503
    assert "META_API_KEY" in r.json()["detail"]


def test_api_camera_scan_without_key_gives_clear_503(monkeypatch):
    # Camera scans arrive as a JPEG blob named camera-scan.jpg — same path as photo.
    monkeypatch.delenv("META_API_KEY", raising=False)
    r = client.post("/api/solve", files={"file": ("camera-scan.jpg", _tiny_png(), "image/jpeg")})
    assert r.status_code == 503


def test_api_rejects_unsupported_and_empty():
    r = client.post("/api/solve", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    r = client.post("/api/solve/text", json={"text": "too short"})
    assert r.status_code == 400


def test_health_lists_all_modes():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["modes"] == ["camera", "photo", "pdf", "text"]


def _tiny_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="PNG")
    return buf.getvalue()


def test_fallback_answers_who_the_question_asks_about(monkeypatch):
    monkeypatch.delenv("META_API_KEY", raising=False)
    q = "Ali has 3 times as many marbles as Ben. Together they have 120 marbles. How many marbles does Ben have?"
    spec, result, _log = plan_problem(problem_from_text(q))
    assert result.ok, result.errors
    assert spec["model"]["asked"] == "ben"
    assert spec["model"]["answer"] == 30


def _reply(content):
    from types import SimpleNamespace

    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_invalid_json_is_retried_then_verified(monkeypatch):
    import json
    import threading
    from pathlib import Path

    from agent import client as agent_client
    from agent.plan import V2_WAVES

    good = json.loads((Path(__file__).resolve().parent.parent / "specs" / "examples" / "v2" / "remainder-money.json")
                      .read_text(encoding="utf-8"))
    lock, served = threading.Lock(), []

    def chat(*a, **k):  # the first wave's drafts are not JSON; the next wave's are good
        with lock:
            served.append(1)
            first = len(served) <= len(V2_WAVES[0])
        return _reply("not json" if first else json.dumps(good))

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", chat)
    monkeypatch.setattr("agent.plan.careful_answer", lambda q: 150.0)  # the quick check is unavailable here
    spec, result, log = plan_problem(problem_from_text(good["question"]))
    assert result.ok and spec["answer"] == 150
    assert "invalid JSON" in log[1]


def test_model_outage_gives_503_with_no_internals(monkeypatch):
    from agent import client as agent_client

    def down(*a, **k):
        raise agent_client.ModelUnavailableError("The maths model is not available right now.")

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", down)
    r = client.post("/api/solve/text", json={"text": MARBLES_Q})
    assert r.status_code == 503
    assert "not available" in r.json()["detail"]


def test_unverifiable_answer_is_never_shown(monkeypatch):
    from agent import client as agent_client

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", lambda *a, **k: _reply("{}"))
    r = client.post("/api/solve/text", json={"text": MARBLES_Q})
    assert r.status_code == 422
    assert "schema" not in r.json()["detail"]


def test_image_mime_detected_from_bytes():
    from agent.extract import _image_mime

    assert _image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert _image_mime(_tiny_png()) == "image/png"


def test_render_uses_unique_file_and_leaves_no_spec_behind(monkeypatch, tmp_path):
    import json
    import subprocess
    from pathlib import Path
    from types import SimpleNamespace

    from app import main

    root = Path(__file__).resolve().parent.parent
    spec = json.loads((root / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd[2])
        assert Path(cmd[2]).exists()
        Path(cmd[3]).write_bytes(b"mp4")
        Path(cmd[3]).with_suffix(".wav").write_bytes(b"wav")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(main, "OUT_DIR", tmp_path)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("voice.tts.available", lambda: True)
    first = client.post("/api/render", json={"spec": spec}).json()["video_url"]
    second = client.post("/api/render", json={"spec": spec}).json()["video_url"]
    assert first != second
    assert not any(Path(p).exists() for p in seen)
    assert not list(tmp_path.glob("*.wav"))


def test_public_deploy_password_is_admin_only_and_ai_needs_sign_in(monkeypatch):
    import base64

    monkeypatch.setenv("PUBLIC_DEPLOY", "1")
    assert client.get("/").status_code == 200  # the sign-in page is the front door
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/example").status_code == 200  # built-in examples need no account
    res = client.post("/api/solve/text", json={"text": "Ali has 3 times as many marbles as Ben. How many?"})
    assert res.status_code == 401 and res.json()["detail"] == "Sign in first."
    assert client.get("/admin").status_code == 401  # only the owner's Google account (tests/test_admin.py)


def test_unsupported_shapes_are_refused_before_any_model_call(monkeypatch):
    import pytest as _pytest

    from agent import client as agent_client
    from agent.plan import unsupported_reason

    def no_calls(*a, **k):
        raise AssertionError("model should not be called")

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", no_calls)
    q = "The figure below shows 3 bags of marbles. Ali has 3 times as many marbles as Ben. How many marbles does Ben have?"
    with _pytest.raises(ValueError, match="figure"):
        plan_problem(problem_from_text(q))
    # Before-after and "how many more" are planned as BarSpec v2 now, not refused.
    assert unsupported_reason("Ali had 3 times as many stamps as Ben. After Ali gave 20 stamps to Ben, they had the same.") is None
    assert unsupported_reason(MARBLES_Q) is None
    for q in ["Mrs Lee baked 120 cookies. She sold 3/5 of them. How many cookies were left?",
              "A pen costs $3 more than a pencil. Together they cost $7. How much does the pencil cost?"]:
        assert unsupported_reason(q) is None, q


def _marbles_reply():
    import json
    from pathlib import Path

    good = (Path(__file__).resolve().parent.parent / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8")
    return _reply(json.dumps(json.loads(good)))


def test_independent_check_agrees(monkeypatch):
    from agent import check
    from agent import client as agent_client

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", lambda *a, **k: _marbles_reply())
    monkeypatch.setattr(check, "independent_answer", lambda q: 90.0)
    spec, result, log = plan_problem(problem_from_text(MARBLES_Q))
    assert result.check == "agreed"
    assert "agreed: 90" in log[-1]


def test_independent_check_disagreement_retries_then_declines(monkeypatch):
    from agent import check
    from agent import client as agent_client
    from agent.plan import V2_WAVES

    calls = []
    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", lambda messages, **k: calls.append(messages) or _marbles_reply())
    monkeypatch.setattr(check, "independent_answer", lambda q: 30.0)
    monkeypatch.setattr("agent.plan.careful_answer", lambda q: 30.0)
    with pytest.raises(ValueError):
        plan_problem(problem_from_text(MARBLES_Q))
    assert len(calls) == sum(len(w) for w in V2_WAVES)  # a v1/v2 race, then the later v2 waves
    # v1 drafts verify but disagree; the v2 drafts get the v1 JSON and fail. The checker's number never leaks.
    assert not any("30" in c[-1]["content"].split(MARBLES_Q)[-1] for c in calls)
