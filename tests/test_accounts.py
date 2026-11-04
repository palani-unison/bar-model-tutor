"""Accounts: the store (CRUD, cascade delete, snapshots), signed sessions, Google sign-in (mocked), dev login,
child login on a family device, rate limiting."""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import accounts, db
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.delenv("DB_LOCAL_COPY", raising=False)
    monkeypatch.delenv("PUBLIC_DEPLOY", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("ALLOW_DEV_LOGIN", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setattr(accounts, "child_login_limit", accounts.RateLimit(10, 300))
    monkeypatch.setattr(accounts, "parent_login_limit", accounts.RateLimit(20, 300))
    monkeypatch.setattr(accounts, "kid_name_limit", accounts.RateLimit(5, 600))
    monkeypatch.setattr(accounts, "kid_client_limit", accounts.RateLimit(15, 600))
    db._ready = None
    yield tmp_path
    db._ready = None


# ---------------------------------------------------------------- store
def test_db_crud_and_cascade_delete():
    p = db.upsert_parent("google:1", "Mei")
    assert db.upsert_parent("google:1", "Mei Ling")["id"] == p["id"]  # same sub, same parent
    a = db.add_child(p["id"], "Ah Boy", "P4", "🦊", "brave-otter-42")
    b = db.add_child(p["id"], "Girl", "P6", "🐼", "calm-panda-11")
    assert [c["nickname"] for c in db.list_children(p["id"])] == ["Ah Boy", "Girl"]
    assert db.update_child(a["id"], p["id"], level="P5")["level"] == "P5"
    other = db.upsert_parent("google:2", "Other")
    assert db.update_child(a["id"], other["id"], level="P1") is None  # not their child
    assert db.delete_child(a["id"], other["id"]) is False
    db.add_attempts(a["id"], "before_after", "P4", [("unit", "first try", 6.0), ("answer", "shown", 30.0)])
    db.set_mastery(a["id"], "before_after", 1120.0, 2, 12.0)
    assert len(db.attempts(a["id"])) == 2 and db.get_mastery(a["id"], "before_after")["rating"] == 1120.0
    assert db.delete_child(a["id"], p["id"]) is True
    assert db.attempts(a["id"]) == [] and db.get_mastery(a["id"], "before_after") is None  # cascade
    db.add_attempts(b["id"], "remainder", "P6", [("unit", "first try", 5.0)])
    assert db.delete_parent(p["id"])
    assert db.get_child(b["id"]) is None and db.attempts(b["id"]) == []


def test_store_has_no_question_text_columns(fresh_db):
    db.upsert_parent("google:1", "Mei")
    con = sqlite3.connect(fresh_db / "app.db")
    cols = {r[1] for t in ("parents", "children", "attempts", "mastery") for r in con.execute(f"PRAGMA table_info({t})")}
    assert not cols & {"question", "text", "photo", "image", "audio", "email", "spec"}


def test_local_copy_snapshots_to_durable_path(fresh_db, monkeypatch):
    durable, local = fresh_db / "vol" / "app.db", fresh_db / "local" / "app.db"
    monkeypatch.setenv("DB_PATH", str(durable))
    monkeypatch.setenv("DB_LOCAL_COPY", str(local))
    commits = []
    db.set_persist_hook(lambda: commits.append(1))
    try:
        db.upsert_parent("google:9", "Snap")
        assert local.exists() and not durable.exists()  # debounced: nothing written to the volume yet
        db.flush()
        assert durable.exists() and commits == [1]
        local.unlink()
        db._ready = None
        assert db.get_parent(1)["display_name"] == "Snap"  # a new container restores from the snapshot
    finally:
        db.set_persist_hook(None)
        if db._timer:
            db._timer.cancel()


# ---------------------------------------------------------------- sessions
def test_session_signing_and_tampering():
    token = accounts.sign({"p": 3}, 60)
    assert accounts.unsign(token)["p"] == 3
    raw, sig = token.rsplit(".", 1)
    import base64
    import json
    forged = base64.urlsafe_b64encode(json.dumps({"p": 4, "exp": 9999999999}).encode()).decode().rstrip("=")
    assert accounts.unsign(f"{forged}.{sig}") is None  # payload swapped
    assert accounts.unsign(f"{raw}.{'0' * len(sig)}") is None  # signature swapped
    assert accounts.unsign("garbage") is None and accounts.unsign(None) is None
    assert accounts.unsign(accounts.sign({"p": 3}, -10)) is None  # expired


def test_session_cookie_flags_and_tampered_cookie_is_anonymous(monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    c = TestClient(app)
    r = c.post("/api/auth/dev", json={"name": "Mum"})
    cookie = r.headers.get_list("set-cookie")
    assert any("bmt_session=" in h and "HttpOnly" in h and "SameSite=lax" in h for h in cookie)
    assert c.get("/api/me").json()["parent"]["name"] == "Mum"
    tampered = c.cookies.get("bmt_session")[:-2] + "00"
    c.cookies.clear()
    c.cookies.set("bmt_session", tampered)
    assert c.get("/api/me").json()["parent"] is None
    assert c.get("/api/children").status_code == 401


# ---------------------------------------------------------------- Google and dev login
def test_dev_login_is_off_by_default():
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": "x"}).status_code == 404
    assert c.get("/api/me").json()["dev_login"] is False


def test_dev_login_is_off_when_google_is_configured(monkeypatch):
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    assert TestClient(app).post("/api/auth/dev", json={"name": "x"}).status_code == 404


def test_google_sign_in_verified_and_refused(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    seen = {}

    def fake_verify(token, request, audience):
        seen["audience"] = audience
        if token != "good":
            raise ValueError("Token has wrong audience")
        return {"iss": "https://accounts.google.com", "sub": "1234", "given_name": "Siew Ling", "email": "x@y.z"}

    from google.oauth2 import id_token
    monkeypatch.setattr(id_token, "verify_oauth2_token", fake_verify)
    c = TestClient(app)
    assert c.post("/api/auth/google", json={"credential": "bad"}).status_code == 401
    r = c.post("/api/auth/google", json={"credential": "good"})
    assert r.status_code == 200 and seen["audience"] == "abc.apps.googleusercontent.com"
    me = c.get("/api/me").json()
    assert me["parent"]["name"] == "Siew" and me["family"] == []
    with db.connect() as con:  # no email kept, only the sub and a first name
        row = dict(con.execute("SELECT * FROM parents").fetchone())
    assert row["google_sub"] == "google:1234" and "x@y.z" not in str(row)


def test_google_wrong_issuer_refused(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "abc")
    from google.oauth2 import id_token
    monkeypatch.setattr(id_token, "verify_oauth2_token", lambda t, r, a: {"iss": "evil.com", "sub": "1"})
    assert TestClient(app).post("/api/auth/google", json={"credential": "x"}).status_code == 401


# ---------------------------------------------------------------- children and child login
def _parent(monkeypatch, name="Mum"):
    monkeypatch.setenv("ALLOW_DEV_LOGIN", "1")
    c = TestClient(app)
    assert c.post("/api/auth/dev", json={"name": name}).status_code == 200
    c.post("/api/me/consent", json={"version": accounts.TERMS_VERSION, "agree": True})
    return c


def test_children_crud_and_child_login_on_family_device(monkeypatch):
    c = _parent(monkeypatch)
    r = c.post("/api/children", json={"nickname": "Kai <b>", "level": "P5", "avatar": "🐯"})
    kid = r.json()["child"]
    assert kid["nickname"] == "Kai b" and re.fullmatch(r"[A-Z][a-z]+\d{3}", kid["code_name"])
    assert c.post("/api/children", json={"nickname": "x", "level": "P9"}).status_code == 422
    assert c.patch(f"/api/children/{kid['id']}", json={"avatar": "🦉"}).json()["child"]["avatar"] == "🦉"
    # The child picks their avatar on this (family) device: no password, and the parent role is dropped.
    r = c.post("/api/child/login", json={"child_id": kid["id"]})
    assert r.status_code == 200
    me = c.get("/api/me").json()
    assert me["child"]["nickname"] == "Kai b" and me["parent"] is None and len(me["family"]) == 1
    assert c.get("/api/children").status_code == 401  # a child can't manage profiles
    assert c.get("/api/child/progress").json()["progress"] == []
    # By code name too.
    assert c.post("/api/child/login", json={"code_name": kid["code_name"].upper()}).status_code == 200


def test_code_name_is_useless_on_another_device_or_family(monkeypatch):
    c = _parent(monkeypatch)
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P5", "avatar": "🐯"}).json()["child"]
    stranger = TestClient(app)  # never had a parent sign in
    assert stranger.post("/api/child/login", json={"code_name": kid["code_name"]}).status_code == 403
    other = _parent(monkeypatch, "Other")  # another family's device
    assert other.post("/api/child/login", json={"code_name": kid["code_name"]}).status_code == 404
    assert other.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 404
    assert other.delete(f"/api/children/{kid['id']}").status_code == 404
    assert other.get(f"/api/child/progress?child_id={kid['id']}").status_code == 404


def test_child_login_is_rate_limited(monkeypatch):
    c = _parent(monkeypatch)
    codes = [c.post("/api/child/login", json={"code_name": f"no-such-{i}"}).status_code for i in range(12)]
    assert codes[:10] == [404] * 10 and codes[10:] == [429, 429]


def test_parent_deletes_child_and_account(monkeypatch):
    c = _parent(monkeypatch)
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P5", "avatar": "🐯"}).json()["child"]
    db.add_attempts(kid["id"], "remainder", "P5", [("unit", "first try", 4.0)])
    assert c.get(f"/api/child/progress?child_id={kid['id']}").status_code == 200
    assert c.delete(f"/api/children/{kid['id']}").json() == {"ok": True}
    assert db.attempts(kid["id"]) == [] and c.get("/api/children").json()["children"] == []
    c.post("/api/children", json={"nickname": "Two", "level": "P3", "avatar": "🐼"})
    assert c.delete("/api/me").json() == {"ok": True}
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM parents").fetchone()[0] == 0


def test_logout_keeps_family_device_unless_forgotten(monkeypatch):
    c = _parent(monkeypatch)
    c.post("/api/auth/logout")
    me = c.get("/api/me").json()
    assert me["parent"] is None and me["family"] == []
    c.post("/api/auth/logout", json={"forget_device": True})
    assert c.get("/api/me").json()["family"] is None


# ---------------------------------------------------------------- hero names, onboarding, kid passwords
def test_hero_names_are_kid_style_and_unique(monkeypatch):
    p = db.upsert_parent("google:1", "Mei")
    for _ in range(40):
        code = accounts.new_code_name()
        assert re.fullmatch(r"[A-Z][a-z]+[1-9]\d\d", code) and code[:-3] in accounts.HERO_WORDS
    assert len(accounts.HERO_WORDS) >= 60 and len(set(accounts.HERO_WORDS)) == len(accounts.HERO_WORDS)
    db.add_child(p["id"], "Old", "P4", "🦊", "brave-otter-42")
    assert db.code_name_taken("THOR123") is False
    db.add_child(p["id"], "New", "P4", "🦊", "Thor123")
    assert db.code_name_taken("thor123") and db.code_name_taken("Brave-Otter-42")  # case-insensitive
    # Every name is taken: the generator gives up rather than reusing one.
    monkeypatch.setattr(db, "code_name_taken", lambda code: True)
    with pytest.raises(Exception):
        accounts.new_code_name()


def test_onboarding_name_children_and_me(monkeypatch):
    c = _parent(monkeypatch, "Siew Ling")
    me = c.get("/api/me").json()
    assert me["parent"]["kids"] == 0 and me["max_children"] == accounts.MAX_CHILDREN  # new parent: onboarding
    assert c.patch("/api/me", json={"name": "Mrs <Tan>"}).json()["parent"]["name"] == "Mrs Tan"
    assert c.patch("/api/me", json={"name": "<>"}).status_code == 422
    c.post("/api/auth/dev", json={"name": "Siew Ling"})  # signing in again keeps the name the parent chose
    assert c.get("/api/me").json()["parent"]["name"] == "Mrs Tan"
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P3", "avatar": "🐼"}).json()["child"]
    assert kid["has_password"] is False and c.get("/api/me").json()["parent"]["kids"] == 1
    assert TestClient(app).patch("/api/me", json={"name": "x"}).status_code == 401


def _kid_with_password(monkeypatch, pw="Moon-Tiger-7", name="Mum"):
    c = _parent(monkeypatch, name)
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P5", "avatar": "🐯"}).json()["child"]
    r = c.post(f"/api/children/{kid['id']}/password", json={"password": pw})
    assert r.status_code == 200 and r.json()["password"] == pw.lower() and r.json()["child"]["has_password"]
    return c, kid


def _parent_of(kid):
    return db.get_child(kid["id"])["parent_id"]


def test_password_is_stored_as_salted_hash_only(monkeypatch, fresh_db):
    c, kid = _kid_with_password(monkeypatch)
    _, kid2 = _kid_with_password(monkeypatch, name="Dad")
    con = sqlite3.connect(fresh_db / "app.db")
    rows = dict(con.execute("SELECT id, pw_hash FROM children").fetchall())
    assert all(h.startswith("scrypt$") and "moon" not in h for h in rows.values())
    assert rows[kid["id"]] != rows[kid2["id"]]  # same password, different salt
    assert "scrypt" not in str(c.get("/api/children").json()) and "scrypt" not in str(c.get("/api/me").json())
    assert c.post(f"/api/children/{kid['id']}/password", json={"password": "abc"}).status_code == 422
    assert re.fullmatch(r"[a-z]+-[a-z]+-[1-9]", c.get("/api/children/password-suggestion").json()["password"])
    assert c.post(f"/api/children/{kid['id']}/password", json={}).json()["password"].count("-") == 2


def test_kid_signs_in_with_hero_name_and_password_on_any_device(monkeypatch):
    _, kid = _kid_with_password(monkeypatch)
    phone = TestClient(app)  # never saw a parent
    assert phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "wrong"}).status_code == 401
    assert phone.post("/api/kid/login", json={"hero": "Nobody999", "password": "moon-tiger-7"}).status_code == 401
    r = phone.post("/api/kid/login", json={"hero": kid["code_name"].lower(), "password": " Moon-tiger-7 "})
    assert r.status_code == 200
    me = phone.get("/api/me").json()
    assert me["child"]["code_name"] == kid["code_name"] and me["parent"] is None and me["family"] is None
    assert phone.get("/api/child/progress").status_code == 200
    sess = accounts.session_of(phone.cookies)
    assert sess["c"] == kid["id"] and sess["f"] == _parent_of(kid)  # the same session the family device makes


def test_kid_without_password_cannot_sign_in_by_password(monkeypatch):
    c = _parent(monkeypatch)
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P5", "avatar": "🐯"}).json()["child"]
    assert TestClient(app).post("/api/kid/login", json={"hero": kid["code_name"], "password": ""}).status_code == 401
    assert TestClient(app).post("/api/kid/login", json={"hero": kid["code_name"], "password": "anything"}).status_code == 401


def test_reset_invalidates_old_password_and_old_sessions(monkeypatch):
    parent, kid = _kid_with_password(monkeypatch)
    phone = TestClient(app)
    assert phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "moon-tiger-7"}).status_code == 200
    family = TestClient(app)  # the family device: parent signs in, child taps their avatar
    family.post("/api/auth/dev", json={"name": "Mum"})
    assert family.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 200
    assert parent.post(f"/api/children/{kid['id']}/password", json={"password": "sun-frog-3"}).status_code == 200
    assert phone.get("/api/me").json()["child"] is None  # signed out everywhere
    assert phone.get("/api/child/progress").status_code == 401
    assert family.get("/api/me").json()["child"] is None
    assert phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "moon-tiger-7"}).status_code == 401
    assert phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "sun-frog-3"}).status_code == 200
    assert family.post("/api/child/login", json={"child_id": kid["id"]}).status_code == 200  # avatar tap still works
    assert family.get("/api/me").json()["child"]["id"] == kid["id"]


def test_another_family_cannot_reset_or_see_password(monkeypatch):
    _, kid = _kid_with_password(monkeypatch)
    other = _parent(monkeypatch, "Other")
    assert other.post(f"/api/children/{kid['id']}/password", json={"password": "mine-1234"}).status_code == 404
    assert kid["id"] not in [c["id"] for c in other.get("/api/children").json()["children"]]
    assert TestClient(app).post(f"/api/children/{kid['id']}/password", json={"password": "mine-1234"}).status_code == 401
    assert TestClient(app).get("/api/children/password-suggestion").status_code == 401
    kid_client = TestClient(app)
    kid_client.post("/api/kid/login", json={"hero": kid["code_name"], "password": "moon-tiger-7"})
    assert kid_client.post(f"/api/children/{kid['id']}/password", json={"password": "mine-1234"}).status_code == 401


def test_kid_login_rate_limited_per_hero_name_and_client(monkeypatch):
    _, kid = _kid_with_password(monkeypatch)
    phone = TestClient(app)
    codes = [phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": f"no-{i}"}).status_code for i in range(6)]
    assert codes == [401] * 5 + [429]
    r = TestClient(app).post("/api/kid/login", json={"hero": kid["code_name"].upper(), "password": "moon-tiger-7"})
    assert r.status_code == 429 and "parent" in r.json()["detail"]  # per name: even the right password waits
    # Per client: many names from one address.
    monkeypatch.setattr(accounts, "kid_name_limit", accounts.RateLimit(100, 600))
    monkeypatch.setattr(accounts, "kid_client_limit", accounts.RateLimit(15, 600))
    other = TestClient(app)
    codes = [other.post("/api/kid/login", json={"hero": f"Nobody{i}", "password": "x1234"}).status_code for i in range(16)]
    assert codes[:15] == [401] * 15 and codes[15] == 429


def test_deleting_child_or_account_removes_password(monkeypatch, fresh_db):
    parent, kid = _kid_with_password(monkeypatch)
    phone = TestClient(app)
    phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "moon-tiger-7"})
    assert parent.delete(f"/api/children/{kid['id']}").json() == {"ok": True}
    assert db.child_password(kid["id"]) is None and phone.get("/api/me").json()["child"] is None
    assert phone.post("/api/kid/login", json={"hero": kid["code_name"], "password": "moon-tiger-7"}).status_code == 401
    kid2 = parent.post("/api/children", json={"nickname": "Two", "level": "P3", "avatar": "🐼"}).json()["child"]
    parent.post(f"/api/children/{kid2['id']}/password", json={"password": "star-kite-2"})
    assert parent.delete("/api/me").json() == {"ok": True}
    con = sqlite3.connect(fresh_db / "app.db")
    assert con.execute("SELECT COUNT(*) FROM children").fetchone()[0] == 0


def test_kid_link_prefills_hero_name_and_is_not_a_key(monkeypatch):
    _, kid = _kid_with_password(monkeypatch)
    phone = TestClient(app)
    r = phone.get(f"/k/{kid['code_name']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/?kid={kid['code_name']}"
    assert "set-cookie" not in r.headers and phone.get("/api/me").json()["child"] is None
    assert phone.get("/k/<b>x", follow_redirects=False).headers["location"] == "/?kid=bx"


def test_kid_link_and_login_are_the_front_door_on_a_public_deploy(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEPLOY", "1")  # admin-only now: kids sign in with hero name + password
    c = TestClient(app)
    assert c.get("/k/Thor123", follow_redirects=False).status_code in (302, 303, 307)
    assert c.post("/api/kid/login", json={"hero": "Thor123", "password": "x1234"}).status_code in (401, 404)


def test_old_sessions_without_version_work_until_a_reset(monkeypatch):
    c = _parent(monkeypatch)
    kid = c.post("/api/children", json={"nickname": "Kai", "level": "P5", "avatar": "🐯"}).json()["child"]
    old = TestClient(app)  # a cookie signed before kid passwords existed: no "v"
    old.cookies.set("bmt_session", accounts.sign({"c": kid["id"], "f": _parent_of(kid)}, 60))
    assert old.get("/api/me").json()["child"]["id"] == kid["id"]
    c.post(f"/api/children/{kid['id']}/password", json={"password": "moon-tiger-7"})
    assert old.get("/api/me").json()["child"] is None


def test_db_migrates_children_table_without_password_columns(fresh_db):
    con = sqlite3.connect(fresh_db / "app.db")
    con.executescript("CREATE TABLE parents (id INTEGER PRIMARY KEY, google_sub TEXT NOT NULL UNIQUE, "
                      "display_name TEXT NOT NULL DEFAULT '', created REAL NOT NULL);"
                      "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parents(id) "
                      "ON DELETE CASCADE, nickname TEXT NOT NULL, level TEXT NOT NULL, avatar TEXT NOT NULL, "
                      "code_name TEXT NOT NULL UNIQUE, created REAL NOT NULL);"
                      "INSERT INTO parents VALUES (1, 'google:1', 'Mei', 0);"
                      "INSERT INTO children VALUES (1, 1, 'Kai', 'P4', '🦊', 'brave-otter-42', 0);")
    con.commit()
    con.close()
    child = db.get_child(1)
    assert child["code_name"] == "brave-otter-42" and child["pw_ver"] == 0 and not child["has_password"]
    assert db.child_by_code("Brave-Otter-42", 1)["id"] == 1  # old code names keep working
