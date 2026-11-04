# Board templates: one teacher's picture per problem type

Singapore teachers don't improvise a new diagram each time. Every problem family has a standard
picture and a standard way of talking it through. This catalogue fixes those pictures, so the
planner picks a template (it doesn't invent a layout), the board looks the same every time for the
same kind of problem, and the child learns the *method*, not just this answer.

Status key:
- **✅ drawable now:** existing v2 primitives (stages, bars, segments, brackets, `move_part`).
- **🟡 needs a small primitive.**
- **🔴 needs a new primitive.**

## 1. The teaching script: same skeleton for every template

| Beat | What the teacher does | Timeline actions | Live lesson |
|---|---|---|---|
| 1. Read | Reads the question, highlights the 2–3 key facts | `write_question`, `highlight` | — |
| 2. Draw | Draws the template's picture, one fact per step | template-specific | — |
| 3. Mark | Puts the known numbers on the picture, and "?" on what's asked | `bracket`, `question_mark` | — |
| 4. Think | Pauses: "What do we know about 1 unit?" | `pause` | **checkpoint** |
| 5. Key step | Finds 1 unit (or the key quantity), shown on the picture | `write`, `label_unit` | checkpoint |
| 6. Answer | Builds the answer from the units | `write` | checkpoint |
| 7. Say it | Answer sentence | `answer` | — |
| 8. Check (new) | Puts the answer back into the story: "60 + 20 = 80, yes, the same" | `write` (check line) | optional "Does it work?" |

On the board, the check line is the last `write`, just before the boxed answer (so the answer stays
last): `"Check: 60 - 20 = 40 = 20 + 20 ✓"`. It is drawn in green, with the tick drawn by hand.

Beat 8 is new. It teaches the habit of checking, and it's free to verify: the check line is just
more arithmetic for `verify_v2`.

## 2. The templates

| # | Template | Picture | Families (`heuristic`) | Status |
|---|---|---|---|---|
| A | **Part–whole** | One bar split into parts; a brace over the whole for the total; "?" on the missing part | part_whole, fraction_of, multi_step (sum/difference) | ✅ |
| B | **Comparison** | 2–3 bars left-aligned, same unit size; a difference brace at the right end | comparison, multiples, how_many_more, ratio | ✅ |
| C | **Before → after** | Two rows ("At first", "In the end"), the same bars; what changed is cut off (dashed, crossed) or added (blue), with an arrow for transfers | before_after, equal_end, constant_total (transfer) | ✅ |
| D | **Same difference (ages)** | "Now" and "In N years": both bars get the **same** added piece, and the difference brace is drawn identically in both rows, to show it never changes | constant_difference | ✅ (it's C with a matching brace) |
| E | **Remainder (zoom)** | One bar split by the first fraction, with the spent part shaded; the remainder is **re-split** for the second fraction, using LCM units so everything shares one unit size | remainder, fraction_of (2-stage) | ✅ with LCM units; 🟡 a "zoom" inset would be clearer |
| F | **Percentage bar** | A bar labelled 100%, with a light 10% tick scale; increase or decrease drawn as an added or cut piece; for a changing base, the new bar is relabelled 100% | percentage, percentage_change (discount, GST, increase) | ✅ units; 🟡 the % scale and relabelling |
| G | **Ratio change (repeated identity)** | Before and after rows; the **unchanged** person is drawn identically in both; units are re-split to a common size (LCM) so the change is a whole number of units | ratio_change | ✅ |
| H | **Units and value table** | A small table: item, how many, value each, total; the bar shows counts as units | units_and_value | 🔴 `table` |
| I | **Two scenarios** | Two rows, "If each gets 5 → short of 24" and "If each gets 2 → short of 6"; the change per person and the change in shortage are lined up | excess_shortage | ✅ as two bars with labels; 🟡 a nicer row style |
| J | **Assume and adjust** | Table: "Assume all 20 are chickens → 40 legs. Actual 56. Each swap +2 legs" | assumption | 🔴 `table` |
| K | **Even it out (average)** | Bars of different heights; the excess is moved across until they level (animated) | average | 🟡 `level` animation (until then, the `working` template) |
| L | **Rate / speed** | A unitary table (1 min → 3 ℓ; 12 min → ?), or a number line with distance segments and times | rate, speed | 🔴 `table`, 🔴 `number_line` |
| M | **Working only** | No picture: the verified working lines | anything else | ✅ |

## 3. How a spec chooses a template

Add `"board": "<A–M name>"` to v2 specs (e.g. `"board": "before_after"`). The planner prompt carries
**one golden example per board**. Most already exist in `specs/examples/v2/`, one per family. The
verifier checks that the board's shape rules hold (`agent/verify_v2.py`, `_check_board`):
- `working` needs the working template; a bar board (A–G) can't use the working template.
- `before_after` needs 2–3 stages, with the same bar names in the first and last stage.
- `constant_difference` needs the same, and in every stage the longer bar **ends with segment(s) worth
  exactly the difference**, the same difference in every row (the "matching brace" of template D).
- `remainder` needs one bar whose `marked` parts follow the question's fractions in order, each a
  fraction of the whole or of what is left (so 2/5 then 1/3 of the rest is 5 units: 2, 1, 2).
- `percentage` needs a bar made only of units in which every % in the question is a whole number of
  units (25% → 4 units, 20% → 5 units; "100 or 10 units" was too strict for the golden examples).
- `ratio_change` needs every quantity drawn in two stages drawn with the same segments.

These rules turn "looks like a teacher's picture" into something code can check. Each error is one
sentence that says what to change, for the planner's retry.

The board is presentation, not maths. So when a draft's **only** problems are its board shape or its
check line, `plan.py` drops the `board` name (or the check line) and re-verifies, instead of throwing
a correct, verified answer away (`_trim_extras`). The answer is still verified and second-solved exactly as before.

## 4. New primitives (in order of value)

1. 🔴 **`table`:** `{"do": "table", "id", "columns": [...], "rows": [[...]]}`. Cells are text or
   numbers; every number obeys the same grounding and traceability as working lines. Drawn
   hand-style (wobbly grid), cell by cell; a `fill_cell` action reveals a cell so a checkpoint can
   ask for it. Unlocks H, J and L.
2. 🔴 **`number_line`:** `{"do": "number_line", "id", "from", "to", "marks": [...], "segments": [...]}`
   for speed and time and for ages. Unlocks L, and gives D a timeline.
3. 🟡 **The percentage scale and relabelling** on a bar (F).
4. 🟡 **A `zoom` inset** for the remainder (E): the remainder part enlarged and re-split beside the bar.
5. 🟡 **`level`:** move the parts above the mean level onto the shorter bars (K).

Each primitive goes in four places, all of them dispatching on `do`: `renderer/render.py`, the live
board in `index.html`, `verify_v2`, and the lesson checkpoints.

## 5. Plan

1. **Today, no new drawing code:**
   - Add `board` to the schema.
   - Write the board-shape rules into `verify_v2`.
   - Restructure `plan_v2.md` around the A–M catalogue, with the teaching script (beats 1–8) and one golden example per board.
   - Add beat 8 (check line) to every golden example.
   - Re-run the public and private evals: must stay 0 wrong, and coverage must not drop.
2. **Next:** `table`, then `number_line`, with golden examples, renderer, board, verifier and tests.
3. **Then:** the 🟡 polish (percent scale, zoom, level), driven by which boards the school data
   uses most (the private eval breakdown by `board`).
4. **QA:** render a still per board for every golden example and review them as a contact sheet.

## 5b. Live lesson: "build it yourself" (`agent/board_task.py`, `app/static/ui/builder.js`)

Before the teacher draws (beats 2–3), the lesson can stop and let the child make that step on a builder that
takes the board's place. Code checks the move against the verified spec, with the same ladder as a number
checkpoint (two hints, then the teacher shows it) and the same score outcomes; then the real step is drawn.

| Task (key) | Templates | Stops before | Expected (from verified data only) |
|---|---|---|---|
| units (`build`) | A (parts of one bar), B, first row of C/G | the first `draw_bar` | segment `expr` unit counts; equivalent ratios accepted (6:2 for 3:1) |
| grow (`grow`) | D | the second stage's `draw_stage` | every bar of the first stage gets the "+ years" piece |
| move (`move`) | C transfer | `move_part` | its `from` / `to` |
| mark (`mark`) | any bar board | `question_mark` | its `bar` (and `segments`) |

A spec that can't give a clean whole-number answer (a plain amount in a bar, a fraction of a unit, more than
12 units, unlabelled parts) gets no board task. Board tasks are offered only when the client says it can draw
them (`"board": true` in `start`/`prepare`), and strong learners (`checkpoints: "key"`) skip them. Spoken moves
("Kavya has four units and Lily has one", "from Ali to Ben", "add years to both") go through a tiny grammar over
the bar labels and number words; anything else gets "Can you show me on the board?".

## 6. Status (Nov 3) and eval numbers

Done:
- **Step 1:** the `board` field (schema, docs, verifier shape rules, §3), `plan_v2.md` rebuilt around
  this catalogue (beats 1–8, a board table, one example per board), and a verified `Check: ... ✓` line
  in every golden example. A draft whose only problem is its board, check line or table keeps its
  verified answer with the extra dropped (`plan._trim_extras`).
- **Step 2:** `table` + `fill_cell` (H, J, L): verifier, renderer, live board, layout, lesson checkpoints
  ("To fill in the Water for 1 min, what is 18 divided by 3?"), tutor numbers. Examples:
  `units_and_value-files-table`, `assumption-legs-table`, `rate-tap-table`.
- **Step 3:** `number_line` + `mark_point` (L, journeys and meetings), the same places. Examples:
  `speed-two-parts-line`, `speed-meeting-line`. (`start`/`end` and `jumps` rather than `from`/`to` and
  `segments`, which the schema already uses for other steps.)
- **Step 4 (part):** template F relabelling: on a `percentage` board each all-unit bar shows its
  percentage at its end ("100%", "120%"). Not done: the 10% tick scale, the remainder zoom inset, `level`.
- Board polish: every "?" in Patrick Hand (Caveat's reads as "2"); long answers wrap onto two lines
  in the video as on the live board.
- Three new answer guards in `plan.verify_v2`, each found as a wrong answer in these evals and each
  flagging only that spec among 1100–1600 past verified specs: per-item comparisons written on totals,
  a count swapped between two items, and "altogether" that leaves out what was added later.

Text evals (`evals/run_evals.py --suite text`, 8 workers; private = school papers):

| Run | Public | Private | Private "wrong" |
|---|---|---|---|
| Round 5 log (before) | 107/107, 0 wrong | 162/180 | 2 disputed (h6-110 key, 5-31 cm/m) |
| Same-day baseline, `3f6bc05` | 107/107, 0 wrong | 164/180 | the same 2 |
| Step 1, `d772ec1` | 107/107, 0 wrong | 164/180 | the 2 + h6-100 (misread; guarded since) |
| Step 3, `b62fd32` | 107/107, 0 wrong | 162/180 | the 2 + h6-100 (another misread; guarded since) |
| After the guards, `f5c103d` | **107/107, 0 wrong** | **162/180** | **only the 2 disputed** |

The other changes between runs are model variance (verification failures on percent arithmetic and
spoken sums, which have nothing to do with boards). In the step-3 public run, 80 of 81 answered v2
specs named a board, 71 had a check line, and 8 drew a table.
