"""Adaptive pacing in the lesson: challenge first (right -> recap, wrong -> guided), key checkpoints, an early
final answer, and recording for a signed-in child; over the WebSocket with TTS mocked."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.lesson import Lesson
from app import accounts, db, lesson_ws
from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "specs" / "examples"
MARBLES = json.loads((EXAMPLES / "marbles.json").read_text(encoding="utf-8"))
CAKES = json.loads((EXAMPLES / "v2" / "units_and_value-cakes.json").read_text(encoding="utf-8"))
CHALLENGE = {"mode": "challenge_first", "speed": 1.5, "checkpoints": "key"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")
    monkeypatch.setattr(accounts, "child_login_limit", accounts.RateLimit(100, 300))
    db._ready = None
    yield
    db._ready = None


# ---------------------------------------------------------------- the Lesson alone
def _until_ask(lesson):
    while True:
        ev = lesson.next_event()
        if ev is None or ev["type"] != "step":
            return ev


def test_challenge_right_gives_one_recap_event():
    lesson = Lesson(MARBLES, pacing=CHALLENGE)
    assert lesson.next_event()["step"]["do"] == "write_question"
    ask = lesson.next_event()
    assert ask["type"] == "ask" and ask["key"] == "challenge" and "How many marbles" in ask["say"]
    v = lesson.answer("ninety")
    assert v.result == "correct" and lesson.recap_pending
    recap = lesson.next_event()
    assert recap["type"] == "recap" and recap["steps"][-1]["step"]["do"] == "answer"
    assert [s["index"] for s in recap["steps"]][0] == 1 and len(recap["steps"]) == sum(s["do"] != "pause" for s in MARBLES["timeline"][1:])
    assert lesson.next_event() is None and lesson.done and lesson.score == {"challenge": "first try"}


def test_challenge_wrong_falls_back_to_every_checkpoint():
    lesson = Lesson(MARBLES, pacing=CHALLENGE)
    assert lesson.checkpoint_count == 3  # the challenge + the key ones (unit, answer)
    lesson.next_event()
    lesson.next_event()
    v = lesson.answer("100")
    assert v.result == "guided" and lesson.waiting is None
    assert sorted(cp.key for cp in lesson.stops.values()) == ["answer", "unit", "units"]
    keys = []
    while (ev := _until_ask(lesson)) is not None:
        keys.append(ev["key"])
        lesson.answer(str(int(lesson.waiting.expected)))
    assert keys == ["units", "unit", "answer"] and lesson.score["challenge"] == "missed"


def test_key_checkpoints_only_unit_and_answer():
    lesson = Lesson(MARBLES, pacing={"mode": "guided", "speed": 1.0, "checkpoints": "key"})
    assert sorted(cp.key for cp in lesson.stops.values()) == ["answer", "unit"]
    cakes = Lesson(CAKES, pacing={"mode": "guided", "speed": 1.0, "checkpoints": "key"})
    assert sorted(cp.key for cp in cakes.stops.values()) == ["answer", "unit"]
    assert len(Lesson(CAKES).stops) == 4  # anonymous: unchanged


def test_early_final_answer_switches_to_recap_with_pacing_only():
    paced = Lesson(MARBLES, pacing={"mode": "guided", "speed": 1.0, "checkpoints": "all"})
    assert _until_ask(paced)["key"] == "units"
    v = paced.answer("is it 90?")
    assert v.result == "correct" and paced.next_event()["type"] == "recap"
    assert paced.score == {"units": "early", "answer": "early"}
    anon = Lesson(MARBLES)
    _until_ask(anon)
    assert anon.answer("90").result == "try_again"  # anonymous lessons behave exactly as before


def test_seconds_are_recorded_per_checkpoint():
    lesson = Lesson(MARBLES, pacing={"mode": "guided", "speed": 1.0, "checkpoints": "key"})
    _until_ask(lesson)
    lesson.answer("30")
    (key, outcome, seconds), = lesson.results()
    assert key == "unit" and outcome == "first try" and seconds is not None and seconds >= 0


# ---------------------------------------------------------------- over the socket
def _child_client(level="P4"):
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": "Mum"}).status_code == 200
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    kid = c.post("/api/children", json={"nickname": "Kai", "level": level, "avatar": "🐯"}).json()["child"]
    assert c.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 200
    return c, kid


def _play(client, spec, answer_for):
    """Run a lesson; answer_for(ask_event, lesson_event) returns the text to send. Returns all events."""
    events = []
    with client.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": spec})
        first = ws.receive_json()
        events.append(first)
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


RIGHT = {"units": "4", "unit": "30", "answer": "90", "challenge": "90"}


def test_ws_challenge_right_then_recap(monkeypatch):
    c, kid = _child_client(level="P6")  # marbles (P3-P4) is below P6: challenge first
    events = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])
    lesson = events[0]
    assert lesson["type"] == "lesson" and lesson["pacing"]["mode"] == "challenge_first" and lesson["checkpoints"] == 3
    types = [e["type"] for e in events]
    assert types.count("ask") == 1 and types.count("recap") == 1 and "step" in types
    verdict = next(e for e in events if e["type"] == "verdict")
    assert verdict["result"] == "correct" and verdict["recap"] is True
    recap = next(e for e in events if e["type"] == "recap")
    assert recap["audio"] and recap["steps"][-1]["step"]["do"] == "answer"
    end = events[-1]
    assert end["score"] == {"challenge": "first try"} and end["next_pacing"]["mode"] == "challenge_first"
    rows = db.attempts(kid["id"])
    assert [(r["checkpoint_key"], r["outcome"]) for r in rows] == [("challenge", "first try")]
    assert rows[0]["heuristic"] == "part_whole" and rows[0]["level"] == "P3-P4"


def test_ws_challenge_wrong_then_guided(monkeypatch):
    c, kid = _child_client(level="P6")
    events = _play(c, MARBLES, lambda ev: "100" if ev["key"] == "challenge" else RIGHT[ev["key"]])
    asks = [e["key"] for e in events if e["type"] == "ask"]
    assert asks == ["challenge", "units", "unit", "answer"] and "recap" not in [e["type"] for e in events]
    guided = next(e for e in events if e["type"] == "verdict")
    assert guided["result"] == "guided" and guided["checkpoints"] == 3 and not guided["waiting"]
    assert events[-1]["score"]["challenge"] == "missed"
    assert {r["checkpoint_key"] for r in db.attempts(kid["id"])} == {"challenge", "units", "unit", "answer"}


def test_ws_pacing_changes_after_a_good_lesson():
    c, kid = _child_client(level="P4")
    first = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])
    assert first[0]["pacing"]["mode"] == "guided" and first[0]["pacing"]["checkpoints"] == "all"
    assert first[-1]["next_pacing"]["mode"] == "challenge_first"
    second = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])
    assert second[0]["pacing"]["mode"] == "challenge_first" and second[0]["pacing"]["speed"] >= 1.25
    progress = c.get("/api/child/progress").json()["progress"]
    assert progress[0]["heuristic"] == "part_whole" and progress[0]["n"] == 4


def test_ws_struggling_child_stays_guided_with_hints():
    c, kid = _child_client(level="P4")
    events = _play(c, MARBLES, lambda ev: "7")  # wrong every time: hints, then shown
    assert [e["result"] for e in events if e["type"] == "verdict"].count("revealed") == 3
    assert any(e["type"] == "verdict" and e["result"] == "try_again" for e in events)
    assert events[-1]["next_pacing"] == {**events[-1]["next_pacing"], "mode": "guided", "speed": 1.0, "checkpoints": "all"}


def test_ws_anonymous_lesson_is_unchanged_and_stores_nothing():
    events = _play(TestClient(app), MARBLES, lambda ev: RIGHT[ev["key"]])
    assert "pacing" not in events[0] and "next_pacing" not in events[-1]
    assert events[-1]["score"] == {"units": "first try", "unit": "first try", "answer": "first try"}
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
