# Testing — What Was Run and What Was Observed

Last updated: 2026-11-04. Every number below comes from a saved result file or a run on this date.
Companion docs: [how-it-works.md](how-it-works.md) · [built-so-far.md](built-so-far.md) · [evals.md](evals.md).

## 1. How to reproduce

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q                                          # offline tests, no key needed
python evals/run_evals.py --suite all --limit 200  # evals, needs META_API_KEY
python evals/e2e_browser.py                        # headless browser lesson (start the server first)
python tests/e2e_board_browser.py                  # board tasks on a phone and a desktop viewport
```

`--limit` defaults to 100, so pass a higher limit to run the whole public set (107).

## 2. Unit and integration tests — 757 passed

Command: `pytest -q` → **757 passed** (Nov 4). No network and no API key:
model calls are faked.

Largest files: `test_lesson_v2.py` (lesson steps and checkpoints for v2), `test_verify_v2.py` (each verifier
rule), `test_prepare.py` (beat 1), `test_plan_v2.py` (routing, waves, agreement, decline),
`test_v2_examples.py` (every golden example verifies and plays), `test_render_v2.py`, `test_spec_v2.py`,
`test_twins.py`, `test_accounts.py`, `test_glimmer_pipeline.py`, `test_board_task.py`, `test_lesson_ws.py`
(the socket, including the voice-turn rules: a clear number ends the turn at once, silence ends it after 3 s,
tapping off waits for the last words, an empty turn).

## 3. Evals (live models)

| Eval | Result | File (`evals/results/`) |
|---|---|---|
| Public word-problem set, all 107 (typed → plan → verify → second answer) | **107/107 correct, 0 wrong** | `text-20261102T163957Z.json` |
| First 100 of the public set, with the fine-tuned Glimmer racing | 100/100, 0 wrong; Glimmer's plan won 42 | `text-20261103T024102Z.json` |
| School-style held-out set, 180 (not published) | 162 correct, 16 declined, 2 scored wrong: an answer-key error, and cm vs m | private |
| Fresh school questions, 150, never used before (P4–P6, mostly 3+ marks), Nov 4 | 137/150 as scored; 4 of the 5 "wrong" were wrong answer keys, so 141 right, 1 really wrong (a question missing a number, now declined), 8 declined | private |
| Hand-picked set of 300 (complex before–after, transfer, ratio change, hard wording; every key re-solved by hand, 14 fixed): 200 practice | 196/200 correct, 1 unit mix-up (270¢ vs $2.70), 3 declined | private |
| Same set: 100 held back, run once after the fixes | **99/100 correct, 0 wrong**, 1 declined | private |
| Real scanned worksheet pages, 80 questions | 75/80 read exactly | private |
| Photos: 32 questions drawn as worksheet photos | 29/32 correct, 0 wrong; numbers read 32/32 | `photo-20261102T103847Z.json` |
| Lessons played by simulated children (offline) | 29/29 pass | `lesson-20261102T103530Z.json` |
| Adversarial child questions ("just tell me", off-topic) | **0 unsafe replies shown** out of 124 | `tutor-20261102T103700Z.json` |
| Spoken answers: Piper speech → realtime ASR, 10 numbers × 2 speeds | 20/20 understood | `voice-20261102T103955Z.json` |
| "Check my bar model": 72 synthetic drawings with 5 mistake types | 69/72 first mistake found; 0 false alarms on 12 correct | `review-20261102T105253Z.json` |

The tutor declines rather than show an answer it couldn't verify, so "declined" is not "wrong".
The school-style questions are kept private; 9 samples are in `evals/samples/school.json`.

**What the fresh questions taught us** (Nov 4, fixed and tested): litres printed as ℓ/mℓ, cm, mm, cents and the
minutes between clock times are allowed numbers; a spoken chain ("106 plus 52 plus 53 is 211") is added from the
start; "the $262 part" is an amount, not 262 parts; "$154 more than" no longer trips the "altogether" rule; a
question missing a count ("gave 204 to each of her friends") is declined instead of guessed. Still open: 6–7 bars
across stages can fail to fit the board, and an answer can come in cents when the key uses dollars.
The evals exit with code 1 if any wrong answer or unsafe reply was shown.

## 4. Browser checks

- `evals/e2e_browser.py`: a full lesson in headless Chromium, typed answers and a fake-microphone spoken answer.
  Passes on a local server (the only failure is Google's sign-in script refusing `127.0.0.1`, expected).
- `tests/e2e_board_browser.py`: every board task (units, transfer, same years, "?" on a brace) on an iPhone 13
  viewport and a desktop, in all four skins. Passes (Nov 3), with screenshots in `out/`.
- `tests/e2e_pacing_browser.py`: pacing changes between two lessons for the same child.

## 5. Not tested yet

| Gap | Why |
|---|---|
| Touch dragging on a real phone | Board tasks were tested with a mouse in Playwright; tapping works as a fallback. |
| The microphone on a real phone, and Google sign-in in the installed PWA | Needs a real device. |
| Results by school level | The evals don't record level, so P4 vs P6 accuracy isn't known. |
| Real children | By design: all evals use problems we wrote or collected and synthetic voices. |
