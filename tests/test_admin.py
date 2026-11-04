"""Admin dashboard: Google admin by email (never stored), "Report a problem" flags, plan outcomes, export."""
import base64

import pytest
from fastapi.testclient import TestClient

from app import accounts, admin, db
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")  # a public deploy
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    monkeypatch.setenv("ADMIN_EMAILS", "Owner@Example.com, second@example.com")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setattr(accounts, "parent_login_limit", accounts.RateLimit(50, 300))
    db._ready = None
    yield tmp_path
    db._ready = None


def _google(monkeypatch, email: str, verified=True, sub="1"):
    from google.oauth2 import id_token

    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda token, request, audience: {
        "iss": "https://accounts.google.com", "sub": sub, "given_name": "Pat", "email": email,
        "email_verified": verified})


def test_owner_google_account_is_admin_others_are_not(monkeypatch):
    _google(monkeypatch, "owner@example.com", sub="9")
    owner = TestClient(app)
    r = owner.post("/api/auth/google", json={"credential": "x"})
    assert r.status_code == 200 and r.json().get("admin") is True
    assert owner.get("/api/me").json()["admin"] is True
    assert owner.get("/admin").status_code == 200  # no password prompt
    with db.connect() as con:
        assert "owner@example.com" not in str([dict(r) for r in con.execute("SELECT * FROM parents")])
    owner.post("/api/auth/logout")
    assert owner.get("/admin").status_code == 401  # signing out ends admin too

    _google(monkeypatch, "parent@example.com", sub="10")
    parent = TestClient(app)
    assert parent.post("/api/auth/google", json={"credential": "x"}).json().get("admin") is None
    assert parent.get("/api/me").json()["admin"] is False
    assert parent.get("/admin").status_code == 401
    assert parent.get("/admin/export.json").status_code == 401

    _google(monkeypatch, "owner@example.com", verified=False, sub="11")
    assert TestClient(app).post("/api/auth/google", json={"credential": "x"}).json().get("admin") is None


def test_no_password_opens_admin(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "s3cret")  # an old secret may still hold one: it must not work
    basic = base64.b64encode(b"any:s3cret").decode()
    c = TestClient(app)
    for path in ("/admin", "/admin/export.json", "/docs", "/api/models"):
        r = c.get(path, headers={"Authorization": f"Basic {basic}"})
        assert r.status_code == 401 and "www-authenticate" not in r.headers
    assert TestClient(app).get("/showcase").status_code == 200  # guests: open to everyone


def test_report_a_problem_reaches_the_dashboard_and_export(monkeypatch):
    _google(monkeypatch, "parent@example.com", sub="20")
    parent = TestClient(app)
    parent.post("/api/auth/google", json={"credential": "x"})
    spec = {"version": 2, "question": "Ali has 3 times as many marbles as Ben. They have 48. How many has Ben?",
            "answer": 12}
    assert parent.post("/api/flags", json={"reason": "nope"}).status_code == 400
    r = parent.post("/api/flags", json={"reason": "wrong_answer", "note": "key says 14", "spec": spec})
    assert r.status_code == 200
    assert TestClient(app).post("/api/flags", json={"reason": "other", "note": "hi"}).status_code == 401  # signed out

    pid = parent.get("/api/me").json()["parent"]["id"]
    admin.record_plan({}, "declined", question="A tank is 3/5 full...", detail="figure needed")
    db.add_plan_event(pid, None, "ok", heuristic="multiples", seconds=12.0, question="kept?")

    _google(monkeypatch, "owner@example.com", sub="21")
    owner = TestClient(app)
    owner.post("/api/auth/google", json={"credential": "x"})
    page = owner.get("/admin").text
    assert "key says 14" in page and "Ali has 3 times" in page and "A tank is 3/5 full" in page
    assert "Parents" in page and "Families" in page and "Methods" in page
    assert owner.get(f"/admin/family/{pid}").status_code == 200

    flag_id = r.json()["id"]
    assert owner.post(f"/admin/flags/{flag_id}", data={"status": "fixed", "admin_note": "key was wrong"},
                      follow_redirects=False).status_code == 303
    rows = owner.get("/admin/export.json").json()
    flagged = [x for x in rows if x["id"] == f"flag-{flag_id}"][0]
    assert flagged["status"] == "fixed" and flagged["tutor_answer"] == "12"
    assert any(x["id"].startswith("declined-") and "tank" in x["question"] for x in rows)
    assert not any(x["question"] == "kept?" for x in rows)  # a solved question isn't kept in the plan log


def test_report_a_problem_with_a_non_string_question_does_not_crash(monkeypatch):
    _google(monkeypatch, "parent@example.com", sub="40")
    parent = TestClient(app)
    parent.post("/api/auth/google", json={"credential": "x"})
    r = parent.post("/api/flags", json={"reason": "other", "note": "", "spec": {"question": 123}})
    assert r.status_code == 200

    _google(monkeypatch, "owner@example.com", sub="41")
    owner = TestClient(app)
    owner.post("/api/auth/google", json={"credential": "x"})
    rows = owner.get("/admin/export.json").json()
    assert [x for x in rows if x["id"] == f"flag-{r.json()['id']}"][0]["question"] == "123"


def test_flags_and_plan_events_go_with_the_account(monkeypatch):
    _google(monkeypatch, "parent@example.com", sub="30")
    parent = TestClient(app)
    parent.post("/api/auth/google", json={"credential": "x"})
    parent.post("/api/flags", json={"reason": "confusing", "question": "Some question text here"})
    pid = parent.get("/api/me").json()["parent"]["id"]
    db.add_plan_event(pid, None, "declined", question="q")
    assert parent.delete("/api/me").status_code == 200
    assert db.admin_query("SELECT COUNT(*) AS n FROM flags")[0]["n"] == 0
    assert db.admin_query("SELECT COUNT(*) AS n FROM plan_events")[0]["n"] == 0
