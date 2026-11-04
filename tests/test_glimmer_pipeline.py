"""Offline tests for the Glimmer fine-tune pipeline: split, JSONL formatting, manifest. No network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from training.glimmer import common, split
from training.glimmer.label import is_val


def _paper(tmp: Path, pid: str, questions: list[dict], subject: str = "Mathematics", grade: str = "Primary 5") -> None:
    (tmp / "responses").mkdir(parents=True, exist_ok=True)
    (tmp / "responses" / f"{pid}.json").write_text(json.dumps({"paper": {"subject": subject, "grade": grade},
                                                                 "questions": questions}))


WORD = ("Ali had 3 times as many stamps as Ben. They had 240 stamps altogether. "
        "How many stamps did Ben have?")


def _q(text: str, answer, n: int = 1, kind: str = "open_ended") -> dict:
    return {"type": kind, "question_number": n, "question_text": text, "correct_answer": answer, "topic": "Whole Numbers"}


@pytest.mark.parametrize("raw,want", [("$1700", 1700), ("84 cm", 84), ("48.55%", 48.55), ("3/4", 0.75),
                                      ("2 1/2", 2.5), ("1,250", 1250), ("$9.60", 9.6), ("60 marbles", 60)])
def test_parse_key_reads_one_number(raw, want):
    assert split.parse_key(raw) == pytest.approx(want)


@pytest.mark.parametrize("raw", ["(a) 17/30; (b) 270", "38 and 75", "6 : 13", "", "$20\\pi$ cm", "(a) 36\n(b) 135"])
def test_parse_key_rejects_several_or_none(raw):
    assert split.parse_key(raw) is None


def test_skip_reason_filters_figures_tables_parts_and_sums():
    assert split.skip_reason(WORD) is None
    assert split.skip_reason(WORD + " ![img-1.jpeg](p1-img-1.jpeg)") == "image"
    assert split.skip_reason("The figure below shows a rectangle. " + WORD) is not None
    assert split.skip_reason("(a) " + WORD + " (b) How many did Ali have?") is not None
    assert split.skip_reason("| a | b |\n|---|---|\n| 1 | 2 | " + WORD) is not None
    assert split.skip_reason("Find the value of 3/4 + 1/2 and write your answer in simplest form.") is not None


def test_held_out_papers_are_stable_and_about_the_right_share():
    ids = [f"paper-{i:04d}" for i in range(2000)]
    first = [split.held_out_paper(p) for p in ids]
    assert first == [split.held_out_paper(p) for p in ids]  # deterministic
    assert 0.11 < sum(first) / len(ids) < 0.19
    assert is_val("paper-0001") == is_val("paper-0001")


def test_split_never_lets_a_test_question_into_train(tmp_path, monkeypatch):
    held = next(f"p{i}" for i in range(500) if split.held_out_paper(f"p{i}"))
    kept = next(f"p{i}" for i in range(500) if not split.held_out_paper(f"p{i}"))
    other = WORD.replace("240", "320")
    evalq = WORD.replace("240", "480")
    _paper(tmp_path, held, [_q(WORD, "60")])
    _paper(tmp_path, kept, [_q(WORD, "60"), _q(other, "80", 2), _q(evalq, "120", 3), _q(other + " ![x](y.png)", "80", 4)])
    _paper(tmp_path, "science", [_q(WORD, "60")], subject="Science")
    (tmp_path / "dataset").mkdir()
    (tmp_path / "dataset" / "school-sample-60.json").write_text(json.dumps([{"id": "e1", "question": evalq, "answer": "120"}]))
    rows, info = split.build_pool(private=tmp_path)
    train = {r["question"] for r in rows if r["split"] == "train"}
    test = {r["question"] for r in rows if r["split"] == "test"}
    assert train == {other}                       # the repeat of a held-out-paper question and the eval question are test
    assert WORD in test and evalq in test
    assert not train & test
    assert info["maths_papers"] == 2 and info["eval_in_pool"] == 1
    assert info["skipped"]["image"] == 1


def test_jsonl_messages_match_inference_request():
    from agent.glimmer import request_messages, system_prompt

    msgs = request_messages(WORD)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == system_prompt()
    assert json.loads(msgs[1]["content"])["question"] == WORD
    before_after = request_messages("Ali had some stamps. After he gave 20 stamps to Ben, he had 40 left. How many did he have at first?")
    assert json.loads(before_after[1]["content"])["looks_like"]


def test_build_keeps_only_verified_v2_rows_that_match_the_key(tmp_path, monkeypatch):
    from training.glimmer import label

    monkeypatch.setattr(common, "GLIMMER", tmp_path)
    monkeypatch.setattr(common, "RUNS_DOC", tmp_path / "docs")
    monkeypatch.setattr(label, "run_dir", lambda rid: common.run_dir(rid, tmp_path))
    monkeypatch.setattr(label, "update_manifest", lambda rid, s, d: common.update_manifest(rid, s, d, base=tmp_path, docs=tmp_path / "docs"))
    d = common.run_dir("t", tmp_path)
    pool = [{"id": f"q{i}", "paper": f"paper-{i}", "level": "P5", "family": "remainder", "question": WORD,
             "answer": 60.0, "split": "test" if i == 4 else "train"} for i in range(5)]
    spec = {"version": 2, "answer": 60}
    labels = [
        {"id": "q0", "outcome": "keep", "version": 2, "spec": spec, "level": "P5"},
        {"id": "q1", "outcome": "keep", "version": 1, "spec": {"model": {"answer": 60}}, "level": "P5"},
        {"id": "q2", "outcome": "disputed", "version": 2, "spec": spec, "level": "P5"},
        {"id": "q3", "outcome": "declined", "level": "P5"},
        {"id": "q4", "outcome": "keep", "version": 2, "spec": spec, "level": "P5"},  # a test row: never used
    ]
    common.write_jsonl(d / "pool.jsonl", pool)
    common.write_jsonl(d / "labels.jsonl", labels)
    report = label.run_build("t")
    rows = common.read_jsonl(d / "train.jsonl") + common.read_jsonl(d / "val.jsonl")
    assert len(rows) == 1 and report["train"]["n"] + report["val"]["n"] == 1
    msgs = rows[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant"]
    assert json.loads(msgs[2]["content"]) == spec


def test_manifest_merges_sections_and_commits_only_a_sanitised_copy(tmp_path):
    docs = tmp_path / "docs"
    common.update_manifest("r1", "split", {"train": {"n": 3}, "question": "secret text"}, base=tmp_path, docs=docs)
    m = common.update_manifest("r1", "train", {"hyper": {"lora_rank": 8}, "file": str(tmp_path / "x.jsonl"),
                                              "rows": [{"question": "secret", "id": "a"}]}, base=tmp_path, docs=docs)
    assert m["split"]["question"] == "secret text" and m["train"]["hyper"]["lora_rank"] == 8
    for key in ("run_id", "seed", "base_model", "git_commit", "updated"):
        assert key in m
    public = (docs / "r1.json").read_text()
    assert "secret" not in public and str(tmp_path) not in public
    assert json.loads(public)["train"]["rows"] == [{"id": "a"}]


def test_sha256_and_stable_fraction(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("abc")
    assert common.sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert common.sha256_file(tmp_path / "missing") is None
    assert 0 <= common.stable_fraction("x", "s") < 1


class _Checker:
    def __init__(self, answer):
        self._answer, self.rechecked = answer, False

    def answer(self):
        return self._answer

    def recheck(self, log):
        self.rechecked = True
        return False


def test_planner_hook_is_off_by_default_and_never_spends_the_recheck(monkeypatch):
    from concurrent.futures import Future

    from agent import glimmer, plan
    from agent.verify import VerifyResult

    problem = {"question": WORD}
    monkeypatch.delenv("PLANNER_MODEL", raising=False)
    assert plan._start_glimmer(problem) is None  # off: Spark ladder only

    monkeypatch.setenv("PLANNER_MODEL", "glimmer")
    monkeypatch.setattr(glimmer, "available", lambda: True)
    monkeypatch.setattr(glimmer, "glimmer_spec", lambda p: {"version": 2, "answer": 60})
    monkeypatch.setattr(plan, "_check_draft", lambda kind, spec, q: VerifyResult(ok=True, errors=[]))

    def draft():
        return plan._start_glimmer(problem)

    found = plan._take_glimmer(draft(), _Checker(60.0), WORD, [])
    assert found and found[1].check == "agreed" and found[1].planner == "glimmer"

    checker = _Checker(45.0)
    assert plan._take_glimmer(draft(), checker, WORD, []) is None and not checker.rechecked

    def down(p):
        raise RuntimeError("down")

    monkeypatch.setattr(glimmer, "glimmer_spec", down)
    log: list[str] = []
    assert plan._take_glimmer(draft(), _Checker(60.0), WORD, log) is None  # any Glimmer error falls back
    assert log == ["attempt glimmer -> error RuntimeError: down"]

    missed: Future = Future()
    missed.set_result((None, "invalid JSON"))
    assert plan._take_glimmer(missed, _Checker(60.0), WORD, []) is None


def test_glimmer_hit_wins_the_race_before_spark(monkeypatch):
    """A verified, agreed Glimmer draft returns while the Spark drafts are still running."""
    import threading

    from agent import glimmer, plan
    from agent.verify import VerifyResult

    release = threading.Event()

    def slow_spark(*a, **k):
        release.wait(5)
        return None, "model unavailable"

    monkeypatch.setenv("PLANNER_MODEL", "glimmer")
    monkeypatch.setattr(glimmer, "available", lambda: True)
    monkeypatch.setattr(glimmer, "glimmer_spec", lambda p: {"version": 2, "answer": 60})
    monkeypatch.setattr(plan, "_check_draft", lambda kind, spec, q: VerifyResult(ok=True, errors=[]))
    monkeypatch.setattr(plan, "_draft", slow_spark)
    log: list[str] = []
    found = plan._run_waves({"question": WORD}, None, plan.V2_WAVES, _Checker(60.0), log,
                            plan._start_glimmer({"question": WORD}))
    release.set()
    assert found and found[1].planner == "glimmer"
    assert not any(line.startswith("attempt 0") for line in log)  # Spark had not finished
