"""Public deploy (PUBLIC_DEPLOY=1): sign-in is the front door, the AI needs a family, examples stay open."""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import access, accounts, db
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
MARBLES = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def public_deploy(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")
    monkeypatch.setattr(accounts, "parent_login_limit", accounts.RateLimit(20, 300))
    monkeypatch.setattr(access, "quota", access.DailyQuota())
    db._ready = None
    yield
    db._ready = None


def _parent(monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": "Mum"}).status_code == 200
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    return c


def test_ai_endpoints_need_a_signed_in_family():
    anon = TestClient(app)
    for method, path, body in (("post", "/api/solve/text", {"json": {"text": "x" * 20}}),
                               ("post", "/api/twin", {"json": {"spec": {"version": 2}}}),
                               ("post", "/api/render", {"json": {"spec": {}}})):
        res = getattr(anon, method)(path, **body)
        assert res.status_code == 401 and res.json()["detail"] == "Sign in first.", path


def test_anonymous_lesson_may_start_an_example_but_not_plan():
    anon = TestClient(app)
    token = anon.get("/api/lesson/token").json()["token"]
    with anon.websocket_connect(f"/ws/lesson?token={token}") as ws:
        ws.send_json({"type": "prepare", "question": "Ali has 3 times as many marbles as Ben. Together 120."})
        ev = ws.receive_json()
        assert ev["type"] == "error" and ev["sign_in"] is True
        ws.send_json({"type": "start", "spec": {**MARBLES, "question": "Something not in the examples"}})
        assert ws.receive_json()["sign_in"] is True
        ws.send_json({"type": "start", "spec": MARBLES})
        assert ws.receive_json()["type"] != "error"


def test_daily_quota_per_family(monkeypatch):
    monkeypatch.setattr(access, "PLANS_PER_FAMILY_DAY", 2)
    q = access.DailyQuota()
    q.spend("f1")
    q.spend("f1")
    with pytest.raises(HTTPException) as exc:
        q.spend("f1")
    assert exc.value.status_code == 429
    q.spend("f2")  # another family is not affected
    q.spend("admin")  # the owner is never limited
    for _ in range(5):
        q.spend("admin")


def test_signed_in_parent_passes_the_gate(monkeypatch):
    c = _parent(monkeypatch)
    assert access.member(c.cookies) and access.member(c.cookies).startswith("f")
    assert c.post("/api/solve/text", json={"text": "short"}).status_code == 400  # past the gate, then validated
