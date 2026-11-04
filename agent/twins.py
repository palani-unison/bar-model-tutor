"""'Try one like this': a twin of a verified v2 spec, with the same structure and new numbers.

A twin is made by code, never invented by a model:

  1. Find the question's *amounts*: the numbers in the relations that are values ("$60 left",
     "24 more beads", "41 years old"), not structure ("2/5 of it", "3 times as many", "25%").
     A number is structure when it multiplies or divides a quantity, or is part of a fraction.
  2. Pick new "nice" values for the amounts (same step: whole tens stay whole tens, $19.50 stays
     in 50 cents), seeded, so the same seed always gives the same twin.
  3. Re-solve the new relations by code and keep only values where every quantity, unit and
     segment is still a positive whole number (or has no more decimals than the original).
  4. Map every old value (amounts, solved quantities, units, segments, unit multiples) to its new
     value and substitute it in the question, the quotes, the board labels and the narration.
  5. The gate, all of it, or there is no twin:
       - `verify_v2` (the same gate as the planner's: schema, grounding, solve, working, narration),
       - the whiteboard can lay it out,
       - the answer equals the code-computed answer from the substituted relations,
       - no old amount survives in the question,
       - the independent second solver, which reads only the new question text, agrees.
     When the substituted narration does not verify (a working number the map can't place), the
     planner is asked to plan the new question from scratch, and the same gate applies, plus "the
     planner's answer equals the code's answer".

On any failure: no twin ("not available"), never an unchecked one. Model calls: only the second
solver (and the planner fallback), both mockable.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from fractions import Fraction

import sympy as sp

from . import check as _check
from .spec_v2 import SpecError, _clean, bars_of, parse_expr, solve_spec

logger = logging.getLogger("bar_model_tutor.twins")

MAX_CANDIDATES = 600      # random value sets tried (cheap: the relations are solved once, symbolically)
MAX_FULL_CHECKS = 25      # candidates that reach the full verify_v2 gate
COUNT_MAX = 12            # in "6*14", the small factor is a count (kept) and the big one an amount
UNIT_MULTIPLES = 60       # "5 units = 150": unit multiples up to this are mapped too
SKIP_KEYS = {"eq", "expr", "id", "bar", "quantity", "do", "heuristic", "template", "level", "version",
             "name", "style", "side", "title"}

# A number in prose: "1,250", "19.50", "60"; never part of a fraction ("2/5") or a word.
_TOKEN = re.compile(r"(?<![\w.,/])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d/]|\.\d|,\d{3})")


class TwinUnavailable(Exception):
    """No verified twin could be made for this spec. The message says why (for logs and evals)."""


@dataclass
class Twin:
    spec: dict
    solved: dict
    how: str                       # "substitution" | "planner"
    seconds: float
    values: dict = field(default_factory=dict)  # old amount -> new amount
    check: str = "agreed"

    def as_json(self) -> dict:
        return {"available": True, "spec": self.spec, "solved": self.solved, "how": self.how,
                "seconds": round(self.seconds, 2), "check": self.check,
                "values": {_fmt_plain(k): _fmt_plain(v) for k, v in self.values.items()}}


# ---------------------------------------------------------------- numbers
def _key(x) -> Fraction:
    return Fraction(str(round(float(x), 6)))


def _fmt_plain(x) -> str:
    x = float(x)
    return str(int(round(x))) if abs(x - round(x)) < 1e-9 else f"{x:.6f}".rstrip("0").rstrip(".")


def _decimals(literal: str) -> int:
    return len(literal.split(".")[1]) if "." in literal else 0


def _fmt_like(value: Fraction, literal: str) -> str:
    """The new value written the way the old one was: '19.50' -> '24.50', '1,250' -> '1,500'."""
    d = _decimals(literal)
    v = float(value)
    if d == 0 and value.denominator != 1:
        text = _fmt_plain(v)
    else:
        text = f"{v:.{d}f}"
    if "," in literal or (len(literal.replace(",", "").split(".")[0]) >= 4 and "," in literal):
        whole, _, frac = text.partition(".")
        text = f"{int(whole):,}" + (f".{frac}" if frac else "")
    return text


def _tokens(text: str) -> list[tuple[int, int, str, Fraction]]:
    return [(m.start(1), m.end(1), m.group(1), _key(m.group(1).replace(",", ""))) for m in _TOKEN.finditer(text or "")]


def _substitute(text: str, mapping: dict[Fraction, Fraction]) -> str:
    out, at = [], 0
    for start, end, literal, value in _tokens(text):
        if value in mapping and mapping[value] != value:
            out += [text[at:start], _fmt_like(mapping[value], literal)]
            at = end
    return "".join(out) + text[at:]


# ---------------------------------------------------------------- amounts vs structure (relations)
def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _has_name(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) for n in ast.walk(node))


def _is_amount(node: ast.Constant, parents: dict) -> bool:
    """A value the story gives ("60 left"), not a multiplier or a fraction ("3 times", "2/5 of")."""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Mult, ast.Div)):
            other = parent.right if child is parent.left else parent.left
            if child is node:  # the number itself multiplies or divides
                if (isinstance(parent.op, ast.Mult) and isinstance(other, ast.Constant)
                        and float(node.value) > COUNT_MAX >= float(other.value)):
                    pass  # "4 trays of 24": 24 is the amount, 4 the count
                else:
                    return False
            elif _has_name(other):  # (100 - 20) * price: a percentage, structure
                return False
        child, parent = parent, parents.get(parent)
    return True


def _side_constants(side: str) -> tuple[str, list[tuple[int, int, float, bool]]] | None:
    """(text, [(start, end, value, is_amount)]) for one side of a relation or one expression."""
    text = side if not re.search(r"[×÷−–%]", side) else _clean(side)
    pad = len(text) - len(text.lstrip())
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None
    parents = _parents(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            found.append((pad + node.col_offset, pad + node.end_col_offset, float(node.value), _is_amount(node, parents)))
    return text, found


def _eq_sides(eq: str) -> list[str]:
    parts = str(eq).replace("==", "=").split("=")
    return parts if len(parts) == 2 else []


def _rewrite(text: str, constants, value_for) -> str:
    out, at = [], 0
    for start, end, value, amount in sorted(constants):
        new = value_for(value, amount)
        if new is not None and _key(new) != _key(value):
            out += [text[at:start], _fmt_plain(new)]
            at = end
    return "".join(out) + text[at:]


def analyse(spec: dict) -> dict:
    """Which numbers are amounts (vary) and which are structure (keep). Pure code, no model."""
    amounts: set[Fraction] = set()
    structure: set[Fraction] = set()
    for rel in spec.get("relations") or []:
        for side in _eq_sides(rel.get("eq", "")):
            parsed = _side_constants(side)
            if parsed is None:
                raise TwinUnavailable(f"can't read relation {rel.get('eq')!r}")
            for _s, _e, value, amount in parsed[1]:
                (amounts if amount else structure).add(_key(value))
    for bar in bars_of(spec):
        for seg in bar.get("segments") or []:
            parsed = _side_constants(str(seg.get("expr", "")))
            for _s, _e, value, amount in (parsed[1] if parsed else []):
                if not amount:
                    structure.add(_key(value))
    question = spec.get("question", "")
    literals: dict[Fraction, list[str]] = {}
    for _s, _e, literal, value in _tokens(question):
        literals.setdefault(value, []).append(literal)
    varied = sorted(a for a in amounts if a in literals and a not in structure and a > 0)
    kept_question = {v for v in literals if v not in varied}
    return {"amounts": amounts, "varied": varied, "structure": structure | kept_question,
            "literals": literals}


# ---------------------------------------------------------------- choosing new values
def _step(value: Fraction, literal: str) -> Fraction:
    d = _decimals(literal)
    if d:
        if (value * 2).denominator == 1:
            return Fraction(1, 2) if value >= 5 else Fraction(1, 10 ** d)
        return Fraction(1, 10 ** d)
    v = int(value)
    if v >= 100 and v % 50 == 0:
        return Fraction(10)
    if v >= 20 and v % 10 == 0:
        return Fraction(10)
    if v >= 10 and v % 5 == 0:
        return Fraction(5)
    return Fraction(1)


def _choices(value: Fraction, literal: str) -> list[Fraction]:
    step = _step(value, literal)
    lo, hi = max(step, value / 2), max(value * 2, value + 6 * step)
    if _decimals(literal) and value.denominator != 1:  # $19.50 -> $24.50: keep the cents, change the dollars
        cents = value - int(value)
        return [n + cents for n in range(max(1, int(lo)), int(hi) + 1) if n + cents != value]
    k0, k1 = int(-(-lo // step)), int(hi // step)
    return [k * step for k in range(k0, k1 + 1) if k * step != value]


def _nice(values, decimals: int) -> bool:
    for v in values:
        f = _key(v)
        if f <= 0:
            return False
        if (f * 10 ** decimals).denominator != 1:
            return False
    return True


def _original_decimals(solution) -> int:
    vals = list(solution.quantities.values()) + list(solution.units.values()) + [solution.asked]
    vals += [v for seg in solution.segments.values() for v in seg]
    worst = 0
    for v in vals:
        f = _key(v)
        for d in range(0, 4):
            if (f * 10 ** d).denominator == 1:
                worst = max(worst, d)
                break
        else:
            return 99
    return worst


class _Fast:
    """The relations solved once with the amounts as symbols: a candidate is then a quick evaluation."""

    def __init__(self, spec: dict, varied: list[Fraction]):
        self.ok = False
        quantities = [q["id"] for q in spec.get("quantities") or []]
        syms = {q: sp.Symbol(q, real=True) for q in quantities}
        self.params = [sp.Symbol(f"amount_{i}", real=True) for i in range(len(varied))]
        index = {v: i for i, v in enumerate(varied)}
        eqs = []
        try:
            for rel in spec.get("relations") or []:
                sides = []
                for side in _eq_sides(rel["eq"]):
                    text, consts = _side_constants(side)
                    out, at = [], 0
                    for start, end, value, amount in sorted(consts):
                        if amount and _key(value) in index:
                            out += [text[at:start], f"amount_{index[_key(value)]}"]
                            at = end
                    sides.append(parse_expr("".join(out) + text[at:], {**syms, **{str(p): p for p in self.params}}))
                eqs.append(sides[0] - sides[1])
            sol = sp.solve(eqs, list(syms.values()), dict=True)
            if len(sol) != 1:
                return
            self.exprs = {q: sp.simplify(syms[q].subs(sol[0])) for q in quantities}
            if any(e.free_symbols - set(self.params) for e in self.exprs.values()):
                return
            self.funcs = {q: sp.lambdify(self.params, e, "math") for q, e in self.exprs.items()}
            self.ok = True
        except Exception:  # noqa: BLE001 — not linear / not solvable symbolically: fall back to solve_spec
            self.ok = False

    def plausible(self, values: list[Fraction], decimals: int) -> bool:
        try:
            out = [f(*[float(v) for v in values]) for f in self.funcs.values()]
        except (ZeroDivisionError, ValueError, OverflowError):
            return False
        for v in out:
            if v < -1e-9:
                return False
            if abs(v * 10 ** decimals - round(v * 10 ** decimals)) > 1e-6:
                return False
        return True


# ---------------------------------------------------------------- building the twin
def _map_values(old, new, varied_map: dict[Fraction, Fraction], spec: dict) -> dict[Fraction, Fraction] | None:
    """Every old value -> its new value. None when one old value would need two new ones."""
    mapping: dict[Fraction, Fraction] = {}

    def put(a, b, strict=True) -> bool:
        ka, kb = _key(a), _key(b)
        if ka in mapping and mapping[ka] != kb:
            return not strict
        mapping[ka] = kb
        return True

    pairs = list(varied_map.items())
    pairs += [(old.asked, new.asked)]
    pairs += [(old.quantities[q], new.quantities[q]) for q in old.quantities if q in new.quantities]
    pairs += [(old.units[u], new.units[u]) for u in old.units if u in new.units]
    for bid, segs in old.segments.items():
        pairs += list(zip(segs, new.segments.get(bid, [])))
    for a, b in pairs:
        if not put(a, b):
            return None
    # Working lines, in order: "Wheels if all were motorcycles = 32 × 2 = 64" makes 64 -> (new 32) × 2.
    from .verify import _eval_expr

    for step in spec.get("timeline") or []:
        if step.get("do") not in ("write", "fill_cell", "mark_point"):  # a table cell or number line value can be a sum too
            continue
        parts = str(step.get("text", "")).split("=")
        for left, right in zip(parts, parts[1:]):
            toks = _tokens(right)
            if len(toks) != 1 or right.replace(toks[0][2], "").strip(" $") not in ("",) and not re.fullmatch(
                    r"\s*\$?\s*[\d.,]+\s*[a-zA-Z ]*", right):
                continue
            try:
                was = _eval_expr(left)
                now = _eval_expr(_substitute(left, mapping))
            except (ZeroDivisionError, ValueError, SyntaxError, TypeError):
                continue
            if was is None or now is None or _key(was) != toks[0][3] or len(_tokens(left)) < 2:
                continue
            if not put(toks[0][3], now):
                return None
    # Unit multiples ("3 units = 90") and bracketed runs of segments: only where unambiguous.
    extra: dict[Fraction, set[Fraction]] = {}
    for u, value in old.units.items():
        if u in new.units:
            for k in range(1, UNIT_MULTIPLES + 1):
                extra.setdefault(_key(k * value), set()).add(_key(k * new.units[u]))
    for bid, segs in old.segments.items():
        nsegs = new.segments.get(bid, [])
        for i in range(len(segs)):
            for j in range(i + 1, len(segs) + 1):
                extra.setdefault(_key(sum(segs[i:j])), set()).add(_key(sum(nsegs[i:j])))
    for a, bs in extra.items():
        if a not in mapping and len(bs) == 1:
            mapping[a] = next(iter(bs))
    return mapping


def _apply(spec: dict, varied_map: dict[Fraction, Fraction], mapping: dict[Fraction, Fraction]) -> dict:
    twin = copy.deepcopy(spec)

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k == "eq" and isinstance(v, str):
                    sides = []
                    for side in _eq_sides(v):
                        text, consts = _side_constants(side)
                        sides.append(_rewrite(text, consts,
                                              lambda value, amount: varied_map.get(_key(value)) if amount else None))
                    node[k] = "=".join(sides) if len(sides) == 2 else v
                elif k == "expr" and isinstance(v, str):
                    parsed = _side_constants(v)
                    if parsed:
                        text, consts = parsed
                        node[k] = _rewrite(text, consts, lambda value, amount: mapping.get(_key(value)) if amount else None)
                elif k in SKIP_KEYS:
                    continue
                elif isinstance(v, str):
                    node[k] = _substitute(v, mapping)
                else:
                    walk(v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    node[i] = _substitute(v, mapping)
                else:
                    walk(v)

    walk(twin)
    return twin


def _gate(twin: dict, code_answer: float) -> str | None:
    """Code checks, in order. None = passed; otherwise why not."""
    from .plan import _layout_error, verify_v2

    try:
        result = verify_v2(twin)
    except Exception as exc:  # noqa: BLE001
        return f"verifier error: {exc}"
    if not result.ok:
        return "verify: " + "; ".join(result.errors[:3])
    try:
        if abs(solve_spec(twin).asked - code_answer) > 1e-6 or abs(float(twin["answer"]) - code_answer) > 1e-6:
            return "answer differs from the code-computed answer"
    except SpecError as exc:
        return f"solve: {exc}"
    drawn = _layout_error(twin)
    if drawn:
        return f"layout: {drawn}"
    return None


def _second_solver(question: str, code_answer: float) -> str | None:
    answer = _check.independent_answer(question)
    if answer is None:
        return "independent check unavailable"
    if not _check.agrees(code_answer, answer):
        return f"independent check disagreed ({answer:g} vs {code_answer:g})"
    return None


def _planner_twin(spec: dict, question: str, code_answer: float) -> tuple[dict | None, str]:
    """Fallback: plan the new question from scratch (the planner verifies and cross-checks it itself)."""
    from .plan import plan_problem

    try:
        planned, result, _log = plan_problem({"question": question, "level": spec.get("level", "")})
    except Exception as exc:  # noqa: BLE001
        return None, f"planner: {exc}"
    if planned.get("version") != 2 or not result.ok or getattr(result, "check", None) != "agreed":
        return None, "planner gave no verified, cross-checked v2 spec"
    if abs(float(planned["answer"]) - code_answer) > 1e-6:
        return None, f"planner answer {planned['answer']} != code answer {code_answer:g}"
    if planned.get("heuristic") != spec.get("heuristic"):
        relabelled = dict(planned, heuristic=spec.get("heuristic"))
        if _gate(relabelled, code_answer) is None:
            planned = relabelled
    return planned, ""


def make_twin(spec: dict, seed: int | None = None, use_planner: bool = True) -> Twin:
    """A verified twin of a verified v2 spec, or TwinUnavailable. Deterministic for a given seed."""
    started = time.perf_counter()
    if spec.get("version") != 2:
        raise TwinUnavailable("twins are made from v2 specs only")
    from .plan import verify_v2

    if not verify_v2(spec).ok:
        raise TwinUnavailable("the original spec does not verify")
    old = solve_spec(spec)
    info = analyse(spec)
    varied = info["varied"]
    if not varied:
        raise TwinUnavailable("no amount in the question can change without changing the structure")
    decimals = _original_decimals(old)
    if decimals > 3:
        raise TwinUnavailable("the original values are not exact")
    literal = {v: info["literals"][v][0] for v in varied}
    choices = {v: _choices(v, literal[v]) for v in varied}
    if any(not c for c in choices.values()):
        raise TwinUnavailable("no nice values to choose from")
    if seed is None:
        seed = int(hashlib.sha256(spec.get("question", "").encode()).hexdigest()[:8], 16)
    rng = random.Random(seed)
    fast = _Fast(spec, varied)
    structure = info["structure"]
    tried: set[tuple] = set()
    reasons: list[str] = []
    full_checks = 0
    fallback: tuple[str, float] | None = None

    for _ in range(MAX_CANDIDATES):
        values = tuple(rng.choice(choices[v]) for v in varied)
        if values in tried:
            continue
        tried.add(values)
        if len(set(values)) != len(values) or any(n in structure for n in values):
            continue  # two amounts the same, or an amount equal to a structural number: ambiguous story
        if fast.ok and not fast.plausible(list(values), decimals):
            continue
        varied_map = dict(zip(varied, values))
        new = _solve_candidate(spec, old, varied_map)
        if new is None:
            reasons.append("bars or relations do not solve")
            continue
        all_values = list(new.quantities.values()) + list(new.units.values()) + [new.asked]
        all_values += [v for segs in new.segments.values() for v in segs]
        if not _nice(all_values, decimals) or _key(new.asked) == _key(old.asked):
            continue
        if max(all_values) > 20 * max(list(old.quantities.values()) + [old.asked, 1]):
            continue
        if fallback is None:  # the planner can still plan this question if the narration can't be mapped
            fallback = (_substitute(spec["question"], varied_map), new.asked)
        mapping = _map_values(old, new, varied_map, spec)
        if mapping is None:
            reasons.append("an old number would need two new values")
            continue
        changed = {a for a, b in mapping.items() if a != b}
        if changed & structure:
            reasons.append("a changed number is also a structural number")
            continue
        twin = _apply(spec, varied_map, mapping)
        twin["answer"] = int(round(new.asked)) if _key(new.asked).denominator == 1 else float(new.asked)
        if any(v in {t[3] for t in _tokens(twin["question"])} for v in varied):
            reasons.append("an old amount survived in the question")
            continue
        full_checks += 1
        why = _gate(twin, new.asked)
        if why is None:
            why = _second_solver(twin["question"], new.asked)
            if why is None:
                return Twin(twin, solve_spec(twin).as_json(), "substitution", time.perf_counter() - started,
                            {k: v for k, v in varied_map.items()})
            reasons.append(why)
            if why.startswith("independent check"):
                break  # the checker reads the question: a planner retry would not change its view
        else:
            reasons.append(why)
        if full_checks >= MAX_FULL_CHECKS:
            break

    if use_planner and fallback is not None and not any(r.startswith("independent check") for r in reasons):
        twin_q, code_answer = fallback
        planned, why = _planner_twin(spec, twin_q, code_answer)
        if planned is not None:
            return Twin(planned, solve_spec(planned).as_json(), "planner", time.perf_counter() - started)
        raise TwinUnavailable(f"{why} (after: {max(set(reasons), key=reasons.count) if reasons else 'no candidates'})")
    common = max(set(reasons), key=reasons.count) if reasons else "no candidate values worked"
    raise TwinUnavailable(common)


def _solve_candidate(spec: dict, old, varied_map: dict[Fraction, Fraction]):
    """Solve the relations with the new amounts, then the bars (whose plain numbers follow the quantities)."""
    try:
        relations = _apply({"relations": spec["relations"]}, varied_map, {})["relations"]
        probe = dict(copy.deepcopy(spec), relations=relations)
        rel_only = solve_spec(dict(probe, stages=[], units=[]))
        pre = dict(varied_map)
        for q, value in old.quantities.items():
            if q in rel_only.quantities:
                pre.setdefault(_key(value), _key(rel_only.quantities[q]))
        pre.setdefault(_key(old.asked), _key(rel_only.asked))
        return solve_spec(_apply(probe, varied_map, pre))
    except Exception:  # noqa: BLE001 — SpecError, or SymPy failing on odd values: try other values
        return None


# ---------------------------------------------------------------- the per-lesson cache, filled in the background
def spec_key(spec: dict, n: int = 0) -> str:
    core = {"q": spec.get("question"), "r": [r.get("eq") for r in spec.get("relations") or []], "n": n}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:24]


class TwinCache:
    """Twins per lesson spec. `prefetch` starts one in the background while the lesson plays."""

    def __init__(self, max_items: int = 128, workers: int = 2):
        self._futures: OrderedDict[str, Future] = OrderedDict()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="twin")
        self.max_items = max_items

    def _run(self, spec: dict, n: int) -> dict:
        try:
            seed = int(spec_key(spec, n)[:8], 16)
            return make_twin(spec, seed=seed).as_json()
        except TwinUnavailable as exc:
            logger.info("twin not available: %s", exc)
            return {"available": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 — never show a half-made twin
            logger.warning("twin failed: %s", exc)
            return {"available": False, "reason": f"error: {type(exc).__name__}"}

    def prefetch(self, spec: dict, n: int = 0) -> Future | None:
        if not isinstance(spec, dict) or spec.get("version") != 2:
            return None
        key = spec_key(spec, n)
        with self._lock:
            fut = self._futures.get(key)
            if fut is None:
                fut = self._pool.submit(self._run, copy.deepcopy(spec), n)
                self._futures[key] = fut
                while len(self._futures) > self.max_items:
                    self._futures.popitem(last=False)
            else:
                self._futures.move_to_end(key)
        return fut

    def get(self, spec: dict, n: int = 0, timeout: float = 90.0) -> dict:
        fut = self.prefetch(spec, n)
        if fut is None:
            return {"available": False, "reason": "twins are made from v2 specs only"}
        try:
            return fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001 — timeout
            return {"available": False, "reason": "still working on it"}


cache = TwinCache()
