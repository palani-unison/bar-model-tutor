import json
from pathlib import Path

from agent.review import diagnose

MARBLES = json.loads((Path(__file__).resolve().parent.parent / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))


def _drawing(**over):
    d = {"legible": True, "bars": [{"label": "Ben", "units": 1}, {"label": "Ali", "units": 3}],
         "brace_numbers": [120], "question_mark_on": "Ali",
         "working": ["1 + 3 = 4 units", "4 units = 120", "1 unit = 120 ÷ 4 = 30", "3 units = 3 × 30 = 90"],
         "final_answer": 90}
    d.update(over)
    return d


def test_correct_model_is_all_correct():
    r = diagnose(MARBLES, _drawing())
    assert r["all_correct"] and r["first_mistake"] is None


def test_each_mistake_is_found_in_order():
    cases = {
        "bars": _drawing(bars=[{"label": "Ali", "units": 3}]),
        "units": _drawing(bars=[{"label": "Ben", "units": 1}, {"label": "Ali", "units": 4}]),
        "numbers": _drawing(brace_numbers=[]),
        "question": _drawing(question_mark_on="Ben"),
        "working": _drawing(working=["4 units = 120", "1 unit = 120 ÷ 4 = 40"]),
        "answer": _drawing(final_answer=80),
    }
    for step, drawing in cases.items():
        assert diagnose(MARBLES, drawing)["first_mistake"] == step, step


def test_a_scaled_model_is_still_correct():
    r = diagnose(MARBLES, _drawing(bars=[{"label": "Ben", "units": 2}, {"label": "Ali", "units": 6}]))
    assert r["checks"][1]["ok"] is True


def test_hints_never_give_the_answer():
    for drawing in [_drawing(final_answer=80), _drawing(bars=[{"label": "Ben", "units": 1}, {"label": "Ali", "units": 4}]),
                    _drawing(working=["1 unit = 120 ÷ 4 = 40"], final_answer=None)]:
        say = diagnose(MARBLES, drawing)["say"]
        assert "90" not in say and "30" not in say.replace("120", ""), say


def test_illegible_photo_asks_for_another():
    r = diagnose(MARBLES, {"legible": False, "bars": []})
    assert not r["legible"] and "photo" in r["say"]


def test_question_mark_on_a_similar_name_is_not_accepted():
    spec = json.loads(json.dumps(MARBLES))
    spec["model"]["bars"][0]["label"], spec["model"]["bars"][1]["label"] = "Pen", "Pencil"
    spec["model"]["asked"] = spec["model"]["bars"][1]["id"]
    drawing = _drawing(bars=[{"label": "Pen", "units": 1}, {"label": "Pencil", "units": 3}], question_mark_on="Pen")
    assert diagnose(spec, drawing)["first_mistake"] == "question"
