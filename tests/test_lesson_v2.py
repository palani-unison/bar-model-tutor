"""Live lessons on BarSpec v2 specs: checkpoints, the WebSocket lesson, the tutor's guard and /api/example."""
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.lesson import Lesson, checkpoints, numbers_in, op_line, shows
from agent.spec_v2 import solve_spec
from agent.tutor import _context, allowed_numbers, check_reply
from agent.verify import _close
from app import lesson_ws
from app.main import app

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "specs" / "examples" / "v2").glob("*.json"))


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _named(name):
    return next(_load(p) for p in EXAMPLES if p.stem == name)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    return TestClient(app)


# ---------------------------------------------------------------- checkpoints
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_checkpoints_are_few_deterministic_and_have_stronger_second_hints(path):
    spec = _load(path)
    first, again = checkpoints(spec), checkpoints(copy.deepcopy(spec))
    assert 1 <= len(first) <= 4
    assert [(at, c.key, c.ask, c.expected, c.hints, c.reveal) for at, c in first.items()] == \
           [(at, c.key, c.ask, c.expected, c.hints, c.reveal) for at, c in again.items()]
    assert len({c.key for c in first.values()}) == len(first)
    for cp in first.values():
        assert len(cp.hints) == 2 and cp.hints[0] != cp.hints[1]
        assert any(_close(n, cp.expected) for n in numbers_in(cp.reveal))  # the reveal states the number
        if cp.key in ("unit", "answer"):  # (a sum's operands may equal its result: "4 take away 2")
            assert not any(_close(n, cp.expected) for n in numbers_in(cp.ask))  # the question doesn't


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_no_checkpoint_value_is_shown_before_it_is_asked(path):
    spec = _load(path)
    lesson = Lesson(spec)
    given = numbers_in(spec["question"])
    shown, asked = [], 0
    while not lesson.done:
        ev = lesson.next_event()
        if ev["type"] == "step":
            shown.append(ev["step"])
            continue
        asked += 1
        value = lesson.waiting.expected
        nxt = spec["timeline"][ev["index"]]
        said = any(_close(value, n) for n in numbers_in(nxt.get("text", "") + " " + nxt.get("say", "")))
        assert shows(nxt, value) or said, "it stops just before the step that shows or says it"
        assert not any(shows(s, value) for s in shown)
        if not any(_close(value, g) for g in given):  # a number the question gives may already be said
            for s in shown:
                assert not any(_close(value, n) for n in numbers_in(s.get("text", "") + " " + s.get("say", ""))), s
        lesson.answer(f"{value:g}")
    assert asked == len(checkpoints(spec))


def test_golden_checkpoints():
    cps = lambda name: [(c.key, c.expected) for c in checkpoints(_named(name)).values()]  # noqa: E731
    assert cps("beads-ratio-change") == [("unit", 6), ("answer", 18)]
    assert cps("remainder-money") == [("unit", 30), ("answer", 150)]
    assert cps("stamps-transfer") == [("unit", 20)]  # 1 unit is the answer: asked once
    assert cps("sweets-excess-shortage") == [("step1", 18), ("step2", 3), ("answer", 6)]
    sweets = list(checkpoints(_named("sweets-excess-shortage")).values())
    assert sweets[0].ask == "To find the difference in shortage, what is 24 take away 6?"
    assert sweets[2].ask == "Now you try the last step. How many pupils are there?"
    assert sweets[2].hints[1] == "Try 18 divided by 3."
    beads = list(checkpoints(_named("beads-ratio-change")).values())
    assert beads[0].hints == ["4 units are worth 24. How can we share 24 into 4 equal parts?", "Try 24 divided by 4."]


def test_at_most_four_checkpoints_keeping_unit_and_answer():
    spec = _named("remainder-money")
    at = next(i for i, s in enumerate(spec["timeline"]) if s["do"] == "pause")
    spec["timeline"][at + 1:at + 1] = [{"do": "write", "text": f"{n} + 1 = {n + 1}", "say": f"{n} plus 1."} for n in (6, 8, 10, 12)]
    cps = list(checkpoints(spec).values())
    assert len(cps) == 4
    assert [c.key for c in cps][-2:] == ["unit", "answer"]


def test_answer_is_not_asked_at_an_earlier_line_with_the_same_number():
    spec = _named("remainder-money")
    at = next(i for i, s in enumerate(spec["timeline"]) if s["do"] == "pause")
    spec["timeline"].insert(at + 1, {"do": "write", "text": "Check: 100 + 50 = 150", "say": "A check."})
    cps = checkpoints(spec)
    answer_at = next(i for i, c in cps.items() if c.key == "answer")
    assert spec["timeline"][answer_at]["text"].startswith("5 units")


def test_op_line_reads_working_arithmetic():
    op = op_line("1 unit = 24 ÷ 4 = 6")
    assert op.lhs == "1 unit" and op.result == 6 and op.words == "24 divided by 4"
    assert op_line("3 units = 3 x 6 = 18 beads").words == "3 times 6"
    assert op_line("5 × $30 = $150").result == 150
    assert op_line("4 units = 24") is None  # no arithmetic to ask about
    assert op_line("1 unit = 24 ÷ 4 = 7") is None  # wrong sums are never asked
    assert op_line("Difference in shortage = 24 - 6 = 18").words == "24 take away 6"


# ---------------------------------------------------------------- full lesson over the WebSocket
CHILDREN = {
    "right": (lambda value: f"{value:g}", ["correct"], "first try"),
    "wrong": (lambda value: f"{value + 1:g}", ["try_again", "try_again", "revealed"], "shown"),
    "unsure": (lambda value: "I don't know", ["no_number", "no_number", "revealed"], "shown"),
}


@pytest.mark.parametrize("child", CHILDREN)
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_full_v2_lesson_over_websocket(client, path, child):
    spec = _load(path)
    say, verdicts, mark = CHILDREN[child]
    expected = {c.key: c.expected for c in checkpoints(spec).values()}
    shown = []
    with client.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": spec})
        ev = ws.receive_json()
        assert ev["type"] == "lesson" and ev["checkpoints"] == len(expected)
        assert ev["solved"] == json.loads(json.dumps(solve_spec(spec).as_json()))
        while True:
            ev = ws.receive_json()
            if ev["type"] == "step":
                shown.append(ev["step"])
            elif ev["type"] == "ask":
                value = expected[ev["key"]]
                assert not any(shows(s, value) for s in shown)
                for want in verdicts:
                    ws.send_json({"type": "text", "text": say(value)})
                    verdict = ws.receive_json()
                    assert verdict["type"] == "verdict" and verdict["result"] == want
                    assert verdict["waiting"] == (want != verdicts[-1])
                    if want == "revealed":
                        assert any(_close(n, value) for n in numbers_in(verdict["say"]))
            elif ev["type"] == "end":
                assert ev["score"] == {key: mark for key in expected}
                break
            ws.send_json({"type": "next"})
    # Every step is drawn, except "pause and think", which the checkpoints replace with real questions.
    assert shown == [s for s in spec["timeline"] if s["do"] != "pause"]


def test_unsolvable_or_mismatched_v2_spec_is_refused(client):
    spec = _named("stamps-transfer")
    for bad in (dict(spec, answer=21), dict(spec, relations=spec["relations"][:2])):
        with client.websocket_connect("/ws/lesson") as ws:
            ws.send_json({"type": "start", "spec": bad})
            assert ws.receive_json()["type"] == "error"
    bad = copy.deepcopy(spec)
    next(s for s in bad["timeline"] if s["do"] == "draw_bar")["bar"] = "nobody"
    with client.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": bad})
        assert ws.receive_json()["type"] == "error"


def test_routing_keywords_work_for_v2():
    spec = _named("beads-ratio-change")
    assert {"Amy", "Beth"} <= set(lesson_ws._keywords(spec))
    lesson = Lesson(spec)
    while lesson.next_event()["type"] == "step":
        pass
    assert lesson_ws.route(lesson, "is it 6?") == "answer"
    assert lesson_ws.route(lesson, "why is Amy's bar longer?") == "question"


# ---------------------------------------------------------------- the tutor
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_tutor_reply_cannot_give_away_a_pending_v2_answer(path):
    spec = _load(path)
    lesson = Lesson(spec)
    while lesson.next_event()["type"] == "step":
        pass
    given = numbers_in(spec["question"])
    pending = [p for p in lesson.pending_answers() if not any(_close(p, g) for g in given)]
    for value in pending:
        assert "gives away" in check_reply(f"I think it is {value:g}.", lesson)
    assert "not on the board" in check_reply("Each unit is 9137.", lesson)
    assert check_reply(f"Look at the question again: {given[0]:g}.", lesson) is None


def test_tutor_sees_unit_counts_but_not_unit_values():
    lesson = Lesson(_named("beads-ratio-change"))
    bars = _context(lesson)["bars"]
    assert [(b["id"], b["stage"], b["units"]) for b in bars] == [
        ("amy_b", "At first", 3), ("beth_b", "At first", 5), ("amy_a", "After", 7), ("beth_a", "After", 5)]
    assert bars[2]["segments"][1] == {"size": "4 units", "style": "added", "label": "+24"}
    assert "6" not in json.dumps(bars) and "18" not in json.dumps(bars)
    assert 4 not in allowed_numbers(lesson)  # Amy's "after" bar, with its 4 added units, isn't drawn yet
    while lesson.next_event()["type"] == "step":
        pass
    allowed = allowed_numbers(lesson)
    assert {3, 4, 5, 7, 24} <= allowed and not {6, 18} & allowed
    assert check_reply("Amy bought 24 beads, which is 4 units, so she has 7 units now.", lesson) is None


def test_tutor_only_counts_bars_already_drawn():
    lesson = Lesson(_named("units_and_value-cakes"))
    while lesson.next_event()["type"] == "step":
        pass
    assert lesson.waiting.expected == 8  # 2 cakes are 2 x 4 units; the 13-unit total bar comes later
    assert "gives away 8" in check_reply("The cakes make 8 units.", lesson)


def test_tutor_context_for_working_template_has_no_bars():
    lesson = Lesson(_named("sweets-excess-shortage"))
    assert _context(lesson)["bars"] == []
    assert check_reply("Giving 5 sweets each, she is short of 24 sweets.", lesson) is None


# ---------------------------------------------------------------- /api/example
def test_example_endpoint_serves_v2_with_solved_values(client):
    menu = client.get("/api/example").json()
    assert menu["spec"]["title"].startswith("Bar Model: Comparison")  # marbles stays the default
    names = [e["name"] for e in menu["examples"]]
    assert names[0] == "marbles" and {p.stem for p in EXAMPLES} <= set(names)
    for path in EXAMPLES:
        j = client.get(f"/api/example?name={path.stem}").json()
        assert j["verified"] and j["spec"]["version"] == 2
        assert j["solved"] == json.loads(json.dumps(solve_spec(_load(path)).as_json()))


@pytest.mark.parametrize("name", ["nope", "../marbles", "..%2F..%2F.env", "v2/stamps-transfer", "marbles.json", ""])
def test_example_endpoint_rejects_unknown_names(client, name):
    assert client.get(f"/api/example?name={name}").status_code == 404



def test_a_filled_cell_can_be_a_checkpoint():
    spec = _named("rate-tap-table")
    stops = checkpoints(spec)
    fills = [i for i, s in enumerate(spec["timeline"]) if s["do"] == "fill_cell"]
    assert set(fills) <= set(stops)
    first = stops[fills[0]]
    assert first.expected == 6 and "1 min" in first.ask and "18 divided by 3" in first.ask
    assert stops[fills[1]].key == "answer" and stops[fills[1]].expected == 60


def test_shows_counts_a_filled_cell():
    assert shows({"do": "fill_cell", "text": "10 × 6 = 60"}, 60)



def test_a_number_line_value_can_be_a_checkpoint():
    spec = _named("speed-two-parts-line")
    stops = checkpoints(spec)
    at = next(i for i, s in enumerate(spec["timeline"]) if s["do"] == "mark_point")
    assert stops[at].expected == 210 and stops[at].ask == "To fill in the number line at 3 h, what is 3 times 70?"
