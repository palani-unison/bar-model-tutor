"""My lessons: the store (cap, cascade delete), outcome and status text, recording over the lesson socket
(new lesson, replay updates the same row, anonymous records nothing), and the privacy boundary of the API."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import accounts, db, lesson_ws, lessons
from app.main import app

EXAMPLES = Path(__file__).resolve().parent.parent / "specs" / "examples"
MARBLES = json.loads((EXAMPLES / "marbles.json").read_text(encoding="utf-8"))
CAKES = json.loads((EXAMPLES / "v2" / "units_and_value-cakes.json").read_text(encoding="utf-8"))
RIGHT = {"units": "4", "unit": "30", "answer": "90", "challenge": "90"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setattr(lesson_ws.tts, "speak_wav", lambda text: b"RIFF")
    monkeypatch.setattr(accounts, "child_login_limit", accounts.RateLimit(100, 300))
    db._ready = None
    yield
    db._ready = None


def _family(name="Mum", kids=("Kai",)):
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": name}).status_code == 200
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    out = [c.post("/api/children", json={"nickname": k, "level": "P4", "avatar": "🐯"}).json()["child"] for k in kids]
    return c, out


def _as_child(c, kid):
    assert c.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 200
    return c


def _play(client, spec, answer_for, **start):
    events = []
    with client.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": spec, **start})
        events.append(ws.receive_json())
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "ask":
                ws.send_json({"type": "text", "text": answer_for(ev)})
                verdict = ws.receive_json()
                events.append(verdict)
                while verdict["waiting"]:  # ask again: a wrong answer each time ends in a hint, then "shown"
                    ws.send_json({"type": "text", "text": answer_for(ev)})
                    verdict = ws.receive_json()
                    events.append(verdict)
            elif ev["type"] == "end":
                return events
            ws.send_json({"type": "next"})


def _save(child_id, title="Marbles: 3 times as many", heuristic="part_whole"):
    return db.add_lesson(child_id, json.dumps(MARBLES), None, title, heuristic, "P4", "[1, 0.3]", 9)


# ---------------------------------------------------------------- store
def test_store_cap_keeps_kept_lessons(monkeypatch):
    p = db.upsert_parent("google:1", "Mei")
    kid = db.add_child(p["id"], "Kai", "P4", "🐯", "brave-otter-42")
    monkeypatch.setattr(db, "MAX_LESSONS", 3)
    first = _save(kid["id"])
    db.set_lesson_kept(first, kid["id"], True)
    ids = [_save(kid["id"]) for _ in range(4)]
    left = [r["id"] for r in db.list_lessons(kid["id"])]
    assert first in left and ids[0] not in left and len(left) == 4  # the kept one stays, the oldest other goes
    assert [r["id"] for r in db.list_lessons(kid["id"], "kept")] == [first]


def test_lessons_are_deleted_with_the_child_and_the_account():
    p = db.upsert_parent("google:1", "Mei")
    a = db.add_child(p["id"], "Kai", "P4", "🐯", "brave-otter-42")
    b = db.add_child(p["id"], "Mei", "P5", "🐼", "calm-panda-11")
    la, lb = _save(a["id"]), _save(b["id"])
    assert db.delete_child(a["id"], p["id"])
    assert db.get_lesson(la, a["id"]) is None and db.get_lesson(lb, b["id"]) is not None
    assert db.delete_parent(p["id"])
    assert db.get_lesson(lb, b["id"]) is None


def test_finish_run_and_status_text():
    p = db.upsert_parent("google:1", "Mei")
    kid = db.add_child(p["id"], "Kai", "P4", "🐯", "brave-otter-42")
    lid = _save(kid["id"])
    assert lessons.status_text(db.get_lesson(lid, kid["id"])) == "To redo"  # started, not finished
    db.finish_lesson_run(lid, kid["id"], "redo", 0, 40.0)
    assert db.list_lessons(kid["id"], "redo")[0]["id"] == lid
    assert db.start_lesson_run(lid, kid["id"])
    db.finish_lesson_run(lid, kid["id"], "solved", 0, 30.0)
    row = db.get_lesson(lid, kid["id"])
    assert row["runs"] == 2 and row["solved_on"] == 2 and lessons.status_text(row) == "Solved on 2nd try"
    assert db.list_lessons(kid["id"], "redo") == []
    assert lessons.status_text({"status": "solved", "solved_on": 1}) == "Solved first try"
    assert lessons.status_text({"status": "hints", "hints": 2}) == "Solved with 2 hints"
    assert lessons.status_text({"status": "hints", "hints": 1}) == "Solved with 1 hint"


def test_outcome_from_score():
    assert lessons.outcome({"unit": "first try", "answer": "first try"}, 0) == ("solved", 0)
    assert lessons.outcome({"unit": "with hints", "answer": "first try"}, 2) == ("hints", 2)
    assert lessons.outcome({"unit": "with hints"}, 0) == ("hints", 1)
    assert lessons.outcome({"unit": "shown", "answer": "first try"}, 3) == ("redo", 3)
    assert lessons.outcome({"challenge": "first try"}, 0) == ("solved", 0)


def test_streak_counts_days_in_a_row():
    day = 86400
    now = 100 * day + 3600
    assert lessons.streak([now, now - day, now - 2 * day, now - 4 * day], now=now) == 3
    assert lessons.streak([now - day, now - 2 * day], now=now) == 2  # nothing yet today: still a streak
    assert lessons.streak([now - 3 * day], now=now) == 0
    assert lessons.streak([], now=now) == 0


def test_thumb_from_the_first_stage():
    assert lessons._thumb(MARBLES, None) == [0.333, 1.0]
    sol = lesson_ws.solve_spec(CAKES).as_json()
    thumb = lessons._thumb(CAKES, sol)
    assert thumb and max(thumb) == 1.0 and all(0 <= v <= 1 for v in thumb)


# ---------------------------------------------------------------- recording over the socket
def test_signed_in_lesson_is_saved_and_replay_updates_the_same_row():
    c, (kid,) = _family()
    _as_child(c, kid)
    events = _play(c, MARBLES, lambda ev: "7" if ev["key"] == "unit" else RIGHT[ev["key"]])
    lid = events[0]["lesson_id"]
    row = db.get_lesson(lid, kid["id"], with_spec=True)
    assert json.loads(row["spec"]) == MARBLES and row["status"] == "redo" and row["progress"] == 1 and row["runs"] == 1
    replay = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]], lesson_id=lid)
    assert replay[0]["lesson_id"] == lid
    rows = db.list_lessons(kid["id"])
    assert len(rows) == 1 and rows[0]["runs"] == 2 and rows[0]["status"] == "solved"
    assert lessons.status_text(rows[0]) == "Solved on 2nd try"


def test_hints_are_counted():
    c, (kid,) = _family()
    _as_child(c, kid)
    answers = iter(["4", "25", "26", "30", "90"])
    events = _play(c, MARBLES, lambda ev: next(answers))
    row = db.get_lesson(events[0]["lesson_id"], kid["id"])
    assert row["status"] == "hints" and lessons.status_text(row) == "Solved with 2 hints"  # a clue after 25 and after 26


def test_v2_lesson_saves_solved_values():
    c, (kid,) = _family()
    _as_child(c, kid)
    with c.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "start", "spec": CAKES})
        ev = ws.receive_json()
    row = db.get_lesson(ev["lesson_id"], kid["id"], with_spec=True)
    assert json.loads(row["solved"]) == ev["solved"] and row["status"] == "unfinished" and row["heuristic"] == CAKES["heuristic"]


def test_prepared_lesson_is_saved_when_its_plan_lands(monkeypatch):
    import time

    from agent.verify_v2 import verify_spec_v2
    from app import main as app_main

    monkeypatch.setattr(app_main, "plan_problem", lambda problem: (CAKES, verify_spec_v2(CAKES), []))
    c, (kid,) = _family()
    _as_child(c, kid)
    with c.websocket_connect("/ws/lesson") as ws:
        ws.send_json({"type": "prepare", "question": CAKES["question"]})
        assert ws.receive_json()["type"] == "prepare"
        assert ws.receive_json()["step"]["do"] == "write_question"
        time.sleep(0.3)
        ws.send_json({"type": "next"})
        assert ws.receive_json()["type"] == "plan"
        ev = ws.receive_json()
    assert ev["type"] == "lesson" and ev["prepared"]
    row = db.get_lesson(ev["lesson_id"], kid["id"], with_spec=True)
    assert json.loads(row["spec"]) == CAKES and json.loads(row["solved"]) == ev["solved"]


def test_anonymous_lesson_records_nothing():
    c = TestClient(app)
    events = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]])
    assert "lesson_id" not in events[0]
    p = db.upsert_parent("google:x", "X")
    assert db.list_children(p["id"]) == []
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 0


def test_replay_of_another_childs_lesson_id_makes_a_new_row_and_leaves_theirs():
    c, (kai, mei) = _family(kids=("Kai", "Mei"))
    theirs = _save(mei["id"])
    _as_child(c, kai)
    events = _play(c, MARBLES, lambda ev: RIGHT[ev["key"]], lesson_id=theirs)
    assert events[0]["lesson_id"] != theirs
    other = db.get_lesson(theirs, mei["id"])
    assert other["runs"] == 1 and other["status"] == "unfinished"


# ---------------------------------------------------------------- API and privacy
def test_list_get_keep_own_lessons():
    c, (kid,) = _family()
    a = _save(kid["id"], "Marbles: 3 times as many")
    b = _save(kid["id"], "Cakes: units and value", "units_and_value")
    db.finish_lesson_run(b, kid["id"], "solved", 0, 70.0)
    db.set_lesson_progress(a, kid["id"], 0.4)
    _as_child(c, kid)
    j = c.get("/api/lessons").json()
    assert [r["id"] for r in j["lessons"]] == [a, b]  # the unfinished one is pinned first
    assert j["lessons"][0]["unfinished"] and j["lessons"][0]["progress"] == 0.4 and j["lessons"][0]["label"] == "3 times as many"
    assert j["stats"] == {"lessons": 2, "stars": 0, "streak": 1}
    assert {f["family"] for f in j["families"]} == {"part_whole", "units_and_value"}
    assert all(f["mastered"] is False for f in j["families"])
    assert [r["id"] for r in c.get("/api/lessons", params={"family": "units_and_value"}).json()["lessons"]] == [b]
    assert [r["id"] for r in c.get("/api/lessons", params={"which": "redo"}).json()["lessons"]] == [a]
    one = c.get(f"/api/lessons/{a}").json()
    assert one["spec"] == MARBLES and one["solved"] is None
    assert c.post(f"/api/lessons/{a}/keep", json={"kept": True}).json()["kept"] is True
    assert [r["id"] for r in c.get("/api/lessons", params={"which": "kept"}).json()["lessons"]] == [a]
    assert c.post(f"/api/lessons/{a}/keep", json={"kept": False}).json()["kept"] is False


def test_a_child_never_sees_another_childs_lessons():
    c, (kai, mei) = _family(kids=("Kai", "Mei"))
    mine, theirs = _save(kai["id"]), _save(mei["id"])
    _as_child(c, kai)
    assert [r["id"] for r in c.get("/api/lessons").json()["lessons"]] == [mine]
    assert c.get(f"/api/lessons/{theirs}").status_code == 404
    assert c.post(f"/api/lessons/{theirs}/keep", json={"kept": True}).status_code == 404
    assert db.get_lesson(theirs, mei["id"])["kept"] == 0
    assert c.get("/api/lessons", params={"child_id": mei["id"]}).status_code == 401  # child_id is for parents only


def test_other_families_and_anonymous_get_nothing():
    c1, (kai,) = _family("Mum")
    lid = _save(kai["id"])
    c2, (zoe,) = _family("Other")
    assert c2.get("/api/lessons", params={"child_id": kai["id"]}).status_code == 404  # not their child
    _as_child(c2, zoe)
    assert c2.get("/api/lessons").json()["lessons"] == []
    assert c2.get(f"/api/lessons/{lid}").status_code == 404
    anon = TestClient(app)
    assert anon.get("/api/lessons").status_code == 401
    assert anon.get(f"/api/lessons/{lid}").status_code == 401
    assert anon.post(f"/api/lessons/{lid}/keep", json={"kept": True}).status_code == 401


def test_parent_lists_own_childs_lessons_but_cannot_open_the_spec():
    c, (kai,) = _family()
    lid = _save(kai["id"])
    assert [r["id"] for r in c.get("/api/lessons", params={"child_id": kai["id"]}).json()["lessons"]] == [lid]
    assert c.get(f"/api/lessons/{lid}").status_code == 401  # replay is the child's own
