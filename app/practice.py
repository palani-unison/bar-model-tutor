"""Practice endpoints: "Try one like this" twins and the "My methods" panel (levels and badges).

POST /api/twin          {"spec": <a verified v2 spec>, "n": 0}  -> a twin made and verified by code
                         (agent/twins.py), or {"available": false}. The lesson socket has usually started
                         making it already, so this mostly returns from the cache.
GET  /api/child/methods  the signed-in child's levels and badges, or (for a parent) ?child_id=.
/badges/*.svg            the badge icons.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import badges, twins
from app import access, db
from app.accounts import _parent_id, _public_child, session_of

router = APIRouter()
BADGE_DIR = Path(__file__).resolve().parent / "static" / "badges"
TWIN_WAIT_S = 75.0  # the planner fallback can take this long; substitution answers in well under a second


class TwinRequest(BaseModel):
    spec: dict
    n: int = 0


@router.post("/api/twin")
async def twin(body: TwinRequest, _who: str | None = Depends(access.require_member)):
    if body.spec.get("version") != 2:
        return {"available": False, "reason": "twins are made from v2 lessons only"}
    n = max(0, min(int(body.n), 5))
    out = await run_in_threadpool(twins.cache.get, body.spec, n, TWIN_WAIT_S)
    if out.get("available"):
        twins.cache.prefetch(body.spec, n + 1)  # "another one like this" is ready next time
    return out


@router.get("/api/child/methods")
def child_methods(request: Request, child_id: int | None = None):
    sess = session_of(request.cookies)
    if child_id is not None:
        child = db.get_child(child_id, _parent_id(request))
    elif isinstance(sess.get("c"), int):
        child = db.get_child(sess["c"], sess.get("f"))
    else:
        raise HTTPException(401, "Sign in first.")
    if child is None:
        raise HTTPException(404, "No such child.")
    return {"child": _public_child(child), **badges.child_methods(child)}


@router.get("/badges/{name}")
def badge_icon(name: str):
    """Only the catalogue's own icons: no path is ever built from the request."""
    badge_id = name.removesuffix(".svg")
    if badge_id not in badges.BY_ID or not (BADGE_DIR / f"{badge_id}.svg").is_file():
        raise HTTPException(404, "No such badge.")
    return FileResponse(BADGE_DIR / f"{badge_id}.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})
