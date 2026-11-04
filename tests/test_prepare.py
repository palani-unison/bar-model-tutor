"""Prepare: beat 1 (the question, its key facts, an understanding prompt) taught while the planner works.

Key facts are found by code and must be exact substrings of the question; the socket flow is tested with
plan_problem mocked (a fast plan, a slow one, a decline), and a lesson started from a saved spec is unchanged."""
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import prepare
from agent.lesson import Lesson
from agent.plan import plain_question
from agent.verify_v2 import verify_spec_v2
from app import lesson_ws
from app import main as app_main
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
STICKERS = json.loads((ROOT / "specs" / "examples" / "v2" / "how_many_more-stickers.json").read_text(encoding="utf-8"))
FAMILIES = json.loads((ROOT / "evals" / "dataset" / "v2-families.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)


# ---------------------------------------------------------------- key facts (code, not a model)
@pytest.mark.parametrize("item", [it for it in FAMILIES if it["level"] in ("P4", "P5", "P6")], ids=lambda it: it["id"])
def test_every_fact_is_an_exact_substring_found_where_the_board_looks(item):
    q = plain_question(item["question"])
    facts = prepare.key_facts(q)
    assert 1 <= len(facts) <= prepare.MAX_FACTS
    for f in facts:
        assert f and f == f.strip() and f in q
        assert q.lower().find(f.lower()) == q.find(f)  # the board highlights the first match: it is this one
    for s in prepare.read_steps(q):
        if s["do"] == "highlight":
            assert s["text"] in q


@pytest.mark.parametrize("question, expected", [
    ("Farah folded 5 times as many paper cranes as Hui Min. They folded 240 paper cranes altogether. "
     "How many more paper cranes did Farah fold than Hui Min?", ["5 times as many paper cranes", "240 paper cranes altogether"]),
    ("Mr Lim bought 3 kg of prawns at $18.50 per kg and 4 kg of fish at $9.80 per kg. How much more did he pay?",
     ["3 kg of prawns", "$18.50 per kg", "4 kg of fish", "$9.80 per kg"]),
    ("Ken had $84 more than Lucy. After Ken spent 3/5 of his money and Lucy spent 1/4 of her money, they had the same "
     "amount of money left. How much money did Lucy have at first?",
     ["$84 more than Lucy", "3/5 of his money", "1/4 of her money", "the same amount of money"]),
    ("The ratio of Ann's money to Bee Lian's money was 5 : 3. After Ann gave Bee Lian $20, the ratio became 3 : 5. "
     "How much money did Ann have at first?", ["5 : 3", "$20", "3 : 5"]),
    ("There are 800 pupils in a school. 35% of them are boys. How many girls are there?", ["800 pupils", "35% of them"]),
    ("Aunty Rosna had some eggs. She used 18 eggs to make kueh. In the end, she had 75 eggs. How many eggs did she "
     "have at first?", ["18 eggs", "In the end", "75 eggs"]),
])
def test_key_facts_examples(question, expected):
    assert prepare.key_facts(question) == expected


def test_read_steps_write_the_question_then_highlight_facts_then_what_to_find():
    q = STICKERS["question"]
    steps = prepare.read_steps(q)
    assert steps[0] == {"do": "write_question", "say": q}
    assert [s["text"] for s in steps[1:]] == ["4 times as many stickers", "175 stickers altogether",
                                               "How many more stickers does Kavya have than Lily?"]
    assert "maths" not in json.dumps(steps) and all(s["say"] for s in steps)


def test_skip_read():
    lesson = Lesson(STICKERS)
    assert lesson.skip_read(False) == 1 and lesson.steps[lesson.position]["do"] == "highlight"
    lesson = Lesson(STICKERS)
    assert lesson.skip_read(True) == 2 and lesson.steps[lesson.position]["do"] == "draw_bar"


# ---------------------------------------------------------------- the socket, with the planner mocked
def _plan(monkeypatch, gate: threading.Event | None = None, fail: str | None = None):
    calls = []

    def plan_problem(problem):
        calls.append(problem)
        if gate is not None:
            assert gate.wait(10)
        if fail:
            raise ValueError(fail)
        return STICKERS, verify_spec_v2(STICKERS), ["attempt 1 v2 -> ok"]

    monkeypatch.setattr(app_main, "plan_problem", plan_problem)
    return calls


def test_fast_plan_lands_after_the_current_line(monkeypatch):
    gate = threading.Event()
    calls = _plan(monkeypatch, gate)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": STICKERS["question"]})
        ev = ws.receive_json()
        assert ev["type"] == "prepare" and ev["question"] == STICKERS["question"]
        first = ws.receive_json()
        assert first["type"] == "step" and first["prep"] and first["step"]["do"] == "write_question" and first["audio"]
        assert calls and calls[0]["question"] == STICKERS["question"]  # planning started before the board was drawn
        gate.set()  # the plan is in while the question is still being read
        time.sleep(0.3)
        ws.send_json({"type": "next"})  # the line has played
        plan = ws.receive_json()
        assert plan["type"] == "plan" and plan["spec"] == STICKERS and plan["verified"] and plan["solved"]
        assert set(plan) >= {"spec", "solved", "check", "planner", "attempts", "answer", "timings"}
        lesson = ws.receive_json()
        assert lesson["type"] == "lesson" and lesson["prepared"]
        step = ws.receive_json()
        # No key fact was shown yet, so the spec's own highlight is taught; the question is not written twice.
        assert step["type"] == "step" and step["index"] == 1 and step["step"]["do"] == "highlight"


def test_slow_plan_fills_the_wait_then_starts_at_beat_two(monkeypatch):
    monkeypatch.setattr(lesson_ws, "PROMPT_WAIT_S", 30)
    gate = threading.Event()
    _plan(monkeypatch, gate)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": STICKERS["question"]})
        assert ws.receive_json()["type"] == "prepare"
        steps = []
        while True:
            ev = ws.receive_json()
            if ev["type"] == "ask":
                break
            steps.append(ev["step"])
            ws.send_json({"type": "next"})
        assert [s["do"] for s in steps] == ["write_question", "highlight", "highlight", "highlight"]
        assert all(s.get("text", STICKERS["question"]) in STICKERS["question"] for s in steps)
        assert ev["key"] == "prepare"
        ws.send_json({"type": "text", "text": "how many more stickers Kavya has"})
        verdict = ws.receive_json()
        assert verdict["type"] == "verdict" and verdict["result"] == "noted" and not verdict["waiting"]
        assert verdict["say"] == prepare.THANKS  # thanked, never judged
        fillers = []
        for _ in range(3):
            ws.send_json({"type": "next"})
            ev = ws.receive_json()
            assert ev["type"] == "tutor" and not ev["waiting"]
            fillers.append(ev["say"])
        assert "175 stickers altogether" in fillers[0] and fillers[1:] == [prepare.CHECKING, prepare.PICTURE]
        ws.send_json({"type": "next"})  # nothing left to say: idle until the plan lands
        gate.set()
        assert ws.receive_json()["type"] == "plan"
        lesson = ws.receive_json()
        assert lesson["type"] == "lesson" and lesson["prepared"]
        step = ws.receive_json()
        assert step["step"]["do"] == "draw_bar"  # key facts were shown: straight into beat 2


def test_plan_lands_while_the_prompt_is_open(monkeypatch):
    monkeypatch.setattr(lesson_ws, "PROMPT_WAIT_S", 30)
    gate = threading.Event()
    _plan(monkeypatch, gate)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": STICKERS["question"]})
        assert ws.receive_json()["type"] == "prepare"
        while ws.receive_json()["type"] != "ask":
            ws.send_json({"type": "next"})
        gate.set()
        assert ws.receive_json()["type"] == "plan"
        assert ws.receive_json()["type"] == "lesson"
        assert ws.receive_json()["step"]["do"] == "draw_bar"


def test_failed_plan_declines_gracefully_after_the_current_line(monkeypatch):
    msg = "Sorry, I could not check my working on this problem, so I won't show an answer I'm not sure of."
    gate = threading.Event()
    _plan(monkeypatch, gate, fail=msg)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": STICKERS["question"]})
        assert ws.receive_json()["type"] == "prepare"
        assert ws.receive_json()["step"]["do"] == "write_question"
        gate.set()
        time.sleep(0.3)
        ws.send_json({"type": "next"})
        ev = ws.receive_json()
        assert ev["type"] == "plan_failed" and ev["say"] == msg and ev["audio"]
        assert "spec" not in ev and "answer" not in ev


def test_question_refused_outright_is_declined_before_reading(monkeypatch):
    q = "Look at the figure below. The shaded part is 3/8 of the rectangle. What fraction is not shaded?"
    with TestClient(app).websocket_connect("/ws/lesson") as ws:  # the real plan_problem: it refuses before any model call
        ws.send_json({"type": "prepare", "question": q})
        assert ws.receive_json()["type"] == "prepare"
        ev = ws.receive_json()
        assert ev["type"] == "plan_failed" and "figure" in ev["say"]


def test_prepare_needs_a_real_question(monkeypatch):
    calls = _plan(monkeypatch)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": "hi"})
        assert ws.receive_json()["type"] == "error"
    assert not calls


def test_photo_problem_is_planned_as_extracted(monkeypatch):
    calls = _plan(monkeypatch)
    problem = {"question": STICKERS["question"], "level": "P4", "source": "vision", "numbers": [{"value": 4}]}
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "problem": problem, "timings": {"read_s": 2.1}})
        assert ws.receive_json()["question"] == STICKERS["question"]
        ws.receive_json()
    assert calls[0] == problem


def test_start_from_a_saved_spec_does_not_prepare(monkeypatch):
    calls = _plan(monkeypatch)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": STICKERS})
        lesson = ws.receive_json()
        assert lesson["type"] == "lesson" and "prepared" not in lesson
        step = ws.receive_json()
        assert step["index"] == 0 and step["step"]["do"] == "write_question" and "prep" not in step
    assert not calls


def test_extract_endpoint_reads_without_planning(monkeypatch):
    calls = _plan(monkeypatch)
    monkeypatch.setattr(app_main, "extract_problem", lambda data, ct, name, qno: {"question": f"Q{qno} " + STICKERS["question"]})
    r = TestClient(app).post("/api/extract", files={"file": ("p.jpg", b"\xff\xd8\xff", "image/jpeg")},
                             data={"question_number": "12."})
    assert r.status_code == 200 and r.json()["problem"]["question"].startswith("Q12 ") and "read_s" in r.json()["timings"]
    assert not calls
