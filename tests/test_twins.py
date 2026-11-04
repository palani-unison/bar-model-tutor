"""'Try one like this' twins (agent/twins.py): same structure, new numbers, verified by code, never unchecked.

Every model call is mocked: the independent second solver and the planner fallback.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import check, twins
from agent.plan import verify_v2
from agent.spec_v2 import solve_spec
from app import db, lesson_ws
from app.main import app

V2 = Path(__file__).resolve().parent.parent / "specs" / "examples" / "v2"
EXAMPLES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(V2.glob("*.json"))}
MARBLES = json.loads((V2.parent / "marbles.json").read_text(encoding="utf-8"))


def _shape(text: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "N", str(text).replace(" ", ""))


@pytest.fixture
def checker_agrees(monkeypatch):
    """The second solver reads the twin question and (being right) agrees with the code's answer."""
    calls = []

    def answer(question):
        calls.append(question)
        return ANSWERS.get(question)

    ANSWERS = {}
    real_gate = twins._gate

    def gate(twin, code_answer):  # remember the code answer for the question, as a correct solver would find
        ANSWERS[twin["question"]] = float(code_answer)
        return real_gate(twin, code_answer)

    monkeypatch.setattr(twins, "_gate", gate)
    monkeypatch.setattr(check, "independent_answer", answer)
    return calls


@pytest.fixture(autouse=True)
def no_planner(monkeypatch):
    import agent.plan as plan

    def boom(*_a, **_k):
        raise AssertionError("the planner must be mocked in tests")

    monkeypatch.setattr(plan, "plan_problem", boom)


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_twin_keeps_the_structure_and_verifies(name, checker_agrees):
    spec = EXAMPLES[name]
    twin = twins.make_twin(spec, seed=7, use_planner=False)
    t = twin.spec
    assert twin.how == "substitution"
    # Same structure: kind, board, quantities, relation shapes, segment shapes, the lesson's steps.
    for key in ("heuristic", "template", "level", "title"):
        assert t[key] == spec[key]
    assert [q["id"] for q in t["quantities"]] == [q["id"] for q in spec["quantities"]]
    assert [u["name"] for u in t["units"]] == [u["name"] for u in spec["units"]]
    assert [_shape(r["eq"]) for r in t["relations"]] == [_shape(r["eq"]) for r in spec["relations"]]
    assert [s["do"] for s in t["timeline"]] == [s["do"] for s in spec["timeline"]]
    assert [len(b["segments"]) for st in t["stages"] for b in st["bars"]] == \
           [len(b["segments"]) for st in spec["stages"] for b in st["bars"]]
    assert [_shape(seg["expr"]) for st in t["stages"] for b in st["bars"] for seg in b["segments"]] == \
           [_shape(seg["expr"]) for st in spec["stages"] for b in st["bars"] for seg in b["segments"]]
    # New numbers, and a verified answer that the code computes from the new relations.
    assert t["question"] != spec["question"] and t["answer"] != spec["answer"]
    assert verify_v2(t).ok
    assert solve_spec(t).asked == pytest.approx(float(t["answer"]))
    assert t["timeline"][0]["say"] == t["question"]
    assert twin.solved["asked"] == pytest.approx(float(t["answer"]))
    # The second solver saw only the new question.
    assert checker_agrees and checker_agrees[-1] == t["question"]
    for old in twin.values:
        assert old not in re.findall(r"\d+(?:\.\d+)?", t["question"])


def test_twin_values_stay_nice(checker_agrees):
    cakes = twins.make_twin(EXAMPLES["units_and_value-cakes"], seed=3, use_planner=False).spec
    assert re.search(r"\$\d+\.50\b", cakes["question"])  # $19.50 -> $NN.50, written with cents
    remainder = twins.make_twin(EXAMPLES["remainder-money"], seed=3, use_planner=False).spec
    assert "2/5" in remainder["question"] and "1/3" in remainder["question"]  # structure untouched
    assert float(remainder["answer"]).is_integer()


def test_same_seed_same_twin_and_seeds_differ(checker_agrees):
    spec = EXAMPLES["remainder-money"]
    a = twins.make_twin(spec, seed=11, use_planner=False).spec
    b = twins.make_twin(spec, seed=11, use_planner=False).spec
    assert a == b
    others = {twins.make_twin(spec, seed=s, use_planner=False).spec["question"] for s in range(6)}
    assert len(others) > 1


def test_checker_disagreeing_means_no_twin(monkeypatch):
    monkeypatch.setattr(check, "independent_answer", lambda q: 99999.0)
    with pytest.raises(twins.TwinUnavailable, match="disagreed"):
        twins.make_twin(EXAMPLES["stamps-transfer"], seed=1)


def test_checker_unavailable_means_no_twin(monkeypatch):
    monkeypatch.setattr(check, "independent_answer", lambda q: None)
    with pytest.raises(twins.TwinUnavailable, match="unavailable"):
        twins.make_twin(EXAMPLES["stamps-transfer"], seed=1)


def test_failed_gate_is_never_offered(monkeypatch, checker_agrees):
    monkeypatch.setattr(twins, "_gate", lambda twin, answer: "verify: narration says 7")
    with pytest.raises(twins.TwinUnavailable):
        twins.make_twin(EXAMPLES["remainder-money"], seed=1, use_planner=False)


def test_unverified_original_or_v1_is_refused(checker_agrees):
    with pytest.raises(twins.TwinUnavailable):
        twins.make_twin(MARBLES)
    broken = dict(EXAMPLES["remainder-money"], answer=999)
    with pytest.raises(twins.TwinUnavailable, match="does not verify"):
        twins.make_twin(broken)


def test_planner_fallback_must_match_the_code_answer(monkeypatch, checker_agrees):
    """When the substituted narration can't verify, the planner plans the new question; its answer must equal the code's."""
    import agent.plan as plan

    spec = EXAMPLES["remainder-money"]
    monkeypatch.setattr(twins, "_gate", lambda twin, answer: "verify: a working number the map can't place")
    seen = {}

    def fake_plan(problem, log=None):
        q = problem["question"]
        seen["q"] = q
        m = int(re.search(r"\$(\d+) left", q).group(1))
        planned = json.loads(json.dumps(spec))
        planned["question"] = q
        result = plan.VerifyResult(ok=True, errors=[])
        result.check = "agreed"
        planned["answer"] = m * 5 // 2 + (1 if seen.get("wrong") else 0)
        return planned, result, []

    monkeypatch.setattr(plan, "plan_problem", fake_plan)
    seen["wrong"] = True
    with pytest.raises(twins.TwinUnavailable, match="planner answer"):
        twins.make_twin(spec, seed=2)
    assert "$60" not in seen["q"] and "2/5" in seen["q"]
    seen["wrong"] = False
    twin = twins.make_twin(spec, seed=2)
    assert twin.how == "planner" and twin.spec["question"] == seen["q"]


def test_cache_precomputes_once_and_reports_unavailable(monkeypatch, checker_agrees):
    calls = []
    real = twins.make_twin
    monkeypatch.setattr(twins, "make_twin", lambda spec, seed=None, use_planner=True: calls.append(seed) or real(spec, seed, False))
    cache = twins.TwinCache()
    spec = EXAMPLES["fraction_of-muffins"]
    cache.prefetch(spec)
    first, again = cache.get(spec), cache.get(spec)
    assert first["available"] and first is again and len(calls) == 1
    assert verify_v2(first["spec"]).ok
    assert cache.get(MARBLES) == {"available": False, "reason": "twins are made from v2 specs only"}
    monkeypatch.setattr(twins, "make_twin", lambda *a, **k: (_ for _ in ()).throw(twins.TwinUnavailable("nope")))
    assert cache.get(spec, n=1) == {"available": False, "reason": "nope"}


# ---------------------------------------------------------------- the API and the lesson socket
@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.setattr(lesson_ws.tts, "speak_compressed", lambda text: (b"ID3", "audio/mpeg"))
    monkeypatch.setattr(twins, "cache", twins.TwinCache())
    db._ready = None
    yield TestClient(app)
    db._ready = None


def test_api_twin(server, checker_agrees):
    out = server.post("/api/twin", json={"spec": EXAMPLES["stamps-transfer"]}).json()
    assert out["available"] and out["how"] == "substitution" and out["check"] == "agreed"
    assert verify_v2(out["spec"]).ok and out["solved"]["asked"] == out["spec"]["answer"]
    assert server.post("/api/twin", json={"spec": MARBLES}).json()["available"] is False


def test_twin_lesson_is_challenge_first_and_offers_another(server, checker_agrees):
    twin = server.post("/api/twin", json={"spec": EXAMPLES["stamps-transfer"]}).json()["spec"]
    with server.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": twin, "twin": {"after_miss": True}})
        lesson = ws.receive_json()
        assert lesson["type"] == "lesson" and lesson["twin"] is True
        assert lesson["pacing"]["mode"] == "challenge_first" and "child" not in lesson
        ws.receive_json()  # the question is written
        ws.send_json({"type": "next"})
        ask = ws.receive_json()
        assert ask["type"] == "ask" and ask["key"] == "challenge"
        ws.send_json({"type": "text", "text": str(twin["answer"])})
        assert ws.receive_json()["result"] == "correct"
        ws.send_json({"type": "next"})
        while (ev := ws.receive_json())["type"] != "end":
            ws.send_json({"type": "next"})
    assert ev["twin"] == {"offer": True, "after_miss": False}
    ids = [b["id"] for b in ev["badges"]]
    assert "came_back" in ids and "before_after_builder" in ids  # anonymous: shown, not stored
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM badges").fetchone()[0] == 0
