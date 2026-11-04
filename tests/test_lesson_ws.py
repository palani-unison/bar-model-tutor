import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import lesson_ws
from app.main import app

MARBLES = json.loads((Path(__file__).resolve().parent.parent / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))


def _no_voice(monkeypatch):
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")


def test_full_lesson_over_websocket(monkeypatch):
    _no_voice(monkeypatch)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    answers = {"units": "4", "unit": "I think 25", "answer": "ninety"}
    seen = []
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": MARBLES})
        assert ws.receive_json()["type"] == "lesson"
        while True:
            ev = ws.receive_json()
            seen.append(ev["type"])
            assert ev.get("audio") or ev["type"] in ("verdict",) or not ev.get("say")
            if ev["type"] == "ask":
                ws.send_json({"type": "text", "text": answers[ev["key"]]})
                verdict = ws.receive_json()
                if ev["key"] == "unit":  # 25 is wrong: a hint, still waiting; then right
                    assert verdict["result"] == "try_again" and verdict["waiting"]
                    ws.send_json({"type": "text", "text": "30"})
                    verdict = ws.receive_json()
                assert verdict["result"] == "correct"
            elif ev["type"] == "end":
                assert ev["score"] == {"units": "first try", "unit": "with hints", "answer": "first try"}
                break
            ws.send_json({"type": "next"})
    assert seen.count("ask") == 3


def test_unverified_spec_is_refused(monkeypatch):
    _no_voice(monkeypatch)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    bad = json.loads(json.dumps(MARBLES))
    bad["model"]["answer"] = 999
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": bad})
        assert ws.receive_json()["type"] == "error"


def test_socket_needs_a_token_when_password_is_set(monkeypatch):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("PUBLIC_DEPLOY", "1")
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect("/ws/lesson?token=1.bad") as ws:
            ws.receive_json()
    assert lesson_ws.token_ok(lesson_ws.make_token())


def test_routing_sends_numbers_to_code_and_questions_to_tutor():
    from agent.lesson import Lesson

    lesson = Lesson(MARBLES)
    assert lesson_ws.route(lesson, "why 3 units?") == "question"  # not waiting yet
    while lesson.next_event()["type"] == "step":
        pass
    assert lesson_ws.route(lesson, "is it 4?") == "answer"
    assert lesson_ws.route(lesson, "I don't know") == "answer"
    assert lesson_ws.route(lesson, "why do we add them?") == "question"


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, event):
        self.sent.append(event)


def _waiting_session(monkeypatch):
    import asyncio

    from agent.lesson import Lesson

    _no_voice(monkeypatch)
    monkeypatch.setattr(lesson_ws, "STABLE_PARTIAL_S", 0.05)
    session = lesson_ws.LessonSession(_FakeWS())
    session.lesson = Lesson(MARBLES)
    while session.lesson.next_event()["type"] == "step":
        pass  # now waiting on "How many units?" (4)
    return session, asyncio


def test_stable_partial_is_checked_once(monkeypatch):
    session, asyncio = _waiting_session(monkeypatch)

    async def go():
        session._on_partial("I think it is")  # no number: no early check
        session._on_partial("I think it is four")
        await asyncio.sleep(0.15)
        session._on_partial("I think it is four units")  # same turn, later partial: no second timer
        await asyncio.sleep(0.15)
        await session._on_final("I think it is four units.")

    asyncio.run(go())
    verdicts = [e for e in session.ws.sent if e["type"] == "verdict"]
    assert [v["result"] for v in verdicts] == ["correct"]
    assert [e.get("how") for e in session.ws.sent if e["type"] == "heard"] == ["stable partial"]


def test_changing_partial_waits_and_final_corrects_a_hint(monkeypatch):
    session, asyncio = _waiting_session(monkeypatch)

    async def go():
        session._on_partial("three")
        await asyncio.sleep(0.15)  # stable "three": checked early, a hint
        session._on_partial("three no")
        await session._on_final("three no five")  # a different number for the same open question counts
        session._on_partial("fo")
        session._on_partial("four")
        await session._on_final("four")  # final before the partial was stable: checked once, from the final

    asyncio.run(go())
    assert [e["result"] for e in session.ws.sent if e["type"] == "verdict"] == ["try_again", "try_again", "correct"]


# ---------------------------------------------------------------- turn-taking: the child taps for each turn
def test_voice_turn_deadlines():
    turn = lesson_ws.VoiceTurn(10.0)
    assert turn.deadline() == 10.0 + lesson_ws.NO_SPEECH_S  # nothing said yet
    turn.heard_final("why do we", 11.0)
    turn.heard_partial("count all", 12.0)
    assert turn.text() == "why do we count all"
    assert turn.deadline() == 12.0 + lesson_ws.TURN_SILENCE_S and not turn.due(14.9) and turn.due(15.0)
    turn.stop(13.0)
    assert turn.deadline() == 13.0 + lesson_ws.STOP_GRACE_S
    assert lesson_ws.TURN_SILENCE_S == 3.0


def _turn_session(monkeypatch, silence=0.2):
    session, asyncio = _waiting_session(monkeypatch)
    monkeypatch.setattr(lesson_ws, "TURN_SILENCE_S", silence)
    monkeypatch.setattr(lesson_ws, "ack_audio", lambda: {})
    asked = []
    monkeypatch.setattr(lesson_ws, "answer_question", lambda lesson, text: asked.append(text) or {"say": "Good question."})
    return session, asyncio, asked


def _kinds(session):
    return [(e["type"], e.get("turn") or e.get("text") or e.get("result"))
            for e in session.ws.sent if e["type"] in ("mic", "heard", "verdict", "tutor")]


def test_a_question_with_pauses_is_one_turn_ended_by_silence(monkeypatch):
    session, asyncio, asked = _turn_session(monkeypatch)

    async def go():
        session._begin_turn()
        await session._on_final("why do we")  # the recogniser ends a sentence at a short pause: not the turn
        await asyncio.sleep(0.1)
        session._on_partial("count all")
        await asyncio.sleep(0.1)
        await session._on_final("count all the units?")
        assert asked == [] and session.turn is not None
        await asyncio.sleep(0.5)  # TURN_SILENCE_S (0.2 here) with no new words

    asyncio.run(go())
    assert asked == ["why do we count all the units?"]
    heard = [e for e in session.ws.sent if e["type"] == "heard" and e.get("final")]
    assert [(h["text"], h["how"]) for h in heard] == [("why do we count all the units?", "silence")]
    assert ("mic", "end") in _kinds(session) and session.muted and session.turn is None


def test_a_number_answer_ends_the_turn_at_once(monkeypatch):
    session, asyncio, asked = _turn_session(monkeypatch, silence=5)

    async def go():
        session._begin_turn()
        await session._on_final("four")

    asyncio.run(go())
    assert _kinds(session) == [("mic", "end"), ("heard", "four"), ("verdict", "correct")]


def test_tap_off_ends_the_turn_when_the_last_words_arrive(monkeypatch):
    session, asyncio, asked = _turn_session(monkeypatch, silence=5)

    async def go():
        session._begin_turn()
        session._on_partial("why is")
        await session.mic(False)  # tapped off mid-sentence
        assert session.turn is not None  # waits for the recogniser's last words
        await session._on_final("why is it like that")

    asyncio.run(go())
    assert asked == ["why is it like that"]
    assert [e for e in session.ws.sent if e["type"] == "mic"][0] == {"type": "mic", "on": False, "ack": True}


def test_tap_off_without_a_final_ends_after_the_grace_and_drops_the_late_final(monkeypatch):
    session, asyncio, asked = _turn_session(monkeypatch, silence=5)
    monkeypatch.setattr(lesson_ws, "STOP_GRACE_S", 0.1)

    async def go():
        session._begin_turn()
        session._on_partial("why is it")
        await session.mic(False)
        await asyncio.sleep(0.4)
        await session._on_final("why is it three")  # the same words, late: not a second turn

    asyncio.run(go())
    assert asked == ["why is it"]
    assert len([e for e in session.ws.sent if e["type"] == "heard"]) == 1


def test_saying_nothing_ends_the_turn_empty(monkeypatch):
    session, asyncio, asked = _turn_session(monkeypatch)
    monkeypatch.setattr(lesson_ws, "NO_SPEECH_S", 0.1)

    async def go():
        session._begin_turn()
        await asyncio.sleep(0.4)

    asyncio.run(go())
    assert _kinds(session) == [("mic", "empty")] and session.turn is None and asked == []
