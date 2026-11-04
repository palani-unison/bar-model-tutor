"""Golden v2 specs (specs/examples/v2) and the public v2 family eval set (evals/dataset/v2-families.json)."""
import json
import re
from pathlib import Path

import jsonschema
import pytest

from agent.spec_v2 import bars_of, solve_spec

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "specs" / "examples" / "v2").glob("*.json"))
SCHEMA = json.loads((ROOT / "specs" / "barspec.v2.schema.json").read_text(encoding="utf-8"))
FAMILIES_PATH = ROOT / "evals" / "dataset" / "v2-families.json"
FAMILIES = {
    "how_many_more", "multi_step", "before_after", "equal_end", "constant_total", "constant_difference",
    "remainder", "fraction_of", "percentage", "percentage_change", "ratio_change", "units_and_value",
    "excess_shortage", "assumption", "average",
}


def _spec(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_validates_and_solves_to_its_answer(path):
    spec = _spec(path)
    jsonschema.validate(spec, SCHEMA)
    assert solve_spec(spec).asked == pytest.approx(spec["answer"])


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_example_timeline_basics(path):
    """The cheap timeline rules from docs/spec-v2.md §4-5 (verify_v2 checks the rest)."""
    spec = _spec(path)
    bars = {b["id"]: b for b in bars_of(spec)}
    stages = {s["id"] for s in spec["stages"]}
    timeline = spec["timeline"]
    assert sum(s["do"] == "pause" for s in timeline) >= 1
    assert sum(s["do"] == "answer" for s in timeline) == 1
    for step in timeline:
        if step["do"] != "write_question":
            assert len(step["say"].split()) <= 20, step["say"]
        if step["do"] == "highlight":
            assert step["text"] in spec["question"], step["text"]
        if step["do"] == "draw_stage":
            assert step["stage"] in stages
        for key in ("bar", "from", "to"):
            if key in step:
                assert step[key] in bars, step
        assert all(b in bars for b in step.get("bars", []))
        if "segments" in step:
            first, last = step["segments"]
            assert 0 <= first <= last < len(bars[step["bar"]]["segments"]), step
        if step["do"] == "move_part":
            assert bars[step["from"]]["segments"][step["from_segment"]].get("style") == "removed"
            assert bars[step["to"]]["segments"][step["to_segment"]].get("style") == "added"


def test_every_family_has_a_golden_example():
    assert FAMILIES <= {_spec(p)["heuristic"] for p in EXAMPLES}


def test_family_eval_items_are_well_formed():
    items = json.loads(FAMILIES_PATH.read_text(encoding="utf-8"))
    assert isinstance(items, list) and items
    assert FAMILIES <= set(SCHEMA["properties"]["heuristic"]["enum"])
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)), "ids must be unique"
    for item in items:
        for key in ("id", "heuristic", "problem_type", "level", "question", "answer"):
            assert key in item, (item.get("id"), key)
        assert item["heuristic"] in FAMILIES, item["id"]
        assert item["problem_type"] == item["heuristic"], item["id"]
        assert re.fullmatch(rf"{item['heuristic']}-\d+", item["id"]), item["id"]
        assert re.fullmatch(r"P[3-6]", item["level"]), item["id"]
        assert isinstance(item["question"], str) and item["question"].strip(), item["id"]
        assert isinstance(item["answer"], (int, float)) and not isinstance(item["answer"], bool), item["id"]
    assert {item["heuristic"] for item in items} == FAMILIES
