import copy
import json
from pathlib import Path

from agent.verify import verify_spec

ROOT = Path(__file__).resolve().parent.parent
MARBLES = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))


def test_marbles_passes():
    r = verify_spec(copy.deepcopy(MARBLES))
    assert r.ok, r.errors
    assert r.total_units == 4
    assert r.unit_value == 30


def test_wrong_answer_fails():
    spec = copy.deepcopy(MARBLES)
    spec["model"]["answer"] = 60
    r = verify_spec(spec)
    assert not r.ok
    assert any("answer mismatch" in e for e in r.errors)


def test_missing_pause_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"] = [s for s in spec["timeline"] if s["do"] != "pause"]
    r = verify_spec(spec)
    assert not r.ok
    assert any("pause" in e for e in r.errors)


def test_unknown_bar_in_timeline_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"][3] = {"do": "draw_bar", "id": "nobody", "say": "Draw a bar."}
    r = verify_spec(spec)
    assert not r.ok


def test_long_say_line_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"][1]["say"] = " ".join(["word"] * 25)
    r = verify_spec(spec)
    assert not r.ok
    assert any("20 words" in e for e in r.errors)


def _replace_step(spec, do, **fields):
    for step in spec["timeline"]:
        if step["do"] == do:
            step.update(fields)


def test_total_not_in_question_fails():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = spec["question"].replace("120", "200")
    r = verify_spec(spec)
    assert not r.ok
    assert any("not a number in the question" in e for e in r.errors)


def test_difference_must_match_the_bars():
    spec = copy.deepcopy(MARBLES)
    spec["problem_type"] = "comparison"
    spec["question"] = "Ali has 20 more marbles than Ben. Together they have 120 marbles. How many does Ali have?"
    r = verify_spec(spec)
    assert not r.ok
    assert any("'20 more'" in e and "bars give 60" in e for e in r.errors)


def test_wrong_multiple_fails():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = spec["question"].replace("3 times", "5 times")
    r = verify_spec(spec)
    assert not r.ok
    assert any("times" in e for e in r.errors)


def test_asked_must_match_final_question():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = spec["question"].replace("does Ali have", "does Ben have")
    r = verify_spec(spec)
    assert not r.ok
    assert any("model.asked" in e for e in r.errors)


def test_wrong_working_line_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"].insert(-1, {"do": "write", "text": "1 unit = 120 ÷ 4 = 55", "say": "One unit is worth this."})
    r = verify_spec(spec)
    assert not r.ok
    assert any("working" in e for e in r.errors)


def test_wrong_units_worth_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"].insert(-1, {"do": "write", "text": "4 units = 100", "say": "Four units are worth this."})
    r = verify_spec(spec)
    assert not r.ok


def test_answer_step_must_state_answer():
    spec = copy.deepcopy(MARBLES)
    _replace_step(spec, "answer", text="Ali has 7 marbles.")
    r = verify_spec(spec)
    assert not r.ok
    assert any("answer text" in e for e in r.errors)


def test_narration_cannot_invent_numbers():
    spec = copy.deepcopy(MARBLES)
    _replace_step(spec, "pause", say="Pause and think. Is it 77?")
    r = verify_spec(spec)
    assert not r.ok
    assert any("narration says 77" in e for e in r.errors)


def test_unit_label_must_be_unit_value():
    spec = copy.deepcopy(MARBLES)
    _replace_step(spec, "label_unit", text="35")
    assert not verify_spec(spec).ok


def test_uncheckable_type_is_not_verified():
    spec = copy.deepcopy(MARBLES)
    spec["problem_type"] = "before-after"
    r = verify_spec(spec)
    assert not r.ok
    assert any("cannot be verified" in e for e in r.errors)


def test_ratio_is_checked():
    spec = copy.deepcopy(MARBLES)
    spec["problem_type"] = "ratio"
    spec["question"] = "Ben and Ali share 120 marbles in the ratio 1 : 3. How many marbles does Ali have?"
    assert verify_spec(spec).ok
    spec["model"]["answer"] = 999
    assert not verify_spec(spec).ok
    spec = copy.deepcopy(spec)
    spec["model"]["answer"] = 90
    spec["question"] = spec["question"].replace("1 : 3", "2 : 3")
    assert any("ratio" in e for e in verify_spec(spec).errors)


def test_fractional_units_fail():
    spec = copy.deepcopy(MARBLES)
    spec["model"]["bars"][1]["units"] = 2.5
    r = verify_spec(spec)
    assert not r.ok
    assert any("whole number" in e for e in r.errors)


def test_undrawable_action_fails():
    spec = copy.deepcopy(MARBLES)
    spec["timeline"].insert(5, {"do": "cut_bar", "id": "ali", "say": "Cut the bar."})
    r = verify_spec(spec)
    assert not r.ok
    assert any("cannot be drawn" in e for e in r.errors)


def test_working_with_decimals_and_rounding_passes():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = "A dog is 9 times as heavy as a kitten. Together they weigh 22.5 kg. How heavy is the dog?"
    spec["model"]["bars"] = [{"id": "kitten", "label": "kitten", "units": 1}, {"id": "dog", "label": "dog", "units": 9}]
    spec["model"].update(total=22.5, asked="dog", answer=20.25)
    spec["timeline"] = [
        {"do": "write_question", "say": spec["question"]},
        {"do": "pause", "say": "Pause and think."},
        {"do": "write", "text": "1 + 9 = 10 units", "say": "That makes 10 units."},
        {"do": "write", "text": "10 units = 22.5 kg", "say": "10 units are 22.5 kg."},
        {"do": "write", "text": "1 unit = 22.5 ÷ 10 = 2.25 kg", "say": "1 unit is 2.25 kg."},
        {"do": "write", "text": "9 units = 9 × 2.25 = 20.25 kg", "say": "9 units are 20.25 kg."},
        {"do": "answer", "text": "The dog weighs 20.25 kg.", "say": "The dog weighs 20.25 kg."},
    ]
    r = verify_spec(spec)
    assert r.ok, r.errors


def test_simplified_ratio_passes():
    spec = copy.deepcopy(MARBLES)
    spec["problem_type"] = "ratio"
    spec["question"] = "Ben and Ali share 120 marbles in the ratio 2 : 6. How many marbles does Ali have?"
    r = verify_spec(spec)
    assert r.ok, r.errors


def test_long_question_may_be_read_aloud():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = "During the school holidays, " + spec["question"] + " Show all your working clearly in the space below."
    spec["timeline"][0]["say"] = spec["question"]
    r = verify_spec(spec)
    assert r.ok, r.errors


def _comparison(question, bars, asked, answer, timeline, **model):
    return {
        "title": "Bar Model: Comparison", "level": "P4", "problem_type": "comparison", "question": question,
        "model": {"bars": bars, "asked": asked, "answer": answer, **model},
        "timeline": [{"do": "write_question", "say": question}, {"do": "pause", "say": "Pause and think."}] + timeline,
    }


MORE_Q = "Ali has 20 more marbles than Ben. Together they have 100 marbles. How many marbles does Ali have?"
MORE_STEPS = [
    {"do": "write", "text": "2 units = 100 - 20 = 80", "say": "Take away the extra 20."},
    {"do": "write", "text": "1 unit = 80 ÷ 2 = 40", "say": "1 unit is 40."},
    {"do": "write", "text": "Ali = 40 + 20 = 60", "say": "Ali has 60."},
    {"do": "answer", "text": "Ali has 60 marbles.", "say": "So, Ali has 60 marbles."},
]


def test_more_than_with_total_passes():
    bars = [{"id": "ben", "label": "Ben", "units": 1}, {"id": "ali", "label": "Ali", "units": 1, "extra": 20}]
    r = verify_spec(_comparison(MORE_Q, bars, "ali", 60, MORE_STEPS, total=100))
    assert r.ok, r.errors
    assert r.unit_value == 40


def test_extra_on_the_wrong_bar_fails():
    bars = [{"id": "ben", "label": "Ben", "units": 1, "extra": 20}, {"id": "ali", "label": "Ali", "units": 1}]
    steps = [dict(s) for s in MORE_STEPS[:2]] + [{"do": "answer", "text": "Ali has 40 marbles.", "say": "So, Ali has 40 marbles."}]
    r = verify_spec(_comparison(MORE_Q, bars, "ali", 40, steps, total=100))
    assert not r.ok
    assert any("Ali minus Ben is 20" in e for e in r.errors)


def test_fewer_than_is_the_same_model():
    q = "Ben has 20 fewer marbles than Ali. Together they have 100 marbles. How many marbles does Ali have?"
    bars = [{"id": "ben", "label": "Ben", "units": 1}, {"id": "ali", "label": "Ali", "units": 1, "extra": 20}]
    r = verify_spec(_comparison(q, bars, "ali", 60, MORE_STEPS, total=100))
    assert r.ok, r.errors


def test_times_with_difference_and_no_total():
    q = "Ali has 3 times as many marbles as Ben. Ali has 40 more marbles than Ben. How many marbles does Ben have?"
    bars = [{"id": "ben", "label": "Ben", "units": 1}, {"id": "ali", "label": "Ali", "units": 3}]
    steps = [
        {"do": "bracket_difference", "text": "40", "say": "Ali has 40 more."},
        {"do": "write", "text": "3 - 1 = 2 units", "say": "The difference is 2 units."},
        {"do": "write", "text": "2 units = 40", "say": "2 units are 40."},
        {"do": "write", "text": "1 unit = 40 ÷ 2 = 20", "say": "1 unit is 20."},
        {"do": "answer", "text": "Ben has 20 marbles.", "say": "So, Ben has 20 marbles."},
    ]
    spec = _comparison(q, bars, "ben", 20, steps, difference={"more": "ali", "less": "ben", "value": 40})
    r = verify_spec(spec)
    assert r.ok, r.errors
    spec["model"]["difference"] = {"more": "ben", "less": "ali", "value": 40}
    assert not verify_spec(spec).ok


def test_question_asking_for_difference_is_refused():
    spec = copy.deepcopy(MARBLES)
    spec["question"] = spec["question"].replace("How many marbles does Ali have?", "How many more marbles does Ali have than Ben?")
    r = verify_spec(spec)
    assert not r.ok
    assert any("asks for a difference" in e for e in r.errors)


def _beads():
    return json.loads((Path(__file__).resolve().parent.parent / "specs" / "examples" / "beads-difference.json").read_text(encoding="utf-8"))


def test_bar_action_without_id_fails():
    # Seen live on Railway: label_unit with no id passed verify, then crashed the renderer.
    spec = _beads()
    assert verify_spec(spec).ok
    next(s for s in spec["timeline"] if s["do"] == "label_unit").pop("id")
    result = verify_spec(spec)
    assert not result.ok
    assert any("needs the id" in e for e in result.errors)


def test_layout_dry_run_catches_a_board_that_cannot_be_drawn():
    from renderer.render import layout_error

    spec = _beads()
    assert layout_error(spec) is None
    next(s for s in spec["timeline"] if s["do"] == "label_unit").pop("id")
    assert "KeyError" in layout_error(spec)
