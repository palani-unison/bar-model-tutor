"""Parent accounts (Google Sign-In), child profiles and the family-device child login.

Sign-in is the front door on a public deploy (app/access.py); the owner's Google account (ADMIN_EMAILS) is the only admin.

Parent: Google Identity Services in the browser gives an ID token; the server verifies it with google-auth
against GOOGLE_CLIENT_ID and keeps only Google's stable `sub` and a first name (no email).
Dev login (local testing only): when GOOGLE_CLIENT_ID is unset AND ALLOW_DEV_LOGIN=1.

Child, two ways in:
  * On a *family device* (a browser where their parent signed in with Google, which then holds a signed,
    HttpOnly "family" cookie for that parent, 180 days) the child taps their avatar or types their hero name;
    either must belong to that family. No password there.
  * On any device: hero name ("Thor987") + a simple password the parent set. Only a salted scrypt hash is
    stored; a reset bumps the child's pw_ver, which every child session carries, so the old password and every
    session made with it stop working at once. Tries are rate-limited per hero name and per client.
  /k/<HeroName> only opens the kid sign-in with the name filled in; it is not a key.
Picking a child drops the parent role on that browser (a child can't open the parent panel); the parent signs
in again to manage profiles.

Sessions are signed cookies: base64(json).HMAC-SHA256 with SESSION_SECRET (generated at start-up if unset,
which signs everyone out on restart). HttpOnly, SameSite=Lax, Secure on https.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import db

logger = logging.getLogger("bar_model_tutor.accounts")


@asynccontextmanager
async def _lifespan(_app):
    yield
    db.flush()  # at shutdown: the last snapshot reaches DB_PATH (and the Modal Volume) now


router = APIRouter(lifespan=_lifespan)

SESSION_COOKIE, FAMILY_COOKIE = "bmt_session", "bmt_family"
SESSION_TTL_S = 30 * 24 * 3600
FAMILY_TTL_S = 180 * 24 * 3600
LEVELS = ("P1", "P2", "P3", "P4", "P5", "P6")
AVATARS = ("🦊", "🐼", "🐯", "🐨", "🐸", "🦉", "🐙", "🦄", "🐢", "🐧", "🐰", "🦁")
MAX_CHILDREN = 8
# The parent consent terms (docs/terms.md, served at /terms). Bump the date when they change: parents agree again.
TERMS_VERSION = "2026-11-04"
# Hero names: a myth, space or nature word and three digits ("Thor987"). Generic myth names only, no
# film or cartoon characters, and nothing that reads rude when glued to a number.
HERO_WORDS = ("Thor", "Nova", "Atlas", "Luna", "Orion", "Comet", "Blaze", "Echo", "Pixel", "Rocket", "Storm",
              "Tiger", "Falcon", "Phoenix", "Titan", "Zephyr", "Aurora", "Cosmo", "Astro", "Nebula", "Vega", "Lyra",
              "Draco", "Apollo", "Athena", "Odin", "Freya", "Hermes", "Iris", "Juno", "Kestrel", "Lynx", "Maple",
              "Willow", "Cedar", "River", "Coral", "Pebble", "Sunny", "Spark", "Breeze", "Bolt", "Jet", "Zoom",
              "Ranger", "Scout", "Pilot", "Ninja", "Dragon", "Griffin", "Panda", "Otter", "Koala", "Dolphin", "Orca",
              "Puma", "Jaguar", "Eagle", "Hawk", "Fox", "Wolf", "Bear", "Meteor", "Galaxy", "Quasar", "Saturn")
# Suggested kid passwords: two short words and a digit ("moon-tiger-7"). The parent can edit it.
_PW_WORDS = ("moon", "star", "sun", "tiger", "panda", "apple", "mango", "rain", "cloud", "frog", "kite", "boat",
             "lion", "cake", "bell", "leaf", "rock", "wave", "fox", "owl", "bear", "corn", "drum", "fish")
PW_MIN, PW_MAX = 4, 40

_secret_lock = threading.Lock()
_generated_secret: bytes | None = None


def _secret() -> bytes:
    global _generated_secret
    env = os.environ.get("SESSION_SECRET", "")
    if env:
        return env.encode()
    with _secret_lock:
        if _generated_secret is None:
            _generated_secret = secrets.token_bytes(32)
            logger.warning("SESSION_SECRET is not set: generated one; sessions end when the server restarts")
        return _generated_secret


# ---------------------------------------------------------------- signing
def sign(payload: dict, ttl_s: int, now: float | None = None) -> str:
    body = dict(payload, exp=int((now or time.time()) + ttl_s))
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def unsign(token: str | None, now: float | None = None) -> dict | None:
    """The payload if the signature is good and it hasn't expired, else None."""
    try:
        raw, sig = (token or "").rsplit(".", 1)
    except ValueError:
        return None
    good = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict) or body.get("exp", 0) < (now or time.time()):
        return None
    return body


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _set_cookie(response: Response, request: Request, name: str, value: str, ttl: int) -> None:
    response.set_cookie(name, value, max_age=ttl, httponly=True, samesite="lax", secure=_secure(request), path="/")


def session_of(cookies) -> dict:
    """{"p": parent_id} or {"c": child_id, "f": parent_id, "v": pw_ver}, or {} (anonymous). HTTP and WebSocket.

    A child session made before the child's password was last reset (older "v") counts as signed out.
    """
    sess = unsign(cookies.get(SESSION_COOKIE)) or {}
    if isinstance(sess.get("c"), int):
        try:
            row = db.child_password(sess["c"])
        except Exception as exc:  # noqa: BLE001 — anonymous rather than failing the page or the lesson
            logger.warning("child session check failed: %s", exc)
            return {}
        if row is None or row[1] != sess.get("v", 0):
            return {}
    return sess


def _child_session(response: Response, request: Request, child: dict) -> None:
    payload = {"c": child["id"], "f": child["parent_id"], "v": child.get("pw_ver", 0)}
    _set_cookie(response, request, SESSION_COOKIE, sign(payload, SESSION_TTL_S), SESSION_TTL_S)


def family_of(cookies) -> int | None:
    fam = unsign(cookies.get(FAMILY_COOKIE)) or {}
    return fam.get("f") if isinstance(fam.get("f"), int) else None


def child_for_lesson(cookies) -> dict | None:
    """The signed-in child (for the lesson socket), or None: the lesson is anonymous."""
    sess = session_of(cookies)
    if not isinstance(sess.get("c"), int):
        return None
    try:
        return db.get_child(sess["c"], sess.get("f"))
    except Exception as exc:  # noqa: BLE001 — a lesson never fails because the store did
        logger.warning("child lookup failed: %s", exc)
        return None


def _parent_id(request: Request) -> int:
    pid = session_of(request.cookies).get("p")
    if not isinstance(pid, int) or db.get_parent(pid) is None:
        raise HTTPException(401, "Please sign in as a parent.")
    return pid


# ---------------------------------------------------------------- rate limiting
class RateLimit:
    """At most `limit` hits per `window_s` per key, in memory (one container)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now or time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and q[0] <= now - self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


child_login_limit = RateLimit(10, 300)
parent_login_limit = RateLimit(20, 300)
kid_name_limit = RateLimit(5, 600)  # password tries per hero name
kid_client_limit = RateLimit(15, 600)  # password tries per client (a few kids may share a home network)


def _client(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


# ---------------------------------------------------------------- Google
def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def dev_login_allowed() -> bool:
    return not google_client_id() and os.environ.get("ALLOW_DEV_LOGIN", "") == "1"


def verify_google_token(credential: str) -> dict:
    """Google's verified claims for this app's client id. Raises ValueError if the token isn't good."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    client_id = google_client_id()
    if not client_id:
        raise ValueError("Google Sign-In is not configured")
    claims = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("wrong issuer")
    if not claims.get("sub"):
        raise ValueError("no subject")
    return claims


def _sign_in_parent(request: Request, response: Response, sub: str, name: str) -> dict:
    parent = db.upsert_parent(sub, name)
    _set_cookie(response, request, SESSION_COOKIE, sign({"p": parent["id"]}, SESSION_TTL_S), SESSION_TTL_S)
    _set_cookie(response, request, FAMILY_COOKIE, sign({"f": parent["id"]}, FAMILY_TTL_S), FAMILY_TTL_S)
    return {"parent": {"id": parent["id"], "name": parent["display_name"]}}


class GoogleBody(BaseModel):
    credential: str


class DevBody(BaseModel):
    name: str = "Dev Parent"


@router.post("/api/auth/google")
def auth_google(body: GoogleBody, request: Request, response: Response):
    if not parent_login_limit.allow(_client(request)):
        raise HTTPException(429, "Too many sign-in attempts. Please wait a few minutes.")
    try:
        claims = verify_google_token(body.credential)
    except ValueError as exc:
        logger.info("google sign-in refused: %s", exc)
        raise HTTPException(401, "Google sign-in could not be verified.") from exc
    name = str(claims.get("given_name") or claims.get("name") or "Parent").split()[0][:40]
    out = _sign_in_parent(request, response, f"google:{claims['sub']}", name)
    from app import access, admin  # not at import time: both import this module

    if admin.is_admin_email(claims):  # the owner's Google account (ADMIN_EMAILS): checked here, never stored
        _set_cookie(response, request, access.ADMIN_COOKIE, access.admin_cookie(), access.ADMIN_TTL_S)
        out["admin"] = True
    return out


@router.post("/api/auth/dev")
def auth_dev(body: DevBody, request: Request, response: Response):
    """Local testing only: never on unless GOOGLE_CLIENT_ID is unset and ALLOW_DEV_LOGIN=1."""
    if not dev_login_allowed():
        raise HTTPException(404, "Not found")
    name = re.sub(r"[^\w '-]", "", body.name).strip()[:40] or "Dev Parent"
    return _sign_in_parent(request, response, f"dev:{name.lower()}", name)


class LogoutBody(BaseModel):
    forget_device: bool = False


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, body: LogoutBody | None = None):
    """Sign out. The family cookie stays (children can still pick their profile) unless forget_device."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie("bmt_admin", path="/")  # access.ADMIN_COOKIE
    if body is not None and body.forget_device:
        response.delete_cookie(FAMILY_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- who am I
def _public_child(c: dict, with_code: bool = False) -> dict:
    out = {"id": c["id"], "nickname": c["nickname"], "level": c["level"], "avatar": c["avatar"]}
    if with_code:  # the parent's view, and the child's own: hero name and whether a password is set (never the hash)
        out["code_name"] = c["code_name"]
        out["has_password"] = bool(c.get("has_password"))
    return out


@router.get("/api/me")
def me(request: Request):
    sess, family = session_of(request.cookies), family_of(request.cookies)
    out: dict = {"parent": None, "child": None, "family": None,
                 "google_client_id": google_client_id() or None, "dev_login": dev_login_allowed(),
                 "avatars": list(AVATARS), "levels": list(LEVELS), "max_children": MAX_CHILDREN}
    if isinstance(sess.get("p"), int):
        parent = db.get_parent(sess["p"])
        if parent:  # kids: 0 means a new parent, so the page shows onboarding
            out["parent"] = {"id": parent["id"], "name": parent["display_name"],
                             "kids": len(db.list_children(parent["id"])),
                             "consented": parent.get("consent_version") == TERMS_VERSION}
    out["terms_version"] = TERMS_VERSION
    from app import access  # not at import time: it imports this module

    out["admin"] = access.is_admin(request.cookies)
    if isinstance(sess.get("c"), int):
        child = db.get_child(sess["c"], sess.get("f"))
        if child:
            out["child"] = _public_child(child, with_code=True)
    if family is not None:
        out["family"] = [_public_child(c) for c in db.list_children(family)]
    return out


class ConsentBody(BaseModel):
    version: str
    agree: bool


@router.post("/api/me/consent")
def give_consent(body: ConsentBody, request: Request):
    """The parent's agreement to the terms (/terms): needed before adding a child or using the AI."""
    pid = _parent_id(request)
    if not body.agree or body.version != TERMS_VERSION:
        raise HTTPException(422, "Please read and agree to the current terms.")
    db.set_consent(pid, TERMS_VERSION)
    return {"ok": True, "terms_version": TERMS_VERSION}


def family_consented(parent_id: int | None) -> bool:
    parent = db.get_parent(parent_id) if isinstance(parent_id, int) else None
    return bool(parent) and parent.get("consent_version") == TERMS_VERSION


class NameBody(BaseModel):
    name: str


@router.patch("/api/me")
def rename_me(body: NameBody, request: Request):
    """Onboarding step 1, "What should we call you?": the parent's display name (prefilled from Google)."""
    pid = _parent_id(request)
    name = re.sub(r"\s+", " ", re.sub(r"[^\w '-]", "", body.name or "")).strip()[:40]
    if not name:
        raise HTTPException(422, "Please type a name (letters, up to 40).")
    parent = db.set_parent_name(pid, name)
    return {"parent": {"id": parent["id"], "name": parent["display_name"]}}


@router.delete("/api/me")
def delete_me(request: Request, response: Response):
    """Delete the parent's account and every child profile and record under it."""
    pid = _parent_id(request)
    db.delete_parent(pid)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(FAMILY_COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- children (parent only)
class ChildBody(BaseModel):
    nickname: str
    level: str = "P4"
    avatar: str = AVATARS[0]


class ChildPatch(BaseModel):
    nickname: str | None = None
    level: str | None = None
    avatar: str | None = None


def _clean_nickname(name: str) -> str:
    name = re.sub(r"\s+", " ", re.sub(r"[^\w '-]", "", name or "")).strip()[:20]
    if not name:
        raise HTTPException(422, "Please give a first name or nickname (letters and numbers, up to 20).")
    return name


def _check(level: str | None, avatar: str | None) -> None:
    if level is not None and level not in LEVELS:
        raise HTTPException(422, "Level must be P1 to P6.")
    if avatar is not None and avatar not in AVATARS:
        raise HTTPException(422, "Please pick one of the avatars.")


def new_code_name() -> str:
    """A hero name like "Thor987": unique across every family, ignoring case."""
    for _ in range(50):
        code = f"{secrets.choice(HERO_WORDS)}{secrets.randbelow(900) + 100}"
        if not db.code_name_taken(code):
            return code
    raise HTTPException(503, "Could not make a hero name; please try again.")


# ---------------------------------------------------------------- kid passwords
# Not case-sensitive (a phone's auto-capital must not lock a child out) and spaces at the ends are dropped.
def _norm_password(pw: str) -> str:
    return (pw or "").strip().lower()


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(_norm_password(pw).encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        kind, salt, digest = (stored or "").split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)).split("$")[2], digest)


def suggest_password() -> str:
    a, b = secrets.choice(_PW_WORDS), secrets.choice(_PW_WORDS)
    while b == a:
        b = secrets.choice(_PW_WORDS)
    return f"{a}-{b}-{secrets.randbelow(9) + 1}"


@router.get("/api/children")
def children_list(request: Request):
    return {"children": [_public_child(c, with_code=True) for c in db.list_children(_parent_id(request))]}


@router.post("/api/children")
def children_add(body: ChildBody, request: Request):
    pid = _parent_id(request)
    if not family_consented(pid):
        raise HTTPException(403, "Please read and agree to the terms first.")
    _check(body.level, body.avatar)
    if len(db.list_children(pid)) >= MAX_CHILDREN:
        raise HTTPException(422, f"Up to {MAX_CHILDREN} children per family.")
    child = db.add_child(pid, _clean_nickname(body.nickname), body.level, body.avatar, new_code_name())
    return {"child": _public_child(child, with_code=True)}


@router.patch("/api/children/{child_id}")
def children_edit(child_id: int, body: ChildPatch, request: Request):
    pid = _parent_id(request)
    _check(body.level, body.avatar)
    nickname = _clean_nickname(body.nickname) if body.nickname is not None else None
    child = db.update_child(child_id, pid, nickname=nickname, level=body.level, avatar=body.avatar)
    if child is None:
        raise HTTPException(404, "No such child in your family.")
    return {"child": _public_child(child, with_code=True)}


class PasswordBody(BaseModel):
    password: str | None = None  # None: the server picks one ("moon-tiger-7")


@router.get("/api/children/password-suggestion")
def password_suggestion(request: Request):
    _parent_id(request)
    return {"password": suggest_password()}


@router.post("/api/children/{child_id}/password")
def children_password(child_id: int, body: PasswordBody, request: Request):
    """Set or reset a child's password. The old one, and every session signed in with it, stop working at once.

    The password is in this one response so the parent can copy it; it is never returned or logged again.
    """
    pid = _parent_id(request)
    pw = body.password if body.password is not None else suggest_password()
    if not PW_MIN <= len(_norm_password(pw)) <= PW_MAX:
        raise HTTPException(422, f"The password needs {PW_MIN} to {PW_MAX} characters.")
    if not db.set_child_password(child_id, pid, hash_password(pw)):
        raise HTTPException(404, "No such child in your family.")
    child = db.get_child(child_id, pid)
    return {"child": _public_child(child, with_code=True), "password": _norm_password(pw)}


@router.delete("/api/children/{child_id}")
def children_delete(child_id: int, request: Request):
    if not db.delete_child(child_id, _parent_id(request)):
        raise HTTPException(404, "No such child in your family.")
    return {"ok": True}


# ---------------------------------------------------------------- child login: family device, or hero name + password
class ChildLogin(BaseModel):
    child_id: int | None = None
    code_name: str | None = None


@router.post("/api/child/login")
def child_login(body: ChildLogin, request: Request, response: Response):
    family = family_of(request.cookies)
    if family is None:
        raise HTTPException(403, "Ask a parent to sign in on this device first.")
    if not child_login_limit.allow(f"{_client(request)}|{family}"):
        raise HTTPException(429, "Too many tries. Please wait a few minutes.")
    child = None
    if body.child_id is not None:
        child = db.get_child(body.child_id, family)
    elif body.code_name:
        child = db.child_by_code(body.code_name.strip()[:40], family)
    if child is None:
        raise HTTPException(404, "That hero name isn't in this family.")
    _child_session(response, request, child)
    return {"child": _public_child(child)}


class KidLogin(BaseModel):
    hero: str
    password: str


ASK_PARENT = "That hero name and password don't match. Check with your parent; they can reset your password."


@router.post("/api/kid/login")
def kid_login(body: KidLogin, request: Request, response: Response):
    """Hero name + password, on any device."""
    hero = re.sub(r"[^\w-]", "", body.hero or "")[:40]
    if not kid_client_limit.allow(_client(request)) or not kid_name_limit.allow(hero.lower()):
        raise HTTPException(429, "Too many tries. Please wait 10 minutes, or ask your parent for help.")
    child = db.child_by_hero(hero) if hero else None
    stored = db.child_password(child["id"])[0] if child else None
    if not check_password(body.password, stored):
        if child is None:
            hash_password(body.password or "x")  # same work either way, so timing doesn't tell which names exist
        raise HTTPException(401, ASK_PARENT)
    _child_session(response, request, child)
    return {"child": _public_child(child, with_code=True)}


@router.get("/k/{hero}")
def kid_link(hero: str):
    """A convenience, not a key: opens the kid sign-in with the hero name filled in. The password is still needed."""
    return RedirectResponse("/?kid=" + quote(re.sub(r"[^\w-]", "", hero)[:40]), status_code=303)


@router.get("/api/child/progress")
def child_progress(request: Request, child_id: int | None = None):
    """Mastery per heuristic: the signed-in child's own, or (for a parent) one of their children's."""
    sess = session_of(request.cookies)
    if child_id is not None:
        child = db.get_child(child_id, _parent_id(request))
    elif isinstance(sess.get("c"), int):
        child = db.get_child(sess["c"], sess.get("f"))
    else:
        raise HTTPException(401, "Sign in first.")
    if child is None:
        raise HTTPException(404, "No such child.")
    rows = [{"heuristic": m["heuristic"], "rating": round(m["rating"]), "n": m["n"],
             "avg_seconds": m["avg_seconds"]} for m in db.all_mastery(child["id"])]
    return {"child": _public_child(child), "progress": rows}


