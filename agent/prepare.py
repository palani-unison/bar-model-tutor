"""Beat 1 ("Read", docs/board-templates.md §1) before there is a plan: the question, its key facts and an
understanding prompt, while the planner works in the background (app/lesson_ws.py, "prepare").

Everything here is code, not a model: the board shows only the question's own text, and every highlight
is an exact substring of the question (the board's highlight needs that), found by simple regexes.
Nothing is checked or judged, so no maths is invented and no answer can leak.
"""
from __future__ import annotations

import re

from .lesson import _final_question

MAX_FACTS = 4
_NUM = r"\$?\d[\d,]*(?:\.\d+)?"
_OWNER = r"(?:\s+(?:the|her|his|their|its|my|our))?"
# In priority order: an earlier pattern wins a span, and later matches that overlap it are dropped.
_PATTERNS = [
    # "5 times as many paper cranes" / "twice as much money" / "3/5 as many stamps" (up to the "as" that compares)
    re.compile(r"\b(?:\d+(?:\.\d+)?\s+times|twice|thrice|half|\d+/\d+)\s+as\s+(?:many|much|old|long|heavy|tall|big)"
               r"(?:\s+[a-z]+){0,2}?(?=\s+as\b)"),
    re.compile(r"\b(?:\d+(?:\.\d+)?\s+times|twice|thrice)\s+as\s+(?:many|much|old|long|heavy|tall|big)\b"),
    # "$84 more than Lucy" / "36 more English books than Chinese books"
    re.compile(_NUM + r"(?:\s+[A-Za-z]+){0,3}?\s+(?:more|less|fewer)\s+(?:[A-Za-z]+\s+){0,3}?than(?:\s+[A-Z][a-z]+)?"),
    # "1/3 of the remainder", "2/7 of her money", "3/8 of them"
    re.compile(r"\b\d+\s*/\s*\d+(?:\s+of" + _OWNER + r"\s+[a-z]+)?"),
    # "25% of the price", "20 per cent"
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|per\s?cent)(?:\s+of" + _OWNER + r"\s+[a-z]+)?"),
    # a ratio "3 : 5" (or "1:2:3")
    re.compile(r"\b\d+\s*:\s*\d+(?:\s*:\s*\d+)?"),
    # a number with its unit or noun: "4386 commuters", "$18.50 per kg", "3 kg of prawns", "9 years old"
    re.compile(_NUM + r"(?:\s*(?:kg|g|ml|cm|km|m|l|ℓ)\b(?:\s+of\s+[a-z]+)?)?"
               r"(?:\s+(?!(?:of|and|to|in|on|at|for|from|with|by|as|than|more|less|the|a|an|is|are|was|were|"
               r"had|has|have|each|every|then|so|but|or|if|when|after|before|altogether|times|into|onto|last|"
               r"would|will|did|does|can|could|sat|moved|became|joined|saved|got|gave|left)\b)[a-z]+){0,2}"
               r"(?:\s+per\s+[a-z]+|\s+each|\s+old|\s+left)?(?:\s+altogether)?\b"),
    # when things happen
    re.compile(r"\b(?:[Aa]t first|[Ii]n the end|the same (?:number|amount)(?:\s+of\s+[a-z]+)?)\b"),
]


def key_facts(question: str) -> list[str]:
    """The question's key facts, in reading order, as exact substrings: numbers with their nouns, comparisons
    ("times as many", "more than"), fractions, percentages, ratios, "at first" / "in the end", "the same number". At most MAX_FACTS,
    from the sentences before the one that asks (that one is shown as what we must find)."""
    ask = _final_question(question)
    given = question[: question.rfind(ask)] if ask else question
    spans: list[tuple[int, int]] = []
    for pattern in _PATTERNS:
        for m in pattern.finditer(given):
            a, b = m.start(), m.end()
            text = m.group(0).rstrip(" ,.")
            b = a + len(text)
            if not text or not re.search(r"\d|first|end|altogether|same|twice|thrice|half", text):
                continue
            if any(a < y and x < b for x, y in spans):
                continue
            if question.lower().find(text.lower()) != a:
                continue  # the board highlights the first place the text appears: it must be this one
            spans.append((a, b))
    spans.sort()
    return [given[a:b] for a, b in spans][:MAX_FACTS]


def read_steps(question: str) -> list[dict]:
    """The prepare phase's board steps (the same step shapes as a spec's timeline): write the question and
    read it aloud, highlight each key fact, then highlight the sentence that asks."""
    steps = [{"do": "write_question", "say": question}]
    facts = key_facts(question)
    for i, fact in enumerate(facts):
        steps.append({"do": "highlight", "text": fact, "say": (f"What do we know? {fact}." if i == 0 else f"{fact}.")})
    ask = _final_question(question)
    if ask:
        steps.append({"do": "highlight", "text": ask, "say": "And this is what we must find."})
    return steps


# The understanding prompt and the filler while the plan is still coming. Honest: nothing here is checked.
PROMPT = "While I get the bar model ready: in your own words, what does the question want us to find?"
THANKS = "Thank you! Keep that in mind while we draw."
HEARD_LATER = "Good thinking! Hold on to that. I'll come back to it once the bar model is on the board."
NOT_SURE = "That's okay! We'll find out together."


def numbers_line(question: str) -> str:
    """'Let's look at the numbers we know again: 4386 commuters, 5120 commuters.' read from the facts, or ''."""
    facts = [f for f in key_facts(question) if re.search(r"\d", f)]
    return f"Let's look at what we know again: {', '.join(facts)}." if facts else ""


CHECKING = "I'm checking every number on the bar model before I draw it. Nearly ready!"
# Only for very slow plans (P6 on Spark can take over a minute): a thinking nudge, still judging nothing.
PICTURE = ("Here's a tip while we wait: picture the people in the question as bars. "
           "Who do you think will have the longer bar?")
