"""BarSpec v2: parse and solve a spec's quantities, relations and bars (see docs/spec-v2.md).

A v2 spec states the question's facts as equations ("relations") over named quantities,
and draws bars made of segments ("3*u", "20", "u" added, "u" removed) across one or more
stages (before / after). `solve_spec` turns that into exact numbers:

  1. The relations alone must fix the asked value and every quantity a bar draws,
     with exactly one non-negative solution (exact rational arithmetic, SymPy).
  2. Each bar's counted segments must add up to its quantity; that fixes the unit
     values (u, p, ...), which must be positive and the same for every bar.

Expressions are parsed from a tiny grammar (numbers, names, + - * / and brackets)
by walking Python's AST, never with eval: specs come from the model and from browsers.
verify_v2.py builds every other check on top of this module.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

import sympy as sp

ID = re.compile(r"^[a-z][a-z0-9_]*$")
UNIT_NAME = re.compile(r"^[a-z]$")
COUNTED_STYLES = {"normal", "marked", "added"}  # "removed" parts are drawn but not counted
STYLES = COUNTED_STYLES | {"removed"}
MAX_EXPR_CHARS = 200
MAX_RELATIONS = 24
MAX_QUANTITIES = 24
MAX_NUMBER = 10**9


class SpecError(ValueError):
    """The spec can't be solved as written. The message says why, for the planner's retry."""


def _clean(text: str) -> str:
    return (str(text).replace("×", "*").replace("÷", "/").replace("−", "-").replace("–", "-")
            .replace("%", "/100").strip())


def parse_expr(text: str, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    """'3*u - 20' -> SymPy expression. Only numbers, known names, + - * / and brackets."""
    text = _clean(text)
    if not text or len(text) > MAX_EXPR_CHARS:
        raise SpecError(f"expression {text[:40]!r} is empty or too long")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise SpecError(f"can't read expression {text!r}") from exc

    def walk(node: ast.AST) -> sp.Expr:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            if abs(node.value) > MAX_NUMBER:
                raise SpecError(f"number {node.value} is too large")
            return sp.Rational(str(node.value))
        if isinstance(node, ast.Name):
            if node.id not in symbols:
                raise SpecError(f"unknown name {node.id!r} in {text!r}")
            return symbols[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise SpecError(f"division by zero in {text!r}")
            return left / right
        raise SpecError(f"only numbers, names, + - * / and brackets are allowed: {text!r}")

    return walk(tree)


def expr_numbers(text: str) -> list[float]:
    """Number literals written in an expression or relation ('2/5*money' -> [2, 5])."""
    try:
        tree = ast.parse(_clean(text).replace("==", "=").replace("=", "+", 1), mode="eval")
    except SyntaxError:
        return []
    return [float(n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)]


def parse_relation(eq: str, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    """'ali_after = ben_after' -> ali_after - ben_after (an expression that must equal 0)."""
    parts = _clean(eq).replace("==", "=").split("=")
    if len(parts) != 2:
        raise SpecError(f"a relation needs exactly one '=': {eq!r}")
    return parse_expr(parts[0], symbols) - parse_expr(parts[1], symbols)


@dataclass
class Solution:
    """Exact solved values (floats for the JSON side)."""

    quantities: dict[str, float] = field(default_factory=dict)
    units: dict[str, float] = field(default_factory=dict)
    segments: dict[str, list[float]] = field(default_factory=dict)  # bar id -> value of each segment
    asked: float = 0.0

    def as_json(self) -> dict:
        return {"quantities": self.quantities, "units": self.units, "segments": self.segments, "asked": self.asked}


def bars_of(spec: dict) -> list[dict]:
    return [bar for stage in spec.get("stages") or [] for bar in stage.get("bars") or []]


def _symbols(spec: dict) -> tuple[dict[str, sp.Symbol], list[str], list[str]]:
    quantities = [q.get("id") for q in spec.get("quantities") or []]
    units = [u.get("name") for u in spec.get("units") or []]
    if not quantities:
        raise SpecError("spec has no quantities")
    if len(quantities) > MAX_QUANTITIES or len(spec.get("relations") or []) > MAX_RELATIONS:
        raise SpecError("too many quantities or relations")
    for name in quantities:
        if not isinstance(name, str) or not ID.match(name):
            raise SpecError(f"quantity id {name!r} must be lower_snake_case")
    for name in units:
        if not isinstance(name, str) or not UNIT_NAME.match(name):
            raise SpecError(f"unit name {name!r} must be one lowercase letter, such as u or p")
    names = quantities + units
    if len(set(names)) != len(names):
        raise SpecError("quantity ids and unit names must all be different")
    return {n: sp.Symbol(n, real=True) for n in names}, quantities, units


def _value(expr: sp.Expr, solution: dict) -> sp.Expr:
    return sp.nsimplify(sp.sympify(expr).subs(solution)) if solution else sp.sympify(expr)


def _unique(eqs: list[sp.Expr], unknowns: list[sp.Symbol], needed: dict[str, sp.Expr], what: str,
            positive: set[str] = frozenset()) -> dict[str, sp.Rational]:
    """The single non-negative value of each `needed` expression under `eqs`."""
    try:
        solutions = sp.solve(eqs, unknowns, dict=True) if eqs else [{}]
    except (NotImplementedError, ValueError, TypeError) as exc:
        raise SpecError(f"{what} could not be solved: {exc}") from exc
    if not solutions:
        raise SpecError(f"{what} contradict each other: there is no solution")
    found: list[dict[str, sp.Rational]] = []
    undetermined: set[str] = set()
    for sol in solutions:
        values, ok = {}, True
        for name, expr in needed.items():
            v = sp.simplify(sp.sympify(expr).subs(sol))
            if v.free_symbols:
                undetermined.add(name)
                ok = False
                continue
            if not v.is_real or v < 0 or (name in positive and v <= 0):
                ok = False
                continue
            values[name] = sp.nsimplify(v)
        if ok:
            found.append(values)
    if undetermined and not found:
        raise SpecError(f"{what} do not fix {', '.join(sorted(undetermined))}: add the fact from the question that does")
    distinct = {tuple(sorted(v.items())) for v in found}
    if not distinct:
        raise SpecError(f"{what} only have negative or zero solutions")
    if len(distinct) > 1:
        raise SpecError(f"{what} allow more than one answer")
    return found[0]


def solve_spec(spec: dict) -> Solution:
    """Solve a v2 spec. Raises SpecError with a reason the planner can act on."""
    symbols, quantities, units = _symbols(spec)
    relations = spec.get("relations") or []
    eqs = [parse_relation(r.get("eq", ""), symbols) for r in relations]

    try:
        asked_expr = parse_expr((spec.get("asked") or {}).get("expr", ""), symbols)
    except SpecError as exc:
        raise SpecError(f"asked: {exc}") from exc
    bars = bars_of(spec)
    ids = [b.get("id") for b in bars]
    if len(set(ids)) != len(ids):
        raise SpecError("bar ids must be unique across all stages")
    drawn = {b.get("quantity") for b in bars}
    for q in drawn:
        if q not in quantities:
            raise SpecError(f"a bar draws unknown quantity {q!r}")

    # 1) The question's facts alone fix the answer and every drawn quantity.
    needed = {"asked": asked_expr, **{q: symbols[q] for q in sorted(drawn)}}
    base = _unique(eqs, list(symbols.values()), needed, "the relations")
    known = {symbols[q]: v for q, v in base.items() if q in symbols}

    # Units the relations themselves fix (e.g. "ben_before = u").
    try:
        rel_solutions = sp.solve(eqs, list(symbols.values()), dict=True) if eqs else [{}]
    except (NotImplementedError, ValueError, TypeError):
        rel_solutions = [{}]
    for u in units:
        vals = {sp.simplify(symbols[u].subs(s)) for s in rel_solutions}
        if len(vals) == 1 and not next(iter(vals)).free_symbols:
            known[symbols[u]] = next(iter(vals))

    # 2) The bars: counted segments add up to the bar's quantity; that fixes the units.
    unit_syms = {u: symbols[u] for u in units}
    seg_exprs: dict[str, list[sp.Expr]] = {}
    bar_eqs: list[tuple[str, sp.Expr]] = []
    for bar in bars:
        segs = bar.get("segments") or []
        if not segs:
            raise SpecError(f"bar {bar.get('id')!r} has no segments")
        exprs = []
        for i, seg in enumerate(segs):
            style = seg.get("style", "normal")
            if style not in STYLES:
                raise SpecError(f"bar {bar.get('id')!r} segment {i} has unknown style {style!r}")
            try:
                exprs.append(parse_expr(seg.get("expr", ""), unit_syms))
            except SpecError as exc:
                raise SpecError(f"bar {bar.get('id')!r} segment {i}: {exc} (segments use only numbers and unit names)") from exc
        seg_exprs[bar["id"]] = exprs
        counted = sum((e for e, s in zip(exprs, segs) if s.get("style", "normal") in COUNTED_STYLES), sp.Integer(0))
        bar_eqs.append((bar["id"], counted - known[symbols[bar["quantity"]]]))

    open_units = [unit_syms[u] for u in units if unit_syms[u] not in known]
    pending = [(bid, sp.simplify(e.subs(known))) for bid, e in bar_eqs]
    if open_units:
        unit_vals = _unique([e for _, e in pending if e.free_symbols], open_units,
                            {u.name: u for u in open_units}, "the bars", positive={u.name for u in open_units})
        known.update({unit_syms[n]: v for n, v in unit_vals.items()})
    for bid, e in pending:
        residual = sp.simplify(e.subs(known))
        if residual.free_symbols:
            raise SpecError(f"bar {bid!r} uses a unit that no bar or relation fixes")
        if residual != 0:
            raise SpecError(f"bar {bid!r}: its counted segments don't add up to its quantity")

    solution = Solution(
        quantities={q: float(base[q]) for q in sorted(drawn)},
        units={u: float(known[unit_syms[u]]) for u in units if unit_syms[u] in known},
        asked=float(base["asked"]),
    )
    # Every quantity the relations fix, drawn or not, is useful to the lesson and the checks.
    for q in quantities:
        vals = {sp.simplify(symbols[q].subs(s)) for s in rel_solutions}
        if len(vals) == 1:
            v = next(iter(vals))
            if not v.free_symbols and v.is_real and v >= 0:
                solution.quantities.setdefault(q, float(v))
    for bar in bars:
        values = [sp.simplify(e.subs(known)) for e in seg_exprs[bar["id"]]]
        for i, v in enumerate(values):
            if v.free_symbols or v <= 0:
                raise SpecError(f"bar {bar['id']!r} segment {i} must have a positive value")
        solution.segments[bar["id"]] = [float(v) for v in values]
    return solution

