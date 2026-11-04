# BarSpec v2: the contract

v2 lets one verifier, one renderer and one live board handle every bar-model word-problem family:
before–after, equal-end, remainder, percentage, ratio change, "how many more", multi-step, plus
non-bar families (excess–shortage, assumption, average, rate) drawn as verified working only.

- Schema: `specs/barspec.v2.schema.json`.
- Golden examples: `specs/examples/v2/*.json`. Each solves to its hand-checked answer.
- Solver: `agent/spec_v2.py` (`solve_spec`).
- Verifier: `agent/verify_v2.py`, which `verify_spec_v2(spec)` returns a `VerifyResult` from.
- v1 specs (no `version`, with a `model` field) keep working through `agent/verify.py`. Use
  `spec.get("version") == 2` to dispatch.

## 1. The idea in one paragraph

The spec states the question's facts as **equations over named quantities** (`relations`). Each
relation quotes the phrase it came from. Code solves the relations exactly (SymPy, rational
arithmetic). They must fix the asked value **without the bars**. Separately, the **bars** show the
same quantities as **segments** in units (`3*u`) and plain amounts (`20`), across one or more
**stages** (before / after). Each bar's counted segments must add up to its quantity's solved
value, which fixes the unit values. So the picture can't disagree with the maths, and the maths
can't drift from the question.

## 2. Fields

| Field | Meaning |
|---|---|
| `version` | Always `2`. |
| `heuristic` | The family (enum in the schema). Informational; used for routing and evals. |
| `template` | `bar` (one stage), `bar-stages` (2–3 stages: before/after...), `working` (no bars, so `stages` is `[]`). |
| `board` | Optional. The teacher's picture for the family (docs/board-templates.md): `part_whole`, `comparison`, `before_after`, `constant_difference`, `remainder`, `percentage`, `ratio_change`, `units_value_table`, `two_scenarios`, `assume_adjust`, `average_level`, `rate_speed`, `working`. Its shape is checked (§4.5b). |
| `units` | Unit symbols the segments use: `[{"name": "u", "meaning": "1 unit"}]`. A second one (`p`) is allowed for ratio problems. Max 3. |
| `quantities` | Named amounts: `{"id": "ali_before", "label": "Ali's stamps at first"}`. ids are `lower_snake_case`. |
| `relations` | `{"eq": "ali_after = ali_before - 20", "from": "Ali gave 20 stamps to Ben"}`. `from` is copied from the question. |
| `asked` | `{"expr": "ben_before", "label": "..."}`. Any expression over quantities, e.g. `ali_after - ben_after` for "how many more". |
| `answer` | The number the question asks for. Must equal the solved `asked`. |
| `stages[]` | `{"id", "label": "At first", "bars": [...]}`. Max 3 stages, max 3 bars each. |
| `bar` | `{"id", "quantity": "<quantity id>", "label": "Ali", "segments": [...]}`. Bar ids are unique across all stages. Two bars in different stages may draw the same quantity (e.g. Beth unchanged). |
| `segment` | `{"expr": "3*u", "style": "normal", "label": "optional"}`. `expr` uses **only numbers and unit names**. |

### Expressions

Numbers (integer or decimal; no thousands commas), names, `+ - * /`, brackets. `×`, `÷`, `−` and
`%` (meaning `/100`) are accepted. Nothing else: no `**`, no functions. They are parsed by walking
the AST, never with `eval`.

### Segment styles

| Style | Counted in the bar's value? | Drawn as |
|---|---|---|
| `normal` (default) | yes | plain part of the bar (`k*u` is drawn as k equal unit boxes after `divide_units`) |
| `marked` | yes | hatched or tinted part with its `label` ("spent on bag"); used for remainder and percentage |
| `added` | yes | blue part appended to the bar, with its label ("+20") |
| `removed` | **no** | dashed, crossed-out part at the end of the bar ("gave 20"): what left this bar |

Drawn length is **true to scale across all stages**: every segment's solved value × one shared
scale. A bar that lost 1u in "after" is drawn as 2u plus a dashed 1u, so it lines up with its 3u
"before" bar.

## 3. Solving (agent/spec_v2.py)

1. Parse every relation and `asked`. Unknown names, bad syntax and banned operators raise `SpecError`.
2. Solve the **relations alone**. `asked` and every quantity a bar draws must have exactly one
   non-negative value, otherwise "the relations do not fix X" or "allow more than one answer".
3. For each bar: Σ(counted segment exprs) = the quantity's value. Solve for the unit symbols,
   which must be positive, unique and consistent across bars.
4. Every segment must have a positive value.

`solve_spec(spec) -> Solution(quantities, units, segments{bar_id: [values]}, asked)`. The server
sends `solution.as_json()` to the browser as `solved` alongside the spec, so the board can draw
true to scale without its own solver.

## 4. Verification (agent/verify_v2.py builds these on top of solve_spec)

1. JSON schema (`specs/barspec.v2.schema.json`).
2. `solve_spec` succeeds, and `answer` == solved `asked`.
3. **Grounding**: every `from` is a phrase in the question (case- and space-insensitive). Every
   number literal in `relations` is in the question, or is structural: 0, 1, 2, 10, 100, or a
   word number ("twice", "half", "three") in the question. Fraction and ratio digits count as in
   the question ("2/5" gives 2 and 5; "3 : 5" gives 3 and 5).
4. **Bars**: every `quantity` exists; template `bar` has 1 stage, `bar-stages` has 2–3, and
   `working` has none.
5. **Timeline**:
   - Every `bar`/`stage`/`from`/`to` id exists; segment indexes are in range.
   - `move_part` goes from a `removed` segment to an `added` one.
   - At least one `pause`; exactly one `answer`, whose text and say state the answer.
   - Every `say` is ≤ 20 words, except `write_question`.
5b. **Board shape** (when `board` is given; docs/board-templates.md §3): `working` needs the working
   template and a bar board can't use it; `before_after`, `constant_difference` and `ratio_change`
   need 2–3 stages (`before_after` and `constant_difference` with the same bar labels in the first
   and last stage); `constant_difference` ends the longer bar of every stage with segment(s) worth the difference; `ratio_change` draws a
   quantity that appears in two stages with the same segments; `remainder` has a bar whose `marked`
   parts are the question's fractions in order (of the whole or of what is left); `percentage` has
   a bar made only of units in which every % of the question is whole units.
6. **Working lines** (`write`): each `=` chain is consistent arithmetic. "N units" means N × u, and
   "N parts" means N × p. Lines are checked **in order**, and every operand must already be known:
   a question number, a solved value (quantities, units, segments), a unit count on the board, or
   the result of an earlier checked line.
7. **Narration numbers**: every number in any `say`/`text` is known in the same sense (structural
   small integers 1–12 are allowed for counting units and stages).
8. **Drawable**: the renderer's `layout_error(spec)` is None.
9. (In `plan.py`, as now) the independent second solver agrees with `answer`.

## 5. Timeline actions

| `do` | Fields | Draws |
|---|---|---|
| `write_question` | | the question at the top (say = the question, any length) |
| `highlight` | `text` (exact substring of the question) | yellow highlighter on that phrase |
| `draw_stage` | `stage` | the stage label ("At first") at the left of its row |
| `draw_bar` | `bar` | the bar outline, its name, segment boundaries, styles and segment labels |
| `divide_units` | `bar` | unit dividers and a small "u" inside each `k*u` segment of that bar |
| `move_part` | `from`, `from_segment`, `to`, `to_segment` | a curved arrow from a removed segment to an added one |
| `bracket` | `bar` + `segments: [i, j]` + `side: top/bottom`, **or** `bars: [ids]` + `side: right` | a brace over segments i..j of one bar, or a vertical brace over several bars (a total), labelled with `text` |
| `question_mark` | `bar` (+ optional `segments: [i, j]`) | a red brace and "?" under the bar or those segments |
| `label_unit` | `bar`, `text` | the value of 1 unit above the first unit box of that bar |
| `pause` | `seconds` | nothing (think time; in a live lesson it becomes a question to the child) |
| `write` | `text` | the next line of working in the right-hand column; a `Check: ... ✓` line (beat 8) is green with a drawn tick |
| `table` | `id`, `columns` (2–4), `rows` (1–6, one cell per column) | a hand-drawn table where the bars go (working template only); a `"?"` cell waits for `fill_cell` |
| `fill_cell` | `table`, `row` (0 = first row under the headers), `col`, `text` | rubs out that `"?"` and writes the value (or its short sum, `"18 ÷ 3 = 6"`) in blue |
| `number_line` | `id`, `start` < `end`, `marks` (2–8: `{"at", "label", "value"}`), `jumps` (0–6: `{"from", "to", "label"}`) | an axis where the bars go (working template only): a tick and label under each mark, its value above (a `"?"` waits for `mark_point`), a curved green arrow per jump |
| `mark_point` | `line`, `at`, `text` | rubs out the `"?"` above that mark and writes its value (or short sum) in blue |
| `answer` | `text` | the boxed answer statement |

`working` template: only `write_question`, `highlight`, `pause`, `write`, `table`, `fill_cell`,
`number_line`, `mark_point` and `answer` (one table or number line per board). Table cells (≤ 18 characters) obey check 6 like working lines, in timeline order: a given
cell's numbers must already be known, and a filled cell's sum must be true.

## 6. Live lesson checkpoints (agent/lesson.py)

Key values are computed from the solution:
- the value of 1 unit (`u`), when units exist;
- the result of each `write`, `fill_cell` or `mark_point` with an operator (e.g. "18 ÷ 3 = 6"), ask "What is 18 ÷ 3?"
  ("To fill in the Water for 1 min, what is 18 divided by 3?" for a cell);
- the answer.

A checkpoint stops the board just before the first step that would show its value (the same
`shows()` rule as v1). There are at most 4 per lesson. Code compares the child's number, so a
model never judges it.

## 7. Limits (keep the board readable)

At most 3 stages, 3 bars per stage, 6 segments per bar and 20 unit boxes per bar. Labels are short
(≤ 14 characters). Expressions are ≤ 200 characters, relations ≤ 24.
