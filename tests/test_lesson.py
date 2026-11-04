import json
from pathlib import Path

from agent.lesson import Lesson, checkpoints, numbers_in

EXAMPLES = Path(__file__).resolve().parent.parent / "specs" / "examples"


def _spec(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _run_to_question(lesson):
    shown = []
    while (ev := lesson.next_event()) and ev["type"] == "step":
        shown.append(ev["step"])
    return shown, ev


def test_numbers_in_digits_and_words():
    assert numbers_in("I think it's 4 units") == [4]
    assert numbers_in("ninety") == [90]
    assert numbers_in("twenty four beads") == [24]
    assert numbers_in("a hundred and twenty") == [120]
    assert numbers_in("$1,200") == [1200]
    assert numbers_in("three, no, four") == [3, 4]
    assert numbers_in("I don't know") == []


def test_marbles_checkpoints_ask_units_unit_and_answer():
    cps = checkpoints(_spec("marbles.json"))
    assert [(c.key, c.expected) for c in cps.values()] == [("units", 4), ("unit", 30), ("answer", 90)]


def test_answer_is_asked_before_any_line_shows_it():
    lesson = Lesson(_spec("marbles.json"))
    seen = []
    while not lesson.done:
        ev = lesson.next_event()
        if ev["type"] == "ask":
            if ev["key"] == "answer":
                assert not any("90" in (s.get("text", "") + s.get("say", "")) for s in seen)
            lesson.answer(str(int(lesson.waiting.expected)))
        else:
            seen.append(ev["step"])


def test_pause_step_is_replaced_by_a_real_question():
    shown, ev = _run_to_question(Lesson(_spec("marbles.json")))
    assert ev["type"] == "ask" and ev["key"] == "units"
    assert all(s["do"] != "pause" for s in shown)


def test_wrong_then_hints_then_reveal():
    lesson = Lesson(_spec("marbles.json"))
    _run_to_question(lesson)
    v1 = lesson.answer("3")
    assert v1.result == "try_again" and "3 is not quite" in v1.say
    v2 = lesson.answer("I don't know")
    assert v2.result == "no_number"
    v3 = lesson.answer("5")
    assert v3.result == "revealed" and "4 units" in v3.say
    assert lesson.score["units"] == "shown"
    assert lesson.next_event()["type"] == "step"  # the lesson moves on


def test_correct_on_first_try():
    lesson = Lesson(_spec("marbles.json"))
    _run_to_question(lesson)
    v = lesson.answer("four units")
    assert v.result == "correct"
    assert lesson.score["units"] == "first try"


def test_difference_problem_asks_difference_units_and_skips_duplicate_answer():
    cps = checkpoints(_spec("beads-difference.json"))
    # Tom is the 1-unit bar, so "what is 1 unit" already answers the question.
    assert [(c.key, c.expected) for c in cps.values()] == [("units", 2), ("unit", 24)]
    assert "more units does Mei have than Tom" in next(iter(cps.values())).ask


def test_pending_answers_lists_what_the_child_has_not_found():
    lesson = Lesson(_spec("marbles.json"))
    assert sorted(lesson.pending_answers()) == [4, 30, 90]


def test_tutor_reply_cannot_give_away_a_pending_answer():
    from agent.tutor import check_reply

    lesson = Lesson(_spec("marbles.json"))
    _run_to_question(lesson)  # waiting on "how many units altogether?" (4)
    assert "gives away 4" in check_reply("There are 4 units.", lesson)
    assert "gives away 90" in check_reply("Ali has 90 marbles.", lesson)
    assert "not on the board" in check_reply("Each unit is 37.", lesson)
    assert check_reply("Ali has 3 units and Ben has 1 unit. Together they have 120 marbles.", lesson) is None
