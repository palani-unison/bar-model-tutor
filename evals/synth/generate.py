"""Original synthetic P3-P6 word problems with code-computed answers (training data for Glimmer).

Every problem here is written from a parametrised template in this file: no school paper and
nothing from evals/private is used, and the 75 items in evals/dataset/v2-families.json are only a
style reference (they stay the held-out eval; `generate` drops any exact repeat of them).

Each template picks numbers by code, states the question's facts as SymPy equations over the
GIVEN numbers, and solves them. The answer is kept only if the facts fix it uniquely, it equals
the value the template built the numbers from, and it is a whole number or a clean decimal (at
most 2 places). So the ground truth never comes from a model.

Usage:
  python evals/synth/generate.py --n 1500 --seed 7          # -> evals/synth/out/problems.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

import sympy as sp

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "evals" / "synth" / "out"

NAMES = ["Ahmad", "Siti", "Wei Ming", "Mei Ling", "Ravi", "Priya", "Kumar", "Aisha", "Hafiz", "Nurul",
         "Jun Jie", "Xin Yi", "Arjun", "Divya", "Farah", "Daniel", "Chloe", "Ethan", "Zara", "Irfan",
         "Bala", "Kavitha", "Hui Min", "Jia Hui", "Syafiq", "Rui En", "Marcus", "Natalie", "Haziq",
         "Shalini", "Kai Wen", "Yusof", "Lakshmi", "Bryan", "Sofia", "Ming Hao", "Aryan", "Nadia",
         "Darren", "Huda", "Shi Hui", "Vikram", "Amirah", "Gabriel", "Rachel", "Zhi Hao", "Meena", "Imran"]
ADULTS = ["Mr Tan", "Mrs Lim", "Mdm Wong", "Mr Ong", "Mrs Nair", "Mr Rahman", "Mdm Chua", "Mr Goh",
          "Mrs Pillai", "Mdm Zainab", "Mr Lee", "Mrs Koh", "Uncle Ah Seng", "Aunty Fatimah", "Mr Singh",
          "Mrs Teo", "Mdm Ho", "Mr Yeo"]
THINGS = ["stickers", "marbles", "stamps", "beads", "cards", "shells", "erasers", "keychains",
          "paper cranes", "rubber bands", "picture cards", "foreign coins"]
HE = {"Ahmad", "Wei Ming", "Ravi", "Kumar", "Hafiz", "Jun Jie", "Arjun", "Daniel", "Ethan", "Irfan",
      "Bala", "Syafiq", "Marcus", "Haziq", "Kai Wen", "Yusof", "Bryan", "Ming Hao", "Aryan", "Darren",
      "Vikram", "Gabriel", "Zhi Hao", "Imran", "Mr Tan", "Mr Ong", "Mr Rahman", "Mr Goh", "Mr Lee",
      "Uncle Ah Seng", "Mr Singh", "Mr Yeo"}


def he(name: str) -> str:
    return "he" if name in HE else "she"


def his(name: str) -> str:
    return "his" if name in HE else "her"


def money(x) -> str:
    x = Fraction(x)
    return f"${int(x)}" if x.denominator == 1 else f"${float(x):.2f}"


def num(x) -> str:
    x = Fraction(x)
    if x.denominator == 1:
        return str(int(x))
    return f"{float(x):.2f}".rstrip("0").rstrip(".")


def frac(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def clean(value) -> Fraction | None:
    """A whole number or a decimal with at most 2 places, else None."""
    f = Fraction(str(value)) if not isinstance(value, Fraction) else value
    return f if (f * 100).denominator == 1 else None


# ---------------------------------------------------------------- ground truth by SymPy
def solve(eqs: list, target, symbols: list) -> Fraction:
    """Solve the question's stated facts; the target must be fixed uniquely and be clean and >= 0."""
    sols = sp.solve(eqs, symbols, dict=True)
    values = {sp.nsimplify(sp.sympify(target).subs(s)) for s in sols}
    if len(values) != 1:
        raise ValueError(f"facts do not fix the answer: {values}")
    v = values.pop()
    if v.free_symbols or not v.is_rational or v < 0:
        raise ValueError(f"answer not a fixed rational >= 0: {v}")
    f = clean(Fraction(int(v.p), int(v.q)))
    if f is None:
        raise ValueError(f"answer not clean: {v}")
    return f


S = sp.Rational


def R(x) -> sp.Rational:
    x = Fraction(x)
    return S(x.numerator, x.denominator)


def syms(names: str):
    return sp.symbols(names, nonnegative=True)


# ---------------------------------------------------------------- templates
# Each template: rng -> (question, answer_by_construction, eqs, target, symbols, level).
# `make` checks the SymPy answer equals the constructed one.
TEMPLATES: dict[str, list] = {}


def template(family: str):
    def deco(fn):
        TEMPLATES.setdefault(family, []).append(fn)
        return fn
    return deco


def two_names(rng):
    return rng.sample(NAMES, 2)


# --- how many more
@template("how_many_more")
def hmm_two_counts(rng):
    place = rng.choice([("A library", "visitors"), ("A hawker centre", "customers"), ("A bookshop", "customers"),
                        ("A swimming complex", "swimmers"), ("A cinema", "moviegoers"), ("A bakery", "buns sold")])
    days = rng.sample(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], 2)
    a, b = sorted(rng.sample(range(1200, 9800), 2))
    noun = place[1] if place[1] != "buns sold" else "buns"
    verb = "sold" if place[1] == "buns sold" else "had"
    q = (f"{place[0]} {verb} {a} {noun} on {days[0]} and {b} {noun} on {days[1]}. "
         f"How many more {noun} did it {('sell' if verb == 'sold' else 'have')} on {days[1]} than on {days[0]}?")
    x, y = syms("x y")
    return q, b - a, [sp.Eq(x, a), sp.Eq(y, b)], y - x, [x, y], "P3"


@template("how_many_more")
def hmm_multiple_total(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    k, u = rng.randint(2, 6), rng.randint(12, 90)
    total = (k + 1) * u
    q = rng.choice([
        f"{a} has {k} times as many {thing} as {b}. They have {total} {thing} altogether. How many more {thing} does {a} have than {b}?",
        f"{a} collected {k} times as many {thing} as {b}. They collected {total} {thing} in all. How many more {thing} did {a} collect than {b}?",
    ])
    x, y = syms("x y")
    return q, (k - 1) * u, [sp.Eq(x, k * y), sp.Eq(x + y, total)], x - y, [x, y], "P4"


@template("how_many_more")
def hmm_fraction_group(rng):
    d = rng.choice([5, 7, 8, 9, 11, 12])
    n = rng.randint(d // 2 + 1, d - 1)
    total = d * rng.randint(4, 40)
    group, a_word, b_word = rng.choice([("pupils in a hall", "girls", "boys"), ("people at a concert", "adults", "children"),
                                        ("books in a library corner", "storybooks", "comics")])
    noun = group.split(" ")[0]
    q = f"There are {total} {group}. {n}/{d} of them are {a_word}. How many more {a_word} than {b_word} are there?"
    x, y = syms("x y")
    return q, Fraction(total * (2 * n - d), d), [sp.Eq(x, R(Fraction(n, d)) * total), sp.Eq(x + y, total)], x - y, [x, y], "P4"


@template("how_many_more")
def hmm_price_per_kg(rng):
    who = rng.choice(ADULTS)
    (f1, f2) = rng.sample(["prawns", "fish", "chicken", "beef", "grapes", "durians", "mangoes", "crabs"], 2)
    p1, p2 = Fraction(rng.randint(80, 300), 10), Fraction(rng.randint(30, 150), 10)
    k1, k2 = rng.randint(2, 5), rng.randint(2, 6)
    if k1 * p1 <= k2 * p2:
        k1, k2, p1, p2 = k2, k1, p2, p1
        if k1 * p1 <= k2 * p2:
            raise ValueError("retry")
    q = (f"{who} bought {k1} kg of {f1} at {money(p1)} per kg and {k2} kg of {f2} at {money(p2)} per kg. "
         f"How much more did {he(who)} pay for the {f1} than for the {f2}?")
    x, y = syms("x y")
    return q, k1 * p1 - k2 * p2, [sp.Eq(x, k1 * R(p1)), sp.Eq(y, k2 * R(p2))], x - y, [x, y], "P5"


@template("how_many_more")
def hmm_after_giving(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    u = rng.randint(8, 40)
    k = rng.randint(3, 6)
    give = rng.randint(1, (k - 1) * u - 1)
    q = (f"{a} had {k} times as many {thing} as {b}. {b} had {u} {thing}. After {a} gave away {give} {thing}, "
         f"how many more {thing} did {a} have than {b}?")
    x, y = syms("x y")
    return q, k * u - give - u, [sp.Eq(y, u), sp.Eq(x, k * y)], x - give - y, [x, y], "P4"


# --- multi-step
@template("multi_step")
def ms_trays(rng):
    who = rng.choice(ADULTS)
    food = rng.choice(["curry puffs", "muffins", "ondeh-ondeh", "kuih lapis", "egg tarts", "cupcakes"])
    t, n = rng.randint(4, 12), rng.randint(8, 25)
    sold = rng.randint(t * n // 2, t * n - 3)
    q = f"{who} baked {t} trays of {food} with {n} {food} on each tray. {he(who).capitalize()} sold {sold} {food}. How many {food} did {he(who)} have left?"
    x = syms("x")
    return q, t * n - sold, [sp.Eq(x, t * n)], x - sold, [x], "P3"


@template("multi_step")
def ms_change(rng):
    who = rng.choice(ADULTS)
    (i1, i2) = rng.sample([("plates of chicken rice", 35, 60), ("bowls of laksa", 45, 70), ("cups of kopi", 12, 22),
                           ("packets of nasi lemak", 25, 45), ("slices of kaya toast", 10, 25), ("glasses of bandung", 15, 25)], 2)
    a, b = rng.randint(2, 5), rng.randint(2, 4)
    p1, p2 = Fraction(rng.randint(i1[1], i1[2]), 10), Fraction(rng.randint(i2[1], i2[2]), 10)
    cost = a * p1 + b * p2
    note = next((n for n in (10, 20, 50, 100) if n > cost), None)
    if note is None:
        raise ValueError("retry")
    q = (f"{who} bought {a} {i1[0]} at {money(p1)} each and {b} {i2[0]} at {money(p2)} each. "
         f"{he(who).capitalize()} paid with a ${note} note. How much change did {he(who)} get?")
    x = syms("x")
    return q, note - cost, [sp.Eq(x, a * R(p1) + b * R(p2))], note - x, [x], "P4"


@template("multi_step")
def ms_pack_sell(rng):
    who = rng.choice(ADULTS)
    item, box = rng.choice([("eggs", "trays"), ("oranges", "bags"), ("cookies", "tins"), ("pencils", "boxes")])
    per = rng.choice([6, 10, 12, 20, 24, 30])
    boxes = rng.randint(15, 60)
    keep = rng.randint(2, 9)
    price = Fraction(rng.randint(15, 90), 10)
    q = (f"{who} packed {per * boxes} {item} into {box} of {per} {item} each. {he(who).capitalize()} sold each of the {box} for {money(price)}. "
         f"{he(who).capitalize()} sold all the {box} except {keep}. How much money did {he(who)} receive?")
    x = syms("x")
    return q, (boxes - keep) * price, [sp.Eq(x * per, per * boxes)], (x - keep) * R(price), [x], "P5"


@template("multi_step")
def ms_buses(rng):
    n_bus, seats = rng.randint(3, 9), rng.choice([40, 44, 45, 48, 50])
    cap = n_bus * seats
    teachers = rng.randint(6, 20)
    pupils = rng.randint(cap - teachers - 40, cap - teachers - 1)
    place = rng.choice(["the Singapore Zoo", "the Science Centre", "Gardens by the Bay", "Sentosa", "the Bird Paradise"])
    q = (f"A school hired {n_bus} buses for a trip to {place}. Each bus has {seats} seats. There were {pupils} pupils and "
         f"{teachers} teachers on the trip, and each person took one seat. How many seats were empty?")
    x = syms("x")
    return q, cap - pupils - teachers, [sp.Eq(x, n_bus * seats)], x - pupils - teachers, [x], "P4"


@template("multi_step")
def ms_sell_rates(rng):
    who = rng.choice(ADULTS)
    d = rng.choice([4, 5, 7, 8])
    n = rng.randint(1, d - 1)
    g1, g2 = rng.sample([2, 3, 4, 5, 6], 2)
    total = d * g1 * g2 * rng.randint(2, 6)
    p1, p2 = rng.randint(2, 5), rng.randint(2, 5)
    item = rng.choice(["cookies", "pineapple tarts", "rice dumplings", "spring rolls"])
    q = (f"{who} made {total} {item}. {he(who).capitalize()} sold {n}/{d} of them at {g1} for ${p1} and the rest at {g2} for ${p2}. "
         f"How much money did {he(who)} collect?")
    a = Fraction(total * n, d)
    b = total - a
    if a % g1 or b % g2:
        raise ValueError("retry")
    x, y = syms("x y")
    return q, a / g1 * p1 + b / g2 * p2, [sp.Eq(x, R(Fraction(n, d)) * total), sp.Eq(y, total - x)], x / g1 * p1 + y / g2 * p2, [x, y], "P6"


# --- before-after
@template("before_after")
def ba_chain(rng):
    name = rng.choice(NAMES)
    thing = rng.choice(THINGS)
    start = rng.randint(40, 200)
    give, buy = rng.randint(10, start - 5), rng.randint(10, 80)
    end = start - give + buy
    q = (f"{name} had some {thing}. {he(name).capitalize()} gave {give} {thing} to {his(name)} friend and then bought {buy} more {thing}. "
         f"{he(name).capitalize()} had {end} {thing} in the end. How many {thing} did {he(name)} have at first?")
    x = syms("x")
    return q, start, [sp.Eq(x - give + buy, end)], x, [x], "P3"


@template("before_after")
def ba_two_spend(rng):
    a, b = two_names(rng)
    k, j = rng.randint(3, 6), rng.randint(2, 3)
    if j >= k:
        raise ValueError("retry")
    u = rng.randint(10, 60)
    sb = rng.randint(1, u - 1)
    # k*u - sa = j*(u - sb)  ->  sa = k*u - j*(u - sb)
    sa = k * u - j * (u - sb)
    if sa <= 0 or sa >= k * u:
        raise ValueError("retry")
    jw = {2: "twice", 3: "3 times"}[j]
    q = (f"{a} had {k} times as much money as {b}. After {a} spent ${sa} and {b} spent ${sb}, {a} had {jw} as much money as {b}. "
         f"How much money did {a} have at first?")
    x, y = syms("x y")
    return q, k * u, [sp.Eq(x, k * y), sp.Eq(x - sa, j * (y - sb))], x, [x, y], "P5"


@template("before_after")
def ba_fraction_then(rng):
    name = rng.choice(NAMES)
    d = rng.choice([3, 4, 5, 6])
    start = d * rng.randint(8, 40)
    gift = rng.choice([10, 15, 20, 25, 30, 40, 50])
    left_after = start - start // d + gift
    spend = rng.randint(5, left_after - 5)
    end = left_after - spend
    item, item2 = rng.sample(["a pair of shoes", "a bag", "a watch", "a book", "a jacket", "a toy robot", "a board game"], 2)
    q = (f"{name} had some money. {he(name).capitalize()} spent 1/{d} of it on {item}. Then {his(name)} mother gave {('him' if name in HE else 'her')} ${gift}. "
         f"After {he(name)} spent ${spend} on {item2}, {he(name)} had ${end} left. How much money did {he(name)} have at first?")
    x = syms("x")
    return q, start, [sp.Eq(x - x / d + gift - spend, end)], x, [x], "P5"


@template("before_after")
def ba_used_bought(rng):
    who = rng.choice(ADULTS)
    item, pack = rng.choice([("eggs", "trays of 30 eggs each"), ("apples", "bags of 12 apples each"), ("bottles of water", "cartons of 24 bottles each")])
    size = int(pack.split(" of ")[1].split(" ")[0])
    start = rng.randint(10, 60)
    used = rng.randint(5, start)
    packs = rng.randint(2, 4)
    end = start - used + packs * size
    q = (f"{who} had some {item}. {he(who).capitalize()} used {used} {item}. Then {he(who)} bought {packs} {pack}. "
         f"In the end, {he(who)} had {end} {item}. How many {item} did {he(who)} have at first?")
    x = syms("x")
    return q, start, [sp.Eq(x - used + packs * size, end)], x, [x], "P4"


@template("before_after")
def ba_ratio_fraction(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    k = rng.randint(3, 5)
    u = rng.randint(10, 40)
    give, buy = rng.randint(5, 80), rng.randint(5, 60)
    after_a, after_b = k * u - give, u + buy
    f = Fraction(after_a, after_b)
    if after_a <= 0 or f.denominator > 9 or f.numerator > 9 or f == 1:
        raise ValueError("retry")
    rel = (f"{f.numerator} times as many {thing} as {b}" if f.denominator == 1 else
           f"{frac(f)} as many {thing} as {b}")
    q = (f"{a} had {k} times as many {thing} as {b}. After {a} gave away {give} {thing} and {b} bought {buy} {thing}, "
         f"{a} had {rel}. How many {thing} did {a} have at first?")
    x, y = syms("x y")
    return q, k * u, [sp.Eq(x, k * y), sp.Eq(x - give, R(f) * (y + buy))], x, [x, y], "P6"


# --- equal end
@template("equal_end")
def ee_give(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    y = rng.randint(10, 80)
    x = y + 2 * rng.randint(4, 40)
    q = f"{a} has {x} {thing} and {b} has {y} {thing}. How many {thing} must {a} give {b} so that they both have the same number of {thing}?"
    g = syms("g")
    return q, (x - y) // 2, [sp.Eq(x - g, y + g)], g, [g], "P3"


@template("equal_end")
def ee_pour(rng):
    k = rng.randint(3, 7)
    d = rng.randint(4, 30)
    if (2 * d) % (k - 1):
        raise ValueError("retry")
    u = 2 * d // (k - 1)
    unit = rng.choice(["litres of water", "litres of juice", "kg of rice", "kg of sand"])
    word = "poured" if "litres" in unit else "moved"
    cont = rng.choice([("Container A", "Container B"), ("Tank P", "Tank Q"), ("Pail X", "Pail Y")])
    q = (f"{cont[0]} had {k} times as much {unit.split(' of ')[1]} as {cont[1]}. After {d} {unit} were {word} from {cont[0]} into {cont[1]}, "
         f"both had the same amount of {unit.split(' of ')[1]}. How much {unit.split(' of ')[1]} was in {cont[0]} at first?")
    x, y = syms("x y")
    return q, k * u, [sp.Eq(x, k * y), sp.Eq(x - d, y + d)], x, [x, y], "P4"


@template("equal_end")
def ee_both_receive(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    k = rng.randint(3, 6)
    u = rng.randint(6, 40)
    ra = rng.randint(2, 20)
    rb = (k - 1) * u + ra
    q = (f"{a} had {k} times as many {thing} as {b}. After {b} received {rb} {thing} from {his(b)} teacher and {a} received {ra} {thing}, "
         f"they had the same number of {thing}. How many {thing} did {a} have at first?")
    x, y = syms("x y")
    return q, k * u, [sp.Eq(x, k * y), sp.Eq(x + ra, y + rb)], x, [x, y], "P5"


@template("equal_end")
def ee_fraction_giveaway(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    d = rng.choice([4, 5, 7, 8])
    n = rng.randint(1, d - 1)
    unit = rng.randint(4, 20)
    av, bv = n * unit, d * unit
    gone = bv - av
    q = (f"{a} had {n}/{d} as many {thing} as {b}. After {b} gave away {gone} {thing}, they had the same number of {thing}. "
         f"How many {thing} did {a} have?")
    x, y = syms("x y")
    return q, av, [sp.Eq(x, R(Fraction(n, d)) * y), sp.Eq(x, y - gone)], x, [x, y], "P5"


@template("equal_end")
def ee_spent_fractions(rng):
    a, b = two_names(rng)
    fa, fb = rng.choice([(Fraction(3, 5), Fraction(1, 4)), (Fraction(2, 3), Fraction(1, 3)), (Fraction(1, 2), Fraction(1, 4)),
                         (Fraction(3, 4), Fraction(1, 2)), (Fraction(2, 5), Fraction(1, 5)), (Fraction(5, 6), Fraction(1, 2))])
    # (1-fa) A = (1-fb) B and A = B + diff
    ra, rb = 1 - fa, 1 - fb
    m = rng.randint(1, 8)
    bval = Fraction(ra.denominator * rb.denominator * m * ra.numerator, 1)
    aval = bval * rb / ra
    diff = aval - bval
    if diff <= 0 or aval.denominator != 1:
        raise ValueError("retry")
    q = (f"{a} had ${num(diff)} more than {b}. After {a} spent {frac(fa)} of {his(a)} money and {b} spent {frac(fb)} of {his(b)} money, "
         f"they had the same amount of money left. How much money did {b} have at first?")
    x, y = syms("x y")
    return q, bval, [sp.Eq(x, y + R(diff)), sp.Eq((1 - R(fa)) * x, (1 - R(fb)) * y)], y, [x, y], "P6"


# --- constant total
@template("constant_total")
def ct_give_multiple(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    k = rng.randint(2, 5)
    v = rng.randint(8, 40)
    total = (k + 1) * v
    g = rng.randint(3, v - 1)
    a0 = v + g  # A gives g to B, then B = k * A
    kw = "twice" if k == 2 else f"{k} times"
    q = (f"{a} and {b} had {total} {thing} altogether. After {a} gave {b} {g} {thing}, {b} had {kw} as many {thing} as {a}. "
         f"How many {thing} did {a} have at first?")
    x, y = syms("x y")
    return q, a0, [sp.Eq(x + y, total), sp.Eq(y + g, k * (x - g))], x, [x, y], "P4"


@template("constant_total")
def ct_give_ask_receiver(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    k = rng.randint(2, 5)
    v = rng.randint(5, 30)
    total = (k + 1) * v
    g = rng.randint(2, v - 1)
    # after A gives g to B: A = k * B ; B at first = v - g
    kw = "twice" if k == 2 else f"{k} times"
    q = (f"{a} and {b} had {total} {thing} altogether. After {a} gave {g} {thing} to {b}, {a} had {kw} as many {thing} as {b}. "
         f"How many {thing} did {b} have at first?")
    x, y = syms("x y")
    return q, v - g, [sp.Eq(x + y, total), sp.Eq(x - g, k * (y + g))], y, [x, y], "P5"


@template("constant_total")
def ct_ratio_swap(rng):
    a, b = two_names(rng)
    r1 = rng.choice([(5, 3), (7, 5), (4, 1), (5, 2), (3, 1), (7, 3)])
    r2 = rng.choice([(3, 5), (1, 1), (2, 3), (1, 2), (3, 4)])
    s1, s2 = sum(r1), sum(r2)
    if Fraction(r1[0], s1) <= Fraction(r2[0], s2):
        raise ValueError("retry")
    import math
    l = s1 * s2 // math.gcd(s1, s2)
    total = l * rng.randint(2, 12)
    a0 = total * r1[0] // s1
    a1 = total * r2[0] // s2
    g = a0 - a1
    r2txt = "the same amount" if r2 == (1, 1) else None
    end = (f"they had the same amount of money" if r2txt else f"the ratio became {r2[0]} : {r2[1]}")
    q = (f"The ratio of {a}'s money to {b}'s money was {r1[0]} : {r1[1]}. After {a} gave {b} ${g}, {end}. "
         f"How much money did {a} have at first?")
    x, y = syms("x y")
    return q, a0, [sp.Eq(r1[1] * x, r1[0] * y), sp.Eq(r2[1] * (x - g), r2[0] * (y + g))], x, [x, y], "P6"


@template("constant_total")
def ct_boxes_fraction(rng):
    f = rng.choice([Fraction(3, 4), Fraction(2, 3), Fraction(3, 5), Fraction(1, 2), Fraction(4, 5)])
    m = rng.randint(3, 12)
    a_after = f.denominator * m
    b_after = f.numerator * m
    total = a_after + b_after
    g = rng.randint(2, b_after - 1)
    item = rng.choice(["apples", "oranges", "books", "toys"])
    q = (f"Box A and Box B contained {total} {item} altogether. After {g} {item} were moved from Box A to Box B, "
         f"Box B had {frac(f)} as many {item} as Box A. How many {item} were in Box B at first?")
    x, y = syms("x y")
    return q, b_after - g, [sp.Eq(x + y, total), sp.Eq(y + g, R(f) * (x - g))], y, [x, y], "P5"


# --- constant difference
@template("constant_difference")
def cd_ages_when(rng):
    parent = rng.choice(["Mrs Chua", "Mr Lim", "Mdm Siti", "Mr Kumar", "Mrs Ong", "Mr Tan"])
    child = rng.choice(["son", "daughter"])
    c = rng.randint(4, 15)
    p = c + rng.randint(22, 38)
    later = c + rng.randint(3, 20)
    q = f"{parent} is {p} years old and {his(parent)} {child} is {c} years old. How old will {parent} be when {his(parent)} {child} is {later} years old?"
    x = syms("x")
    return q, p + later - c, [sp.Eq(x - later, p - c)], x, [x], "P4"


@template("constant_difference")
def cd_ages_times(rng):
    parent = rng.choice(["Mr Rahman", "Mrs Lee", "Mdm Kaur", "Mr Wong", "Mrs Tan"])
    child = rng.choice(["son", "daughter"])
    k, j = rng.choice([(4, 3), (5, 3), (5, 4), (3, 2), (6, 4), (7, 4)])
    n = rng.randint(2, 12)
    # p = k c ; p + n = j (c + n)  ->  c (k - j) = n (j - 1)
    if (n * (j - 1)) % (k - j):
        raise ValueError("retry")
    c = n * (j - 1) // (k - j)
    if c < 3 or c > 20:
        raise ValueError("retry")
    jw = "twice" if j == 2 else f"{j} times"
    q = f"{parent} is {k} times as old as {his(parent)} {child}. In {n} years' time, {he(parent)} will be {jw} as old as {his(parent)} {child}. How old is {parent} now?"
    x, y = syms("x y")
    return q, k * c, [sp.Eq(x, k * y), sp.Eq(x + n, j * (y + n))], x, [x, y], "P5"


@template("constant_difference")
def cd_same_gift(rng):
    a, b = two_names(rng)
    k = rng.choice([2, 3])
    g = rng.randint(5, 40)
    yb = rng.randint(5, 40)
    # (xa + g) = k (yb + g)  -> xa = k*yb + (k-1)*g
    xa = k * yb + (k - 1) * g
    rel = "sister" if b not in HE else "brother"
    kw = "twice" if k == 2 else f"{k} times"
    q = (f"{a} had ${xa} and {his(a)} {rel} {b} had ${yb}. Their grandmother gave each of them the same amount of money. "
         f"Then {a} had {kw} as much money as {b}. How much money did their grandmother give each of them?")
    m = syms("m")
    return q, g, [sp.Eq(xa + m, k * (yb + m))], m, [m], "P5"


@template("constant_difference")
def cd_years_ago(rng):
    parent = rng.choice(["Mdm Lim", "Mr Chan", "Mrs Rao", "Mr Hassan"])
    child = rng.choice(["son", "daughter"])
    k, j = rng.choice([(5, 3), (4, 2), (6, 3), (7, 3), (5, 2)])
    ago, later = rng.randint(2, 6), rng.randint(3, 10)
    # p - ago = k (c - ago); p + later = j (c + later)
    x, y = syms("x y")
    sol = sp.solve([sp.Eq(x - ago, k * (y - ago)), sp.Eq(x + later, j * (y + later))], [x, y], dict=True)[0]
    p, c = sol[x], sol[y]
    if not (p.is_integer and c.is_integer) or c <= ago or p > 80:
        raise ValueError("retry")
    words = {2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six"}
    jw = "twice" if j == 2 else f"{j} times"
    q = (f"{words[ago]} years ago, {parent} was {k} times as old as {his(parent)} {child}. In {later} years' time, {he(parent)} will be "
         f"{jw} as old as {his(parent)} {child}. How old is {parent} now?")
    return q, int(p), [sp.Eq(x - ago, k * (y - ago)), sp.Eq(x + later, j * (y + later))], x, [x, y], "P6"


# --- remainder
@template("remainder")
def rem_known_total(rng):
    name = rng.choice(NAMES)
    thing = rng.choice(["sweets", "stickers", "biscuits", "marbles"])
    a, b = rng.choice([(4, 3), (3, 4), (5, 2), (4, 2), (3, 5), (6, 5)])
    total = a * b * rng.randint(2, 8)
    left = total - total // a
    left = left - left // b
    q = (f"{name} had {total} {thing}. {he(name).capitalize()} gave 1/{a} of them to {his(name)} brother and 1/{b} of the remainder to {his(name)} sister. "
         f"How many {thing} did {he(name)} have left?")
    x = syms("x")
    rem = total - S(total, a)
    return q, left, [sp.Eq(x, rem - rem / b)], x, [x], "P4"


@template("remainder")
def rem_left_given(rng):
    who = rng.choice(ADULTS + NAMES)
    a, b = rng.choice([(3, 4), (4, 3), (5, 4), (3, 2), (4, 5), (5, 3), (6, 5)])
    total = a * b * rng.randint(2, 15) * rng.choice([1, 2, 5])
    left = total - total // a
    left = left - left // b
    i1, i2 = rng.sample(["groceries", "a shirt", "a bag", "books", "a lamp", "a pair of shoes", "a gift"], 2)
    q = (f"{who} had some money. {he(who).capitalize()} spent 1/{a} of it on {i1} and 1/{b} of the remainder on {i2}. "
         f"{he(who).capitalize()} had ${left} left. How much money did {he(who)} have at first?")
    x = syms("x")
    r = x - x / a
    return q, total, [sp.Eq(r - r / b, left)], x, [x], "P5"


@template("remainder")
def rem_afternoon(rng):
    who = rng.choice(["A baker", "A hawker", "A shopkeeper", "A florist"])
    item = {"A baker": "buns", "A hawker": "satay sticks", "A shopkeeper": "notebooks", "A florist": "roses"}[who]
    f1 = rng.choice([Fraction(3, 8), Fraction(1, 4), Fraction(2, 5), Fraction(1, 3), Fraction(3, 5)])
    f2 = rng.choice([Fraction(2, 5), Fraction(1, 2), Fraction(1, 3), Fraction(3, 4), Fraction(2, 3)])
    base = f1.denominator * f2.denominator
    total = base * rng.randint(2, 15)
    after = (total - total * f1) * f2
    if after.denominator != 1:
        raise ValueError("retry")
    q = (f"{who} made some {item}. {he('x').capitalize() if False else 'It'} sold {frac(f1)} of them in the morning and {frac(f2)} of the remainder in the afternoon. "
         f"{who} sold {after} {item} in the afternoon. How many {item} were made?")
    q = q.replace(" It sold", " The " + who.split(" ")[1] + " sold")
    x = syms("x")
    return q, total, [sp.Eq((x - R(f1) * x) * R(f2), int(after))], x, [x], "P5"


@template("remainder")
def rem_compare_spent(rng):
    who = rng.choice(ADULTS)
    f1 = rng.choice([Fraction(2, 7), Fraction(1, 4), Fraction(1, 5), Fraction(1, 3), Fraction(2, 9)])
    f2 = rng.choice([Fraction(1, 2), Fraction(2, 3), Fraction(3, 4), Fraction(3, 5)])
    total = f1.denominator * f2.denominator * rng.randint(2, 20)
    d1 = total * f1
    d2 = (total - d1) * f2
    diff = d2 - d1
    if diff <= 0 or diff.denominator != 1:
        raise ValueError("retry")
    i1, i2 = rng.sample(["a dress", "a pair of shoes", "a handbag", "a fan", "a rice cooker"], 2)
    q = (f"{who} spent {frac(f1)} of {his(who)} money on {i1} and {frac(f2)} of the remainder on {i2}. "
         f"{he(who).capitalize()} spent ${diff} more on {i2.split(' ', 1)[1] if i2.startswith('a ') else i2} than on {i1.split(' ', 1)[1] if i1.startswith('a ') else i1}. "
         f"How much money did {he(who)} have at first?")
    q = q.replace("on pair of shoes", "on the shoes").replace("on dress", "on the dress").replace("on handbag", "on the handbag") \
         .replace("on fan", "on the fan").replace("on rice cooker", "on the rice cooker")
    x = syms("x")
    return q, total, [sp.Eq((x - R(f1) * x) * R(f2) - R(f1) * x, int(diff))], x, [x], "P6"


@template("remainder")
def rem_then_buy(rng):
    name = rng.choice(NAMES)
    thing = rng.choice(THINGS)
    f1 = rng.choice([Fraction(1, 5), Fraction(1, 4), Fraction(1, 3), Fraction(2, 5)])
    f2 = rng.choice([Fraction(3, 8), Fraction(1, 2), Fraction(1, 4), Fraction(2, 3)])
    total = f1.denominator * f2.denominator * rng.randint(2, 10)
    rem = total - total * f1
    left = rem - rem * f2
    if left.denominator != 1:
        raise ValueError("retry")
    buy = rng.randint(5, 40)
    a, b = rng.sample([n for n in NAMES if n != name], 2)
    q = (f"{name} had some {thing}. {he(name).capitalize()} gave {frac(f1)} of them to {a} and {frac(f2)} of the remainder to {b}. "
         f"Then {he(name)} bought {buy} more {thing} and had {left + buy} {thing}. How many {thing} did {he(name)} have at first?")
    x = syms("x")
    r = x - R(f1) * x
    return q, total, [sp.Eq(r - R(f2) * r + buy, int(left + buy))], x, [x], "P6"


# --- fraction of
@template("fraction_of")
def fo_not(rng):
    d = rng.choice([5, 7, 8, 9, 12])
    n = rng.randint(1, d - 1)
    total = d * rng.randint(3, 9)
    grp, has, hasnt = rng.choice([("pupils in a class", "wear spectacles", "do not wear spectacles"),
                                  ("children at a party", "like durians", "do not like durians"),
                                  ("members in a club", "can swim", "cannot swim")])
    q = f"There are {total} {grp}. {n}/{d} of them {has}. How many {grp.split(' ')[0]} {hasnt}?"
    x = syms("x")
    return q, total - total * n // d, [sp.Eq(x, total - R(Fraction(n, d)) * total)], x, [x], "P4"


@template("fraction_of")
def fo_kg_left(rng):
    who = rng.choice(ADULTS)
    d = rng.choice([4, 8, 5])
    n = rng.randint(1, d - 1)
    kg = rng.randint(2, 9)
    item = rng.choice(["rice", "flour", "sugar", "minced meat"])
    verb = {"rice": "cooked", "flour": "used", "sugar": "used", "minced meat": "cooked"}[item]
    left = kg - Fraction(kg * n, d)
    if clean(left) is None:
        raise ValueError("retry")
    q = f"{who} bought {kg} kg of {item}. {he(who).capitalize()} {verb} {n}/{d} of the {item}. How many kilograms of {item} did {he(who)} have left?"
    x = syms("x")
    return q, left, [sp.Eq(x, kg - R(Fraction(n, d)) * kg)], x, [x], "P5"


@template("fraction_of")
def fo_more_than_rest(rng):
    d = rng.choice([5, 7, 8, 9])
    n = rng.randint(d // 2 + 1, d - 1)
    m = rng.randint(2, 20)
    total = d * m
    diff = (2 * n - d) * m
    a, b = rng.choice([("English books", "Chinese books"), ("red beads", "blue beads"), ("adults", "children"), ("fiction books", "non-fiction books")])
    where = {"English books": "on a shelf", "red beads": "in a box", "adults": "at a show", "fiction books": "in a cupboard"}[a]
    q = (f"{n}/{d} of the {a.split(' ')[-1] if a != 'adults' else 'people'} {where} are {a} and the rest are {b}. "
         f"There are {diff} more {a} than {b}. How many {a.split(' ')[-1] if a != 'adults' else 'people'} are there {where}?")
    x = syms("x")
    return q, total, [sp.Eq(R(Fraction(n, d)) * x - (x - R(Fraction(n, d)) * x), diff)], x, [x], "P5"


@template("fraction_of")
def fo_two_fractions(rng):
    name = rng.choice(NAMES)
    f1, f2 = rng.choice([(Fraction(2, 5), Fraction(1, 4)), (Fraction(1, 3), Fraction(1, 4)), (Fraction(3, 8), Fraction(1, 4)),
                         (Fraction(1, 2), Fraction(1, 3)), (Fraction(2, 3), Fraction(1, 6))])
    from math import lcm
    total = lcm(f1.denominator, f2.denominator) * rng.randint(2, 15)
    diff = total * (f1 - f2)
    if diff.denominator != 1:
        raise ValueError("retry")
    i1, i2 = rng.sample(["book", "pen", "cap", "file", "water bottle"], 2)
    q = (f"{name} spent {frac(f1)} of {his(name)} money on a {i1} and {frac(f2)} of {his(name)} money on a {i2}. "
         f"The {i1} cost ${diff} more than the {i2}. How much money did {he(name)} have at first?")
    x = syms("x")
    return q, total, [sp.Eq(R(f1) * x - R(f2) * x, int(diff))], x, [x], "P6"


@template("fraction_of")
def fo_mixed_kg(rng):
    who = rng.choice(ADULTS)
    kg = rng.randint(2, 6)
    a, b = rng.choice([(Fraction(3, 4), Fraction(1, 2)), (Fraction(1, 4), Fraction(1, 2)), (Fraction(2, 5), Fraction(1, 5)), (Fraction(3, 4), Fraction(1, 4))])
    item, use1, use2 = rng.choice([("flour", "prata", "cookies"), ("sugar", "kueh", "a cake"), ("butter", "tarts", "bread")])
    left = kg - a - b
    q = f"{who} had {kg} kg of {item}. {he(who).capitalize()} used {frac(a)} kg of it to make {use1} and {frac(b)} kg to make {use2}. How many kilograms of {item} did {he(who)} have left?"
    x = syms("x")
    return q, left, [sp.Eq(x, kg - R(a) - R(b))], x, [x], "P4"


# --- percentage
@template("percentage")
def pc_of(rng):
    p = rng.choice([5, 10, 15, 20, 25, 30, 35, 40, 45, 60, 75])
    total = rng.choice([200, 300, 400, 500, 600, 800, 1000, 1200, 1500])
    grp, verb = rng.choice([("pupils in a school", "walk to school"), ("visitors at a museum", "are tourists"), ("runners in a race", "are women")])
    q = f"There are {total} {grp}. {p}% of them {verb}. How many {grp.split(' ')[0]} {verb}?"
    x = syms("x")
    return q, Fraction(total * p, 100), [sp.Eq(x, S(p, 100) * total)], x, [x], "P5"


@template("percentage")
def pc_discount(rng):
    p = rng.choice([5, 10, 15, 20, 25, 30, 40])
    price = rng.choice([40, 60, 80, 120, 150, 180, 240, 300, 360])
    item = rng.choice(["rice cooker", "fan", "pair of sports shoes", "school bag", "blender", "bicycle helmet"])
    q = f"A {item} costs ${price}. During a sale, it is sold at a {p}% discount. What is the sale price of the {item}?"
    x = syms("x")
    return q, price - Fraction(price * p, 100), [sp.Eq(x, price - S(p, 100) * price)], x, [x], "P5"


@template("percentage")
def pc_gst(rng):
    price = rng.choice([50, 80, 120, 150, 200, 250, 300, 450, 500, 1200])
    item = rng.choice(["bicycle", "television", "washing machine", "laptop", "sofa"])
    who = rng.choice(ADULTS)
    q = f"The price of a {item} is ${price} before GST. {who} has to pay 9% GST on it. How much does {he(who)} pay for the {item} altogether?"
    x = syms("x")
    return q, price + Fraction(price * 9, 100), [sp.Eq(x, price + S(9, 100) * price)], x, [x], "P5"


@template("percentage")
def pc_after_discount(rng):
    p = rng.choice([10, 20, 25, 30, 40, 50, 15])
    usual = rng.choice([40, 60, 80, 100, 120, 160, 200, 240])
    sale = usual - Fraction(usual * p, 100)
    item = rng.choice(["pair of shoes", "dress", "watch", "backpack", "toaster"])
    q = f"After a {p}% discount, a {item} cost {money(sale)}. What was the usual price of the {item}?"
    x = syms("x")
    return q, usual, [sp.Eq(x - S(p, 100) * x, R(sale))], x, [x], "P6"


@template("percentage")
def pc_fraction_of_group(rng):
    total = rng.choice([40, 50, 80, 200, 250, 400, 500])
    part = total * rng.choice([1, 2, 3, 4, 6, 7, 9]) // 10
    grp, verb = rng.choice([("cupcakes", "were sold"), ("pupils", "passed a swimming test"), ("seats in a hall", "were taken")])
    q = f"There were {total} {grp}. {part} of them {verb}. What percentage of the {grp} {verb}?"
    x = syms("x")
    return q, Fraction(part * 100, total), [sp.Eq(x, S(part, total) * 100)], x, [x], "P5"


# --- percentage change
@template("percentage_change")
def pchg_increase(rng):
    p = rng.choice([10, 20, 25, 30, 40, 50])
    old = Fraction(rng.choice([250, 300, 350, 400, 450, 500, 550, 600, 800, 1200]), 100)
    new = old + old * p / 100
    if clean(new) is None:
        raise ValueError("retry")
    item = rng.choice(["bowl of laksa", "plate of char kway teow", "cup of bubble tea", "packet of chicken rice"])
    q = f"A {item} cost {money(old)} last year. This year, the price increased by {p}%. How much does a {item} cost this year?"
    x = syms("x")
    return q, new, [sp.Eq(x, R(old) + S(p, 100) * R(old))], x, [x], "P5"


@template("percentage_change")
def pchg_find_pct(rng):
    p = rng.choice([5, 10, 15, 20, 25, 30, 40, 60])
    old = rng.choice([200, 400, 500, 800, 1000, 1200, 1600, 2000])
    up = rng.random() < 0.5
    new = old + old * p // 100 if up else old - old * p // 100
    what = rng.choice(["visitors to a library", "pupils in a choir", "cars in a car park", "members of a club"])
    days = rng.choice([("on Saturday", "on Sunday"), ("in May", "in June"), ("last year", "this year")])
    word = "increase" if up else "decrease"
    verb = "rose" if up else "dropped"
    q = f"The number of {what} {verb} from {old} {days[0]} to {new} {days[1]}. What was the percentage {word} in the number of {what}?"
    x = syms("x")
    return q, p, [sp.Eq(x, abs(S(new - old, old)) * 100)], x, [x], "P5"


@template("percentage_change")
def pchg_two_steps(rng):
    who = rng.choice(ADULTS)
    sal = rng.choice([2000, 2400, 3000, 3200, 4000, 5000])
    p1, p2 = rng.choice([10, 20, 5]), rng.choice([5, 10, 20])
    final = Fraction(sal) * (100 + p1) / 100 * (100 + p2) / 100
    if clean(final) is None:
        raise ValueError("retry")
    q = (f"{who}'s monthly salary was ${sal}. It increased by {p1}% in the first year. In the second year, {his(who)} new salary increased by another {p2}%. "
         f"What was {his(who)} monthly salary at the end of the second year?")
    x = syms("x")
    return q, final, [sp.Eq(x, sal * S(100 + p1, 100) * S(100 + p2, 100))], x, [x], "P6"


@template("percentage_change")
def pchg_up_down(rng):
    p = rng.choice([10, 20, 25, 50])
    first = rng.choice([100, 200, 400, 500, 800])
    final = Fraction(first) * (100 + p) / 100 * (100 - p) / 100
    if clean(final) is None:
        raise ValueError("retry")
    item = rng.choice(["fan", "microwave oven", "kettle", "study lamp"])
    q = (f"A shop increased the price of a {item} by {p}%. Later, it reduced the new price by {p}%. The {item} then cost {money(final)}. "
         f"What was the price of the {item} at first?")
    x = syms("x")
    return q, first, [sp.Eq(x * S(100 + p, 100) * S(100 - p, 100), R(final))], x, [x], "P6"


@template("percentage_change")
def pchg_then_fell(rng):
    p = rng.choice([10, 20, 25, 50])
    jan = rng.choice([40, 80, 120, 160, 200, 240, 400])
    feb = jan + jan * p // 100
    fell = rng.randint(5, feb // 3)
    mar = feb - fell
    q = (f"The number of members in a club increased by {p}% from January to February. From February to March, the number fell by {fell} members. "
         f"There were {mar} members in March. How many members were there in January?")
    x = syms("x")
    return q, jan, [sp.Eq(x * S(100 + p, 100) - fell, mar)], x, [x], "P6"


# --- ratio change
@template("ratio_change")
def rc_some_joined(rng):
    a, b = rng.choice([(3, 4), (2, 5), (3, 5), (4, 7), (2, 3)])
    c = rng.randint(a + 1, a + 4)
    m = rng.randint(3, 10)
    girls = b * m
    joined = (c - a) * m
    grp = rng.choice([("boys", "girls", "a choir"), ("adults", "children", "a hall"), ("red balloons", "blue balloons", "a bag")])
    q = (f"The ratio of the number of {grp[0]} to {grp[1]} in {grp[2]} was {a} : {b}. There were {girls} {grp[1]}. "
         f"After some {grp[0]} {'joined' if grp[0] in ('boys', 'adults') else 'were added'}, the ratio became {c} : {b}. How many {grp[0]} {'joined' if grp[0] in ('boys', 'adults') else 'were added'}?")
    x, y = syms("x y")
    return q, joined, [sp.Eq(x, S(a, b) * girls), sp.Eq((x + y) * b, c * girls)], y, [x, y], "P5"


@template("ratio_change")
def rc_one_spent(rng):
    a, b = two_names(rng)
    r1 = rng.choice([(4, 3), (5, 3), (5, 4), (3, 2), (7, 4)])
    r2 = rng.choice([(2, 3), (1, 1), (1, 2), (3, 4)])
    if Fraction(*r2) >= Fraction(*r1):
        raise ValueError("retry")
    from math import lcm
    unit_b = lcm(r1[1], r2[1])
    m = rng.randint(2, 15)
    bval = unit_b * m
    a0 = bval * r1[0] // r1[1]
    a1 = bval * r2[0] // r2[1]
    spent = a0 - a1
    end = "they had the same amount of money" if r2 == (1, 1) else f"the ratio became {r2[0]} : {r2[1]}"
    q = (f"The ratio of {a}'s money to {b}'s money was {r1[0]} : {r1[1]}. After {a} spent ${spent}, {end}. "
         f"How much money did {b} have?")
    x, y = syms("x y")
    return q, bval, [sp.Eq(r1[1] * x, r1[0] * y), sp.Eq(r2[1] * (x - spent), r2[0] * y)], y, [x, y], "P5"


@template("ratio_change")
def rc_taken_out(rng):
    r1 = rng.choice([(3, 5), (2, 3), (4, 5), (1, 2), (3, 4)])
    r2 = rng.choice([(6, 7), (1, 1), (4, 3), (3, 2), (5, 4)])
    if Fraction(*r2) <= Fraction(*r1):
        raise ValueError("retry")
    from math import lcm
    ua = lcm(r1[0], r2[0])
    m = rng.randint(1, 8)
    gold = ua * m
    g0 = gold * r1[1] // r1[0]
    g1 = gold * r2[1] // r2[0]
    out = g0 - g1
    if out <= 0:
        raise ValueError("retry")
    fish = rng.choice([("goldfish", "guppies", "a tank"), ("red pens", "blue pens", "a box"), ("lemons", "limes", "a basket")])
    q = (f"In {fish[2]}, the ratio of {fish[0]} to {fish[1]} was {r1[0]} : {r1[1]}. After {out} {fish[1]} were taken out, "
         f"the ratio became {r2[0]} : {r2[1]}. How many {fish[0]} were there in {fish[2]}?")
    x, y = syms("x y")
    return q, gold, [sp.Eq(r1[1] * x, r1[0] * y), sp.Eq(r2[1] * x, r2[0] * (y - out))], x, [x, y], "P6"


@template("ratio_change")
def rc_moved(rng):
    r1 = rng.choice([(5, 4), (3, 2), (7, 5), (4, 3), (5, 3)])
    r2 = rng.choice([(7, 8), (1, 1), (2, 3), (3, 4), (4, 5)])
    if Fraction(*r2) >= Fraction(*r1):
        raise ValueError("retry")
    s1, s2 = sum(r1), sum(r2)
    from math import lcm
    total = lcm(s1, s2) * rng.randint(1, 4)
    a0, a1 = total * r1[0] // s1, total * r2[0] // s2
    moved = a0 - a1
    end = "both classes had the same number of pupils" if r2 == (1, 1) else f"the ratio became {r2[0]} : {r2[1]}"
    q = (f"The ratio of the number of pupils in Class A to Class B was {r1[0]} : {r1[1]}. After {moved} pupils moved from Class A to Class B, "
         f"{end}. How many pupils were in Class A at first?")
    x, y = syms("x y")
    return q, a0, [sp.Eq(r1[1] * x, r1[0] * y), sp.Eq(r2[1] * (x - moved), r2[0] * (y + moved))], x, [x, y], "P6"


@template("ratio_change")
def rc_both_change(rng):
    a, b = two_names(rng)
    thing = rng.choice(THINGS)
    r1 = rng.choice([(2, 3), (3, 4), (1, 2), (3, 5)])
    r2 = rng.choice([(4, 3), (1, 1), (3, 2), (5, 4)])
    m = rng.randint(4, 30)
    a0, b0 = r1[0] * m, r1[1] * m
    take = rng.randint(1, b0 - 1)
    b1 = b0 - take
    if (b1 * r2[0]) % r2[1]:
        raise ValueError("retry")
    a1 = b1 * r2[0] // r2[1]
    buy = a1 - a0
    if buy <= 0:
        raise ValueError("retry")
    end = "they had the same number of " + thing if r2 == (1, 1) else f"the ratio became {r2[0]} : {r2[1]}"
    q = (f"The ratio of {a}'s {thing} to {b}'s {thing} was {r1[0]} : {r1[1]}. After {a} bought {buy} {thing} and {b} gave away {take} {thing}, "
         f"{end}. How many {thing} did {b} have at first?")
    x, y = syms("x y")
    return q, b0, [sp.Eq(r1[1] * x, r1[0] * y), sp.Eq(r2[1] * (x + buy), r2[0] * (y - take))], y, [x, y], "P6"


# --- units and value
@template("units_and_value")
def uv_same_number(rng):
    who = rng.choice(ADULTS)
    (i1, p1), (i2, p2) = rng.sample([("pen", 2), ("pencil", 1), ("ruler", Fraction(3, 2)), ("file", 3), ("notebook", Fraction(5, 2)), ("eraser", Fraction(1, 2))], 2)
    n = rng.randint(4, 20)
    total = n * (p1 + p2)
    q = (f"{who} bought the same number of {i1}s and {i2}s. Each {i1} cost {money(p1)} and each {i2} cost {money(p2)}. "
         f"{he(who).capitalize()} paid {money(total)} altogether. How many {i1}s did {he(who)} buy?")
    x = syms("x")
    return q, n, [sp.Eq(x * R(p1) + x * R(p2), R(total))], x, [x], "P4"


@template("units_and_value")
def uv_twice_as_many(rng):
    name = rng.choice(NAMES)
    k = rng.choice([2, 3])
    (i1, p1), (i2, p2) = rng.sample([("apple", Fraction(3, 5)), ("orange", Fraction(9, 20)), ("pear", Fraction(4, 5)), ("banana", Fraction(1, 4)), ("kiwi", Fraction(1, 2))], 2)
    n = rng.randint(3, 15)
    total = n * p1 + k * n * p2
    if clean(total) is None:
        raise ValueError("retry")
    kw = "twice" if k == 2 else "3 times"
    q = (f"{name} bought {kw} as many {i2}s as {i1}s. An {i1} cost {money(p1)} and an {i2} cost {money(p2)}. ".replace("An b", "A b").replace("An p", "A p").replace("An k", "A k")
         + f"{he(name).capitalize()} paid {money(total)} altogether. How many {i2}s did {he(name)} buy?")
    q = q.replace("an banana", "a banana").replace("an pear", "a pear").replace("an kiwi", "a kiwi")
    x = syms("x")
    return q, k * n, [sp.Eq(x / k * R(p1) + x * R(p2), R(total))], x, [x], "P5"


@template("units_and_value")
def uv_more_items(rng):
    who = rng.choice(ADULTS)
    (i1, p1), (i2, p2) = rng.sample([("plate of chicken rice", Fraction(7, 2)), ("bowl of noodles", Fraction(3)), ("cup of milo", Fraction(3, 2)),
                                     ("plate of fried rice", Fraction(4)), ("bowl of soup", Fraction(5, 2))], 2)
    n = rng.randint(2, 12)
    more = rng.randint(1, 6)
    total = (n + more) * p1 + n * p2
    if clean(total) is None:
        raise ValueError("retry")
    pl = lambda s: s.replace("plate", "plates").replace("bowl", "bowls").replace("cup", "cups")
    q = (f"At a canteen, a {i1} costs {money(p1)} and a {i2} costs {money(p2)}. {who} bought {more} more {pl(i1)} than {pl(i2)}. "
         f"{he(who).capitalize()} paid {money(total)} altogether. How many {pl(i2)} did {he(who)} buy?")
    x = syms("x")
    return q, n, [sp.Eq((x + more) * R(p1) + x * R(p2), R(total))], x, [x], "P5"


@template("units_and_value")
def uv_for_every(rng):
    who = rng.choice(ADULTS)
    p1, p2 = rng.sample([2, 3, 4, 5], 2)
    a, b = rng.choice([(3, 2), (2, 1), (4, 3), (1, 2), (3, 1)])
    sets = rng.randint(2, 8)
    total = sets * (a * p1 + b * p2)
    f1, f2 = rng.sample(["nasi lemak", "mee goreng", "roti john", "fishball noodles"], 2)
    q = (f"{who} bought some packets of {f1} at ${p1} each and some packets of {f2} at ${p2} each. "
         f"For every {a} packet{'s' if a > 1 else ''} of {f1}, {he(who)} bought {b} packet{'s' if b > 1 else ''} of {f2}. "
         f"{he(who).capitalize()} spent ${total} altogether. How many packets of food did {he(who)} buy altogether?")
    x, y = syms("x y")
    return q, sets * (a + b), [sp.Eq(b * x, a * y), sp.Eq(p1 * x + p2 * y, total)], x + y, [x, y], "P6"


# --- excess-shortage
@template("excess_shortage")
def es_pupils_left_need(rng):
    who = rng.choice(ADULTS)
    a, b = sorted(rng.sample(range(2, 9), 2))
    n = rng.randint(6, 30)
    stock = a * n + rng.randint(1, (b - a) * n - 1)
    left, need = stock - a * n, b * n - stock
    item = rng.choice(["pencils", "stickers", "sweets", "erasers"])
    q = (f"{who} has some {item} for {his(who)} pupils. If {he(who)} gives each pupil {a} {item}, {he(who)} will have {left} {item} left. "
         f"If {he(who)} gives each pupil {b} {item}, {he(who)} will need {need} more {item}. How many pupils are there?")
    x, s = syms("x s")
    return q, n, [sp.Eq(s, a * x + left), sp.Eq(s, b * x - need)], x, [x, s], "P4"


@template("excess_shortage")
def es_both_left(rng):
    who = rng.choice(ADULTS)
    a, b = sorted(rng.sample(range(2, 9), 2))
    n = rng.randint(3, 15)
    l2 = rng.randint(1, 10)
    stock = b * n + l2
    l1 = stock - a * n
    item = rng.choice(["kueh", "curry puffs", "goodie bags", "pieces of cake"])
    q = (f"{who} bought some {item} for a party. If each guest gets {a} {item}, there will be {l1} {item} left over. "
         f"If each guest gets {b} {item}, there will be {l2} {item} left over. How many {item} did {he(who)} buy?")
    x, s = syms("x s")
    return q, stock, [sp.Eq(s, a * x + l1), sp.Eq(s, b * x + l2)], s, [x, s], "P5"


@template("excess_shortage")
def es_sweets_shortage(rng):
    a, b = sorted(rng.sample(range(3, 11), 2))
    n = rng.randint(5, 25)
    stock = a * n + rng.randint(1, (b - a) * n - 1)
    left, short = stock - a * n, b * n - stock
    item = rng.choice(["sweets", "chocolates", "biscuits"])
    q = (f"Some children shared a bag of {item}. If each child took {a} {item}, there would be {left} {item} left. "
         f"If each child took {b} {item}, there would be a shortage of {short} {item}. How many {item} were in the bag?")
    x, s = syms("x s")
    return q, stock, [sp.Eq(s, a * x + left), sp.Eq(s, b * x - short)], s, [x, s], "P5"


@template("excess_shortage")
def es_money(rng):
    who = rng.choice(ADULTS)
    p1 = Fraction(rng.choice([2, 3, 4, 5, 6]))
    p2 = p1 + Fraction(rng.choice([1, 2, 5]), 2)
    n = rng.randint(6, 40)
    left = rng.randint(1, 30)
    have = n * p1 + left
    short = n * p2 - have
    if short <= 0 or clean(short) is None:
        raise ValueError("retry")
    item = rng.choice(["cupcakes", "notebooks", "plants", "tickets"])
    q = (f"{who} wanted to buy some {item} for {his(who)} class. If {he(who)} bought the {item} at {money(p1)} each, {he(who)} would have ${left} left. "
         f"If {he(who)} bought the same number of {item} at {money(p2)} each, {he(who)} would be short of {money(short)}. How much money did {who} have?")
    x, m = syms("x m")
    return q, have, [sp.Eq(m, R(p1) * x + left), sp.Eq(m, R(p2) * x - R(short))], m, [x, m], "P6"


# --- assumption
@template("assumption")
def as_legs(rng):
    a2, a4 = rng.choice([("chickens", "goats"), ("ducks", "rabbits"), ("chickens", "cows"), ("ducks", "dogs")])
    n4 = rng.randint(2, 30)
    n2 = rng.randint(2, 30)
    q = (f"There are {n2 + n4} {a2} and {a4} on a farm. They have {2 * n2 + 4 * n4} legs altogether. "
         f"A {a2[:-1]} has 2 legs and a {a4[:-1]} has 4 legs. How many {rng.choice([a2, a4]) if False else a4} are there?")
    x, y = syms("x y")
    return q, n4, [sp.Eq(x + y, n2 + n4), sp.Eq(2 * x + 4 * y, 2 * n2 + 4 * n4)], y, [x, y], "P4"


@template("assumption")
def as_quiz(rng):
    name = rng.choice(NAMES)
    total = rng.choice([10, 15, 20, 25, 30])
    gain, lose = rng.randint(3, 6), rng.randint(1, 3)
    right = rng.randint(total // 2, total)
    score = gain * right - lose * (total - right)
    q = (f"In a quiz of {total} questions, a pupil gets {gain} points for each correct answer and loses {lose} point{'s' if lose > 1 else ''} for each wrong answer. "
         f"{name} answered all {total} questions and scored {score} points. How many questions did {he(name)} answer correctly?")
    x, y = syms("x y")
    return q, right, [sp.Eq(x + y, total), sp.Eq(gain * x - lose * y, score)], x, [x, y], "P5"


@template("assumption")
def as_tickets(rng):
    pa, pc = rng.choice([(12, 7), (10, 6), (15, 9), (8, 5), (20, 12)])
    n = rng.randint(20, 60)
    kids = rng.randint(3, n - 3)
    total = pa * (n - kids) + pc * kids
    q = (f"A shop sold {n} tickets for a show. Each adult ticket cost ${pa} and each child ticket cost ${pc}. "
         f"The shop collected ${total} altogether. How many child tickets were sold?")
    x, y = syms("x y")
    return q, kids, [sp.Eq(x + y, n), sp.Eq(pa * x + pc * y, total)], y, [x, y], "P5"


@template("assumption")
def as_notes(rng):
    who = rng.choice(ADULTS)
    a, b = rng.choice([(2, 5), (2, 10), (5, 10), (1, 5)])
    n = rng.randint(10, 40)
    nb = rng.randint(2, n - 2)
    val = a * (n - nb) + b * nb
    kind = "notes" if a >= 2 else "coins and notes"
    q = (f"{who} has {n} notes. Some are ${a} notes and the rest are ${b} notes. The total value of the notes is ${val}. "
         f"How many ${b} notes does {he(who)} have?")
    if a == 1:
        q = q.replace("$1 notes", "$1 coins").replace(f"has {n} notes", f"has {n} coins and notes").replace("value of the notes", "value of the coins and notes")
    x, y = syms("x y")
    return q, nb, [sp.Eq(x + y, n), sp.Eq(a * x + b * y, val)], y, [x, y], "P5"


@template("assumption")
def as_late_parcels(rng):
    name = rng.choice([n for n in NAMES if n in HE])
    pay, fine = rng.choice([(4, 1), (3, 1), (5, 2), (6, 2)])
    n = rng.randint(30, 80)
    late = rng.randint(2, n // 4)
    earned = pay * (n - late) - fine * late
    q = (f"A delivery rider is paid ${pay} for each parcel he delivers on time. For each parcel he delivers late, he is not paid and ${fine} is taken from his pay. "
         f"Last week, {name} delivered {n} parcels and was paid ${earned}. How many parcels did he deliver late?")
    x, y = syms("x y")
    return q, late, [sp.Eq(x + y, n), sp.Eq(pay * x - fine * y, earned)], y, [x, y], "P6"


# --- average
@template("average")
def av_joins(rng):
    n = rng.randint(3, 8)
    a = rng.randint(30, 50)
    b = a + rng.randint(1, 4)
    new = (n + 1) * b - n * a
    grp, unit = rng.choice([("boys", "kg"), ("girls", "kg"), ("players", "kg")])
    name = rng.choice(NAMES)
    q = (f"The average mass of {n} {grp} is {a} kg. When {name} joined them, the average mass of the {n + 1} {grp} became {b} kg. "
         f"What is {name}'s mass?")
    x = syms("x")
    return q, new, [sp.Eq(n * a + x, (n + 1) * b)], x, [x], "P5"


@template("average")
def av_leaves(rng):
    n = rng.randint(4, 9)
    a = rng.randint(10, 20)
    b = a - rng.randint(1, 3)
    left = n * a - (n - 1) * b
    test = rng.choice(["a spelling test", "a maths quiz", "a science test"])
    q = (f"The average score of {n} pupils in {test} was {a}. When one pupil left the group, the average score of the remaining {n - 1} pupils became {b}. "
         f"What was the score of the pupil who left?")
    x = syms("x")
    return q, left, [sp.Eq(n * a - x, (n - 1) * b)], x, [x], "P5"


@template("average")
def av_combined(rng):
    who = rng.choice(ADULTS)
    n1, n2 = rng.randint(2, 5), rng.randint(2, 5)
    a1, a2 = rng.randint(15, 40), rng.randint(10, 35)
    tot = n1 * a1 + n2 * a2
    avg = Fraction(tot, n1 + n2)
    if clean(avg) is None:
        raise ValueError("retry")
    q = (f"{who}'s {n1} children saved an average of ${a1} each. {his(who).capitalize()} {n2} nephews saved an average of ${a2} each. "
         f"What was the average amount saved by the {n1 + n2} children?")
    x = syms("x")
    return q, avg, [sp.Eq(x * (n1 + n2), tot)], x, [x], "P5"


@template("average")
def av_group_size(rng):
    a = rng.randint(55, 80)
    rise = rng.randint(2, 5)
    n = rng.randint(4, 12)
    new = a + rise * (n + 1)
    if new > 100:
        raise ValueError("retry")
    q = (f"A group of pupils had an average score of {a} in a test. When a new pupil who scored {new} joined the group, "
         f"the average score went up to {a + rise}. How many pupils were in the group at first?")
    x = syms("x")
    return q, n, [sp.Eq(a * x + new, (a + rise) * (x + 1))], x, [x], "P6"


@template("average")
def av_heights(rng):
    n = rng.randint(6, 10)
    a = rng.randint(140, 160)
    k = 2
    ak = a - rng.randint(2, 8)
    b = a + rng.randint(1, 5)
    new = (n - k + 1) * b - (n * a - k * ak)
    if not 120 < new < 200:
        raise ValueError("retry")
    q = (f"The average height of {n} players in a team was {a} cm. Two players with an average height of {ak} cm left the team, and one new player joined. "
         f"The average height of the {n - 1} players became {b} cm. What is the height of the new player?")
    x = syms("x")
    return q, new, [sp.Eq(n * a - k * ak + x, (n - 1) * b)], x, [x], "P6"


# ---------------------------------------------------------------- driver
def make(fn, rng) -> dict | None:
    try:
        q, built, eqs, target, symbols, level = fn(rng)
        truth = solve(eqs, target, symbols)
    except (ValueError, ZeroDivisionError, IndexError, KeyError):
        return None
    if Fraction(built) != truth:
        raise AssertionError(f"{fn.__name__}: built {built} but SymPy solved {truth}: {q}")
    ans = int(truth) if truth.denominator == 1 else float(truth)
    return {"question": " ".join(q.split()), "answer": ans, "level": level, "template": fn.__name__}


def generate(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    held_out = {" ".join(x["question"].split()).lower()
                for x in json.loads((ROOT / "evals" / "dataset" / "v2-families.json").read_text())}
    seen: set[str] = set()
    per_family = n // len(TEMPLATES)
    items: list[dict] = []
    for family, fns in TEMPLATES.items():
        got, tries = 0, 0
        while got < per_family and tries < per_family * 200:
            tries += 1
            fn = fns[got % len(fns)] if tries < per_family * 50 else rng.choice(fns)
            item = make(fn, rng)
            if not item or item["question"].lower() in seen or item["question"].lower() in held_out:
                continue
            seen.add(item["question"].lower())
            items.append({"id": f"synth-{family}-{got:04d}", "heuristic": family, **item})
            got += 1
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(OUT / "problems.jsonl"))
    args = ap.parse_args()
    items = generate(args.n, args.seed)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    c = Counter(i["heuristic"] for i in items)
    print(f"{len(items)} problems -> {args.out}")
    for fam, k in sorted(c.items()):
        print(f"  {fam:20} {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
