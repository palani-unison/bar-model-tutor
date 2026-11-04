import copy
import json
from pathlib import Path

import jsonschema
import pytest

from agent.spec_v2 import SpecError, expr_numbers, parse_expr, solve_spec

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "specs" / "examples" / "v2").glob("*.json"))
SCHEMA = json.loads((ROOT / "specs" / "barspec.v2.schema.json").read_text(encoding="utf-8"))


def _load(name):
    return json.loads((ROOT / "specs" / "examples" / "v2" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_golden_examples_match_schema_and_solve(path):
    spec = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(spec, SCHEMA)
    assert solve_spec(spec).asked == pytest.approx(spec["answer"])


def test_bars_are_true_to_scale_values():
    sol = solve_spec(_load("stamps-transfer.json"))
    assert sol.units == {"u": 20}
    assert sol.segments["ali_a"] == [40, 20] and sol.segments["ben_a"] == [20, 20]


def test_relations_must_fix_the_answer():
    spec = _load("stamps-transfer.json")
    spec["relations"] = spec["relations"][:3]  # drop "they had the same number"
    with pytest.raises(SpecError, match="do not fix"):
        solve_spec(spec)


def test_contradictory_relations_fail():
    spec = _load("remainder-money.json")
    spec["relations"].append({"eq": "money = 100", "from": "x"})
    with pytest.raises(SpecError, match="contradict|no solution|negative"):
        solve_spec(spec)


def test_bars_must_add_up_to_their_quantity():
    spec = _load("stamps-transfer.json")
    spec["stages"][1]["bars"][1]["segments"][1]["style"] = "removed"  # Ben's +20 no longer counted
    with pytest.raises(SpecError, match="bars|ben_a|add up"):
        solve_spec(spec)


def test_units_must_be_consistent_across_bars():
    spec = _load("beads-ratio-change.json")
    spec["stages"][0]["bars"][1]["segments"][0]["expr"] = "4*u"  # Beth 4u but Amy 3u = 18 -> u = 6, 24 != 30
    with pytest.raises(SpecError):
        solve_spec(spec)


@pytest.mark.parametrize("bad", ["__import__('os')", "u ** 2", "len(u)", "u.real", "[u]", "lambda: 1", "1e999"])
def test_expression_parser_rejects_anything_but_arithmetic(bad):
    with pytest.raises(SpecError):
        parse_expr(bad, {})


def test_expr_numbers_reads_double_equals_relations():
    # parse_relation accepts "==", so the grounding check must see its numbers too.
    assert expr_numbers("x == 45") == [45] == expr_numbers("x = 45")
    assert expr_numbers("35% × x = 2/5*y") == [35, 100, 2, 5]


def test_percent_and_unicode_operators():
    import sympy as sp

    x = sp.Symbol("x")
    assert parse_expr("35% × x", {"x": x}) == sp.Rational(35, 100) * x
    assert parse_expr("60 ÷ 2 − 1", {}) == 29
