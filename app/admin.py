"""The owner's admin dashboard, and "Report a problem" (flags) from parents and children.

Who is admin:
- Only a parent who signs in with Google using an address listed in ADMIN_EMAILS (comma-separated). The address is
  checked at sign-in only and never stored; sign-in sets the admin cookie (app/access.py) next to the parent session.
  There is no admin password.
- Locally (PUBLIC_DEPLOY unset) the dashboard is open, like the rest of the app.

GET  /admin                      dashboard: usage, plan outcomes, families, children, methods, flags, declines
GET  /admin/family/{parent_id}   one family: children, their methods and recent lessons
GET  /admin/lesson/{lesson_id}   one saved lesson's question, answer and checked plan
POST /admin/flags/{flag_id}      set a flag's status (new / seen / fixed / dismissed) and the owner's note
GET  /admin/export.json          flagged and declined questions, in the eval dataset shape, to improve the tutor
POST /api/flags                  a signed-in parent or child reports a problem with a question or lesson
"""
from __future__ import annotations

import html
import json
import os
import statistics
import time
from collections import defaultdict

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import access, db
from app.accounts import session_of

router = APIRouter()

REASONS = {
    "wrong_answer": "The answer looks wrong",
    "confusing": "The explanation was confusing",
    "wrong_reading": "It read my question wrongly",
    "declined": "It couldn't do my question",
    "bug": "Something didn't work",
    "other": "Something else",
}
STATUSES = ("new", "seen", "fixed", "dismissed")
FLAGS_PER_FAMILY_DAY = 30
TZ_HOURS = float(os.environ.get("ADMIN_TZ_HOURS", "8"))  # the dashboard shows Singapore time by default


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin_email(claims: dict) -> bool:
    """A verified Google address on the ADMIN_EMAILS list."""
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified") in (True, "true", "True")
    return bool(email) and verified and email in admin_emails()


def _require_admin(request: Request) -> None:
    """The middleware in app/main.py already asked for the password or the admin cookie on a public deploy."""
    if access.public() and not access.is_admin(request.cookies):
        raise HTTPException(401, "Admin only.")


# ---------------------------------------------------------------- flags (parents and children)
class FlagBody(BaseModel):
    reason: str
    note: str = Field("", max_length=1000)
    question: str = Field("", max_length=3000)
    spec: dict | None = None
    lesson_id: int | None = None


def _who(cookies) -> tuple[int | None, int | None]:
    sess = session_of(cookies)
    if isinstance(sess.get("c"), int):
        return sess.get("f"), sess["c"]
    if isinstance(sess.get("p"), int):
        return sess["p"], None
    return None, None


def _answer_of(spec: dict | None) -> str:
    if not isinstance(spec, dict):
        return ""
    answer = spec.get("answer") if spec.get("version") == 2 else (spec.get("model") or {}).get("answer")
    if isinstance(answer, dict):
        answer = answer.get("value", "")
    return str(answer if answer is not None else "")[:60]


@router.post("/api/flags")
def report_problem(body: FlagBody, request: Request):
    if body.reason not in REASONS:
        raise HTTPException(400, "Please pick what went wrong.")
    parent_id, child_id = _who(request.cookies)
    if access.public() and parent_id is None:
        raise HTTPException(401, access.SIGN_IN_FIRST)
    asked = body.question or (body.spec or {}).get("question") or ""
    question = (asked if isinstance(asked, str) else str(asked)).strip()
    if not question and not body.note.strip():
        raise HTTPException(400, "Please say what went wrong.")
    if parent_id is not None:
        today = db.admin_query("SELECT COUNT(*) AS n FROM flags WHERE parent_id = ? AND created > ?",
                               (parent_id, time.time() - 86400))[0]["n"]
        if today >= FLAGS_PER_FAMILY_DAY:
            raise HTTPException(429, "Thank you! That's a lot of reports today; please try again tomorrow.")
    spec_json = json.dumps(body.spec, separators=(",", ":"))[:200_000] if isinstance(body.spec, dict) else None
    lesson_id = body.lesson_id
    if lesson_id is not None and child_id is not None and db.get_lesson(lesson_id, child_id) is None:
        lesson_id = None  # only a lesson of this child's
    flag_id = db.add_flag(parent_id, child_id, lesson_id, body.reason, body.note.strip(), question, spec_json,
                          _answer_of(body.spec))
    return {"ok": True, "id": flag_id}


# ---------------------------------------------------------------- recording plan outcomes (app/lesson_ws.py, app/main.py)
def record_plan(cookies, outcome: str, *, source: str = "lesson", body: dict | None = None,
                seconds: float | None = None, question: str = "", detail: str = "") -> None:
    """One plan_events row. Never raises: observability must not break a lesson."""
    try:
        parent_id, child_id = _who(cookies)
        spec = (body or {}).get("spec") or {}
        heuristic = str(spec.get("heuristic") or spec.get("problem_type") or (spec.get("model") or {}).get("type") or "")
        db.add_plan_event(parent_id, child_id, outcome, source=source, heuristic=heuristic,
                          planner=str((body or {}).get("planner") or ""), seconds=seconds,
                          question=question or None, detail=detail)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("bar_model_tutor.admin").warning("plan event not recorded", exc_info=True)


# ---------------------------------------------------------------- dashboard
CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1f;--mute:#6b6b70;--line:#e3e3e0;--ok:#1a7f4b;--warn:#b25e00;--bad:#b3261e;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#121214;--card:#1c1c1f;--ink:#ececec;--mute:#9a9aa0;--line:#2c2c30;--ok:#4cc38a;--warn:#f0a040;--bad:#ff6b5e;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
nav{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 14px}a{color:var(--acc)}.mute{color:var(--mute)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}.card b{display:block;font-size:24px}
.card span{color:var(--mute);font-size:13px}.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:0}td.n{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.ok{color:var(--ok)}.declined{color:var(--warn)}.error,.new{color:var(--bad)}.q{max-width:520px;white-space:pre-wrap}
form.inline{display:flex;gap:6px;flex-wrap:wrap;align-items:center}input,select,button{font:inherit;padding:4px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{cursor:pointer}details pre{white-space:pre-wrap;font-size:12px;max-height:320px;overflow:auto}
.bar{display:inline-block;height:8px;border-radius:4px;background:var(--acc);vertical-align:middle}
"""


def _t(ts: float | None) -> str:
    if not ts:
        return "–"
    return time.strftime("%d %b %H:%M", time.gmtime(ts + TZ_HOURS * 3600))


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "–"


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{_e(title)}</title><style>{CSS}</style><body><main>{body}</main></body></html>")


def _table(head: list[str], rows: list[list[str]], numeric: set[int] = frozenset(), empty: str = "Nothing yet.") -> str:
    if not rows:
        return f"<p class=mute>{empty}</p>"
    th = "".join(f"<th>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td{' class=n' if i in numeric else ''}>{c}</td>" for i, c in enumerate(r)) + "</tr>"
                  for r in rows)
    return f"<div class=wrap><table><tr>{th}</tr>{trs}</table></div>"


def _flag_rows(flags: list[dict], names: dict) -> list[list[str]]:
    rows = []
    for f in flags:
        who = names.get(("c", f["child_id"])) or names.get(("p", f["parent_id"])) or "anonymous"
        spec = ""
        if f.get("spec"):
            try:
                spec = json.dumps(json.loads(f["spec"]), indent=1)[:20000]
            except ValueError:
                spec = f["spec"][:20000]
        lesson = f" · <a href='/admin/lesson/{f['lesson_id']}'>lesson</a>" if f.get("lesson_id") else ""
        opts = "".join(f"<option{' selected' if s == f['status'] else ''}>{s}</option>" for s in STATUSES)
        rows.append([
            f"#{f['id']}<br><span class=mute>{_t(f['created'])}</span>",
            f"<span class='pill {_e(f['status'])}'>{_e(f['status'])}</span>",
            _e(REASONS.get(f["reason"], f["reason"])) + (f"<br><i>“{_e(f['note'])}”</i>" if f["note"] else ""),
            f"<div class=q>{_e(f['question'])}</div>" + (f"<span class=mute>Tutor's answer: {_e(f['answer'])}</span>" if f["answer"] else "")
            + (f"<details><summary>Checked plan</summary><pre>{_e(spec)}</pre></details>" if spec else ""),
            _e(who) + (f"<br><a href='/admin/family/{f['parent_id']}'>family</a>" if f["parent_id"] else "") + lesson,
            f"<form class=inline method=post action='/admin/flags/{f['id']}'><select name=status>{opts}</select>"
            f"<input name=admin_note placeholder='note' value='{_e(f['admin_note'])}' size=14><button>Save</button></form>",
        ])
    return rows


def _names() -> dict:
    names = {}
    for p in db.admin_query("SELECT id, display_name FROM parents"):
        names[("p", p["id"])] = f"{p['display_name'] or 'Parent'} (parent #{p['id']})"
    for c in db.admin_query("SELECT id, nickname, code_name FROM children"):
        names[("c", c["id"])] = f"{c['nickname']} · {c['code_name']}"
    return names


def dashboard(days: int = 7) -> str:
    now = time.time()
    since = now - days * 86400
    u = access.usage()
    q = db.admin_query
    events = q("SELECT * FROM plan_events WHERE created > ? ORDER BY created DESC", (since,))
    ok = [e for e in events if e["outcome"] == "ok"]
    secs = sorted(e["seconds"] for e in ok if e["seconds"] is not None)
    p50 = f"{statistics.median(secs):.0f} s" if secs else "–"
    p90 = f"{secs[int(0.9 * (len(secs) - 1))]:.0f} s" if secs else "–"
    declined = sum(e["outcome"] == "declined" for e in events)
    errors = sum(e["outcome"] == "error" for e in events)
    active_children = q("SELECT COUNT(DISTINCT child_id) AS n FROM attempts WHERE created > ?", (since,))[0]["n"]
    active_families = q("SELECT COUNT(DISTINCT c.parent_id) AS n FROM attempts a JOIN children c ON c.id = a.child_id "
                        "WHERE a.created > ?", (since,))[0]["n"]
    open_flags = q("SELECT COUNT(*) AS n FROM flags WHERE status = 'new'")[0]["n"]
    cards = [
        (u["parents"], "Parents"), (u["children"], "Children"), (active_families, f"active families ({days} d)"),
        (active_children, f"active children ({days} d)"), (len(events), f"new problems ({days} d)"),
        (_pct(len(ok), len(events)), "planned and verified"), (declined, "declined"), (errors, "errors"),
        (p50, "median plan time"), (p90, "90th pct plan time"), (u["lessons"], "saved lessons"),
        (u["attempts"], "checkpoint answers"), (open_flags, "new reports"),
        (f"{u['plans_today']['total']} / {u['limits']['per_day']}", "plans today / daily cap"),
    ]
    out = ["<h1>Bar Model Tutor · admin</h1>",
           f"<p class=mute>Times in UTC{TZ_HOURS:+g}. Showing the last "
           + " · ".join(f"<a href='/admin?days={d}'{' style=font-weight:700' if d == days else ''}>{d} days</a>" for d in (1, 7, 30, 365))
           + " · <a href='/admin/export.json'>Export flagged + declined questions (JSON)</a> · <a href='/'>Open the app</a></p>",
           "<nav><a href='#daily'>Daily</a><a href='#flags'>Reports</a><a href='#declines'>Declined</a>"
           "<a href='#families'>Families</a><a href='#children'>Children</a><a href='#methods'>Methods</a></nav>",
           "<div class=cards>" + "".join(f"<div class=card><b>{_e(v)}</b><span>{_e(k)}</span></div>" for v, k in cards) + "</div>"]

    # daily
    by_day: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    fmt = lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts + TZ_HOURS * 3600))  # noqa: E731
    for e in events:
        by_day[fmt(e["created"])][e["outcome"]] += 1
    for a in q("SELECT created, child_id FROM attempts WHERE created > ?", (since,)):
        d = by_day[fmt(a["created"])]
        d["answers"] += 1
        d.setdefault("kids", set()).add(a["child_id"])  # type: ignore[union-attr]
    for row in q("SELECT created FROM lessons WHERE created > ?", (since,)):
        by_day[fmt(row["created"])]["lessons"] += 1
    peak = max([sum(d[k] for k in ("ok", "declined", "error")) for d in by_day.values()] or [1]) or 1
    rows = []
    for day in sorted(by_day, reverse=True):
        d = by_day[day]
        n = d["ok"] + d["declined"] + d["error"]
        rows.append([day, f"<span class=bar style='width:{max(2, round(90 * n / peak))}px'></span> {n}",
                     str(d["ok"]), str(d["declined"]), str(d["error"]), str(d["lessons"]), str(d["answers"]),
                     str(len(d.get("kids", ())))])
    out += ["<h2 id=daily>Daily</h2>",
            _table(["Day", "New problems", "Verified", "Declined", "Errors", "Lessons started", "Checkpoint answers",
                    "Active children"], rows, {2, 3, 4, 5, 6, 7})]

    names = _names()
    flags = q("SELECT * FROM flags ORDER BY (status = 'new') DESC, created DESC LIMIT 200")
    out += ["<h2 id=flags>Reports from families</h2>",
            _table(["#", "Status", "What went wrong", "Question", "From", "Action"], _flag_rows(flags, names),
                   empty="No reports yet.")]

    dec = [e for e in events if e["outcome"] != "ok"][:100]
    out += ["<h2 id=declines>Questions the tutor declined or failed on</h2>",
            _table(["When", "Outcome", "Question", "Why", "From", "Time"],
                   [[_t(e["created"]), f"<span class={_e(e['outcome'])}>{_e(e['outcome'])}</span>",
                     f"<div class=q>{_e(e['question'] or '(not kept)')}</div>", _e(e["detail"]),
                     _e(names.get(("c", e["child_id"])) or names.get(("p", e["parent_id"])) or "anonymous"),
                     f"{e['seconds']:.0f} s" if e["seconds"] is not None else "–"] for e in dec],
                   empty="None in this period.")]

    fams = q("""SELECT p.id, p.display_name, p.created, p.consent_version,
                  (SELECT COUNT(*) FROM children c WHERE c.parent_id = p.id) AS kids,
                  (SELECT COUNT(*) FROM lessons l JOIN children c ON c.id = l.child_id WHERE c.parent_id = p.id) AS lessons,
                  (SELECT MAX(l.updated) FROM lessons l JOIN children c ON c.id = l.child_id WHERE c.parent_id = p.id) AS last,
                  (SELECT COUNT(*) FROM plan_events e WHERE e.parent_id = p.id AND e.created > ?) AS plans,
                  (SELECT COUNT(*) FROM flags f WHERE f.parent_id = p.id) AS flags
                FROM parents p ORDER BY COALESCE(last, p.created) DESC LIMIT 500""", (since,))
    out += ["<h2 id=families>Families</h2>",
            _table(["Parent", "Joined", "Terms", "Children", "Lessons", f"Problems ({days} d)", "Reports", "Last lesson"],
                   [[f"<a href='/admin/family/{f['id']}'>{_e(f['display_name'] or 'Parent')} #{f['id']}</a>", _t(f["created"]),
                     _e(f["consent_version"] or "not yet"), str(f["kids"]), str(f["lessons"]), str(f["plans"]),
                     str(f["flags"]), _t(f["last"])] for f in fams], {3, 4, 5, 6})]

    kids = q("""SELECT c.id, c.parent_id, c.nickname, c.code_name, c.level, c.created,
                  (SELECT COUNT(*) FROM lessons l WHERE l.child_id = c.id) AS lessons,
                  (SELECT COUNT(*) FROM attempts a WHERE a.child_id = c.id) AS answers,
                  (SELECT COUNT(*) FROM attempts a WHERE a.child_id = c.id AND a.outcome = 'first try') AS first,
                  (SELECT MAX(a.created) FROM attempts a WHERE a.child_id = c.id) AS last
                FROM children c ORDER BY COALESCE(last, c.created) DESC LIMIT 500""")
    out += ["<h2 id=children>Children</h2>",
            _table(["Child", "Family", "Level", "Lessons", "Checkpoint answers", "First try", "Last active"],
                   [[_e(f"{k['nickname']} · {k['code_name']}"), f"<a href='/admin/family/{k['parent_id']}'>#{k['parent_id']}</a>",
                     _e(k["level"]), str(k["lessons"]), str(k["answers"]), _pct(k["first"], k["answers"]), _t(k["last"])]
                    for k in kids], {3, 4, 5})]

    methods = q("""SELECT heuristic, COUNT(*) AS n, SUM(outcome = 'first try') AS first, SUM(outcome = 'with hints') AS hints,
                     SUM(outcome = 'shown') AS shown, COUNT(DISTINCT child_id) AS kids, AVG(seconds) AS secs
                   FROM attempts WHERE created > ? GROUP BY heuristic ORDER BY n DESC""", (since,))
    plans_by = defaultdict(lambda: [0, 0])
    for e in events:
        plans_by[e["heuristic"] or "?"][0 if e["outcome"] == "ok" else 1] += 1
    out += ["<h2 id=methods>Methods</h2>",
            _table(["Method", "Children", "Checkpoint answers", "First try", "With hints", "Shown", "Avg answer time",
                    "Problems verified / not"],
                   [[_e(m["heuristic"]), str(m["kids"]), str(m["n"]), _pct(m["first"] or 0, m["n"]), _pct(m["hints"] or 0, m["n"]),
                     _pct(m["shown"] or 0, m["n"]), f"{m['secs']:.0f} s" if m["secs"] else "–",
                     "{} / {}".format(*plans_by.get(m["heuristic"], [0, 0]))] for m in methods], {1, 2, 3, 4, 5, 6})]
    return _page("Admin · Bar Model Tutor", "".join(out))


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, days: int = 7):
    _require_admin(request)
    return HTMLResponse(dashboard(max(1, min(days, 3650))))


@router.get("/admin/family/{parent_id}", response_class=HTMLResponse)
def admin_family(parent_id: int, request: Request):
    _require_admin(request)
    parent = db.get_parent(parent_id)
    if parent is None:
        raise HTTPException(404, "No such family.")
    out = [f"<p><a href='/admin'>← Admin</a></p><h1>{_e(parent['display_name'] or 'Parent')} · family #{parent_id}</h1>",
           f"<p class=mute>Terms: {_e(parent.get('consent_version') or 'not yet')}</p>"]
    for c in db.list_children(parent_id):
        mastery = db.all_mastery(c["id"])
        lessons = db.admin_query("SELECT id, title, heuristic, level, status, runs, hints, seconds, solved_on, updated "
                                 "FROM lessons WHERE child_id = ? ORDER BY updated DESC LIMIT 50", (c["id"],))
        out += [f"<h2>{_e(c['avatar'])} {_e(c['nickname'])} · {_e(c['code_name'])} · {_e(c['level'])}</h2>",
                _table(["Method", "Rating", "Checkpoints", "Avg answer time"],
                       [[_e(m["heuristic"]), f"{m['rating']:.0f}", str(m["n"]),
                         f"{m['avg_seconds']:.0f} s" if m.get("avg_seconds") else "–"] for m in mastery], {1, 2, 3},
                       empty="No answers yet."),
                "<p></p>",
                _table(["Lesson", "Method", "Status", "Runs", "Hints", "Time", "Updated"],
                       [[f"<a href='/admin/lesson/{l['id']}'>{_e(l['title'])}</a>", _e(l["heuristic"]), _e(l["status"]),
                         str(l["runs"]), str(l["hints"]), f"{l['seconds']:.0f} s" if l["seconds"] else "–", _t(l["updated"])]
                        for l in lessons], {3, 4}, empty="No saved lessons.")]
    return HTMLResponse(_page("Family · admin", "".join(out)))


@router.get("/admin/lesson/{lesson_id}", response_class=HTMLResponse)
def admin_lesson(lesson_id: int, request: Request):
    _require_admin(request)
    rows = db.admin_query("SELECT l.*, c.parent_id, c.nickname FROM lessons l JOIN children c ON c.id = l.child_id "
                          "WHERE l.id = ?", (lesson_id,))
    if not rows:
        raise HTTPException(404, "No such lesson.")
    l = rows[0]
    try:
        spec = json.loads(l["spec"])
    except ValueError:
        spec = {}
    out = [f"<p><a href='/admin/family/{l['parent_id']}'>← Family</a></p><h1>{_e(l['title'])}</h1>",
           f"<p class=mute>{_e(l['nickname'])} · {_e(l['heuristic'])} · {_e(l['status'])} · {l['runs']} runs · {l['hints']} hints</p>",
           f"<div class=card><div class=q>{_e(spec.get('question', ''))}</div><p>Answer: <b>{_e(_answer_of(spec))}</b></p></div>",
           f"<details><summary>Checked plan</summary><pre>{_e(json.dumps(spec, indent=1))}</pre></details>"]
    return HTMLResponse(_page("Lesson · admin", "".join(out)))


@router.post("/admin/flags/{flag_id}")
def admin_flag(flag_id: int, request: Request, status: str = Form(...), admin_note: str = Form("")):
    _require_admin(request)
    if status not in STATUSES:
        raise HTTPException(400, "Unknown status.")
    if not db.set_flag_status(flag_id, status, admin_note.strip()):
        raise HTTPException(404, "No such report.")
    return RedirectResponse("/admin#flags", status_code=303)


@router.get("/admin/export.json")
def admin_export(request: Request):
    """Flagged and declined questions as eval rows ({id, question, answer?, ...}); answers are left for the owner."""
    _require_admin(request)
    rows = []
    for f in db.admin_query("SELECT * FROM flags WHERE status != 'dismissed' ORDER BY created"):
        if f["question"]:
            rows.append({"id": f"flag-{f['id']}", "question": f["question"], "tutor_answer": f["answer"],
                         "answer": None, "reason": f["reason"], "note": f["note"], "status": f["status"]})
    for e in db.admin_query("SELECT * FROM plan_events WHERE outcome != 'ok' AND question IS NOT NULL ORDER BY created"):
        rows.append({"id": f"declined-{e['id']}", "question": e["question"], "answer": None,
                     "reason": e["outcome"], "note": e["detail"]})
    return JSONResponse(rows, headers={"Content-Disposition": "attachment; filename=bar-model-tutor-feedback.json"})
