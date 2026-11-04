import copy
import json
import time
from pathlib import Path

import pytest

from agent.spec_v2 import Solution
from agent.verify import VerifyResult
from agent.verify_v2 import _check_board, question_numbers, solution_or_none, verify_spec_v2

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = sorted((ROOT / "specs" / "examples" / "v2").glob("*.json"))


def _load(name):
    return json.loads((ROOT / "specs" / "examples" / "v2" / f"{name}.json").read_text(encoding="utf-8"))


def _at(spec, do, nth=0):
    """Index of the nth timeline step that does `do`."""
    return [i for i, s in enumerate(spec["timeline"]) if s["do"] == do][nth]


def _fails(spec, *needles):
    r = verify_spec_v2(spec)
    assert not r.ok
    for needle in needles:
        assert any(needle in e for e in r.errors), r.errors
    return r


def _passes(spec):
    r = verify_spec_v2(spec)
    assert r.ok, r.errors
    return r


# --- Good specs pass ----------------------------------------------------------------------------

@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_golden_examples_pass(path):
    r = _passes(json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(r, VerifyResult)
    assert r.total_units is None
    assert str(r).startswith("OK")


@pytest.mark.parametrize("name, u", [("beads-ratio-change", 6), ("remainder-money", 30), ("stamps-transfer", 20),
                                     ("sweets-excess-shortage", None)])
def test_unit_value_is_the_solved_u(name, u):
    assert verify_spec_v2(_load(name)).unit_value == u


def test_verification_is_fast():
    specs = [json.loads(p.read_text(encoding="utf-8")) for p in EXAMPLES]
    verify_spec_v2(specs[0])  # first call loads the schema and warms SymPy
    for spec in specs:
        start = time.perf_counter()
        verify_spec_v2(spec)
        assert time.perf_counter() - start < 0.3


def test_solution_or_none():
    sol = solution_or_none(_load("stamps-transfer"))
    assert isinstance(sol, Solution) and sol.asked == 20
    spec = _load("stamps-transfer")
    spec["relations"] = spec["relations"][:3]
    assert solution_or_none(spec) is None
    assert solution_or_none({"version": 2}) is None


def _parts_spec():
    """Ratio change where both amounts change: units u before, parts p after (u = 14, p = 6)."""
    q = ("The ratio of Ali's stamps to Ben's stamps was 2 : 3. After Ali used 10 stamps and Ben used 30 stamps, "
         "the ratio became 3 : 2. How many stamps did Ben have at first?")
    return {
        "version": 2, "title": "Ratio change: both change", "level": "P6", "heuristic": "ratio_change",
        "template": "bar-stages", "question": q,
        "units": [{"name": "u", "meaning": "1 unit at first"}, {"name": "p", "meaning": "1 part after"}],
        "quantities": [{"id": "ali_before", "label": "Ali at first"}, {"id": "ben_before", "label": "Ben at first"},
                       {"id": "ali_after", "label": "Ali after"}, {"id": "ben_after", "label": "Ben after"}],
        "relations": [
            {"eq": "ali_before / ben_before = 2/3", "from": "The ratio of Ali's stamps to Ben's stamps was 2 : 3"},
            {"eq": "ali_after = ali_before - 10", "from": "Ali used 10 stamps"},
            {"eq": "ben_after = ben_before - 30", "from": "Ben used 30 stamps"},
            {"eq": "ali_after / ben_after = 3/2", "from": "the ratio became 3 : 2"}],
        "asked": {"expr": "ben_before", "label": "Ben's stamps at first"},
        "answer": 42,
        "stages": [
            {"id": "before", "label": "At first", "bars": [
                {"id": "ali_b", "quantity": "ali_before", "label": "Ali", "segments": [{"expr": "2*u"}]},
                {"id": "ben_b", "quantity": "ben_before", "label": "Ben", "segments": [{"expr": "3*u"}]}]},
            {"id": "after", "label": "After", "bars": [
                {"id": "ali_a", "quantity": "ali_after", "label": "Ali", "segments": [
                    {"expr": "3*p"}, {"expr": "10", "style": "removed", "label": "used 10"}]},
                {"id": "ben_a", "quantity": "ben_after", "label": "Ben", "segments": [
                    {"expr": "2*p"}, {"expr": "30", "style": "removed", "label": "used 30"}]}]}],
        "timeline": [
            {"do": "write_question", "say": q},
            {"do": "highlight", "text": "2 : 3", "say": "At first the ratio was 2 to 3."},
            {"do": "draw_stage", "stage": "before", "say": "Let's draw their stamps at first."},
            {"do": "draw_bar", "bar": "ali_b", "say": "Ali has 2 units."},
            {"do": "draw_bar", "bar": "ben_b", "say": "Ben has 3 units."},
            {"do": "highlight", "text": "the ratio became 3 : 2", "say": "Then the ratio became 3 to 2."},
            {"do": "draw_stage", "stage": "after", "say": "We use parts for the new ratio."},
            {"do": "draw_bar", "bar": "ali_a", "say": "Ali has 3 parts left after using 10 stamps."},
            {"do": "draw_bar", "bar": "ben_a", "say": "Ben has 2 parts left after using 30 stamps."},
            {"do": "question_mark", "bar": "ben_b", "say": "We want Ben's stamps at first."},
            {"do": "pause", "seconds": 2, "say": "Pause and think. What is 1 part worth?"},
            {"do": "write", "text": "3 parts = 18", "say": "Ali's 3 parts are 18 stamps."},
            {"do": "write", "text": "1 part = 18 ÷ 3 = 6", "say": "So 1 part is 6."},
            {"do": "write", "text": "2 parts = 2 × 6 = 12", "say": "Ben's 2 parts are 12 stamps."},
            {"do": "write", "text": "Ben at first = 12 + 30 = 42", "say": "Add back the 30 he used to get 42."},
            {"do": "write", "text": "3 units = 42", "say": "Ben's 3 units are 42, so 1 unit is 14."},
            {"do": "answer", "text": "Ben had 42 stamps at first.",
             "say": "So, Ben had 42 stamps at first. Well done!"},
        ],
    }


def _working_spec(question, heuristic, quantities, relations, asked, answer, steps):
    return {
        "version": 2, "title": "Working only", "level": "P5", "heuristic": heuristic, "template": "working",
        "question": question, "units": [], "quantities": [{"id": q, "label": q} for q in quantities],
        "relations": relations, "asked": {"expr": asked, "label": asked}, "answer": answer, "stages": [],
        "timeline": [{"do": "write_question", "say": question},
                     {"do": "pause", "seconds": 2, "say": "Pause and think."}] + steps,
    }


def _percent_spec():
    q = "Mrs Lim spent 35% of her money on a dress. She had $130 left. How much money did she have at first?"
    return _working_spec(q, "percentage", ["money", "dress", "left"], [
        {"eq": "dress = 35% * money", "from": "spent 35% of her money on a dress"},
        {"eq": "left = money - dress", "from": "She had $130 left"},
        {"eq": "left = 130", "from": "She had $130 left"}], "money", 200, [
        {"do": "highlight", "text": "35%", "say": "She spent 35% of her money."},
        {"do": "write", "text": "100% - 35% = 65%", "say": "100% take away 35% leaves 65%."},
        {"do": "write", "text": "65% = $130", "say": "65% of her money is $130."},
        {"do": "write", "text": "1% = 130 ÷ 65 = 2", "say": "So 1% is 130 divided by 65, which is 2."},
        {"do": "write", "text": "100% = 100 × 2 = $200", "say": "All her money is 100 times 2, which is $200."},
        {"do": "answer", "text": "She had $200 at first.", "say": "So, Mrs Lim had $200 at first. Well done!"}])


def test_parts_use_p_and_units_use_u():
    spec = _parts_spec()
    r = _passes(spec)
    assert r.unit_value == 14
    spec["timeline"][11]["text"] = "3 parts = 20"
    _fails(spec, 'timeline[11] working "3 parts = 20" is wrong: 3p is worth 18, not 20')


def test_percentages_and_money_pass():
    _passes(_percent_spec())


def test_thousands_commas_in_the_question():
    q = "A shop had 1,250 pens. It sold 2/5 of them. How many pens were left?"
    spec = _working_spec(q, "fraction_of", ["pens", "sold", "left"], [
        {"eq": "pens = 1250", "from": "A shop had 1,250 pens"},
        {"eq": "sold = 2/5*pens", "from": "It sold 2/5 of them"},
        {"eq": "left = pens - sold", "from": "How many pens were left"}], "left", 750, [
        {"do": "write", "text": "1 fifth = 1,250 ÷ 5 = 250", "say": "One fifth of 1,250 is 250."},
        {"do": "write", "text": "Pens left = 3 × 250 = 750", "say": "3 fifths are left: 3 times 250 is 750."},
        {"do": "answer", "text": "750 pens were left.", "say": "So, 750 pens were left. Well done!"}])
    _passes(spec)


def test_question_numbers_read_every_format():
    q = ("A shop had 1,250 pens at $4.50 each. It sold 35% of them, then 2/5 of the rest. The ratio of red to "
         "blue pens is 3 : 5. Ali has twice as many as Ben, half as many as Cai and three more than Dan.")
    found = question_numbers(q)
    for n in (1250, 4.5, 35, 0.35, 65, 2, 5, 0.4, 3, 8, 0.5):
        assert any(abs(n - f) < 1e-9 for f in found), n
    assert 1.5 in question_numbers("Ali has one and a half cakes.")
    assert 24 in question_numbers("There are twenty-four children.")


def test_unit_conversions_allow_their_factors():
    assert 1000 in question_numbers("A stall sold 20 ℓ of lemonade in 200 mℓ cups.")  # the script-l litre
    assert 100 in question_numbers("A 3 m ribbon is cut into 25 cm pieces.")
    assert 10 in question_numbers("A 4 cm line is split into 8 mm parts.")
    assert 1000 not in question_numbers("Ali has 5 apples and 3 pears.")
    assert 100 in question_numbers("Joan had some 20¢ coins and 50¢ coins.")


def test_clock_times_give_the_time_between_them():
    found = question_numbers("Machine A started at 8 a.m. Both machines stopped at 8.05 a.m.")
    assert 5 in found
    found = question_numbers("The show ran from 9.30 a.m. to 12 noon.")
    assert 150 in found and 2.5 in found


def test_spoken_chains_are_added_from_the_start():
    from agent.verify_v2 import _spoken_sum_problem

    assert _spoken_sum_problem("Harriet had 106, plus 52 plus 53, which makes 211 cards.") is None
    assert _spoken_sum_problem("106 plus 52 plus 53 is 200") == "106 plus 52 plus 53 is 211, not 200"
    assert _spoken_sum_problem("24 divided by 4, which is 7") == "24 divided by 4 is 6, not 7"


def test_implicit_counts_are_allowed_for_averages_only():
    q = "The average mass of Ali, Ben and Cai is 45 kg. Ali and Ben have a total mass of 80 kg. What is Cai's mass?"
    spec = _working_spec(q, "average", ["ali", "ben", "cai"], [
        {"eq": "ali + ben + cai = 3*45", "from": "The average mass of Ali, Ben and Cai is 45 kg"},
        {"eq": "ali + ben = 80", "from": "Ali and Ben have a total mass of 80 kg"}], "cai", 55, [
        {"do": "write", "text": "Total mass = 3 × 45 = 135", "say": "3 children averaging 45 kg make 135 kg."},
        {"do": "write", "text": "Cai = 135 - 80 = 55", "say": "Take away Ali and Ben's 80 kg."},
        {"do": "answer", "text": "Cai's mass is 55 kg.", "say": "So, Cai's mass is 55 kg. Well done!"}])
    _passes(spec)
    spec["heuristic"] = "multi_step"
    _fails(spec, "relations[0] 'ali + ben + cai = 3*45' uses 3, which is not a number in the question")


def test_from_quote_ignores_case_and_spacing():
    spec = _load("stamps-transfer")
    spec["relations"][1]["from"] = "ali GAVE  20 stamps   to ben."
    _passes(spec)


def test_question_say_and_highlight_text_are_not_number_checked():
    spec = _load("beads-ratio-change")
    spec["timeline"][0]["say"] += " Show 99 steps of working."
    _passes(spec)


def test_narration_may_use_unit_counts_their_sums_and_totals():
    spec = _load("beads-ratio-change")
    spec["timeline"][4]["say"] = "Beth has 5 units, so together they have 8 units."
    spec["timeline"][9]["say"] = "Beth still has 5 units, so now there are 12 units."
    spec["timeline"][13]["say"] = "At first they had 48 beads. We want Amy's beads."
    _passes(spec)


def test_failed_result_string_lists_errors():
    spec = _load("stamps-transfer")
    spec["answer"] = 25
    assert str(verify_spec_v2(spec)).startswith("FAILED: answer is 25")


# --- Mutations fail for the right reason --------------------------------------------------------

def test_wrong_answer_fails():
    spec = _load("stamps-transfer")
    spec["answer"] = 25
    _fails(spec, "answer is 25, but the relations give 20 for ben_before")


def test_from_quote_not_in_question_fails():
    spec = _load("stamps-transfer")
    spec["relations"][1]["from"] = "Ali gave 30 stamps to Ben"
    r = _fails(spec, "relations[1] quotes 'Ali gave 30 stamps to Ben', which is not in the question")
    assert len(r.errors) == 1


def test_invented_number_in_relation_fails():
    spec = _load("remainder-money")
    spec["relations"].append({"eq": "remainder = 90", "from": "the remainder"})  # true, but not in the question
    r = _fails(spec, "relations[5] 'remainder = 90' uses 90, which is not a number in the question")
    assert len(r.errors) == 1


def test_invented_number_hidden_behind_double_equals_fails():
    spec = _load("remainder-money")
    spec["relations"].append({"eq": "remainder == 90", "from": "the remainder"})
    _fails(spec, "uses 90, which is not a number in the question")


def test_invented_number_in_narration_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "pause")
    spec["timeline"][i]["say"] = "Pause and think. Is 1 unit worth 45?"
    r = _fails(spec, f"timeline[{i}] says 45, which is not in the question or the solved model")
    assert len(r.errors) == 1


def test_wrong_spoken_arithmetic_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "write", 1)
    spec["timeline"][i]["say"] = "So 1 unit is 24 divided by 4, which is 8."  # 8 is on the board, but wrong here
    _fails(spec, f"timeline[{i}] says it wrong: 24 divided by 4 is 6, not 8")


def test_spoken_arithmetic_may_round_to_the_places_it_shows():
    from agent.verify_v2 import _spoken_sum_problem

    assert _spoken_sum_problem("So 20 divided by 3, which is 6.67.") is None
    assert _spoken_sum_problem("1 plus 3 makes 4 units, and 1,250 divided by 5 is 250.") is None
    assert _spoken_sum_problem("3 times 6 is 19.") == "3 times 6 is 18, not 19"


def test_spoken_unit_count_must_be_on_the_board():
    spec = _load("beads-ratio-change")
    i = _at(spec, "draw_bar")
    spec["timeline"][i]["say"] = "Amy has 10 units."  # small, but no bar has 10 units
    _fails(spec, f"timeline[{i}] says '10 units', but no bar or working line shows that many")


def test_invented_number_in_segment_label_fails():
    spec = _load("beads-ratio-change")
    spec["stages"][1]["bars"][0]["segments"][1]["label"] = "+25"
    _fails(spec, "bar 'amy_a' segment 1 label '+25' shows 25")


def test_wrong_working_arithmetic_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "write", 1)
    spec["timeline"][i]["text"] = "1 unit = 24 ÷ 4 = 5"
    _fails(spec, f'timeline[{i}] working "1 unit = 24 ÷ 4 = 5" is wrong: 6 does not equal 5')


def test_units_working_uses_u():
    spec = _load("beads-ratio-change")
    i = _at(spec, "write")
    spec["timeline"][i]["text"] = "4 units = 30"
    _fails(spec, "is wrong: 4u is worth 24, not 30")


def test_working_operand_used_before_it_is_known_fails():
    spec = _load("sweets-excess-shortage")
    t = spec["timeline"]
    t[4], t[6] = t[6], t[4]  # "18 ÷ 3" now comes before the line that works out 18
    r = _fails(spec, 'timeline[4] working "Number of pupils = 18 ÷ 3 = 6" uses 18 before timeline[6] works it out')
    assert len(r.errors) == 1


def test_invented_working_number_fails():
    spec = _load("sweets-excess-shortage")
    spec["timeline"].insert(4, {"do": "write", "text": "Sweets given out = 45", "say": "Count the sweets given out."})
    _fails(spec, 'timeline[4] working "Sweets given out = 45" uses 45, which is not in the question')


def test_unknown_bar_id_fails():
    spec = _load("stamps-transfer")
    spec["timeline"][_at(spec, "draw_bar")]["bar"] = "nobody"
    _fails(spec, "draw_bar names bar 'nobody', which is not a bar in the stages", "bar 'ben_b' is never drawn")


def test_unknown_stage_id_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "draw_stage", 1)
    spec["timeline"][i]["stage"] = "middle"
    _fails(spec, f"timeline[{i}] draw_stage names stage 'middle'")


def test_segment_index_out_of_range_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "bracket")
    spec["timeline"][i]["segments"] = [1, 2]
    _fails(spec, f"timeline[{i}] bracket segments [1, 2] are out of range: bar 'amy_a' has segments 0 to 1")


def test_move_part_from_a_segment_that_is_not_removed_fails():
    spec = _load("stamps-transfer")
    i = _at(spec, "move_part")
    spec["timeline"][i]["from_segment"] = 0
    r = _fails(spec, f"timeline[{i}] move_part must start at a 'removed' segment, "
                     "but bar 'ali_a' segment 0 is 'normal'")
    assert len(r.errors) == 1


def test_move_part_to_a_segment_that_is_not_added_fails():
    spec = _load("stamps-transfer")
    spec["timeline"][_at(spec, "move_part")]["to_segment"] = 0
    _fails(spec, "move_part must end at an 'added' segment, but bar 'ben_a' segment 0 is 'normal'")


def test_bar_used_before_it_is_drawn_fails():
    spec = _load("beads-ratio-change")
    t = spec["timeline"]
    t[3], t[5] = t[5], t[3]  # divide Amy's bar before drawing it
    _fails(spec, "timeline[3] divide_units uses bar 'amy_b' before a draw_bar step draws it")


def test_missing_pause_fails():
    spec = _load("remainder-money")
    spec["timeline"] = [s for s in spec["timeline"] if s["do"] != "pause"]
    _fails(spec, "at least one 'pause' step")


def test_two_answer_steps_fail():
    spec = _load("remainder-money")
    spec["timeline"].append(copy.deepcopy(spec["timeline"][-1]))
    r = _fails(spec, "exactly one 'answer' step, found 2")
    assert len(r.errors) == 1


def test_say_over_twenty_words_fails():
    spec = _load("stamps-transfer")
    spec["timeline"][2]["say"] = " ".join(["Let's"] + ["draw"] * 20)
    _fails(spec, "timeline[2] say is 21 words; keep it to 20 or fewer")


def test_bar_template_with_two_stages_fails():
    spec = _load("beads-ratio-change")
    spec["template"] = "bar"
    r = _fails(spec, "template 'bar' needs exactly 1 stage, found 2")
    assert len(r.errors) == 1


def test_bar_stages_template_with_one_stage_fails():
    spec = _load("remainder-money")
    spec["template"] = "bar-stages"
    _fails(spec, "template 'bar-stages' needs 2 or 3 stages, found 1")


def test_working_template_with_stages_fails():
    spec = _load("stamps-transfer")
    spec["template"] = "working"
    _fails(spec, "template 'working' draws no bars, so stages must be [] (found 2)",
           "timeline[2] draw_stage needs bars, but the working template has none")


def test_answer_step_that_does_not_state_the_answer_fails():
    spec = _load("beads-ratio-change")
    i = _at(spec, "answer")
    spec["timeline"][i].update(text="Amy had some beads at first.", say="So, that is Amy's beads. Well done!")
    _fails(spec, f"timeline[{i}] answer text 'Amy had some beads at first.' does not state the answer 18",
           f"timeline[{i}] answer say does not state the answer 18")


def test_highlight_must_be_question_text():
    spec = _load("beads-ratio-change")
    spec["timeline"][1]["text"] = "3:5"
    _fails(spec, "timeline[1] highlight text '3:5' is not in the question")


def test_label_unit_must_show_the_unit_value():
    spec = _load("remainder-money")
    i = _at(spec, "label_unit")
    spec["timeline"][i]["text"] = "$25"
    _fails(spec, f"timeline[{i}] label_unit text '$25' should show the value of 1 unit, 30")


def test_bracket_text_must_match_what_it_brackets():
    spec = _load("beads-ratio-change")
    i = _at(spec, "bracket")
    spec["timeline"][i]["text"] = "30"  # Beth's total, but this bracket spans Amy's added 4 units = 24
    _fails(spec, f"timeline[{i}] bracket text '30' does not match what it brackets (24, or 4 units)")


def test_bar_quantity_must_exist():
    spec = _load("stamps-transfer")
    spec["stages"][0]["bars"][1]["quantity"] = "cai"
    _fails(spec, "bar 'ben_b' draws quantity 'cai', which is not in quantities")


def test_schema_errors_name_the_path():
    spec = _load("stamps-transfer")
    del spec["timeline"][3]["say"]
    _fails(spec, "schema: timeline[3]: 'say' is a required property")


def test_relations_that_do_not_fix_the_answer_fail():
    spec = _load("stamps-transfer")
    spec["relations"] = spec["relations"][:3]
    _fails(spec, "the relations do not fix")


def test_unreadable_relation_names_its_index():
    spec = _load("stamps-transfer")
    spec["relations"][0]["eq"] = "ali_before = 3*bob"
    _fails(spec, "relations[0] 'ali_before = 3*bob': unknown name 'bob'")


# --- Board shapes (docs/board-templates.md §3) -------------------------------------------------

def test_every_golden_example_names_its_board_and_checks_its_answer():
    for path in EXAMPLES:
        spec = json.loads(path.read_text(encoding="utf-8"))
        assert spec.get("board"), path.stem
        writes = [s["text"] for s in spec["timeline"] if s["do"] == "write"]
        assert writes[-1].startswith("Check:"), path.stem  # beat 8, verified like any working line


def test_wrong_check_line_fails():
    spec = _load("stamps-transfer")
    i = next(i for i, s in enumerate(spec["timeline"]) if s.get("text", "").startswith("Check:"))
    spec["timeline"][i]["text"] = "Check: 60 - 20 = 40 = 20 + 21 ✓"
    _fails(spec, "40 does not equal 41")


def test_unknown_board_fails_the_schema():
    spec = _load("stamps-transfer")
    spec["board"] = "spiral"
    _fails(spec, "schema: board")


def test_board_is_optional():
    spec = _load("stamps-transfer")
    del spec["board"]
    _passes(spec)


def test_working_board_needs_the_working_template():
    spec = _load("stamps-transfer")
    spec["board"] = "working"
    _fails(spec, "board 'working' draws no bars")
    spec = _load("sweets-excess-shortage")
    spec["board"] = "before_after"
    _fails(spec, "board 'before_after' is a bar picture, but template is 'working'")


def test_before_after_board_needs_two_stages_with_the_same_people():
    spec = _load("how_many_more-stickers")
    spec["board"] = "before_after"
    _fails(spec, "board 'before_after' needs 2 stages")
    spec = _load("stamps-transfer")
    spec["stages"][1]["bars"][1]["label"] = "Bob"
    _fails(spec, "the first and last stages must draw the same people")


def test_constant_difference_board_needs_the_difference_as_one_piece():
    spec = _load("constant_difference-ages")
    dad_now = spec["stages"][0]["bars"][0]
    dad_now["segments"] = [{"expr": "u"}, {"expr": "9"}, {"expr": "u"}]  # the 32 is split up: no piece shows it
    _fails(spec, "end bar 'dad_n' with the difference (32) as its own segment")


def test_constant_difference_board_with_tied_bars_reports_an_error():
    """Two bars with the same total must fail the board check, not crash the sort."""
    def stage(sid):
        return {"id": sid, "label": sid, "bars": [
            {"id": f"a_{sid}", "quantity": "x", "label": "Ann", "segments": [{"expr": "u"}]},
            {"id": f"b_{sid}", "quantity": "y", "label": "Ben", "segments": [{"expr": "u"}]}]}

    spec = {"board": "constant_difference", "template": "bar-stages", "question": "q",
            "units": [{"name": "u", "meaning": "1 unit"}], "stages": [stage("s1"), stage("s2")]}
    solution = Solution(quantities={}, units={"u": 5.0},
                        segments={"a_s1": [5.0], "b_s1": [5.0], "a_s2": [8.0], "b_s2": [3.0]})
    errors = _check_board(spec, solution)
    assert any("end bar" in e and "difference (0)" in e for e in errors), errors


def test_ratio_change_board_keeps_the_unchanged_person_the_same():
    spec = _load("beads-ratio-change")
    spec["stages"][1]["bars"][1]["segments"] = [{"expr": "2*u"}, {"expr": "3*u"}]
    _fails(spec, "'beth' does not change, so draw it with the same segments in every stage")


def test_remainder_board_follows_the_fractions_in_order():
    spec = _load("remainder-money")
    segs = spec["stages"][0]["bars"][0]["segments"]
    segs[0].update(expr="u", label="book")
    segs[1].update(expr="2*u", label="bag")  # 1/5, then 2/3 of the rest: not the story's 2/5 then 1/3
    _fails(spec, "the marked parts must follow the question's fractions in order")
    spec = _load("remainder-money")
    for seg in spec["stages"][0]["bars"][0]["segments"]:
        seg.pop("style", None)
    _fails(spec, "board 'remainder' needs one bar for the whole")


def test_percentage_board_needs_100_percent_in_whole_units():
    spec = _load("percentage-discount")
    spec["stages"][0]["bars"][0]["segments"][1] = {"expr": "42"}  # 100% is no longer drawn in units
    _fails(spec, "board 'percentage' needs a bar for 100% drawn only in units", "25% -> 4 units")


# --- The table primitive (docs/board-templates.md §4.1) -----------------------------------------

def _table_step(spec):
    return next(s for s in spec["timeline"] if s["do"] == "table")


def test_table_examples_pass():
    for name in ("assumption-legs-table", "units_and_value-files-table", "rate-tap-table"):
        _passes(_load(name))


def test_table_cell_numbers_must_be_known():
    spec = _load("rate-tap-table")
    _table_step(spec)["rows"][0][0] = "13 min"  # not in the question, and nothing works it out
    _fails(spec, 'table cell "13 min" uses 13, which is not in the question or the solved model')


def test_fill_cell_arithmetic_is_checked():
    spec = _load("rate-tap-table")
    fill = next(s for s in spec["timeline"] if s["do"] == "fill_cell")
    fill["text"] = "18 ÷ 3 = 5"
    _fails(spec, 'fill_cell "18 ÷ 3 = 5" is wrong: 6 does not equal 5')


def test_fill_cell_must_name_a_blank_cell_of_an_earlier_table():
    spec = _load("rate-tap-table")
    fill = next(s for s in spec["timeline"] if s["do"] == "fill_cell")
    fill["row"] = 0
    _fails(spec, "already shows '18 litres'")
    fill.update(row=9)
    _fails(spec, "is outside table 'rate'")
    fill.update(row=1, table="nope")
    _fails(spec, "names table 'nope', which no earlier table step draws")


def test_table_rows_need_one_cell_per_column():
    spec = _load("rate-tap-table")
    _table_step(spec)["rows"][0] = ["3 min"]
    _fails(spec, "table row 0 needs exactly 2 cells")


def test_table_needs_the_working_template():
    spec = _load("stamps-transfer")
    spec["timeline"].insert(3, {"do": "table", "id": "t", "columns": ["A", "B"], "rows": [["1", "2"]], "say": "A table."})
    _fails(spec, "a table is drawn where the bars go, so it needs template 'working'")


# --- The number line primitive (docs/board-templates.md §4.2) ------------------------------------

def _line_step(spec):
    return next(s for s in spec["timeline"] if s["do"] == "number_line")


def test_number_line_examples_pass():
    for name in ("speed-two-parts-line", "speed-meeting-line"):
        _passes(_load(name))


def test_number_line_labels_must_be_known():
    spec = _load("speed-meeting-line")
    _line_step(spec)["jumps"][0]["label"] = "car: 80 km/h"
    _fails(spec, 'number_line label "car: 80 km/h" uses 80, which is not in the question or the solved model')


def test_mark_point_arithmetic_is_checked():
    spec = _load("speed-two-parts-line")
    step = next(s for s in spec["timeline"] if s["do"] == "mark_point")
    step["text"] = "3 × 70 = 200 km"
    _fails(spec, 'mark_point "3 × 70 = 200 km" is wrong: 210 does not equal 200')


def test_mark_point_needs_a_question_mark_on_an_earlier_line():
    spec = _load("speed-two-parts-line")
    step = next(s for s in spec["timeline"] if s["do"] == "mark_point")
    step["at"] = 0
    _fails(spec, 'give that mark "value": "?" in the number_line step')
    step.update(at=100)
    _fails(spec, "has no mark there")
    step.update(at=210, line="road")
    _fails(spec, "names number line 'road', which no earlier number_line step draws")


def test_number_line_marks_and_jumps_stay_on_the_line():
    spec = _load("speed-two-parts-line")
    _line_step(spec)["marks"][1]["at"] = 400
    _fails(spec, 'every number_line mark needs an "at" from 0 to 320')
    spec = _load("speed-two-parts-line")
    _line_step(spec)["jumps"][0]["to"] = 0
    _fails(spec, "a number_line jump needs two different points")


def test_one_table_or_number_line_per_board():
    spec = _load("speed-meeting-line")
    spec["timeline"].insert(5, {"do": "table", "id": "t", "columns": ["A", "B"], "rows": [["1", "2"]], "say": "A table."})
    _fails(spec, "the board has room for one table or number line")
