"""Build it yourself: board checkpoints (agent/board_task.py), Lesson.answer_board and the "board" socket message."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import learner
from agent.board_task import board_checkpoints, parse_said
from agent.lesson import Lesson
from agent.spec_v2 import solve_spec
from app import access, lesson_ws
from app.main import app

V2 = Path(__file__).resolve().parent.parent / "specs" / "examples" / "v2"


def _spec(name: str) -> dict:
    return json.loads((V2 / f"{name}.json").read_text(encoding="utf-8"))


def _to_board(lesson: Lesson, key: str) -> dict:
    """Play the lesson until the board task `key` is asked, answering number checkpoints right on the way."""
    while True:
        ev = lesson.next_event()
        assert ev is not None, f"no {key} task"
        if ev["type"] == "ask":
            if ev["key"] == key:
                return ev
            assert lesson.waiting.kind == "number" or lesson.waiting.key != key
            if lesson.waiting.kind == "board":
                lesson.answer_board(lesson.waiting.expected if lesson.waiting.key != "mark"
                                    else {"bar": lesson.waiting.expected["bar"], "segment": lesson.waiting.expected["segments"][0]})
            else:
                lesson.answer(str(lesson.waiting.expected))


# ---------------------------------------------------------------- which tasks a spec gets
def test_comparison_gets_units_and_mark_before_the_draw_steps():
    spec = _spec("how_many_more-stickers")
    stops = board_checkpoints(spec, solve_spec(spec))
    build, mark = stops[2], stops[7]
    assert spec["timeline"][2]["do"] == "draw_bar" and spec["timeline"][7]["do"] == "question_mark"
    assert build.key == "build" and build.expected == {"lily_bar": 1, "kavya_bar": 4}  # from the segment exprs
    assert "Tap + and –, then tap Check." in build.ask
    assert mark.expected == {"bar": "kavya_bar", "segments": [1, 1]}  # the question_mark step's target


def test_part_whole_splits_one_bar_into_its_labelled_parts():
    spec = _spec("fraction_of-muffins")
    build = board_checkpoints(spec, solve_spec(spec))[2]
    assert build.expected == {"muffin_bar#0": 5, "muffin_bar#1": 3}
    assert build.task["parts"] and [r["label"] for r in build.task["rows"]] == ["sold", "left"]


def test_transfer_gets_a_move_task_from_move_part():
    spec = _spec("stamps-transfer")
    stops = board_checkpoints(spec, solve_spec(spec))
    assert stops[3].expected == {"ben_b": 1, "ali_b": 3}
    assert stops[11].key == "move" and stops[11].expected == {"from": "ali_a", "to": "ben_a"}
    assert {r["id"] for r in stops[11].task["rows"]} == {"ali_a", "ben_a"}  # only the stage the move is in


def test_no_clean_integer_units_means_no_units_task():
    spec = _spec("multi_step-cookies")  # the bar is "30" + "6*u": a plain amount, not whole units
    keys = {cp.key for cp in board_checkpoints(spec, solve_spec(spec)).values()}
    assert "build" not in keys
    half = _spec("how_many_more-stickers")
    sol = solve_spec(half)
    half["stages"][0]["bars"][1]["segments"] = [{"expr": "u/2"}]  # a planner draft: half a unit has no boxes
    assert "build" not in {cp.key for cp in board_checkpoints(half, sol).values()}
    table = _spec("rate-tap-table")  # no bars at all
    assert board_checkpoints(table, solve_spec(table)) == {}


def test_board_tasks_only_when_the_client_draws_them():
    spec = _spec("how_many_more-stickers")
    plain, built = Lesson(spec), Lesson(spec, board=True)
    assert all(cp.kind == "number" for cp in plain.stops.values())
    assert built.checkpoint_count == plain.checkpoint_count + 2
    assert {i: cp for i, cp in built.stops.items() if cp.kind == "number"}.keys() == plain.stops.keys()


def test_strong_learners_skip_board_tasks():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True, pacing={"mode": "guided", "checkpoints": "key"})
    assert all(cp.kind == "number" for cp in lesson.stops.values())


# ---------------------------------------------------------------- answer_board: the same ladder as a number
def test_right_first_try_and_the_draw_step_follows():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    ask = _to_board(lesson, "build")
    assert ask["board"]["op"] == "units" and [r["id"] for r in ask["board"]["rows"]] == ["lily_bar", "kavya_bar"]
    v = lesson.answer_board({"lily_bar": 1, "kavya_bar": 4})
    assert v.result == "correct" and v.say.startswith("Yes!") and v.board == {"lily_bar": 1, "kavya_bar": 4}
    assert lesson.score["build"] == "first try" and lesson.waiting is None
    nxt = lesson.next_event()
    assert nxt["type"] == "step" and nxt["step"]["do"] == "draw_bar"


def test_wrong_gives_a_hint_then_right_scores_with_hints():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    v = lesson.answer_board({"lily_bar": 1, "kavya_bar": 3})
    assert v.result == "try_again" and "Give Lily 1 unit" in v.say and lesson.waiting is not None
    v = lesson.answer_board({"lily_bar": 4, "kavya_bar": 1})
    assert v.result == "try_again" and "wrong way round" in v.say
    assert lesson.answer_board({"lily_bar": 1, "kavya_bar": 4}).result == "correct"
    assert lesson.score["build"] == "with hints"


def test_an_equivalent_ratio_is_right_and_names_the_simplest():
    lesson = Lesson(_spec("stamps-transfer"), board=True)
    _to_board(lesson, "build")
    v = lesson.answer_board({"ben_b": 2, "ali_b": 6})
    assert v.result == "correct" and "2 and 6 is the same as 1 and 3" in v.say and "simplest" in v.say
    assert lesson.score["build"] == "first try"


def test_two_misses_then_revealed_with_the_right_board():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    for _ in range(2):
        assert lesson.answer_board({"lily_bar": 2, "kavya_bar": 3}).result == "try_again"
    v = lesson.answer_board({"lily_bar": 2, "kavya_bar": 3})
    assert v.result == "revealed" and v.board == {"lily_bar": 1, "kavya_bar": 4} and "Watch me draw it" in v.say
    assert lesson.score["build"] == "shown"
    assert lesson.next_event()["step"]["do"] == "draw_bar"  # reveal = the teacher draws it correctly


def test_show_me_reveals_a_board_task():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    v = lesson.skip()
    assert v.result == "revealed" and v.board == {"lily_bar": 1, "kavya_bar": 4} and lesson.score["build"] == "shown"


def test_an_unusable_move_spends_no_hint():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    for bad in (None, {"lily_bar": 1}, {"lily_bar": 0, "kavya_bar": 4}, {"lily_bar": True, "kavya_bar": 4}, "4"):
        assert lesson.answer_board(bad).result == "show_board"
    assert lesson.hints_used == 0 and lesson.waiting is not None


def test_mark_and_move():
    lesson = Lesson(_spec("stamps-transfer"), board=True)
    _to_board(lesson, "move")
    v = lesson.answer_board({"from": "ben_a", "to": "ali_a"})
    assert v.result == "try_again" and "other way round" in v.say
    assert lesson.answer_board({"from": "ali_a", "to": "ben_a"}).result == "correct"
    assert lesson.next_event()["step"]["do"] == "move_part"
    _to_board(lesson, "mark")
    assert lesson.answer_board({"bar": "ali_b", "segment": 0}).result == "try_again"
    assert lesson.answer_board({"bar": "ben_b", "segment": 0}).result == "correct"
    assert lesson.score["move"] == "with hints" and lesson.score["mark"] == "with hints"


def test_same_difference_both_bars_get_the_years():
    lesson = Lesson(_spec("constant_difference-ages"), board=True)
    ask = _to_board(lesson, "grow")
    assert ask["board"]["op"] == "grow" and lesson.steps[ask["index"]]["do"] == "draw_stage"
    v = lesson.answer_board({"bars": ["son_n"]})
    assert v.result == "try_again" and "Does only Son get older?" in v.say
    assert sorted(parse_said(lesson.waiting, "add years to both")["bars"]) == ["dad_n", "son_n"]
    v = lesson.answer("add it to both of them")
    assert v.result == "correct" and "difference between them never changes" in v.say
    assert lesson.score["grow"] == "with hints"


def test_mark_on_the_right_bar_but_the_wrong_part():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "mark")
    assert "not that part" in lesson.answer_board({"bar": "kavya_bar", "segment": 0}).say
    assert lesson.answer_board({"bar": "kavya_bar", "segment": 1}).result == "correct"


def test_mark_rows_carry_unit_counts_for_the_dividers():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    ask = _to_board(lesson, "mark")
    kavya = next(r for r in ask["board"]["rows"] if r["id"] == "kavya_bar")
    assert [s["units"] for s in kavya["segments"]] == [1, 3]


def test_mark_on_the_whole_bar_brace():
    lesson = Lesson(_spec("constant_total-money"), board=True)  # "Nurul's money at first": the whole of a 2-part bar
    _to_board(lesson, "mark")
    assert lesson.answer_board({"bar": "nurul_b", "whole": True}).result == "correct"
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)  # only the "more" part
    _to_board(lesson, "mark")
    v = lesson.answer_board({"bar": "kavya_bar", "whole": True})
    assert v.result == "try_again" and "not the whole bar" in v.say
    assert lesson.answer_board({"bar": "kavya_bar", "segment": 1}).result == "correct"


def test_board_tasks_never_count_as_numbers_to_hide():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    assert all(isinstance(n, float) for n in lesson.pending_answers())


def test_board_outcomes_feed_the_learner_model():
    assert learner.OFFSETS["build"] == -25.0
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    lesson.answer_board({"lily_bar": 1, "kavya_bar": 4})
    assert ("build", "first try") in [(k, o) for k, o, _ in lesson.results()]


# ---------------------------------------------------------------- voice: a tiny grammar, never a model
def test_spoken_units_are_parsed_against_the_bar_labels():
    lesson = Lesson(_spec("how_many_more-stickers"), board=True)
    _to_board(lesson, "build")
    cp = lesson.waiting
    assert parse_said(cp, "Kavya has four units and Lily has one") == {"kavya_bar": 4, "lily_bar": 1}
    assert parse_said(cp, "Lily is a unit, Kavya's is 4") == {"lily_bar": 1, "kavya_bar": 4}
    assert parse_said(cp, "four") is None
    v = lesson.answer("I'm not sure")
    assert v.result == "show_board" and v.say == "Can you show me on the board?" and lesson.hints_used == 0
    v = lesson.answer("Kavya has four units and Lily has one unit")
    assert v.result == "correct" and v.board == {"kavya_bar": 4, "lily_bar": 1}


def test_spoken_mark_and_move():
    lesson = Lesson(_spec("stamps-transfer"), board=True)
    _to_board(lesson, "move")
    assert parse_said(lesson.waiting, "move one from Ali to Ben") == {"from": "ali_a", "to": "ben_a"}
    assert parse_said(lesson.waiting, "Ben got it from Ali") == {"from": "ben_a", "to": "ali_a"}
    assert parse_said(lesson.waiting, "to Ben from Ali") == {"from": "ali_a", "to": "ben_a"}
    lesson.answer("from Ali to Ben")
    _to_board(lesson, "mark")
    assert parse_said(lesson.waiting, "on Ben") is None  # Ben is in both stages: point at it instead
    lesson2 = Lesson(_spec("units_and_value-cakes"), board=True)
    _to_board(lesson2, "mark")
    assert lesson2.answer("the question mark goes on 1 cake").result == "correct"


# ---------------------------------------------------------------- the socket
def _no_voice(monkeypatch):
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")


def _run_to_board(ws, key):
    while True:
        ev = ws.receive_json()
        assert ev["type"] != "end", f"no {key} task"
        if ev["type"] == "ask" and ev["key"] == key:
            return ev
        if ev["type"] == "ask":
            ws.send_json({"type": "show_me"})
            assert ws.receive_json()["type"] == "verdict"
        ws.send_json({"type": "next"})


def test_board_message_over_the_socket(monkeypatch):
    _no_voice(monkeypatch)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": _spec("how_many_more-stickers"), "board": True})
        assert ws.receive_json()["checkpoints"] == 5
        ask = _run_to_board(ws, "build")
        assert ask["board"]["op"] == "units" and ask["audio"]
        ws.send_json({"type": "board", "op": "mark", "value": {"bar": "lily_bar", "segment": 0}})  # not this task's op
        ws.send_json({"type": "board", "op": "units", "value": {"lily_bar": 1, "kavya_bar": 2}})
        v = ws.receive_json()
        assert v["type"] == "verdict" and v["result"] == "try_again" and v["waiting"] and v["board"] == {"lily_bar": 1, "kavya_bar": 2}
        ws.send_json({"type": "board", "op": "units", "value": {"lily_bar": 1, "kavya_bar": 4}})
        v = ws.receive_json()
        assert v["result"] == "correct" and not v["waiting"]
        ws.send_json({"type": "next"})
        step = ws.receive_json()
        assert step["type"] == "step" and step["step"]["do"] == "draw_bar"
        ws.send_json({"type": "next"})
        _run_to_board(ws, "mark")
        ws.send_json({"type": "show_me"})
        v = ws.receive_json()
        assert v["result"] == "revealed" and v["board"] == {"bar": "kavya_bar", "segments": [1, 1]}
        while (ev := ws.receive_json() if ws.send_json({"type": "next"}) is None else None)["type"] != "end":
            if ev["type"] == "ask":
                ws.send_json({"type": "show_me"})
                ws.receive_json()
        assert ev["score"]["build"] == "with hints" and ev["score"]["mark"] == "shown"


def test_without_board_true_the_lesson_is_unchanged(monkeypatch):
    _no_voice(monkeypatch)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    with TestClient(app).websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": _spec("how_many_more-stickers")})
        assert ws.receive_json()["checkpoints"] == len(Lesson(_spec("how_many_more-stickers")).stops)


@pytest.fixture
def public_deploy(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")
    db._ready = None
    yield
    db._ready = None


def test_board_works_anonymously_on_an_example_and_bypasses_nothing(public_deploy, monkeypatch):
    _no_voice(monkeypatch)
    anon = TestClient(app)
    token = anon.get("/api/lesson/token").json()["token"]
    assert access.lesson_message_allowed(anon.cookies, "board", {}) is None  # gated by having a lesson, not by itself
    with anon.websocket_connect(f"/ws/lesson?token={token}") as ws:
        spec = _spec("how_many_more-stickers")
        ws.send_json({"type": "start", "spec": {**spec, "question": "Not one of the examples"}, "board": True})
        assert ws.receive_json()["sign_in"] is True
        ws.send_json({"type": "board", "op": "units", "value": {"lily_bar": 1, "kavya_bar": 4}})  # no lesson: dropped
        ws.send_json({"type": "start", "spec": spec, "board": True})
        assert ws.receive_json()["type"] == "lesson"  # the first reply after the dropped board message
        _run_to_board(ws, "build")
        ws.send_json({"type": "board", "op": "units", "value": {"lily_bar": 1, "kavya_bar": 4}})
        assert ws.receive_json()["result"] == "correct"
