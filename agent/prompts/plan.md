# Plan prompt v1 — ProblemJSON → BarSpec

You plan a Singapore bar model (MOE style) for a P3–P6 word problem.

Return ONLY JSON validating against specs/barspec.schema.json. Shape:

```json
{
  "title": "Bar Model: <Type>",
  "level": "P3-P4",
  "problem_type": "multiples",
  "question": "<original question text>",
  "model": {
    "bars": [{"id": "ben", "label": "Ben", "units": 1}],
    "total": 120,
    "asked": "ali",
    "answer": 90
  },
  "timeline": [
    {"do": "write_question", "say": "<question read aloud>"},
    {"do": "draw_bar", "id": "ben", "say": "Let's draw Ben's bar first. Ben has 1 unit."},
    {"do": "pause", "seconds": 2.0, "say": "Pause and think. How many units altogether?"},
    {"do": "write", "text": "4 units = 120", "say": "4 units are worth 120."},
    {"do": "answer", "text": "Ali has 90 marbles.", "say": "So, Ali has 90 marbles. Well done!"}
  ]
}
```

Rules:
- Bars aligned left, equal units. Draw known/smaller quantity first.
- Use "units" language, not algebra (no x or y) for P3–P5.
- Working one line at a time: "4 units = 120", "1 unit = 120 ÷ 4 = 30".
- Mark the unknown with question_mark on the asked bar.
- Include at least one `pause` step asking the student to think before a reveal.
- Every `say` line: short, kid-friendly, under 20 words, Singapore English.
  The only exception is `write_question`, which reads the whole question aloud.
- Every number written or spoken must come from the question or follow from the model.
- Bars have whole-number units. `model.total` must be a number stated in the question.
- `model.asked` is the bar the final sentence asks about.
- Exactly one `answer` step; its `text` and `say` both state the answer.
- `bracket_total` text is the total (e.g. "120"); `label_unit` text is the value of 1 unit.
- Every `draw_bar`, `divide_units`, `question_mark` and `label_unit` step must have the `id` of its bar (put `label_unit` on a 1-unit bar).
- Timeline actions only: write_question, highlight, draw_bar, divide_units,
  bracket_total, bracket_difference, question_mark, pause, write, label_unit, answer.
- The arithmetic must be exact. 1 unit = (total − all extras) ÷ total units, and
  answer = asked bar units × 1 unit + its extra.
- "Ali has 20 more than Ben": give Ali `"extra": 20` on top of the same units as Ben.
  "Ben has 20 fewer than Ali" is the same model.
- No total, but a difference ("Ali has 3 times as many as Ben. Ali has 40 more."):
  leave out `total` and give `"difference": {"more": "ali", "less": "ben", "value": 40}`;
  then 1 unit = 40 ÷ (3 − 1). Show it with a `bracket_difference` step whose text is "40".
  Use `bracket_difference` only when the model has `difference`; an extra amount is
  already drawn and labelled on its bar.
