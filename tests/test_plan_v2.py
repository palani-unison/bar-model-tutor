"""Offline tests for the BarSpec v2 planning path (routing, retries, second solver, refusals).

client.chat is mocked as in test_extract_plan.py; no model is called.
"""
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import check
from agent import client as agent_client
from agent import plan
from agent.extract import problem_from_text
from agent.plan import V1_RACE, V2_WAVES, plain_question, plan_problem, unsupported_reason, v2_family
from agent.verify_v2 import verify_spec_v2

ROOT = Path(__file__).resolve().parent.parent
N_DRAFTS = sum(len(w) for w in V2_WAVES)  # every v2 draft the waves can make
MARBLES_Q = "Ali has 3 times as many marbles as Ben. Together they have 120 marbles. How many marbles does Ali have?"


def _prompt_examples() -> list[dict]:
    """The full JSON worked examples in the v2 prompt."""
    text = (ROOT / "agent" / "prompts" / "plan_v2.md").read_text(encoding="utf-8")
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.S)]


STICKERS = _prompt_examples()[0]  # transfer, equal in the end: answer 18
STICKERS_Q = STICKERS["question"]

MARBLES_V2 = {
    "version": 2, "title": "Multiples", "level": "P3", "heuristic": "multiples", "template": "bar",
    "question": MARBLES_Q,
    "units": [{"name": "u", "meaning": "1 unit"}],
    "quantities": [{"id": "ali", "label": "Ali's marbles"}, {"id": "ben", "label": "Ben's marbles"}],
    "relations": [{"eq": "ali = 3*ben", "from": "Ali has 3 times as many marbles as Ben"},
                  {"eq": "ali + ben = 120", "from": "Together they have 120 marbles"}],
    "asked": {"expr": "ali", "label": "Ali's marbles"},
    "answer": 90,
    "stages": [{"id": "all", "label": "Marbles", "bars": [
        {"id": "ben_bar", "quantity": "ben", "label": "Ben", "segments": [{"expr": "u"}]},
        {"id": "ali_bar", "quantity": "ali", "label": "Ali", "segments": [{"expr": "3*u"}]}]}],
    "timeline": [
        {"do": "write_question", "say": MARBLES_Q},
        {"do": "draw_bar", "bar": "ben_bar", "say": "Ben has 1 unit."},
        {"do": "draw_bar", "bar": "ali_bar", "say": "Ali has 3 units."},
        {"do": "bracket", "bars": ["ali_bar", "ben_bar"], "side": "right", "text": "120", "say": "Together, 120 marbles."},
        {"do": "question_mark", "bar": "ali_bar", "say": "We want Ali's marbles."},
        {"do": "pause", "seconds": 2, "say": "Pause and think. How many units altogether?"},
        {"do": "write", "text": "1 + 3 = 4 units", "say": "1 plus 3 is 4 units."},
        {"do": "write", "text": "4 units = 120", "say": "4 units are 120 marbles."},
        {"do": "write", "text": "1 unit = 120 ÷ 4 = 30", "say": "1 unit is 30."},
        {"do": "write", "text": "3 units = 3 × 30 = 90", "say": "3 units are 90."},
        {"do": "answer", "text": "Ali has 90 marbles.", "say": "So, Ali has 90 marbles. Well done!"}],
}


def _reply(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _scripted(monkeypatch, replies):
    """Mock client.chat (thread-safe: drafts in a wave run in parallel) and record every call.

    `replies` is a list handed out in call order (the last one repeats), or a dict from schema
    name ("barspec" for v1, "barspec_v2") to such a list.
    """
    import threading

    calls, lock, served = [], threading.Lock(), {}

    def chat(messages, **kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        with lock:
            calls.append({"messages": messages, **kwargs})
            script = replies.get(name, ["{}"]) if isinstance(replies, dict) else replies
            k = served.get(id(script), 0)
            served[id(script)] = k + 1
            reply = script[min(k, len(script) - 1)]
        return _reply(reply if isinstance(reply, str) else json.dumps(reply))

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", chat)
    return calls


def _schema_name(call) -> str:
    return call["response_format"]["json_schema"]["name"]


# ---------------------------------------------------------------- the gate itself
@pytest.mark.parametrize("path", sorted((ROOT / "specs" / "examples" / "v2").glob("*.json")), ids=lambda p: p.stem)
def test_golden_v2_examples_pass_the_v2_gate(path):
    result = verify_spec_v2(json.loads(path.read_text(encoding="utf-8")))
    assert result.ok, result.errors


@pytest.mark.parametrize("example", _prompt_examples(), ids=lambda e: e["heuristic"])
def test_prompt_worked_examples_pass_the_v2_gate(example):
    result = verify_spec_v2(example)
    assert result.ok, result.errors


def test_gate_rejects_a_quote_that_is_not_in_the_question():
    spec = copy.deepcopy(STICKERS)
    spec["relations"][0]["from"] = "Mei had five times more stickers"
    result = verify_spec_v2(spec)
    assert not result.ok
    assert "not in the question" in str(result)


def test_gate_rejects_numbers_the_question_does_not_give():
    spec = copy.deepcopy(STICKERS)
    spec["relations"][0]["eq"] = "mei_before = 90"  # the planner's own result, not a fact
    result = verify_spec_v2(spec)
    assert not result.ok
    assert "90" in str(result)


def test_gate_rejects_a_wrong_answer_and_wrong_working():
    spec = copy.deepcopy(STICKERS)
    spec["answer"] = 19
    assert "relations give" in str(verify_spec_v2(spec))
    spec = copy.deepcopy(STICKERS)
    step = next(s for s in spec["timeline"] if s.get("text") == "1 unit = 36 ÷ 2 = 18")
    step["text"] = "1 unit = 36 ÷ 2 = 16"
    assert "is wrong" in str(verify_spec_v2(spec))


def test_gate_rejects_long_narration_and_missing_pause():
    spec = copy.deepcopy(STICKERS)
    spec["timeline"] = [s for s in spec["timeline"] if s["do"] != "pause"]
    spec["timeline"][2]["say"] = " ".join(["word"] * 21)
    errors = str(verify_spec_v2(spec))
    assert "pause" in errors and "20" in errors


def test_plain_question_turns_latex_into_what_a_child_reads():
    assert plain_question(r"She ate $\frac{2}{5}$ of the cake and $1\frac{1}{2}$ kg of rice.") == \
        "She ate 2/5 of the cake and 1 1/2 kg of rice."
    assert plain_question(r"It cost $5 after a $20\%$ discount.") == "It cost $5 after a 20% discount."


# ---------------------------------------------------------------- refusals and routing
@pytest.mark.parametrize("q", [
    "The figure below shows a rectangle. Its length is 3 times its breadth. Find its area.",
    "The table below shows the number of books read. How many more books did Ali read than Ben?",
    "Study the graph. How many more cars were sold on Monday than on Tuesday?",
    "Ali has 30 more stamps than Ben. (a) How many stamps does Ben have? (b) How many do they have altogether?",
])
def test_figure_and_multi_part_questions_are_refused_before_any_model_call(monkeypatch, q):
    def no_calls(*a, **k):
        raise AssertionError("model should not be called")

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", no_calls)
    assert unsupported_reason(q)
    with pytest.raises(ValueError, match="won't show an answer"):
        plan_problem(problem_from_text(q))


def test_one_part_of_a_paper_question_is_not_refused():
    q = "Ali had 30 more stamps than Ben. They had 90 stamps altogether. b) How many stamps did Ben have?"
    assert unsupported_reason(plain_question(q)) is None
    assert plain_question(q).endswith("altogether. How many stamps did Ben have?")
    assert plain_question("They shared it in the ratio $1:2:3$. How much did each get?").startswith("They shared it in the ratio 1:2:3.")


def test_tables_as_furniture_are_not_refused():
    assert unsupported_reason("A table costs $40 more than a chair. Together they cost $100. How much is the chair?") is None
    assert unsupported_reason("Ali bought 3 tables and 4 chairs for $560. Each table cost $80 more than each chair. Find the cost of a chair.") is None


@pytest.mark.parametrize("q, family", [
    (STICKERS_Q, "transfer"),
    ("Ali has 3 times as many marbles as Ben. They have 120 marbles altogether. How many more marbles does Ali have than Ben?", "how many more"),
    ("Mrs Lee had some money. She spent 2/5 of it on a bag and 1/3 of the remainder on a book. She had $60 left. How much money did she have at first?", "remainder"),
    ("A bag was sold for $60 after a 25% discount. What was its usual price?", "percentage"),
    ("The ratio of Amy's beads to Beth's beads was 3 : 5. After Amy bought 24 more beads, the ratio became 7 : 5. How many beads did Amy have at first?", "ratio change"),
    ("Sam had $50. He spent $20 on a book. How much money did he have left?", "before-after"),
    ("Mrs Lim is 40 years old and her son is 12 years old. In how many years will Mrs Lim be 3 times as old as her son?", "constant difference"),
    ("If Mrs Tan gives each pupil 5 sweets, she will be short of 24 sweets. If she gives each pupil 2 sweets, she will be short of 6 sweets. How many pupils are there?", "excess-shortage"),
    ("The average of 3 numbers is 20. Two of the numbers are 18 and 25. What is the third number?", "average"),
])
def test_routing_sends_v1_shapes_it_cannot_express_to_v2(q, family):
    assert v2_family(q) == family


@pytest.mark.parametrize("q", [
    MARBLES_Q,
    "Ann and Bala share 150 stickers in the ratio 2 : 3. How many stickers does Bala get?",
    "Ali has 20 more marbles than Ben. Together they have 100 marbles. How many marbles does Ali have?",
    "Hall A has twice as many chairs as Hall B. Hall A has 48 more chairs than Hall B. How many chairs are there in Hall B?",
])
def test_routing_keeps_v1_for_what_it_handles(q):
    assert v2_family(q) is None


# ---------------------------------------------------------------- the planning loop
def test_before_after_goes_straight_to_v2(monkeypatch):
    calls = _scripted(monkeypatch, [STICKERS])
    monkeypatch.setattr(check, "independent_answer", lambda q: 18.0)
    spec, result, log = plan_problem(problem_from_text(STICKERS_Q))
    assert spec["version"] == 2 and spec["answer"] == 18
    assert result.ok and result.check == "agreed"
    assert log[0] == "routed to v2: transfer"
    assert {_schema_name(c) for c in calls} == {"barspec_v2"}
    # Decoding uses the v2 schema, non-strict, with timeline steps left open (see _response_format_v2).
    fmt = calls[0]["response_format"]["json_schema"]
    assert fmt["strict"] is False and fmt["schema"]["properties"]["timeline"]["items"] == {"type": "object"}


def test_v1_race_falls_back_to_v2(monkeypatch):
    calls = _scripted(monkeypatch, {"barspec": ["{}"], "barspec_v2": [MARBLES_V2]})
    monkeypatch.setattr(check, "independent_answer", lambda q: 90.0)
    spec, result, log = plan_problem(problem_from_text(MARBLES_Q))
    assert spec["version"] == 2 and result.check == "agreed"
    assert log[0] == "v1 and v2 race"
    # one draft of each, in parallel (thread timing decides which call is recorded first, and a fast v1 failure
    # may retry before the v2 draft is logged, so only check that both kinds were drafted)
    assert {"barspec", "barspec_v2"} <= {_schema_name(c) for c in calls}


def test_v1_still_answers_what_it_handles(monkeypatch):
    marbles_v1 = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
    _scripted(monkeypatch, {"barspec": [marbles_v1], "barspec_v2": ["{}"]})
    monkeypatch.setattr(check, "independent_answer", lambda q: 90.0)
    spec, result, _log = plan_problem(problem_from_text(MARBLES_Q))
    assert "version" not in spec and spec["model"]["answer"] == 90


def test_v2_failure_is_retried_with_the_verifier_feedback(monkeypatch):
    wrong = copy.deepcopy(STICKERS)
    wrong["answer"] = 19
    first = len(V2_WAVES[0])
    calls = _scripted(monkeypatch, [wrong] * first + [STICKERS])
    monkeypatch.setattr(check, "independent_answer", lambda q: 18.0)
    log: list[str] = []
    spec, result, _ = plan_problem(problem_from_text(STICKERS_Q), log=log)
    assert spec["answer"] == 18 and result.check == "agreed"
    assert all("FAILED" in line for line in log[1:1 + first])
    feedback = calls[first]["messages"][-1]["content"]
    assert "failed verification" in feedback and "19" in feedback


def test_v2_checker_disagreement_retries_then_declines(monkeypatch):
    calls = _scripted(monkeypatch, [STICKERS])
    monkeypatch.setattr(check, "independent_answer", lambda q: 30.0)
    monkeypatch.setattr(plan, "careful_answer", lambda q: 30.0)
    log: list[str] = []
    with pytest.raises(ValueError, match="won't show an answer"):
        plan_problem(problem_from_text(STICKERS_Q), log=log)
    assert len(calls) == N_DRAFTS
    assert calls[-1]["model"] == V2_WAVES[-1][0][1]  # Wonder last
    assert sum("disagreed: 30" in line for line in log) == N_DRAFTS
    assert sum("careful re-check" in line for line in log) == 1  # re-solved once, not per draft
    feedback = calls[-1]["messages"][-1]["content"]
    assert "independent solver got a different answer" in feedback
    assert "30" not in feedback.split("An independent solver")[-1]  # the checker's number is not leaked


def test_careful_recheck_overrules_a_wrong_quick_check(monkeypatch):
    calls = _scripted(monkeypatch, [STICKERS])
    monkeypatch.setattr(check, "independent_answer", lambda q: 30.0)  # the quick check is wrong
    monkeypatch.setattr(plan, "careful_answer", lambda q: 18.0)
    log: list[str] = []
    spec, result, _ = plan_problem(problem_from_text(STICKERS_Q), log=log)
    assert spec["answer"] == 18 and result.check == "agreed"
    assert "careful re-check agreed: 18" in log


def test_after_a_disagreement_only_the_careful_check_counts(monkeypatch):
    """The planner must not win by bending its answer to the quick checker's number."""
    bent = copy.deepcopy(STICKERS)
    _scripted(monkeypatch, [STICKERS] + [bent])
    monkeypatch.setattr(check, "independent_answer", lambda q: 30.0)
    monkeypatch.setattr(plan, "careful_answer", lambda q: 25.0)
    with pytest.raises(ValueError):
        plan_problem(problem_from_text(STICKERS_Q))


def test_invented_constants_are_rejected():
    spec = copy.deepcopy(STICKERS)
    spec["relations"].append({"eq": "price = 1", "from": "Mei had 5 times as many stickers as Hui"})
    assert plan.invented_numbers(spec)
    spec = copy.deepcopy(STICKERS)
    spec["relations"][1]["eq"] = "mei_after = mei_before - 36 + 0*100"
    assert "no reason" in " ".join(plan.invented_numbers(spec))
    assert plan.invented_numbers(STICKERS) == []


def test_questions_pointing_at_an_image_are_refused():
    assert unsupported_reason("Pens cost as shown. ![img-3.jpeg](p1-img-3.jpeg) How much do 4 pens cost?")


def test_careful_check_stands_in_when_the_quick_check_is_unavailable(monkeypatch):
    _scripted(monkeypatch, [STICKERS])  # conftest: the quick checker reports None
    monkeypatch.setattr(plan, "careful_answer", lambda q: 18.0)
    spec, result, log = plan_problem(problem_from_text(STICKERS_Q))
    assert result.check == "agreed" and spec["answer"] == 18


def test_no_answer_is_shown_without_any_independent_check(monkeypatch):
    _scripted(monkeypatch, [STICKERS])
    monkeypatch.setattr(plan, "careful_answer", lambda q: None)
    with pytest.raises(ValueError, match="double-check"):
        plan_problem(problem_from_text(STICKERS_Q))


def test_v2_never_shows_an_unverified_spec(monkeypatch):
    broken = copy.deepcopy(STICKERS)
    broken["relations"] = broken["relations"][:2]  # the relations no longer fix the answer
    _scripted(monkeypatch, [broken])
    monkeypatch.setattr(check, "independent_answer", lambda q: 18.0)
    log: list[str] = []
    with pytest.raises(ValueError):
        plan_problem(problem_from_text(STICKERS_Q), log=log)
    assert all("FAILED" in line for line in log if line.startswith("attempt"))


def test_latex_question_is_planned_as_plain_text(monkeypatch):
    remainder = json.loads((ROOT / "specs" / "examples" / "v2" / "remainder-money.json").read_text(encoding="utf-8"))
    latex = remainder["question"].replace("2/5", r"$\frac{2}{5}$").replace("1/3", r"$\frac{1}{3}$")
    calls = _scripted(monkeypatch, [remainder])
    monkeypatch.setattr(check, "independent_answer", lambda q: 150.0)
    spec, result, _log = plan_problem(problem_from_text(latex))
    assert json.loads(calls[0]["messages"][-1]["content"])["question"] == remainder["question"]
    assert result.ok and spec["answer"] == 150


# ---------------------------------------------------------------- API and review shapes
def test_solve_returns_v2_answer_and_solved_values(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "plan_problem", lambda problem: (STICKERS, verify_spec_v2(STICKERS), ["ok"]))
    body = main._solve({"question": STICKERS_Q})
    assert body["answer"] == 18
    assert body["solved"]["asked"] == 18 and body["solved"]["units"] == {"u": 18.0}
    assert body["verified"] is True


def test_review_says_v2_is_not_supported_yet(monkeypatch):
    from agent.review import check_drawing

    def no_calls(*a, **k):
        raise AssertionError("model should not be called")

    monkeypatch.setattr(agent_client, "chat", no_calls)
    result = check_drawing(b"not an image", STICKERS)
    assert result["supported"] is False and "not supported" in result["say"]


def test_a_timeout_on_one_rung_moves_to_the_next(monkeypatch):
    import threading

    replies, lock, timed_out = iter([None, STICKERS, STICKERS, STICKERS]), threading.Lock(), threading.Event()

    def chat(messages, **kwargs):
        with lock:  # the first wave's drafts run in parallel: hand the timeout to whichever asks first
            reply = next(replies)
        if reply is None:
            timed_out.set()
            raise agent_client.ModelUnavailableError("timed out")
        return _reply(json.dumps(reply))

    monkeypatch.setenv("META_API_KEY", "test")
    monkeypatch.setattr(agent_client, "chat", chat)
    monkeypatch.setattr(check, "independent_answer", lambda q: 18.0)
    spec, result, log = plan_problem(problem_from_text(STICKERS_Q))
    # The other draft may win before the timed-out one is logged (a losing draft is dropped), so check the
    # timeout happened and the plan still came through, not the log order.
    assert spec["answer"] == 18 and timed_out.is_set()


def test_space_thousands_are_read_as_one_number():
    assert plain_question("He earns $4 000 a month. A car costs $20 000.") == "He earns $4000 a month. A car costs $20000."


def test_a_comparison_written_the_wrong_way_round_is_rejected():
    spec = {"relations": [{"eq": "tshirt = shorts + 8", "from": "Each pair of shorts cost $8 more than each T-shirt"}]}
    assert plan.comparison_problems(spec)
    spec["relations"][0]["eq"] = "shorts = tshirt + 8"
    assert plan.comparison_problems(spec) == []
    spec = {"relations": [{"eq": "ben = ali - 5", "from": "Ben has 5 fewer marbles than Ali"}]}
    assert plan.comparison_problems(spec) == []
    assert plan.comparison_problems(STICKERS) == []


def test_answers_that_do_not_come_out_exact_are_rejected():
    assert plan.answer_shape_problem(187.0909090909, "How much money did Henry have left?")
    assert plan.answer_shape_problem(4.125, "How heavy is the box?") is None
    assert plan.answer_shape_problem(13.857, "Give your answer correct to 3 decimal places.") is None
    assert plan.answer_shape_problem(13.8571428, "Round your answer to the nearest minute.") is None


def _golden(name):
    return json.loads((ROOT / "specs" / "examples" / "v2" / f"{name}.json").read_text(encoding="utf-8"))


def test_a_wrong_board_shape_only_drops_the_board():
    spec = _golden("how_many_more-stickers")
    spec["board"] = "before_after"  # one stage: not a before-after picture
    result = plan._check_draft("v2", spec, spec["question"])
    assert result.ok, result.errors
    assert "board" not in spec and plan._check_steps(spec)


def test_a_wrong_check_line_only_drops_the_check_line():
    spec = _golden("stamps-transfer")
    i = plan._check_steps(spec)[0]
    spec["timeline"][i]["text"] = "Check: 60 - 20 = 41 ✓"
    result = plan._check_draft("v2", spec, spec["question"])
    assert result.ok, result.errors
    assert not plan._check_steps(spec) and spec["board"] == "before_after"


def test_real_errors_are_not_trimmed_away():
    spec = _golden("stamps-transfer")
    spec["answer"] = 21
    spec["board"] = "remainder"
    result = plan._check_draft("v2", spec, spec["question"])
    assert not result.ok and spec["board"] == "remainder" and plan._check_steps(spec)



def test_a_table_with_bars_is_dropped_not_the_answer():
    spec = _golden("stamps-transfer")
    spec["timeline"].insert(3, {"do": "table", "id": "t", "columns": ["A", "B"], "rows": [["1", "?"]], "say": "A table."})
    spec["timeline"].insert(4, {"do": "fill_cell", "table": "t", "row": 0, "col": 1, "text": "3", "say": "3 units."})
    result = plan._check_draft("v2", spec, spec["question"])
    assert result.ok, result.errors
    assert not any(s["do"] in ("table", "fill_cell") for s in spec["timeline"])



def test_each_versus_each_on_totals_is_rejected():
    spec = {"question": "Henry spent 2/9 of his money on 7 pairs of shorts and 2/9 of his remaining money on "
                        "3 T-shirts. Each pair of shorts cost $8 more than each T-shirt. How much money did Henry have left?",
            "relations": [{"eq": "shorts_cost = 2/9*money", "from": "spent 2/9 of his money on 7 pairs of shorts"},
                          {"eq": "shorts_cost = tshirts_cost + 8",
                           "from": "Each pair of shorts cost $8 more than each T-shirt"}]}
    assert "compares ONE item with ONE item" in plan.per_item_problems(spec)[0]
    spec["relations"][1]["eq"] = "shorts_cost/7 = tshirts_cost/3 + 8"
    assert plan.per_item_problems(spec) == []
    spec["relations"][1]["eq"] = "shorts_each = tshirt_each + 8"
    spec["relations"].append({"eq": "shorts_cost = 7*shorts_each", "from": "7 pairs of shorts"})
    assert plan.per_item_problems(spec) == []


def test_a_count_swapped_between_two_items_is_rejected():
    spec = {"relations": [{"eq": "3*shorts_each = shorts_total", "from": "on 7 pairs of shorts"},
                          {"eq": "4*cows + 2*birds = 3440", "from": "a total of 3440 birds' and cows' legs"},
                          {"eq": "muffin_price = 2*cookie_price", "from": "was twice the cost of 1 cookie"}]}
    problems = plan.quoted_number_problems(spec)
    assert len(problems) == 1 and "multiplies shorts_each by 3" in problems[0] and "counts 7" in problems[0]
    spec["relations"][0]["eq"] = "7*shorts_each = shorts_total"
    assert plan.quoted_number_problems(spec) == []



def test_altogether_counts_what_came_later():
    spec = {"question": "Mrs Hong baked 4 times as many vanilla muffins as banana muffins. She gave 115 vanilla muffins "
                        "to her neighbours and baked another 65 banana muffins. In the end, she had 36 more banana "
                        "muffins than vanilla muffins. How many muffins did Mrs Hong baked altogether?",
            "relations": [{"eq": "banana_after = banana_before + 65", "from": "baked another 65 banana muffins"}],
            "asked": {"expr": "vanilla_before + banana_before", "label": "Muffins at first"}}
    assert "includes 'baked another 65 banana muffins'" in plan.altogether_problems(spec)[0]
    spec["asked"]["expr"] = "vanilla_before + banana_before + 65"
    assert plan.altogether_problems(spec) == []
    spec["asked"]["expr"] = "vanilla_before + banana_before"
    spec["question"] = spec["question"].replace("altogether?", "altogether at first?")
    assert plan.altogether_problems(spec) == []


def test_altogether_is_not_fooled_by_more_than():
    """A transfer keeps the total, so "at first" amounts answer "altogether"; "$154 more than" is a comparison."""
    spec = {"question": "Christopher had $154 more than Ryan at first. After Christopher gave $262 to Ryan, Ryan had "
                        "3 times as much as Christopher. How much did they have altogether?",
            "relations": [{"eq": "christopher_before = ryan_before + 154", "from": "had $154 more than Ryan at first"}],
            "asked": {"expr": "christopher_before + ryan_before", "label": "Total"}}
    assert plan.altogether_problems(spec) == []


def test_a_missing_count_is_declined():
    q = "Farah had some erasers. She gave 204 erasers to each of her friends and had 176 left. How many at first?"
    assert plan.unsupported_reason(q) == "questions that seem to be missing a number"
    assert plan.unsupported_reason(q.replace("her friends", "her 2 friends")) is None
    assert plan.unsupported_reason("Ali has four cousins. He gave each of his cousins 5 sweets. How many?") is None
    assert plan.unsupported_reason("Miss Tan baked some muffins. If she gives 4 to each of her pupils, she will have 6 "
                                   "muffins remaining. If she gives 5 muffins to each of her pupils, there is no "
                                   "remainder. How many muffins did Miss Tan bake?") is None  # excess-shortage
