"""Live lesson over one WebSocket: the board, the teacher's voice, the child's voice and answers.

Client -> server (JSON text frames):
  {"type": "start", "spec": {...}}   begin a lesson on a verified spec (v1, or v2 with "version": 2);
                                     "lesson_id": a signed-in child's saved lesson being replayed (app/lessons.py)
  {"type": "prepare", "question": "..."} or {"type": "prepare", "problem": {...}}   plan a new problem (typed text,
                                     or POST /api/extract's ProblemJSON for a photo) and teach beat 1 meanwhile
  {"type": "next"}                   the last event finished playing; continue
  {"type": "text", "text": "..."}    the child typed something
  {"type": "show_me"}                the child asks to be shown this step
  {"type": "board", "op": "units"|"mark"|"move", "value": {...}}   the child's move on a "build it yourself" board
                                     task (agent/board_task.py), checked by code against the verified spec. Board
                                     tasks are offered only when "start" (or "prepare") says "board": true, i.e.
                                     the client can draw the builder; the "ask" then carries "board" (the task)
  {"type": "mic", "on": bool}        on: the child tapped the mic to start a turn (the stream opens once, then stays
                                     open); off: the child tapped it to end the turn
Client -> server (binary frames): 24 kHz mono s16le microphone audio while the mic is on.

Server -> client: lesson, step, ask, verdict, tutor, heard, thinking, recap, end, error; prepare, plan, plan_failed.
"mic": {"on": bool, "ack": true} answers the client's mic message; {"on": false, "turn": "end"|"empty"} means the
server ended the child's turn (a clear number answer, TURN_SILENCE_S of silence, or the tap to stop) and muted;
a "verdict" on a board task carries "board" (the move it checked; when revealed, the right one, for the builder);
{"on": false} alone means voice input is gone (the client tears its stream down).
For a v2 spec the "lesson" event carries "solved" (solve_spec's values), so the board draws true to scale.
Every event that speaks carries "audio" (base64 MP3, or WAV without ffmpeg; "audio_type" says which) from Piper,
and every event carries "ts" (server ms) for the timing overlay. Nothing is stored for an anonymous lesson.

Adaptive pacing (agent/learner.py): when a child is signed in (app/accounts.py session cookie, sent with the
socket handshake), the "lesson" event carries "pacing" and the lesson follows it (challenge first, key
checkpoints, a "recap" event with every remaining step). At the end, that child's checkpoint outcomes and
seconds are recorded (no question text) and "end" carries "next_pacing". Anonymous lessons store nothing.

My lessons (app/lessons.py): a signed-in child's lesson is saved (its spec and solved values) when it starts, its
progress at each checkpoint, and its outcome at the end; "lesson" carries "lesson_id". A replay ("lesson_id" in
"start", the child's own row) updates that row instead of adding one.

Practice (agent/twins.py, agent/badges.py): a v2 lesson starts making its "Try one like this" twin in the
background at once. {"type": "start", "spec": twin, "twin": {"after_miss": bool}} starts a twin lesson, always
challenge first (a miss runs the full guided lesson). "end" carries "badges" (earned by evidence in this lesson;
stored only for a signed-in child, who also gets "mastery", their level on this kind of problem) and "twin"
(whether a twin can be offered and whether this lesson had a miss).

Prepare (agent/prepare.py): planning takes ~3 s on Glimmer but 15-40 s on Spark, so "prepare" starts the planner
(app.main._solve, the same body as POST /api/solve) in a thread at once, and while it works teaches beat 1 ("Read")
from code alone: the question written and read aloud ("step" events with "prep": true), its key facts highlighted,
then "What does the question want us to find?" ("ask", key "prepare"; any reply is thanked, never judged: a
"verdict" with result "noted") and at most two filler lines. When the plan lands, after the current line has played,
"plan" carries the /api/solve body and the lesson starts on its spec as for "start", minus the read steps already
shown ("lesson" carries "prepared": true). If planning declines, "plan_failed" says why, and no answer is shown.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from agent import badges, learner, twins
from agent import client as agent_client
from agent import prepare as reading
from app import lessons as saved
from agent.lesson import Lesson, is_v2, numbers_in
from app import access
from app.accounts import child_for_lesson
from agent.extract import problem_from_text
from agent.plan import _layout_error, plain_question
from agent.spec_v2 import Solution, SpecError, bars_of, solve_spec
from agent.tutor import answer_question
from agent.verify import verify_spec
from agent.verify_v2 import verify_spec_v2
from voice import tts
from voice.asr import TranscribeStream

logger = logging.getLogger("bar_model_tutor.lesson")
router = APIRouter()

TOKEN_TTL_S = 6 * 60 * 60
_QUESTION = re.compile(r"\?|\b(?:why|how|what|where|which|who|can you|tell me|explain|don'?t understand|confus)", re.I)


# ---------------------------------------------------------------- auth
# On a public deploy the page fetches a short-lived token signed with SESSION_SECRET and passes it in the
# socket URL, so only our own page opens lesson sockets. Who may plan is still checked per message (access.py).
def _token_key() -> bytes:
    from app.accounts import _secret

    return _secret()


def make_token(now: float | None = None) -> str:
    expires = int((now or time.time()) + TOKEN_TTL_S)
    sig = hmac.new(_token_key(), f"lesson:{expires}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def token_ok(token: str | None) -> bool:
    from app import access

    if not access.public():
        return True
    try:
        expires_s, sig = (token or "").split(".", 1)
        expires = int(expires_s)
    except ValueError:
        return False
    good = hmac.new(_token_key(), f"lesson:{expires}".encode(), hashlib.sha256).hexdigest()
    return expires > time.time() and hmac.compare_digest(sig, good)


@router.get("/api/lesson/token")
def lesson_token():
    return {"token": make_token()}


# ---------------------------------------------------------------- routing
def route(lesson: Lesson, text: str) -> str:
    """'answer' (code checks it), 'question' (the tutor explains) or 'ignore'."""
    if not text.strip():
        return "ignore"
    if lesson.waiting is not None:
        if numbers_in(text):
            return "answer"  # "is it 4?" is an answer attempt: code checks it, not the model
        return "question" if _QUESTION.search(text) else "answer"  # "I don't know" -> a hint
    return "question"


def _keywords(spec: dict) -> list[str]:
    bars = bars_of(spec) if is_v2(spec) else spec["model"]["bars"]
    return list(dict.fromkeys([b["label"] for b in bars] + _question_words(spec["question"])))


def _question_words(question: str) -> list[str]:
    return ["units", "unit", "altogether", "times"] + re.findall(r"\b[a-z]{4,}s\b", question)[:5]  # plural nouns: marbles


# ---------------------------------------------------------------- prepare (beat 1 while the planner works)
QUICK_DECLINE_S = 0.15  # the planner refuses some questions at once (plan.unsupported_reason): wait that long for it
PROMPT_WAIT_S = 15  # an unanswered "what must we find?" is let go after this, so the filler lines can play
PLAN_FAILED = "Sorry, I could not check my working on this problem, so I won't show an answer I'm not sure of."
_NOT_SURE = re.compile(r"don'?t know|not sure|no idea|^\s*(?:no|idk|hmm+|um+)\W*$", re.I)


def problem_for(data: dict) -> dict | None:
    """The problem to plan from a "prepare" message: POST /api/extract's ProblemJSON (photo), or typed text. None
    when there is no real question (as POST /api/solve/text, at least 10 characters)."""
    problem = data.get("problem")
    if isinstance(problem, dict) and isinstance(problem.get("question"), str):
        problem = {k: v for k, v in problem.items()
                   if isinstance(k, str) and isinstance(v, (str, int, float, bool, list, type(None)))}
        problem["question"] = problem["question"][:2000]
    else:
        problem = problem_from_text(str(data.get("question") or "")[:2000])
    return problem if len(problem["question"].strip()) >= 10 else None


class Preparation:
    """One problem being planned in the background while beat 1 is taught (LessonSession.prepare)."""

    def __init__(self, question: str):
        self.question = question
        steps = reading.read_steps(question)
        self.beats: list[dict] = [{"type": "step", "prep": True, "index": f"p{i}", "step": s, "say": s["say"]}
                                  for i, s in enumerate(steps)]
        self.beats.append({"type": "ask", "key": "prepare", "say": reading.PROMPT, "index": -1})
        self.fillers = [line for line in (reading.numbers_line(question), reading.CHECKING, reading.PICTURE) if line][:3]
        self.result: tuple[str, object] | None = None  # ("plan", /api/solve body) | ("failed", what to say)
        self.idle = False  # nothing of ours is playing, so the plan can land at once
        self.asking = False  # the understanding prompt is open
        self.facts_shown = False  # a key fact was highlighted, so the spec's own highlights are skipped
        self.facts = set(reading.key_facts(question))


# ---------------------------------------------------------------- voice timing
STABLE_PARTIAL_S = 0.6  # a partial holding a number this long at a checkpoint is checked without waiting for the final
# Turn-taking: the child taps the mic for each turn. A clear number answer at a checkpoint is checked at once (the
# stable partial above, or a final); anything else (a question, "I don't know", a long thought with pauses) ends
# after TURN_SILENCE_S with no new words from the recogniser. The recogniser finds sentence ends after ~0.4 s,
# far too soon for a child thinking aloud, so its finals are joined into one turn here.
TURN_SILENCE_S = 3.0
NO_SPEECH_S = 8.0  # tapped the mic but said nothing: the turn ends empty, so the button is never stuck on
STOP_GRACE_S = 1.5  # tapped to stop mid-sentence: wait this long for the recogniser's last words (sooner if they come)
ACK_SAY = "Hmm, let me think."
ASR_BYTES_PER_S = 24000 * 2
_ack: dict = {}
# One line per spoken turn, printed even when the app has no logging config (the ?debug=1 overlay shows the client side).
timing_log = logging.getLogger("bar_model_tutor.voice_timing")
if not timing_log.handlers:
    timing_log.addHandler(logging.StreamHandler())
    timing_log.setLevel(logging.INFO)
    timing_log.propagate = False


def ack_audio() -> dict:
    """The acknowledgement clip, synthesised once per process: {"say", "audio", "audio_type"}, or {} without a voice."""
    if not _ack:
        try:
            audio, mime = tts.speak_compressed(ACK_SAY)
            _ack.update(say=ACK_SAY, audio=base64.b64encode(audio).decode(), audio_type=mime)
        except Exception as exc:  # noqa: BLE001 — the caption still shows "thinking"
            logger.warning("ack tts failed: %s", exc)
            return {}
    return dict(_ack)


def check_v2(spec: dict) -> tuple[str | None, Solution | None]:
    """The gate for a v2 lesson, as for a video: (why it can't start, or None; the solved spec for the board)."""
    result = verify_spec_v2(spec)
    if not result.ok:
        return str(result), None
    return _layout_error(spec), solve_spec(spec)  # verified, so it solves


class VoiceTurn:
    """One spoken turn: the recogniser's finished sentences plus the sentence in progress, and when it is over.
    Pure (times passed in), so the turn-ending rules are unit-tested without a socket."""

    def __init__(self, now: float):
        self.started, self.last, self.stopped = now, None, None
        self.segments: list[str] = []
        self.partial = ""

    def heard_partial(self, text: str, now: float) -> None:
        self.partial, self.last = text.strip(), now

    def heard_final(self, text: str, now: float) -> None:
        if text.strip():
            self.segments.append(text.strip())
        self.partial, self.last = "", now

    def stop(self, now: float) -> None:
        """The child tapped the mic off."""
        if self.stopped is None:
            self.stopped = now

    def text(self) -> str:
        return " ".join(self.segments + ([self.partial] if self.partial else [])).strip()

    def deadline(self) -> float:
        if self.stopped is not None:
            return self.stopped + STOP_GRACE_S
        if self.last is None:
            return self.started + NO_SPEECH_S
        return self.last + TURN_SILENCE_S

    def due(self, now: float) -> bool:
        return now >= self.deadline()


class LessonSession:
    def __init__(self, ws: WebSocket, child: dict | None = None):
        self.ws = ws
        self.child = child  # adaptive pacing: the signed-in child, or None (anonymous, nothing recorded)
        self.lesson: Lesson | None = None
        self.asr: TranscribeStream | None = None
        self.asr_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()
        # voice: the stream stays open once the mic is first enabled; the button only mutes.
        self.muted = True
        self.closing = False
        self.pump_task: asyncio.Task | None = None
        self.asr_t0, self.asr_bytes = 0.0, 0  # real-time pacing: the service drops a stream fed slower than real time
        self.partial, self.partial_t = "", 0.0
        self.stable_task: asyncio.Task | None = None
        self.handled: tuple | None = None  # (checkpoint, number) when this turn was already checked from a stable partial
        self.turn: VoiceTurn | None = None  # the child's turn in progress (the mic was tapped on)
        self.turn_task: asyncio.Task | None = None
        self.swallow_until = 0.0  # a turn ended before the recogniser's last final: drop its late echo until then
        self.twin: dict | None = None  # this lesson is a "try one like this" twin: {"after_miss": bool}
        self.asked_question = False
        self.lesson_id: int | None = None  # My lessons: this lesson's saved row (signed-in child only)
        self.hints, self.started = 0, 0.0
        self.prep: Preparation | None = None  # a problem being planned while beat 1 is taught
        self.board_ui = False  # the client draws "build it yourself" board tasks ("board": true in start/prepare)

    async def send(self, event: dict, speak: bool = True) -> None:
        if speak and event.get("say"):
            try:
                audio, mime = await run_in_threadpool(tts.speak_compressed, event["say"])
                event["audio"], event["audio_type"] = base64.b64encode(audio).decode(), mime
            except Exception as exc:  # noqa: BLE001 — captions still work without a voice
                logger.warning("tts failed: %s", exc)
        event["ts"] = round(time.time() * 1000)  # server clock, for the ?debug=1 timing overlay
        await self.ws.send_json(event)

    async def start(self, spec: dict, twin: dict | None = None, lesson_id=None, prepared: bool | None = None) -> None:
        """`prepared`: the read steps are on the board already (from "prepare"); True when its key facts were too."""
        solution = None
        self.prep = None
        self.twin = None if not isinstance(twin, dict) else {"after_miss": bool(twin.get("after_miss"))}
        self.asked_question = False
        if self.twin and self.twin["after_miss"] and self.child is not None:
            # A signed-in child's miss is on record: the last lesson of this kind must really have had one.
            self.twin["after_miss"] = await run_in_threadpool(self._last_lesson_missed, spec)
        if is_v2(spec):
            problem, solution = check_v2(spec)
        else:
            result = verify_spec(spec)
            problem = None if result.ok else str(result)
            problem = problem or _layout_error(spec)
        if not problem:
            try:
                self.lesson = Lesson(spec, solution=solution, pacing=self._pacing(spec), board=self.board_ui)
                if is_v2(spec):
                    twins.cache.prefetch(spec)  # "Try one like this", ready by the end of the lesson
            except (SpecError, KeyError, TypeError, ValueError) as exc:
                problem = f"lesson could not be built: {exc}"
        if problem:
            self.lesson = None
            await self.send({"type": "error", "message": "This bar model could not be checked, so the lesson can't start."}, speak=False)
            logger.warning("lesson refused: %s", problem)
            return
        event = {"type": "lesson", "checkpoints": self.lesson.checkpoint_count, "steps": len(self.lesson.steps)}
        if prepared is not None:
            self.lesson.skip_read(prepared)
            event["prepared"] = True
        if solution is not None:
            event["solved"] = solution.as_json()
        self.lesson_id, self.hints, self.started = None, 0, time.monotonic()
        if self.child is not None:
            self.lesson_id = await run_in_threadpool(self._save, spec, event.get("solved"), lesson_id)
            if self.lesson_id is not None:
                event["lesson_id"] = self.lesson_id
        if self.lesson.pacing is not None:
            event["pacing"] = self.lesson.pacing
            if self.child is not None:
                event["child"] = {"nickname": self.child["nickname"], "avatar": self.child["avatar"]}
        if self.twin is not None:
            event["twin"] = True
        await self.send(event, speak=False)
        await self.advance()

    # ------------------------------------------------------------ prepare: beat 1 while the planner works
    async def prepare(self, data: dict) -> None:
        problem = problem_for(data)
        if problem is None:
            await self.send({"type": "error", "message": "Please enter the full word problem."}, speak=False)
            return
        from app.main import _solve  # not at import time: app.main imports this module

        raw = data.get("timings")
        timings = {k: v for k, v in raw.items() if isinstance(v, (int, float))} if isinstance(raw, dict) else {}
        # The planner starts first, on a worker thread: everything below only fills the wait.
        future = asyncio.get_running_loop().run_in_executor(None, _solve, problem, timings)
        self.lesson, self.twin = None, None
        prep = self.prep = Preparation(plain_question(problem["question"]))
        await asyncio.wait({future}, timeout=QUICK_DECLINE_S)
        asyncio.create_task(self._await_plan(prep, future))
        await self.send({"type": "prepare", "question": prep.question}, speak=False)
        if future.done() and future.exception() is not None:
            prep.idle = True  # refused outright (a figure, several parts): say so now rather than read it first
            return
        await self._prep_next()

    async def _await_plan(self, prep: Preparation, future) -> None:
        from app.admin import record_plan  # the admin dashboard's plan log

        started = time.monotonic()
        try:
            outcome = ("plan", await future)
            event = ("ok", outcome[1], "")
        except (agent_client.MissingKeyError, agent_client.ModelUnavailableError, ValueError) as exc:
            outcome = ("failed", str(exc) or PLAN_FAILED)  # the planner's own graceful decline
            event = ("error" if not isinstance(exc, ValueError) else "declined", None, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("planning failed")
            outcome = ("failed", PLAN_FAILED)
            event = ("error", None, type(exc).__name__)
        seconds = time.monotonic() - started
        await run_in_threadpool(record_plan, self.ws.cookies, event[0], body=event[1], seconds=seconds,
                                question=prep.question if event[0] != "ok" else "", detail=event[2])
        async with self.lock:
            if self.prep is not prep or self.closing:
                return  # the socket closed, or another problem was started meanwhile
            prep.result = outcome
            if prep.idle:
                try:
                    await self._land(prep)
                except Exception:  # noqa: BLE001 — the socket closed while we spoke
                    logger.info("prepared lesson could not start: socket closed")

    async def _prep_next(self) -> None:
        """The next beat 1 event; or, once the plan is in and nothing of ours is playing, the lesson itself."""
        prep = self.prep
        if prep.result is not None:
            await self._land(prep)
            return
        if prep.asking:
            return  # the prompt is open; the plan lands as soon as it's ready
        if prep.beats:
            event = prep.beats.pop(0)
            prep.asking = prep.idle = event["type"] == "ask"
            if event.get("step", {}).get("text") in prep.facts:
                prep.facts_shown = True
            if prep.asking:
                asyncio.create_task(self._let_prompt_go(prep))
            await self.send(event)
            return
        if prep.fillers:
            prep.idle = False
            await self.send({"type": "tutor", "say": prep.fillers.pop(0), "waiting": False})
            return
        prep.idle = True  # nothing left to say: the plan lands the moment it's ready

    async def _let_prompt_go(self, prep: Preparation) -> None:
        await asyncio.sleep(PROMPT_WAIT_S)
        async with self.lock:
            if self.prep is prep and prep.asking and prep.result is None and not self.closing:
                await self._prep_reply(reading.NOT_SURE)

    async def _prep_reply(self, say: str) -> None:
        """Close the prompt with a kind word (nothing is judged: code can't check a sentence)."""
        self.prep.asking = self.prep.idle = False
        await self.send({"type": "verdict", "result": "noted", "say": say, "heard": None, "waiting": False})

    async def _prep_child_said(self, text: str) -> None:
        prep = self.prep
        if prep.asking:
            await self._prep_reply(reading.NOT_SURE if _NOT_SURE.search(text) else reading.THANKS)
            return
        prep.idle = False  # the page stopped the line to listen; this reply's "next" carries on
        await self.send({"type": "tutor", "say": reading.HEARD_LATER, "waiting": False})

    async def _land(self, prep: Preparation) -> None:
        self.prep = None
        kind, value = prep.result
        if kind == "failed":
            await self.send({"type": "plan_failed", "say": value, "message": value})
            return
        await self.send({"type": "plan", **value}, speak=False)
        await self.start(value["spec"], prepared=prep.facts_shown)

    async def advance(self) -> None:
        if self.prep is not None:
            await self._prep_next()
            return
        lesson = self.lesson
        if lesson is None or lesson.waiting is not None:
            return
        event = lesson.next_event()
        if event is not None:
            if event["type"] == "ask" and self.lesson_id is not None:
                await run_in_threadpool(self._progress, lesson.position / max(1, len(lesson.steps)))
            await self.send(event)
            return
        if lesson.done:
            first = sum(v == "first try" for v in lesson.score.values())
            say = (f"We did it together! You worked out {first} of {len(lesson.score)} steps on your own."
                   if lesson.score else "That's the whole bar model. Well done!")
            if lesson.recapped:
                say = "You solved it on your own. Super work!"
            end = {"type": "end", "score": lesson.score, "say": say}
            missed = badges.had_miss(lesson.score.values())
            if self.child is not None:
                end["next_pacing"] = await run_in_threadpool(self._record, lesson)
            if self.lesson_id is not None:
                await run_in_threadpool(self._finish, lesson)
            earned = await run_in_threadpool(self._badges, lesson)
            if earned:
                end["badges"] = earned
                b = earned[0]
                end["say"] = f"{say} You earned the {b['name']} badge. {b['why']}"
            if self.child is not None:
                end["mastery"] = await run_in_threadpool(self._mastery, lesson.spec)
            end["twin"] = {"offer": is_v2(lesson.spec), "after_miss": missed}
            await self.send(end)
            self.lesson = None
            if self.asr is not None:  # nothing left to listen for
                await self._close_asr()
                await self.ws.send_json({"type": "mic", "on": False})

    # ------------------------------------------------------------ adaptive pacing (signed-in child only)
    def _pacing(self, spec: dict) -> dict | None:
        pacing = None
        if self.child is not None:
            try:
                pacing = learner.pacing(self.child, spec)
            except Exception as exc:  # noqa: BLE001 — never block a lesson on the store
                logger.warning("pacing unavailable: %s", exc)
                pacing = {"mode": "guided", "speed": 1.0, "checkpoints": "all", "reasons": []}
        if self.twin is not None:  # a twin: you know the method, so try the answer first; a miss runs every step
            pacing = dict(pacing or {"speed": 1.0, "reasons": []})
            pacing.update(mode="challenge_first", checkpoints="all",
                          reasons=list(pacing.get("reasons") or []) + ["one like the last problem: try the answer first"])
        return pacing

    # ------------------------------------------------------------ My lessons (signed-in child only)
    def _save(self, spec: dict, solved: dict | None, lesson_id) -> int | None:
        return saved.replay_of(self.child, lesson_id) or saved.save_new(self.child, spec, solved)

    def _progress(self, progress: float) -> None:
        try:
            from app import db

            db.set_lesson_progress(self.lesson_id, self.child["id"], progress)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not save lesson progress: %s", exc)

    def _finish(self, lesson: Lesson) -> None:
        try:
            from app import db

            status, hints = saved.outcome(lesson.score, self.hints)
            db.finish_lesson_run(self.lesson_id, self.child["id"], status, hints,
                                 round(time.monotonic() - self.started, 1))
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not save the lesson outcome: %s", exc)

    def _badges(self, lesson: Lesson) -> list[dict]:
        """Badges this lesson earned (stored only for a signed-in child, and then only the new ones)."""
        try:
            earned = badges.evaluate(lesson.spec, lesson.score, asked_question=self.asked_question,
                                     twin_after_miss=bool(self.twin and self.twin["after_miss"]))
            return badges.award(self.child, earned)
        except Exception as exc:  # noqa: BLE001 — a badge must never break the end of a lesson
            logger.warning("badges unavailable: %s", exc)
            return []

    def _last_lesson_missed(self, spec: dict) -> bool:
        try:
            from app import db

            rows = learner._last_lesson(db.attempts(self.child["id"], learner.heuristic_of(spec), limit=20))
            return badges.had_miss(r["outcome"] for r in rows)
        except Exception:  # noqa: BLE001
            return False

    def _mastery(self, spec: dict) -> dict | None:
        try:
            from app import db

            heuristic = learner.heuristic_of(spec)
            wins = learner.challenge_wins(db.attempts(self.child["id"], heuristic, limit=500))
            out = learner.mastery_level(db.get_mastery(self.child["id"], heuristic), self.child.get("level"), wins)
            return {"heuristic": heuristic, "label": heuristic.replace("_", " "), **out}
        except Exception as exc:  # noqa: BLE001
            logger.warning("mastery unavailable: %s", exc)
            return None

    def _record(self, lesson: Lesson) -> dict | None:
        """Outcomes and seconds per checkpoint, then the pacing the next lesson of this kind would get."""
        try:
            learner.record_lesson(self.child, lesson.spec, lesson.results())
            return learner.pacing(self.child, lesson.spec)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not record the lesson: %s", exc)
            return None

    async def _verdict(self, verdict) -> None:
        lesson = self.lesson
        if verdict.result in ("try_again", "no_number"):
            self.hints += 1  # a clue was given (My lessons: "Solved with 2 hints")
        event = {"type": "verdict", "result": verdict.result, "say": verdict.say, "heard": verdict.heard,
                 "waiting": lesson.waiting is not None}
        if verdict.board is not None:
            event["board"] = verdict.board
        if lesson.recap_pending:
            event["recap"] = True  # the next event draws the rest of the board at once
        if verdict.result == "guided":
            event["checkpoints"] = len(lesson.stops)  # the challenge was missed: every checkpoint is back
        await self.send(event)

    async def child_said(self, text: str) -> None:
        if self.prep is not None:
            await self._prep_child_said(text)
            return
        lesson = self.lesson
        if lesson is None:
            return
        kind = route(lesson, text)
        if lesson.waiting is None and lesson.is_final_answer(text):
            await self._verdict(lesson.early_answer())  # the final answer while the board is drawing
            return
        if kind == "answer":
            await self._verdict(lesson.answer(text))
        elif kind == "question":
            self.asked_question = True
            # A short spoken acknowledgement right away (cached), so the child isn't left in silence while Spark thinks.
            # Answers don't need one: code checks them and the verdict follows within ~0.1 s.
            await self.ws.send_json({"type": "thinking", **await run_in_threadpool(ack_audio), "ts": round(time.time() * 1000)})
            reply = await run_in_threadpool(answer_question, lesson, text)
            await self.send({"type": "tutor", **reply, "waiting": lesson.waiting is not None})

    async def board(self, op, value) -> None:
        """A move on the builder. Only for the board task that is waiting, and only its own op; anything else is
        dropped (a late tap after the verdict, a stale page)."""
        lesson = self.lesson
        if self.prep is not None or lesson is None or lesson.waiting is None or lesson.waiting.kind != "board":
            return
        if op != (lesson.waiting.task or {}).get("op"):
            return
        await self._verdict(lesson.answer_board(value))

    async def show_me(self) -> None:
        if self.prep is not None and self.prep.asking:
            await self._prep_reply(reading.NOT_SURE)
        elif self.lesson is not None and self.lesson.waiting is not None:
            await self._verdict(self.lesson.skip())

    # ------------------------------------------------------------ voice
    async def mic(self, on: bool) -> None:
        """The child's tap. On starts a turn; the first one opens the stream (~1.4 s), which then stays open for the
        lesson, so later taps only start and stop frames. Off ends the turn as soon as the recogniser's last words
        are in (STOP_GRACE_S at most): while muted the pump feeds silence, so the recogniser finishes the sentence."""
        if on and self.asr is None:
            if not self._live():
                await self.ws.send_json({"type": "mic", "on": False})
                return
            if not await self._open_asr():
                await self.send({"type": "error", "message": "Voice isn't available right now. You can type your answer."}, speak=False)
                await self.ws.send_json({"type": "mic", "on": False})
                return
            await run_in_threadpool(ack_audio)  # warm the acknowledgement clip now, not on the first question
        if on:
            self.handled, self.partial, self.swallow_until = None, "", 0.0
            self._begin_turn()
        elif self.turn is not None:
            self.turn.stop(time.monotonic())
        self.muted = not on
        await self.ws.send_json({"type": "mic", "on": on, "ack": True})

    # ------------------------------------------------------------ turn-taking (VoiceTurn, TURN_SILENCE_S)
    def _begin_turn(self) -> VoiceTurn:
        self.turn = VoiceTurn(time.monotonic())
        if self.turn_task is not None:
            self.turn_task.cancel()
        self.turn_task = asyncio.create_task(self._turn_clock(self.turn))
        return self.turn

    async def _turn_clock(self, turn: VoiceTurn) -> None:
        """Ends the turn at its deadline (silence, nothing said, or the grace after a tap to stop)."""
        while self.turn is turn:
            wait = turn.deadline() - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 0.25))
                continue
            if self.turn_task is asyncio.current_task():
                self.turn_task = None  # from here on, nothing may cancel the reply half-way (the lesson may end in it)
            async with self.lock:
                if self.turn is turn:
                    await self._end_turn("tap" if turn.stopped is not None else "silence")
            return

    def _clear_answer(self, text: str) -> bool:
        """A number answer at a checkpoint (or the final answer said early): checked at once, no silence wait."""
        lesson = self.lesson
        if self.prep is not None or lesson is None:
            return False
        if lesson.waiting is None:
            return lesson.is_final_answer(text)
        if lesson.waiting.kind == "board":
            return False  # a spoken board move names several bars: let the child finish the sentence
        return route(lesson, text) == "answer" and bool(numbers_in(text))

    async def _end_turn(self, how: str, text: str | None = None) -> None:
        """Close the turn (call with the lock held): mute, tell the client the mic is off, then answer what was said."""
        turn, self.turn = self.turn, None
        said = text or ""
        if turn is not None:
            said = turn.text() if text is None else text
            self.muted = True
            if how in ("tap", "silence") and turn.partial:  # its final is still to come: don't take it as a new turn
                self.swallow_until = time.monotonic() + TURN_SILENCE_S
            await self.ws.send_json({"type": "mic", "on": False, "turn": "end" if said else "empty"})
        if said:
            await self._voice_turn(said, how)

    def _live(self) -> bool:
        return self.lesson is not None or self.prep is not None

    async def _open_asr(self) -> bool:
        lesson = self.lesson
        if not self._live():
            return False
        stream = TranscribeStream(_keywords(lesson.spec) if lesson is not None else _question_words(self.prep.question))
        try:
            await stream.open()
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice input unavailable: %s", exc)
            return False
        if self.closing or not self._live() or self.asr is not None:  # the socket closed (or a stream opened) meanwhile
            await stream.close()
            return self.asr is not None
        self.asr, self.asr_t0, self.asr_bytes = stream, time.monotonic(), 0
        self.partial, self.handled = "", None
        self.asr_task = asyncio.create_task(self._listen(stream))
        self.pump_task = asyncio.create_task(self._pump(stream))
        return True

    async def _pump(self, stream: TranscribeStream) -> None:
        """Keep the stream at real time: the service closes one fed slower than real time (after ~15 s, measured),
        so gaps (muted, the teacher talking, a slow network) are filled with silence."""
        try:
            while self.asr is stream:
                await asyncio.sleep(0.1)
                behind = (time.monotonic() - self.asr_t0) * ASR_BYTES_PER_S - self.asr_bytes
                if behind > 0.25 * ASR_BYTES_PER_S:
                    n = int(min(behind - 0.05 * ASR_BYTES_PER_S, ASR_BYTES_PER_S)) & ~1
                    self.asr_bytes += n
                    await stream.send(bytes(n))
        except Exception:  # noqa: BLE001 — _listen notices the closed stream
            pass

    async def _listen(self, stream: TranscribeStream) -> None:
        try:
            async for ev in stream.events():
                if ev["type"] == "partial":
                    await self.ws.send_json({"type": "heard", "text": ev["text"], "final": False})
                    self._on_partial(ev["text"])
                elif ev["type"] == "final":
                    await self._on_final(ev["text"])
        except Exception as exc:  # noqa: BLE001 — the stream dropped (60-minute limit, network)
            logger.info("asr stream ended: %s", exc)
        finally:
            if self.asr is stream:  # closed by the service, not by us: reopen without bothering the child
                self.asr = None
                if not self.closing:
                    asyncio.create_task(self._reopen())

    async def _reopen(self) -> None:
        for attempt in range(3):
            await asyncio.sleep(0.5 * attempt)
            if self.closing or not self._live() or self.asr is not None:
                return
            if await self._open_asr():
                logger.info("asr stream reopened")
                return
        self.muted = True
        try:
            await self.ws.send_json({"type": "mic", "on": False})
        except Exception:  # noqa: BLE001 — socket already closed
            pass

    def _on_partial(self, text: str) -> None:
        """Keep the turn alive while words come in, and start (or restart) the stable-partial timer when a partial
        holding a number arrives at a checkpoint."""
        now = time.monotonic()
        if self.handled is None:  # (after a stable-partial check, the rest of that sentence is its own business)
            if self.turn is None:
                if now < self.swallow_until:
                    return  # the tail of a turn that already ended
                self._begin_turn()  # words with no turn open (audio already in flight): they start one
            self.turn.heard_partial(text, now)
        if text == self.partial:
            return  # unchanged: let the timer run
        self.partial, self.partial_t = text, now
        if self.stable_task is not None:
            self.stable_task.cancel()
            self.stable_task = None
        lesson = self.lesson
        if (self.handled is None and lesson is not None and lesson.waiting is not None
                and lesson.waiting.kind == "number" and numbers_in(text)):  # "Ali has three... and Ben one": wait
            self.stable_task = asyncio.create_task(self._stable(text))

    async def _stable(self, text: str) -> None:
        await asyncio.sleep(STABLE_PARTIAL_S)
        self.stable_task = None  # from here on, a newer partial must not cancel the check half-way
        async with self.lock:
            lesson = self.lesson
            if (self.partial != text or self.handled is not None or lesson is None or lesson.waiting is None
                    or route(lesson, text) != "answer"):
                return
            self.handled = (lesson.waiting, numbers_in(text)[-1])
            await self._end_turn("stable partial", text)

    async def _on_final(self, text: str) -> None:
        if self.stable_task is not None:
            self.stable_task.cancel()
            self.stable_task = None
        async with self.lock:
            now = time.monotonic()
            handled, self.handled, self.partial = self.handled, None, ""
            if handled is not None:
                if not text.strip():
                    return
                # Already checked from a stable partial. Only a different number for the same, still open
                # question counts again ("four... no, five" after a hint); otherwise it would be checked twice.
                checkpoint, number = handled
                said = numbers_in(text)
                if not (self.lesson is not None and self.lesson.waiting is checkpoint and said and said[-1] != number):
                    logger.info("voice final already checked from a stable partial: %r", text)
                    return
                await self._voice_turn(text, "final")
                return
            if self.turn is None:
                if now < self.swallow_until:
                    self.swallow_until = 0.0
                    logger.info("voice final after its turn ended: %r", text)
                    return
                if not text.strip():
                    return
                self._begin_turn()
            turn = self.turn
            turn.heard_final(text, now)
            said = turn.text()
            if turn.stopped is not None:
                await self._end_turn("tap")  # tapped off and the last words are in
            elif said and self._clear_answer(said):
                await self._end_turn("final")
            # otherwise the turn goes on until TURN_SILENCE_S without new words (_turn_clock)

    async def _voice_turn(self, text: str, how: str) -> None:
        """One spoken turn, through the same path as a typed one. Logs where the time went."""
        heard_at = time.monotonic()
        await self.ws.send_json({"type": "heard", "text": text, "final": True, "how": how, "ts": round(time.time() * 1000)})
        await self.child_said(text)
        timing_log.info("voice turn (%s): last partial change -> heard %.0f ms, heard -> reply sent %.0f ms",
                    how, (heard_at - self.partial_t) * 1000, (time.monotonic() - heard_at) * 1000)

    async def audio(self, pcm: bytes) -> None:
        if self.asr is not None and not self.muted:
            self.asr_bytes += len(pcm)
            try:
                await self.asr.send(pcm)
            except Exception:  # noqa: BLE001 — _listen notices the closed stream
                pass

    async def _close_asr(self) -> None:
        """Stop voice input for good (the lesson ended or the socket closed)."""
        stream, tasks = self.asr, (self.asr_task, self.pump_task, self.stable_task, self.turn_task)
        self.asr, self.asr_task, self.pump_task, self.stable_task, self.turn_task = None, None, None, None, None
        self.turn = None
        for task in tasks:
            if task is not None:
                task.cancel()
        if stream is not None:
            await stream.close()


@router.websocket("/ws/lesson")
async def lesson_socket(ws: WebSocket):
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    await ws.accept()
    session = LessonSession(ws, child=await run_in_threadpool(child_for_lesson, ws.cookies))
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.audio(message["bytes"])
                continue
            data = json.loads(message.get("text") or "{}")
            kind = data.get("type")
            if kind == "mic":
                await session.mic(bool(data.get("on")))
                continue
            blocked = access.lesson_message_allowed(ws.cookies, kind, data)
            if blocked:
                await session.send({"type": "error", "message": blocked, "sign_in": blocked == access.SIGN_IN_FIRST},
                                   speak=False)
                continue
            if kind in ("start", "prepare"):
                session.board_ui = data.get("board") is True
            async with session.lock:
                if kind == "start":
                    await session.start(data.get("spec") or {}, twin=data.get("twin"), lesson_id=data.get("lesson_id"))
                elif kind == "prepare":
                    await session.prepare(data)
                elif kind == "next":
                    await session.advance()
                elif kind == "text":
                    await session.child_said(str(data.get("text", ""))[:500])
                elif kind == "show_me":
                    await session.show_me()
                elif kind == "board":
                    await session.board(data.get("op"), data.get("value"))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("lesson socket failed")
    finally:
        session.closing = True
        await session._close_asr()
