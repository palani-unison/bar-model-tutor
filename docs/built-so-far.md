# What Has Been Built

Last updated: 2026-11-04 (hackathon version). Related: [how-it-works.md](how-it-works.md) ·
[testing.md](testing.md) · [privacy.md](privacy.md) · [../README.md](../README.md)

## 1. The product in one paragraph

A parent signs in, agrees to the terms and adds their child (P3–P6, Singapore). The child snaps or types a
maths word problem. A teacher's voice and a live whiteboard build the Singapore bar model with them, stopping
to ask at every step. The child answers by voice, typing or building the bars on the board, and code checks
every answer against a bar model that code has verified and a second model has agreed with. The answer only
appears after the child gets it or has used two hints.

## 2. What works today

| Area | State |
|---|---|
| Input | Camera, photo, PDF, typed. Several questions on a page → the child picks one. |
| Problem types | BarSpec v2 covers part–whole, comparison, multiples, before–after, transfer, equal-end, same difference, remainder, percentage, ratio change, excess–shortage, units and value, assumption, average, rate; anything else gets a verified "working only" board. |
| Safety core | `verify_v2` (code) checks every plan; Muse Spark 1.2 must agree; otherwise the tutor declines. |
| Live lesson | One WebSocket: live SVG board, Piper voice, streamed voice answers, typed answers, interrupt / "why?" / "wait", hint ladder. Beat 1 starts while the plan is made. |
| Board tasks | Build the units per bar, drag a transfer, add the same years to each bar, place the "?" on the right brace. |
| Personalisation | Rating per method, adaptive pacing (challenge-first or full walk-through), badges and method levels, "Try one like this" twins, My lessons. |
| Accounts | Google sign-in for parents, versioned terms consent, child profiles with hero names and passwords, family-device login, delete a child or the whole account. |
| Fine-tuning | Muse Glimmer 30B + LoRA trained on bar-model plans (Fireworks serverless training). It can race Spark in the planner (`PLANNER_MODEL=glimmer`); off in the live app because it was no more accurate and made the median plan slower. |
| App | Installable PWA, four skins, phone and laptop layouts. |
| Admin | The owner's dashboard (`/admin`, opened by the owner's Google account): usage by day, plan outcomes and timings, families, children, methods, declined questions, and "Report a problem" reports with status and an export for improving the tutor. Each parent still sees only their own children. |
| Showcase | `/showcase`: the demo videos, the deck as a slide carousel, and these docs, inside the app. |
| Deploy | Modal: one container running FastAPI, the WebSocket and the renderer; SQLite snapshotted to a Volume. |

Not in the hackathon version: the iPad co-solve room, the parent coach mode, and geometry (figures redrawn and explained step by step), which are prototypes kept out of this repo.

## 3. File map

### Agent (`agent/`)

| File | What it does |
|---|---|
| `client.py` | The one Meta Model API client: retries, cost-weighted logging, pinned model ids (never Contributor). |
| `extract.py` | Photo / PDF / text → `ProblemJSON`; lists the questions on a page; cleans school LaTeX. |
| `prepare.py` | Beat 1: reads the question aloud and marks its numbers before there is a plan. |
| `plan.py` | `ProblemJSON` → BarSpec: routing, parallel waves, the Glimmer race, the agreement check, decline. |
| `glimmer.py` | The fine-tuned Muse Glimmer planner (Fireworks sampler). |
| `spec_v2.py` | Parses a BarSpec v2 and solves it (SymPy). |
| `verify_v2.py`, `verify.py` | Deterministic checks for v2 and v1 specs. No model calls. |
| `check.py` | The independent second answer (Muse Spark 1.2). |
| `lesson.py` | A verified spec → board steps and checkpoints, with the hint ladder. |
| `board_task.py` | "Build it yourself" board checkpoints and their checks. |
| `tutor.py` | Answers the child's own questions, with guards against leaking the answer. |
| `twins.py` | "Try one like this": same structure, new numbers, re-verified. |
| `learner.py` | Per-method rating and pacing. |
| `badges.py` | Method badges and levels. |
| `review.py` | "Check my bar model" from a photo of the child's drawing. |
| `critic.py` | Stub (vision critic for rendered frames, not used). |
| `prompts/` | `extract.md`, `plan.md`, `plan_v2.md`, `glimmer.md`, `tutor.md`. |

### App (`app/`)

| File | What it does |
|---|---|
| `main.py` | FastAPI app: pages, PWA files, `/api/questions`, `/api/extract`, `/api/solve*`, `/api/check-drawing`, `/api/render`, `/terms`, `/privacy`, `/admin`. |
| `lesson_ws.py` | The live lesson WebSocket (`/ws/lesson`) and its voice turns. |
| `accounts.py` | Google sign-in, consent, child profiles, hero-name login, sessions. |
| `access.py` | Who may use what on a public deploy; daily quotas; admin login. |
| `db.py` | SQLite store (parents, children, attempts, mastery, badges, lessons) and Volume snapshots. |
| `lessons.py` | My lessons: list, replay, keep. |
| `practice.py` | Twins endpoint and the "My methods" panel. |
| `admin.py` | The owner's dashboard, the plan-outcome log and "Report a problem". |
| `showcase.py`, `static/showcase.html` | The Showcase screen: videos, slides, docs. |
| `static/index.html` | The single-page app (sign-in, onboarding, lesson, end panel, parent panel). |
| `static/ui/stage.js`, `app.css` | One focused stage at a time, skins, bottom sheets. |
| `static/ui/builder.js` | The board-task builder (+/–, drag, braces). |
| `static/lessons.js`, `badges/`, `pwa/` | My lessons grid, badge art, PWA icons. |

### Voice, renderer, specs, training

| Path | What it does |
|---|---|
| `voice/asr.py` | Muse Voice Transcribe realtime client. |
| `voice/tts.py` | Piper text-to-speech (offline). |
| `renderer/render.py` | Narrated whiteboard MP4 and stills from a verified spec. |
| `specs/barspec.v2.schema.json`, `specs/examples/v2/` | The v2 contract and 20 golden example lessons. |
| `training/glimmer/` | The Glimmer fine-tune pipeline: split, label, build, train, evaluate (`python -m training.glimmer`). |
| `modal_app.py` | The Modal deployment. |

### Evals and tests

| Path | What it does |
|---|---|
| `evals/run_evals.py`, `suites.py`, `harness.py` | Eval suites: text, photo, lesson, tutor, voice, review. One JSON per run in `evals/results/`. |
| `evals/dataset/` | The public problem set (107). |
| `evals/page_eval.py`, `voice_latency.py`, `e2e_browser.py` | Real scanned pages, spoken-answer latency, a headless browser lesson. |
| `tests/` | 755 offline tests (`pytest -q`), plus Playwright scripts for board tasks and pacing. |

## 4. Known gaps

- The "move" board task checks the transfer loosely.
- The child's first words can be lost while the microphone starts.
- Long questions with 6–7 bars across several stages can be declined because the bars don't fit the board.
- An answer can come in cents where the key wants dollars (the same amount, but not the expected unit).
- Daily quotas live in memory and reset when the server restarts.
- Some tables, number lines and the averaging animation are simple versions (see board-templates.md).
- Meta's Model API terms require users to be 18+; the app is built around a supervising parent who holds the
  account (see the README's safety box).
