"""Mastery levels (agent/learner.py) and method badges (agent/badges.py): thresholds, evidence, privacy."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import badges, learner, twins
from app import accounts, db, lesson_ws
from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "specs" / "examples"
MARBLES = json.loads((EXAMPLES / "marbles.json").read_text(encoding="utf-8"))
REMAINDER = json.loads((EXAMPLES / "v2" / "remainder-money.json").read_text(encoding="utf-8"))
P4 = learner.par_rating("P4")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setattr(lesson_ws.tts, "speak_compressed", lambda text: (b"ID3", "audio/mpeg"))
    monkeypatch.setattr(accounts, "child_login_limit", accounts.RateLimit(100, 300))
    monkeypatch.setattr(twins.cache, "prefetch", lambda spec, n=0: None)  # no background twins here
    db._ready = None
    yield
    db._ready = None


# ---------------------------------------------------------------- levels
def lvl(n, margin, wins=0, updated=None):
    return learner.mastery_level({"rating": P4 + margin, "n": n, "updated": updated}, "P4", wins)


def test_level_thresholds():
    assert lvl(0, 0)["level"] == "Starting" and learner.mastery_level(None, "P4")["level"] == "Starting"
    assert lvl(2, 200)["level"] == "Starting"           # too few steps to trust
    assert lvl(3, -75)["level"] == "Growing"            # gentle: one lesson at par
    assert lvl(3, -76)["level"] == "Starting"
    assert lvl(6, 0)["level"] == "Strong" and lvl(6, -1)["level"] == "Growing"
    assert lvl(10, 75)["level"] == "Strong"             # Master also needs a challenge-first win
    assert lvl(10, 75, wins=1)["level"] == "Master" and lvl(10, 75, wins=1)["next"] is None
    assert lvl(9, 200, wins=3)["level"] == "Strong"


def test_level_progress_and_tips():
    a, b = lvl(1, 0), lvl(2, 0)
    assert a["next"] == "Growing" and 0 < a["progress"] < b["progress"] <= 1
    assert "2 more steps" in a["tip"]
    assert "challenge-first" in lvl(10, 80)["tip"]
    assert "hint" in lvl(6, -20)["tip"]


def test_level_refresher_after_two_weeks():
    old = time.time() - 15 * 24 * 3600
    stale = lvl(6, 10, updated=old)
    assert stale["level"] == "Growing" and stale["refresher"] and "refresher" in stale["tip"].lower()
    assert lvl(6, 10, updated=time.time())["level"] == "Strong"
    assert not lvl(0, 0, updated=old)["refresher"]  # nothing to lose at Starting


# ---------------------------------------------------------------- badge conditions
def test_method_badge_needs_evidence():
    clean = {"units": "first try", "unit": "first try", "answer": "with hints"}
    assert badges.evaluate(REMAINDER, clean) == ["remainder_ranger"]
    assert badges.evaluate(MARBLES, clean) == ["unit_finder"]
    assert badges.evaluate(REMAINDER, {"challenge": "first try"}) == ["remainder_ranger"]
    assert badges.evaluate(REMAINDER, {"unit": "early"}) == ["remainder_ranger"]
    assert badges.evaluate(REMAINDER, {"units": "first try", "unit": "shown", "answer": "first try"}) == []
    assert badges.evaluate(REMAINDER, {"units": "first try", "unit": "with hints", "answer": "first try"}) == []
    assert badges.evaluate(REMAINDER, {"unit": "first try"}) == []  # one step is not enough
    assert badges.evaluate(REMAINDER, {"challenge": "missed", "unit": "first try", "answer": "first try"}) == []
    assert badges.evaluate(REMAINDER, {}) == []


def test_every_heuristic_has_a_method_badge_with_art():
    import re
    schema = json.loads((EXAMPLES.parent / "barspec.v2.schema.json").read_text())
    for h in schema["properties"]["heuristic"]["enum"]:
        assert badges.method_badge(h) is not None, h
    art = Path(__file__).resolve().parent.parent / "app" / "static" / "badges"
    for b in badges.CATALOG:
        svg = (art / f"{b.id}.svg").read_text()
        assert svg.startswith("<svg") and 'viewBox="0 0 96 96"' in svg and not re.search(r"<script|href=", svg)


def test_process_badges():
    shown = {"unit": "shown", "answer": "shown"}
    assert badges.evaluate(REMAINDER, shown) == []
    # "Checked my answer" fires only once lessons have a "check" checkpoint (guarded: absent today).
    assert "checked_my_answer" in badges.evaluate(REMAINDER, {**shown, "check": "first try"})
    assert "checked_my_answer" not in badges.evaluate(REMAINDER, {**shown, "check": "shown"})
    # "Came back after a mistake": a twin after a miss, with the final answer reached by the child.
    assert "came_back" in badges.evaluate(REMAINDER, {"challenge": "first try"}, twin_after_miss=True)
    assert "came_back" in badges.evaluate(REMAINDER, {"challenge": "missed", "answer": "with hints"}, twin_after_miss=True)
    assert "came_back" not in badges.evaluate(REMAINDER, {"challenge": "first try"}, twin_after_miss=False)
    assert "came_back" not in badges.evaluate(REMAINDER, {"challenge": "missed", "answer": "shown"}, twin_after_miss=True)
    assert badges.evaluate(REMAINDER, shown, asked_question=True) == ["curious_mind"]


def test_award_anonymous_stores_nothing_and_child_once():
    assert [b["id"] for b in badges.award(None, ["remainder_ranger", "nope", "remainder_ranger"])] == ["remainder_ranger"]
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM badges").fetchone()[0] == 0
    p = db.upsert_parent("google:1", "Mum")
    kid = db.add_child(p["id"], "Kai", "P4", "🐯", "brave-otter-1")
    first = badges.award(kid, ["remainder_ranger", "curious_mind"])
    assert [b["id"] for b in first] == ["remainder_ranger", "curious_mind"] and first[0]["icon"] == "/badges/remainder_ranger.svg"
    assert badges.award(kid, ["remainder_ranger"]) == []  # never twice
    assert {b["badge"] for b in db.list_badges(kid["id"])} == {"remainder_ranger", "curious_mind"}
    assert db.delete_child(kid["id"], p["id"]) and db.list_badges(kid["id"]) == []  # cascades


def test_badges_table_holds_no_question_text():
    db.upsert_parent("google:1", "Mei")
    with db.connect() as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(badges)")}
    assert cols == {"child_id", "badge", "earned_at"}


# ---------------------------------------------------------------- over the socket and the API
RIGHT = {"units": "4", "unit": "30", "answer": "90", "challenge": "90"}


def _play(client, spec, answer_for, ask_first=None):
    with client.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": spec})
        events = [ws.receive_json()]
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "ask":
                ws.send_json({"type": "text", "text": answer_for(ev)})
                verdict = ws.receive_json()
                events.append(verdict)
                while verdict["waiting"]:
                    ws.send_json({"type": "show_me"})
                    verdict = ws.receive_json()
                    events.append(verdict)
            elif ev["type"] == "end":
                return events
            ws.send_json({"type": "next"})


def _child_client(level="P4"):
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": "Mum"}).status_code == 200
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    kid = c.post("/api/children", json={"nickname": "Kai", "level": level, "avatar": "🐯"}).json()["child"]
    assert c.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 200
    return c, kid


def test_anonymous_lesson_shows_badge_and_stores_nothing():
    end = _play(TestClient(app), MARBLES, lambda ev: RIGHT[ev["key"]])[-1]
    assert [b["id"] for b in end["badges"]] == ["unit_finder"] and "Unit Finder" in end["say"]
    assert "mastery" not in end
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM badges").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_child_lesson_stores_badge_once_and_methods_panel():
    c, kid = _child_client()
    end = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])[-1]
    assert [b["id"] for b in end["badges"]] == ["unit_finder"]
    assert end["mastery"]["heuristic"] == "part_whole" and end["mastery"]["level"] == "Growing"
    again = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])[-1]
    assert "badges" not in again  # already earned: no second celebration
    methods = c.get("/api/child/methods").json()
    got = [b["id"] for b in methods["badges"] if b["earned_at"]]
    assert got == ["unit_finder"] and len(methods["badges"]) == len(badges.CATALOG)
    assert methods["levels"][0]["heuristic"] == "part_whole" and methods["levels_names"] == list(learner.LEVELS)
    # The parent sees the same.
    c.post("/api/auth/dev", json={"name": "Mum"})
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    parent_view = c.get(f"/api/child/methods?child_id={kid['id']}").json()
    assert [b["id"] for b in parent_view["badges"] if b["earned_at"]] == got
    assert TestClient(app).get("/api/child/methods").status_code == 401


def test_struggling_lesson_earns_no_method_badge():
    c, _kid = _child_client()
    end = _play(c, MARBLES, lambda ev: "7")[-1]
    assert "badges" not in end and end["twin"]["after_miss"] is True


def test_badge_icons_are_served_only_from_the_catalogue():
    c = TestClient(app)
    r = c.get("/badges/unit_finder.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert c.get("/badges/..%2Fapp.db").status_code == 404 and c.get("/badges/nope.svg").status_code == 404
