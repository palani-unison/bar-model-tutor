"""Learner model: rating updates and the pacing rules."""
from agent import learner


def test_level_numbers_and_heuristics():
    assert learner.level_number("P5") == 5 and learner.level_number("P3-P4") == 3.5
    assert learner.level_number("P3-P4", top=True) == 4 and learner.level_number(None) is None
    assert learner.heuristic_of({"heuristic": "remainder"}) == "remainder"
    assert learner.heuristic_of({"problem_type": "part-whole with multiples"}) == "part_whole"
    assert learner.heuristic_of({"problem_type": "multiples"}) == "multiples"


def test_rating_goes_up_on_first_tries_and_down_when_shown():
    good = learner.update(None, "P4", "P4", [("unit", "first try", 5.0), ("answer", "first try", 7.0)])
    assert good["rating"] > learner.par_rating("P4") and good["n"] == 2
    assert good["avg_seconds"] == round(0.3 * 7 + 0.7 * 5, 1)  # EMA
    bad = learner.update(None, "P4", "P4", [("unit", "shown", 40.0), ("answer", "with hints", 30.0)])
    assert bad["rating"] < learner.par_rating("P4")


def test_harder_checkpoints_and_problems_are_worth_more():
    easy = learner.update(None, "P4", "P3", [("unit", "first try", 5)])["rating"]
    hard = learner.update(None, "P4", "P6", [("unit", "first try", 5)])["rating"]
    challenge = learner.update(None, "P4", "P4", [("challenge", "first try", 5)])["rating"]
    unit = learner.update(None, "P4", "P4", [("unit", "first try", 5)])["rating"]
    assert hard > easy and challenge > unit
    missed = learner.update(None, "P4", "P4", [("challenge", "missed", 2)])
    assert missed["avg_seconds"] is None  # a missed challenge is a guess, not a working time


def test_k_factor_settles():
    state = None
    for _ in range(12):
        state = learner.update(state, "P4", "P4", [("unit", "with hints", 10)])
    before = state["rating"]
    after = learner.update(state, "P4", "P4", [("unit", "first try", 10)])["rating"]
    assert 0 < after - before <= learner.K_SETTLED


def test_pacing_new_child_is_guided():
    p = learner.pacing_from("P4", "P4", None)
    assert (p["mode"], p["speed"], p["checkpoints"]) == ("guided", 1.0, "all")


def test_pacing_below_level_is_challenge_first():
    p = learner.pacing_from("P6", "P4", None)
    assert p["mode"] == "challenge_first" and p["checkpoints"] == "key" and p["speed"] == 1.5
    assert any("below" in r for r in p["reasons"])


def test_pacing_strong_and_fast_learner():
    state = {"rating": learner.par_rating("P4") + 200, "n": 10, "avg_seconds": 6}
    p = learner.pacing_from("P4", "P4", state)
    assert (p["mode"], p["speed"], p["checkpoints"]) == ("challenge_first", 2.0, "key")
    state["avg_seconds"] = 13
    assert learner.pacing_from("P4", "P4", state)["speed"] == 1.25


def test_pacing_struggling_learner_gets_everything_at_1x():
    weak = {"rating": learner.par_rating("P4") - 120, "n": 6, "avg_seconds": 12}
    p = learner.pacing_from("P4", "P4", weak)
    assert (p["mode"], p["speed"], p["checkpoints"]) == ("guided", 1.0, "all")
    slow = {"rating": learner.par_rating("P4") + 200, "n": 6, "avg_seconds": 40}
    p = learner.pacing_from("P6", "P4", slow)  # slow beats below-level
    assert (p["mode"], p["speed"], p["checkpoints"]) == ("guided", 1.0, "all")
    shown = [{"outcome": "shown"}, {"outcome": "first try"}]
    p = learner.pacing_from("P4", "P4", {"rating": 1300, "n": 6, "avg_seconds": 5}, last=shown)
    assert (p["mode"], p["checkpoints"]) == ("guided", "all")


def test_pacing_doing_fine_gets_key_checkpoints():
    state = {"rating": learner.par_rating("P4") + 30, "n": 5, "avg_seconds": 9}
    p = learner.pacing_from("P4", "P4", state)
    assert (p["mode"], p["speed"], p["checkpoints"]) == ("guided", 1.25, "key")


def test_one_good_lesson_moves_a_child_to_challenge_first():
    """The demo path: a P4 child gets every marbles checkpoint right first time, quickly."""
    state = learner.update(None, "P4", "P3-P4", [("units", "first try", 4), ("unit", "first try", 5), ("answer", "first try", 4)])
    assert learner.pacing_from("P4", "P3-P4", None)["mode"] == "guided"
    assert learner.pacing_from("P4", "P3-P4", state)["mode"] == "challenge_first"


class FakeDB:
    def __init__(self):
        self.rows, self.m = [], {}

    def add_attempts(self, cid, h, level, rows):
        self.rows += [{"checkpoint_key": k, "outcome": o, "seconds": s, "created": 1.0} for k, o, s in rows]

    def attempts(self, cid, h=None, limit=200):
        return list(reversed(self.rows))[:limit]

    def get_mastery(self, cid, h):
        return self.m.get(h)

    def set_mastery(self, cid, h, rating, n, avg):
        self.m[h] = {"rating": rating, "n": n, "avg_seconds": avg}


def test_record_lesson_then_pacing():
    fake, child = FakeDB(), {"id": 1, "level": "P4"}
    spec = {"heuristic": "before_after", "level": "P4"}
    assert learner.pacing(None, spec) is None
    assert learner.pacing(child, spec, db=fake)["mode"] == "guided"
    learner.record_lesson(child, spec, [("unit", "shown", 30), ("answer", "with hints", 25)], db=fake)
    p = learner.pacing(child, spec, db=fake)
    assert (p["mode"], p["checkpoints"], p["heuristic"]) == ("guided", "all", "before_after")
    assert fake.m["before_after"]["n"] == 2
