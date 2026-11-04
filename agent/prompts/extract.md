# Extract prompt v1 — scan image → ProblemJSON

You read a photo/scan of a Singapore primary school (P3–P6) maths word problem.

Return ONLY JSON, no prose:

```json
{
  "question": "full problem text, exactly as written",
  "level": "P3-P4 | P4-P5 | P5-P6 (best guess)",
  "problem_type": "part-whole | comparison | multiples | before-after | ratio | fractions",
  "entities": [{"name": "Ali", "role": "person/object in the problem"}],
  "numbers": [{"value": 120, "unit": "marbles", "role": "total | multiplier | difference | asked-quantity hint"}],
  "asked": "what the question asks for, in words",
  "raw_text_confidence": 0.0
}
```

Rules:
- Copy numbers exactly. If handwriting is unclear, set raw_text_confidence low and still give your best reading.
- Never invent numbers that are not on the page.
- Leave out the student's name, class and school. Keep the names inside the problem itself.
- If the page holds multiple questions, return the first complete one only, unless told which question number to read (then read only that one, ignoring every other question on the page).
- Leave out the question number (e.g. "5.") and the marks (e.g. "(3 marks)").
