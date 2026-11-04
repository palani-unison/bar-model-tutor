# How It Works

Last updated: 2026-11-03. Companion docs: [built-so-far.md](built-so-far.md) (what each file does),
[testing.md](testing.md) (evidence), [spec-v2.md](spec-v2.md) (the plan format),
[board-templates.md](board-templates.md) (the pictures), [privacy.md](privacy.md).

## 1. End-to-end flow

```text
Parent signs in (Google) → agrees to the terms → adds a child (hero name + password)
                                   │
Child (browser / installed PWA)    ▼
  Camera / photo / PDF ──► POST /api/questions   several questions on the page? the child picks one
                      └──► POST /api/extract     the chosen question → ProblemJSON (Muse Spark 1.3 vision)
  Typed question ────────► (no upload; sent straight to the lesson socket)
                                   │
                                   ▼
  WS /ws/lesson  {"type":"prepare", ...}
    ├─ beat 1 at once: the teacher reads the question and marks the numbers (agent/prepare.py)
    └─ meanwhile PLAN (agent/plan.py)
          Muse Spark 1.3 drafts in parallel waves ─┐
          fine-tuned Muse Glimmer races (off live) ─┤──► verify_v2 (code) ──fail──► next wave, with the errors
                                                    │         │ pass
          Muse Spark 1.2 answers on its own ────────┴──► answers agree? ── no ──► careful re-solve / decline
                                                              │ yes
                                                              ▼
  LESSON (agent/lesson.py): board steps + Piper voice + checkpoints, all from the verified spec
    the child answers by voice (Muse Voice Transcribe, streamed), typing or on the board
    code checks each answer: try again → hint 1 → hint 2 → shown
                                                              ▼
  END: badges, method levels, "Try one like this" (a verified twin), saved to My lessons
```

Nothing a model writes reaches the child until code has checked it.

## 2. Step by step

### 2.1 Sign-in and consent (`app/accounts.py`, `app/access.py`)

- A parent signs in with Google. We keep Google's account id and a first name, never the email.
- Before any AI feature works, the parent ticks the three terms boxes (`/terms`, versioned). The version and
  time are stored.
- The parent adds children: nickname, level, an emoji avatar. Each child gets a hero name (e.g. `Thor987`) and
  a password the parent sets and can reset.
- On a family device (where the parent signed in) a child taps their avatar; elsewhere they type their hero
  name and password.
- Anyone can try the built-in example lessons without signing in. Anything that spends the Meta API on a new
  problem needs a signed-in member of a consented family, and is counted against a daily quota
  (`PLANS_PER_FAMILY_DAY`, `PLANS_PER_DAY`).
- The admin dashboard (`/admin`) opens only for the owner's Google account (`ADMIN_EMAILS`). There is no password, and nothing locks the app itself.

### 2.2 Read the question (`agent/extract.py`)

1. **Find the questions.** A photo or PDF goes to `POST /api/questions`. Muse Spark 1.3 lists the questions on
   the page; if there are several, the child taps one.
2. **Read one.** `POST /api/extract` reads that question into a `ProblemJSON`
   (`{question, level, problem_type, entities, numbers, asked, ...}`). PDFs with a text layer are read without a
   model; scanned PDFs are rendered to images first.
3. **Clean.** School LaTeX becomes plain text (`$\frac{2}{5}$` → `2/5`, `$4 000` → `$4000`).
4. **Refuse early.** Questions that need a figure, table or graph, or that have several parts, get a friendly
   "I can't help with this one" before any planning.

Photos are processed in memory and never written to disk.

### 2.3 Teach while planning (`agent/prepare.py`)

The board doesn't wait for the plan. As soon as the question is known, the teacher reads it aloud and marks the
key numbers and who is who. This "beat 1" comes from the question text alone, so it is safe to show before
anything is verified.

### 2.4 Plan (`agent/plan.py`)

- The plan is a **BarSpec v2** ([spec-v2.md](spec-v2.md)): the quantities, the relations between them as
  equations, the bars in each stage (before/after), the working lines and a board template.
- **Routing.** A regex picks the family (percentage, ratio change, remainder, transfer, before–after,
  equal-end, constant difference, units and value, excess–shortage, assumption, average, rate). Known families
  go straight to v2; anything else races a v1 draft against a v2 draft.
- **Waves.** Drafts in a wave run in parallel; the first one that passes the checks and agrees with the second
  solver wins. A failed wave hands its exact errors to the next:
  Spark (low, low) → Spark (low, medium) → Muse Wonder 1.0 (medium).
- **Glimmer race.** With `PLANNER_MODEL=glimmer`, one draft from our fine-tuned Muse Glimmer runs alongside.
  It passes the same gate and must agree with the quick second answer.
- **Presentation extras.** If a draft's only faults are its board shape or a check line, those extras are
  dropped and the spec re-verified, rather than throwing away a correct answer.
- **Offline fallback.** Without `META_API_KEY`, a small regex planner covers "A has N times as many as B;
  together T", so the demo still runs.

### 2.5 Verify (`agent/spec_v2.py`, `agent/verify_v2.py`) — no model involved

- `solve_spec` solves the spec's equations itself (SymPy) and works out every bar and unit.
- `verify_v2` rejects a plan when, for example, a number isn't in the question, the asked quantity is wrong, a
  bar's segments don't add up, units differ in size, the stages don't match, or the board template's shape
  rules fail. Each error is one sentence the planner can act on.
- `layout_error` checks the board can actually draw it on a phone.

### 2.6 Two models must agree (`agent/check.py`)

Muse Spark 1.2 answers the plain question on its own, in parallel, as one number. The plan is accepted only if
both answers match (within 0.005). On a disagreement, one careful re-solve replaces the quick answer; the
planner is only told "an independent solver got a different answer", never the number. If no draft agrees,
the tutor declines: it won't show an answer it couldn't check.

### 2.7 The live lesson (`app/lesson_ws.py`, `agent/lesson.py`)

- One WebSocket carries everything: board steps, the teacher's voice (Piper, offline text-to-speech, as MP3),
  the child's streamed microphone audio, and their answers.
- **Checkpoints** ("How many units altogether?", "What is 1 unit?", the answer) and their expected values come
  from the verified spec, so a model never decides whether the child is right.
- **Hint ladder:** wrong → try again → hint 1 → hint 2 → the teacher shows it. The answer is never shown
  before that; the rule is in code.
- **Board tasks** (`agent/board_task.py`, `ui/builder.js`): on some steps the child builds the model before the
  teacher draws it: the units per bar (+/–), a transfer (drag), the same added years on every bar, or placing
  the "?" on the right brace. Each move is checked against the spec. These appear only when the client says it
  can draw the builder (`"board": true`).
- **Voice turns:** the child taps the mic. A clear number is judged straight away; anything else ends after
  3 s of silence. The child can interrupt the teacher, ask "why?", say "wait" or "show me".
- **Free questions** go to Muse Spark 1.3 (`agent/tutor.py`). A reply is blocked if it contains a number the
  child is still working out, or one that isn't on the board.

WebSocket messages:

| Client → server | Meaning |
|---|---|
| `start` (`spec`, optional `lesson_id`, `board`) | Begin a lesson on a verified spec. Anonymous only for built-in examples. |
| `prepare` (`question` or `problem`, `board`) | Plan a new problem and teach beat 1 meanwhile. Uses one plan of quota. |
| `next` | The last event finished playing; continue. |
| `text` | The child typed something (max 500 characters). |
| `show_me` | Show this step. |
| `board` (`op`: `units` / `mark` / `move` / `grow`, `value`) | A move on a board task. |
| `mic` (`on`) | Start or end a voice turn. Binary frames carry 24 kHz mono PCM while it is on. |

Server → client: `lesson`, `step`, `ask`, `verdict`, `tutor`, `heard`, `thinking`, `recap`, `end`, `error`,
`prepare`, `plan`, `plan_failed`, `mic`. Speaking events carry their audio; every event carries a server
timestamp (`?debug=1` shows a timing overlay).

### 2.8 Personalisation (`agent/learner.py`, `agent/badges.py`)

- Each child has an Elo-like rating per method (part–whole, before–after, same difference, remainder…).
  A new child starts at their school level's par (1000 = P3, +100 per level).
- The next lesson's pacing follows the rating: challenge-first and faster playback where the child is strong,
  a full walk-through with more checkpoints where they aren't.
- Badges are earned by evidence in a lesson ("Before–After Builder", "came back after a miss"); method levels
  run from "Starting" to "Master".

### 2.9 After the lesson

- **Try one like this** (`agent/twins.py`): the same structure with new numbers, re-solved and re-verified by
  code.
- **My lessons** (`app/lessons.py`): a signed-in child's lessons, to replay; ★ keeps one.
- **Check my bar model** (`agent/review.py`, `POST /api/check-drawing`): the child photographs their own drawing;
  Spark only reads it, and code finds the first mistake and gives one hint.
- **Video** (`POST /api/render`, `renderer/render.py`): a narrated whiteboard MP4 of the verified spec.

## 3. Models

| Job | Model |
|---|---|
| Read photos and PDFs, plan, answer the child's questions | `muse-spark-1.3` |
| Independent second answer | `muse-spark-1.2` |
| Escalation on hard problems | `muse-wonder-1.0` |
| Plan racer, fine-tuned with LoRA | Muse Glimmer 30B, on Fireworks AI |
| The child's voice, streamed | `muse-voice-transcribe-1.0` |
| Teacher's voice | Piper (offline; Meta has no TTS API) |

Standard tier only, never Contributor models. All Meta calls go through `agent/client.py`. Names, emails, hero
names and progress are never sent to any model.

## 4. Errors the child or parent can see

| Situation | Result |
|---|---|
| Not signed in, on a new problem | "Sign in first." (examples still work) |
| Parent hasn't agreed to the terms | "A parent needs to agree to the terms first." |
| Daily limit reached | A friendly "come back tomorrow" message |
| Figure, table, graph or multi-part question | Declined before planning |
| No plan passes the checks, or the two answers don't agree | "I could not check my working on this problem, so I won't show an answer I'm not sure of." |
| A model is unavailable | A try-again-later message; nothing unverified is shown |

## 5. Configuration

See the README's settings table. In short: `META_API_KEY` (required for AI), `GOOGLE_CLIENT_ID` and
`SESSION_SECRET` (parent sign-in), `PUBLIC_DEPLOY=1` (public mode), `ADMIN_EMAILS` (who opens `/admin`),
`PLANNER_MODEL=glimmer` with `FIREWORKS_API_KEY` and `GLIMMER_MODEL_PATH` (the Glimmer race),
`PLANS_PER_FAMILY_DAY` and `PLANS_PER_DAY` (quotas). Locally, without `PUBLIC_DEPLOY`, everything is open for
development and the tests.
