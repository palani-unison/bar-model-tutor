"""Bar-model whiteboard renderer.

Turns a bar-model action timeline (the JSON Muse would produce) into a
hand-drawn "teacher at the whiteboard" MP4 with narration.
v1 specs (a `model` field) and v2 specs (`"version": 2`, docs/spec-v2.md) both render.

Usage: python3 render.py marbles.json out.mp4
       python3 render.py marbles.json board.png     # the finished board as a still, no audio
"""
import functools, itertools, json, math, random, subprocess, sys, wave, io, os
import numpy as np
import sympy as sp
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from agent.verify import unit_value_of, verify_spec  # noqa: E402
from agent.spec_v2 import SpecError, parse_expr, solve_spec  # noqa: E402

W, H, S, FPS = 1280, 720, 2, 30          # output size, supersample, fps
BG = (251, 250, 245)
INK = (31, 42, 68)          # dark marker
BLUE = (37, 99, 235)
RED = (220, 38, 38)
GREEN = (22, 128, 61)
YELLOW = (255, 221, 64, 120)
F_HAND = os.path.join(HERE, "fonts/PatrickHand-Regular.ttf")
F_CAV = os.path.join(HERE, "fonts/Caveat.ttf")
VOICE = os.path.join(HERE, "voice.onnx")


@functools.lru_cache(maxsize=None)
def font(path, size):
    return ImageFont.truetype(path, size * S)


# ---------------------------------------------------------------- geometry
def wobbly(p0, p1, seed, amp=1.3, step=6):
    """Points along p0->p1 with a gentle hand-drawn wobble and tiny overshoot."""
    rnd = random.Random(seed)
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    over = rnd.uniform(2, 6)
    x0, y0 = x0 - ux * rnd.uniform(0, 3), y0 - uy * rnd.uniform(0, 3)
    x1, y1 = x1 + ux * over, y1 + uy * over
    n = max(2, int(L / step))
    ph1, ph2 = rnd.uniform(0, 6.3), rnd.uniform(0, 6.3)
    f1, f2 = rnd.uniform(0.6, 1.4), rnd.uniform(2, 3.5)
    pts = []
    for i in range(n + 1):
        t = i / n
        off = amp * (math.sin(t * math.pi * f1 + ph1) + 0.4 * math.sin(t * math.pi * f2 + ph2))
        pts.append((x0 + (x1 - x0) * t + nx * off, y0 + (y1 - y0) * t + ny * off))
    return pts


def brace_v(x, y0, y1, w=18):
    """Vertical curly brace opening left, tip pointing right."""
    pts, ym = [], (y0 + y1) / 2
    for i in range(41):
        t = i / 40
        y = y0 + (y1 - y0) * t
        d = abs(y - ym) / ((y1 - y0) / 2)
        xx = x + w * 0.35 * math.sin(math.pi * min(1, t * 2 if t < .5 else (1 - t) * 2)) + w * 0.65 * max(0, 1 - d * 8)
        pts.append((xx, y))
    return pts


def brace_h(y, x0, x1, h=18):
    """Horizontal curly brace below something, tip pointing down."""
    pts, xm = [], (x0 + x1) / 2
    for i in range(61):
        t = i / 60
        x = x0 + (x1 - x0) * t
        d = abs(x - xm) / ((x1 - x0) / 2)
        yy = y + h * 0.35 * math.sin(math.pi * min(1, t * 2 if t < .5 else (1 - t) * 2)) + h * 0.65 * max(0, 1 - d * 10)
        pts.append((x, yy))
    return pts


def plen(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def partial(pts, frac):
    """First `frac` of a polyline by length."""
    if frac >= 1:
        return pts
    target, acc, out = plen(pts) * frac, 0, [pts[0]]
    for i in range(len(pts) - 1):
        seg = math.dist(pts[i], pts[i + 1])
        if acc + seg >= target:
            r = (target - acc) / seg if seg else 0
            out.append((pts[i][0] + (pts[i + 1][0] - pts[i][0]) * r,
                        pts[i][1] + (pts[i + 1][1] - pts[i][1]) * r))
            return out
        acc += seg
        out.append(pts[i + 1])
    return out


# ---------------------------------------------------------------- elements
class Stroke:
    def __init__(self, pts, color=INK, width=4):
        self.pts, self.color, self.width = pts, color, width
        self.len = plen(pts)

    def draw(self, img, frac):
        d = ImageDraw.Draw(img)
        p = [(x * S, y * S) for x, y in partial(self.pts, frac)]
        if len(p) >= 2:
            d.line(p, fill=self.color, width=self.width * S, joint="curve")
            r = self.width * S / 2
            for q in (p[0], p[-1]):
                d.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=self.color)
        return p[-1][0] / S, p[-1][1] / S


class Text:
    """Text revealed left-to-right like it is being written. `halo` keeps it readable over hatching."""
    def __init__(self, xy, txt, fnt, color=INK, anchor="ls", halo=0):
        self.xy, self.txt, self.fnt, self.color, self.anchor, self.halo = xy, txt, fnt, color, anchor, halo
        l, t, r, b = fnt.getbbox(txt, anchor=anchor)
        self.box = (xy[0] * S + l, xy[1] * S + t, xy[0] * S + r, xy[1] * S + b)
        self.len = (r - l) / S * 1.6   # writing "length" for timing

    def draw(self, img, frac):
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        halo = {"stroke_width": self.halo * S, "stroke_fill": BG} if self.halo else {}
        ImageDraw.Draw(layer).text((self.xy[0] * S, self.xy[1] * S), self.txt,
                                   font=self.fnt, fill=self.color, anchor=self.anchor, **halo)
        x0, y0, x1, y1 = self.box
        cut = int(x0 + (x1 - x0 + 4 * S) * min(1, frac))
        if frac < 1:
            layer.paste((0, 0, 0, 0), (cut, 0, img.size[0], img.size[1]))
        img.alpha_composite(layer)
        return cut / S, y1 / S


class Highlight:
    def __init__(self, box, redraw=()):
        self.box, self.len, self.redraw = box, (box[2] - box[0]) * 1.2, redraw

    def draw(self, img, frac):
        x0, y0, x1, y1 = self.box
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        xe = x0 + (x1 - x0) * min(1, frac)
        ImageDraw.Draw(layer).rounded_rectangle([x0 * S, y0 * S, xe * S, y1 * S], radius=6 * S, fill=YELLOW)
        img.alpha_composite(layer)
        for t in self.redraw:            # keep the ink on top of the highlighter
            t.draw(img, 1)
        return xe, (y0 + y1) / 2


# ---------------------------------------------------------------- layout
DIVIDER_X = 750
BRACE_ROOM = 62                     # right edge of the bars to the total's label
WORK_W = 440
BOARD_BOTTOM = 600
class Board:
    def __init__(self, spec):
        self.spec = spec
        self.f_q = font(F_HAND, 34)
        self.f_lab = font(F_CAV, 46)
        self.f_cap = font(F_HAND, 28)
        self.f_title = font(F_HAND, 24)
        self.bars = {b["id"]: b for b in spec["model"]["bars"]}
        bars = spec["model"]["bars"]
        self.unit_value, _ = unit_value_of(spec["model"])
        self.bx = 170                     # bars start x
        self.bh = 64                      # bar height
        # Bars stay left of the divider, leaving room for the total's brace and label.
        total_labels = [a["text"] for a in spec["timeline"] if a["do"] == "bracket_total"]
        label_w = max((self.f_lab.getlength(t) / S for t in total_labels), default=0)
        area_w = DIVIDER_X - 10 - self.bx - BRACE_ROOM - label_w
        self.unit_w = min(150, area_w / max(b["units"] + self.extra_units(b) for b in bars))
        # Leave room for the "?" under a bar, and keep the last bar above the caption.
        self.bar_step = min(144, (BOARD_BOTTOM - self.bh - 285) / max(1, len(bars) - 1))
        self.by = {b["id"]: 285 + i * self.bar_step for i, b in enumerate(bars)}
        self.work_y = 270
        # Working and answer share the right-hand column, shrinking to fit it.
        steps = spec["timeline"]
        lines = [a["text"] for a in steps if a["do"] == "write"]
        answers = [a["text"] for a in steps if a["do"] == "answer"]
        self.work_step = min(68, (BOARD_BOTTOM - self.work_y - 40) / max(1, len(lines) + 1))
        self.f_work = self.fit_font(lines, 50, min(50, int(self.work_step * .9)))
        self.f_answer = self.fit_font(answers, 54, 54)
        self.q_lines = self.wrap(spec["question"], self.f_q, 1140)
        self.seed = 7

    def fit_font(self, texts, size, limit, width=WORK_W):
        """Largest font up to `limit` where every text fits the working column."""
        size = min(size, limit)
        while size > 20 and any(font(F_CAV, size).getlength(t) / S > width for t in texts):
            size -= 2
        return font(F_CAV, size)

    def wrap(self, text, fnt, maxw):
        lines, cur = [], ""
        for w in text.split():
            t = (cur + " " + w).strip()
            if fnt.getlength(t) / S > maxw and cur:
                lines.append(cur)
                cur = w
            else:
                cur = t
        return lines + [cur]

    def extra_units(self, b):
        """Width of a bar's extra amount, in units: true to scale, but never too thin or too long to read."""
        if not b.get("extra"):
            return 0
        return min(2.5, max(0.6, b["extra"] / self.unit_value))

    def bar_rect(self, bid):
        b = self.bars[bid]
        x0, y0 = self.bx, self.by[bid]
        return x0, y0, x0 + (b["units"] + self.extra_units(b)) * self.unit_w, y0 + self.bh

    def right_edge(self):
        return max(self.bar_rect(bid)[2] for bid in self.bars)

    def elements(self, a):
        """Return list of drawable elements for an action."""
        self.seed += 1
        k, sd = a["do"], self.seed * 31
        if k == "write_question":
            return [Text((60, 120 + i * 44), ln, self.f_q) for i, ln in enumerate(self.q_lines)]
        if k == "highlight":
            for i, ln in enumerate(self.q_lines):
                j = ln.find(a["text"])
                if j >= 0:
                    x0 = 60 + self.f_q.getlength(ln[:j]) / S
                    x1 = x0 + self.f_q.getlength(a["text"]) / S
                    y = 120 + i * 44
                    return [Highlight((x0 - 4, y - 30, x1 + 4, y + 8),
                                      [Text((60, 120 + i * 44), ln, self.f_q)])]
            return []
        if k == "draw_bar":
            x0, y0, x1, y1 = self.bar_rect(a["id"])
            bar = self.bars[a["id"]]
            out = [Text((x0 - 20, (y0 + y1) / 2 + 14), bar["label"], self.f_lab, BLUE, anchor="rs"),
                   Stroke(wobbly((x0, y0), (x1, y0), sd)),
                   Stroke(wobbly((x1, y0), (x1, y1), sd + 1)),
                   Stroke(wobbly((x1, y1), (x0, y1), sd + 2)),
                   Stroke(wobbly((x0, y1), (x0, y0), sd + 3))]
            if bar.get("extra"):
                # The extra amount sits after the units, split off and labelled with its value.
                xu = x0 + bar["units"] * self.unit_w
                out += [Stroke(wobbly((xu, y0 + 2), (xu, y1 - 2), sd + 4, amp=0.8), BLUE, 3),
                        Text(((xu + x1) / 2, (y0 + y1) / 2 + 14), f"{bar['extra']:g}", font(F_CAV, 38), BLUE, anchor="ms")]
            return out
        if k == "divide_units":
            out = [Stroke(wobbly((bx0 + i * self.unit_w, by0 + 2), (bx0 + i * self.unit_w, by1 - 2), sd + j * 10 + i, amp=0.8), RED, 3)
                   for j, bar in enumerate(self.bars)
                   for bx0, by0, _, by1 in [self.bar_rect(bar)]
                   for i in range(1, self.bars[bar]["units"])]
            # little "u" marks showing equal units, across every bar
            for bid, b in self.bars.items():
                bx0, by0, _, _ = self.bar_rect(bid)
                for i in range(b["units"]):
                    cx = bx0 + (i + .5) * self.unit_w
                    out.append(Text((cx, by0 + self.bh / 2 + 12), "u", font(F_CAV, 34), RED, anchor="ms"))
            return out
        if k == "bracket_total":
            top = min(self.by.values())
            bot = max(self.by.values()) + self.bh
            x = self.right_edge() + 22
            return [Stroke(brace_v(x, top, bot, 26), GREEN, 4),
                    Text((x + 40, (top + bot) / 2 + 16), a["text"], self.f_lab, GREEN)]
        if k == "bracket_difference":
            diff = self.spec["model"]["difference"]
            xs = self.bar_rect(diff["less"])[2]
            x0, y0, xl, _ = self.bar_rect(diff["more"])
            return [Stroke(brace_h(y0 - 8, xs, xl, -22), GREEN, 4),
                    Text(((xs + xl) / 2, y0 - 40), a["text"], font(F_CAV, 40), GREEN, anchor="ms")]
        if k == "question_mark":
            x0, y0, x1, y1 = self.bar_rect(a["id"])
            # With little room above the next bar, a smaller "?" clears the brace.
            gap = self.bar_step - self.bh
            size, drop = (56, 72) if gap >= 80 else (32, gap - 3)
            return [Stroke(brace_h(y1 + 8, x0, x1, 22), RED, 4),
                    Text(((x0 + x1) / 2, y1 + drop), "?", font(F_HAND, size), RED, anchor="ms")]
        if k == "label_unit":
            x0, y0, x1, y1 = self.bar_rect(a["id"])
            return [Text(((x0 + x0 + self.unit_w) / 2, y0 - 12), a["text"], font(F_CAV, 40), BLUE, anchor="ms")]
        if k == "write":
            y = self.work_y
            self.work_y += self.work_step
            return [Text((790, y), a["text"], self.f_work)]
        if k == "answer":
            y = self.work_y + 20
            t = Text((790, y), a["text"], self.f_answer, GREEN)
            x0, y0, x1, y1 = [v / S for v in t.box]
            return [t, Stroke(wobbly((x0 - 12, y0 - 10), (x1 + 12, y0 - 10), sd, 1), GREEN, 3),
                    Stroke(wobbly((x1 + 12, y0 - 10), (x1 + 12, y1 + 10), sd + 1, 1), GREEN, 3),
                    Stroke(wobbly((x1 + 12, y1 + 10), (x0 - 12, y1 + 10), sd + 2, 1), GREEN, 3),
                    Stroke(wobbly((x0 - 12, y1 + 10), (x0 - 12, y0 - 10), sd + 3, 1), GREEN, 3)]
        return []

    def background(self, divider=(230, 600)):
        img = Image.new("RGBA", (W * S, H * S), BG + (255,))
        d = ImageDraw.Draw(img)
        rnd = np.random.default_rng(1)
        # faint board smudges
        for _ in range(40):
            x, y, r = rnd.integers(0, W * S), rnd.integers(0, H * S), rnd.integers(40, 200)
            d.ellipse([x - r, y - r, x + r, y + r], fill=(249, 248, 242, 255))
        d.rectangle([0, 0, W * S - 1, H * S - 1], outline=(200, 196, 185), width=10 * S)
        d.text((60 * S, 62 * S), self.spec["title"] + "  ·  " + self.spec["level"],
               font=self.f_title, fill=(130, 130, 140), anchor="ls")
        d.line([(60 * S, 78 * S), (1220 * S, 78 * S)], fill=(225, 222, 212), width=2 * S)
        if divider:
            d.line([(750 * S, divider[0] * S), (750 * S, divider[1] * S)], fill=(225, 222, 212), width=2 * S)
        return img


# ---------------------------------------------------------------- v2 layout (docs/spec-v2.md)
BX = 200                          # v2 bars start x; bar names are right-aligned at BX - 20
WORK_X = 790                      # the working column, as in v1
GREY = (120, 120, 130)            # stage labels
HATCH = (170, 160, 140)           # a marked part's hatching
TINT = (37, 99, 235, 34)          # an added part's light blue
MIN_BH, MAX_BH = 36, 64           # bar height shrinks with the number of bars
PAD = 6                           # kept above and below every bar
STAGE_ROOM, STAGE_GAP = 32, 10    # a stage's label above its first bar; extra gap between stages
TEXT_ROOM, BRACKET_ROOM, QMARK_ROOM = 30, 48, 54   # one layer of labels / a brace and text / a brace and "?"
MIN_K = 0.6                       # rooms (and their fonts) shrink to this when the board is tight
MAX_GAP = 24                      # spare height added between bars, at most this much per gap
LABEL_SIZE = 28                   # a segment label outside its segment


class LayoutError(ValueError):
    """The spec can't be drawn on the board. The message says why, for the planner's retry."""


class Fill:
    """A translucent fill laid down left to right."""
    def __init__(self, box, color):
        self.box, self.color, self.len = box, color, (box[2] - box[0]) * 0.4

    def draw(self, img, frac):
        x0, y0, x1, y1 = self.box
        xe = x0 + (x1 - x0) * min(1, frac)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).rectangle([x0 * S, y0 * S, xe * S, y1 * S], fill=self.color)
        img.alpha_composite(layer)
        return xe, (y0 + y1) / 2


def dashed(p0, p1, seed, color=INK, width=3, dash=10, gap=7):
    """A hand-drawn dashed line, as short strokes."""
    (x0, y0), (x1, y1) = p0, p1
    L = math.dist(p0, p1) or 1
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    rnd, out, t = random.Random(seed), [], 0
    while t < L:
        e, j = min(L, t + dash), rnd.uniform(-0.8, 0.8)
        out.append(Stroke([(x0 + ux * t - uy * j, y0 + uy * t + ux * j),
                           (x0 + ux * e - uy * j, y0 + uy * e + ux * j)], color, width))
        t += dash + gap
    return out


def hatch(box, gap=11):
    """Diagonal hatching inside a box."""
    x0, y0, x1, y1 = box
    out, c = [], x0 + y0 + gap / 2
    while c < x1 + y1:
        xa, xb = max(x0, c - y1), min(x1, c - y0)
        if xb - xa > 3:
            out.append(Stroke([(xa, c - xa), (xb, c - xb)], HATCH, 2))
        c += gap
    return out


def cubic(p0, p1, p2, p3, n=40):
    return [tuple((1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t * t * c + t ** 3 * d
                  for a, b, c, d in zip(p0, p1, p2, p3)) for t in (i / n for i in range(n + 1))]


def unit_count(expr, names):
    """'3*u' -> (3, 'u'), 'u' -> (1, 'u'). Anything else (20, u + 4, u/2) -> None."""
    try:
        e = parse_expr(expr, {n: sp.Symbol(n, real=True) for n in names})
    except SpecError:
        return None
    if len(e.free_symbols) != 1:
        return None
    sym = next(iter(e.free_symbols))
    k = sp.simplify(e / sym)
    return (int(k), sym.name) if k.is_Integer and k > 0 else None


def extent(e):
    """An element's box on the board, in px."""
    if isinstance(e, Stroke):
        xs, ys, r = [p[0] for p in e.pts], [p[1] for p in e.pts], e.width / 2
        return min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r
    if isinstance(e, Text):
        return tuple(v / S for v in e.box)
    return e.box


class BoardV2(Board):
    """A v2 board: stages of bars, true to scale from solve_spec's segment values (docs/spec-v2.md §5)."""

    def __init__(self, spec):
        self.spec = spec
        self.f_q = font(F_HAND, 34)
        self.f_cap = font(F_HAND, 28)
        self.f_title = font(F_HAND, 24)
        self.f_brk = font(F_CAV, 40)          # a right brace's label (a total)
        self.q_lines = self.wrap(spec["question"], self.f_q, 1140)
        self.seed, self.step = 7, -1
        self.top = 120 + 44 * (len(self.q_lines) - 1) + 52        # the question's bottom + 40
        steps = spec["timeline"]
        self.stages = {s["id"]: s for s in spec.get("stages") or []}
        self.bars = {b["id"]: b for s in spec.get("stages") or [] for b in s["bars"]}
        if self.bars:
            try:
                solution = solve_spec(spec)
            except SpecError as exc:
                raise LayoutError(f"the bars can't be drawn because the spec doesn't solve: {exc}") from exc
            self.unit_values, self.values = solution.units, solution.segments
            self.layout_x(steps)
            self.layout_y(steps)
            self.percent_tags = self.layout_percent(steps)
        tables = [a for a in steps if a["do"] == "table"]
        lines = [a for a in steps if a["do"] == "number_line"]
        if (tables or lines) and self.bars:
            raise LayoutError("a table or number line and bars don't fit on one board; use the working template")
        if len(tables) + len(lines) > 1:
            raise LayoutError("the board has room for one table or number line")
        self.table = self.layout_table(tables[0], steps) if tables else None
        self.line = self.layout_line(lines[0], steps) if lines else None
        # Working and answer: the right-hand column, or the full width under the question with no picture.
        self.picture = bool(self.bars or self.table or self.line)
        self.work_x, work_w = (WORK_X, WORK_W) if self.picture else (60, W - 120)
        self.work_w = work_w
        self.work_y = self.top + 40
        lines = [a.get("text", "") for a in steps if a["do"] == "write"]
        answers = [a.get("text", "") for a in steps if a["do"] == "answer"]
        self.work_step = min(68, (BOARD_BOTTOM - self.work_y - 40) / max(1, len(lines) + 1))
        self.f_work = self.fit_font(lines, 50, min(50, int(self.work_step * .9)), work_w)
        self.f_answer = self.fit_font(answers, 54, 54, work_w)

    # ------------------------------------------------------------ layout
    def layout_percent(self, steps):
        """Template F: on a `percentage` board, each bar drawn only in units gets its percentage at its right end,
        grey, the first such bar being 100% ("100%", then "120%" after an increase). Only when the space right of
        the bars is free (no total brace, no transfer arrow) and every tag fits before the divider."""
        if self.spec.get("board") != "percentage" or any(a["do"] == "move_part" or (a["do"] == "bracket" and self.is_right(a))
                                                         for a in steps):
            return {}
        counts = {}
        for bid, units in self.unit_of.items():
            segs = self.bars[bid]["segments"]
            if any(u is None for u, sg in zip(units, segs) if sg.get("style") != "removed") or len({u[1] for u in units if u}) != 1:
                continue
            counts[bid] = sum(u[0] for u, sg in zip(units, segs) if u and sg.get("style") != "removed")
        if not counts:
            return {}
        base = counts[next(iter(counts))]
        tags, f = {}, font(F_CAV, 24)
        for bid, n in counts.items():
            pct = n / base * 100
            if abs(pct * 2 - round(pct * 2)) > 1e-9:
                return {}
            text = f"{pct:g}%"
            x = self.seg_x[bid][-1][1] + 10
            if x + f.getlength(text) / S > DIVIDER_X - 6:
                return {}
            tags[bid] = (x, text)
        return tags

    def layout_x(self, steps):
        """One shared scale across all stages; a drawn length includes removed parts."""
        names = [u["name"] for u in self.spec.get("units") or []]
        totals = [a.get("text", "") for a in steps if a["do"] == "bracket" and self.is_right(a)]
        label_w = max((self.f_brk.getlength(t) / S for t in totals), default=0)
        area = DIVIDER_X - 10 - BX - BRACE_ROOM - label_w
        if area < 200:
            raise LayoutError("the bracket's label is too long to fit beside the bars")
        self.scale = area / max(sum(v) for v in self.values.values())
        self.seg_x, self.unit_of = {}, {}
        for bid, vals in self.values.items():
            x, xs = BX, []
            for i, v in enumerate(vals):
                if v * self.scale < 4:
                    raise LayoutError(f"bar {bid!r} segment {i} is too thin to see when drawn to scale")
                xs.append((x, x + v * self.scale))
                x += v * self.scale
            self.seg_x[bid] = xs
            self.unit_of[bid] = [unit_count(s["expr"], names) for s in self.bars[bid]["segments"]]

    def layout_y(self, steps):
        """Stages top to bottom, each bar with layers above and below it for what the timeline puts there."""
        divided = {a.get("bar") for a in steps if a["do"] == "divide_units"}
        covered = {}                    # bar -> segments under a top brace
        above = {bid: [] for bid in self.bars}
        below = {bid: [] for bid in self.bars}
        for idx, a in enumerate(steps):
            k = a["do"]
            if k == "label_unit":
                above[self.bar_id(a)].append((idx, TEXT_ROOM))
            elif k == "bracket" and not self.is_right(a):
                bid = self.bar_id(a)
                i, j = self.seg_span(a, bid)
                if a.get("side", "top") == "top":
                    covered.setdefault(bid, set()).update(range(i, j + 1))
        # Segment labels: inside if they fit (shrinking), otherwise above (below if a top brace is there,
        # or if the label before it is in the way). Removed parts are crossed out, and divided parts get
        # a "u" per box, so their labels go outside.
        self.seg_label = {}
        for bid, bar in self.bars.items():
            end = {"above": -1e9, "below": -1e9}       # right end of the last outside label on each side
            for i, seg in enumerate(bar["segments"]):
                text = seg.get("label")
                if not text:
                    continue
                x0, x1 = self.seg_x[bid][i]
                size = self.fit_size(text, x1 - x0 - 10, 32, 18)
                if seg.get("style") == "removed" or (bid in divided and self.unit_of[bid][i]) or not size:
                    side = "below" if i in covered.get(bid, ()) else "above"
                    lx0, lx1 = self.label_span(text, x0, x1, LABEL_SIZE)
                    if lx0 < end[side] + 6:
                        side = "below" if side == "above" else "above"
                    end[side] = lx1
                    self.seg_label[(bid, i)] = (side, None)
                else:
                    self.seg_label[(bid, i)] = ("in", size)
            sides = {side for (b, _), (side, _) in self.seg_label.items() if b == bid}
            if "above" in sides:
                above[bid].append(("above", TEXT_ROOM))
            if "below" in sides:
                below[bid].append(("below", TEXT_ROOM))
        for idx, a in enumerate(steps):
            k = a["do"]
            if k == "bracket" and not self.is_right(a):
                (above if a.get("side", "top") == "top" else below)[a["bar"]].append((idx, BRACKET_ROOM))
            elif k == "question_mark":
                bid = self.bar_id(a)
                self.seg_span(a, bid)
                below[bid].append((idx, QMARK_ROOM))

        order = [(si, i, b["id"]) for si, st in enumerate(self.spec["stages"]) for i, b in enumerate(st["bars"])]
        n = len(order)

        def room_above(si, i, bid):
            r = PAD + sum(r for _, r in above[bid])
            return max(STAGE_ROOM, r) if i == 0 else r

        rooms = sum(room_above(si, i, bid) + PAD + sum(r for _, r in below[bid]) for si, i, bid in order)
        rooms += STAGE_GAP * (len(self.spec["stages"]) - 1)
        avail = BOARD_BOTTOM - self.top
        bh, k = max(MIN_BH, MAX_BH - 6 * (n - 1)), 1.0
        if n * bh + rooms > avail:
            bh = max(MIN_BH, int((avail - rooms) / n))
            if n * bh + rooms > avail:
                k = (avail - n * bh) / rooms
                if k < MIN_K:
                    raise LayoutError(f"{n} bars in {len(self.spec['stages'])} stages, with their labels, don't fit "
                                      f"on the board; use fewer bars or stages")
        self.bh, self.k = bh, k
        gap = min(MAX_GAP, (avail - n * bh - rooms * k) / (n - 1)) if n > 1 else 0
        self.by, self.layer = {}, {}
        y = self.top
        for si, i, bid in order:
            if si or i:
                y += gap
            if si and not i:
                y += STAGE_GAP * k
            y += room_above(si, i, bid) * k
            self.by[bid] = y
            off = 0
            for key, r in above[bid]:          # nearest the bar first
                self.layer[(bid, key)] = (y - off, r * k)
                off += r * k
            off = 0
            for key, r in below[bid]:
                self.layer[(bid, key)] = (y + bh + off, r * k)
                off += r * k
            y += bh + (PAD + sum(r for _, r in below[bid])) * k
        self.f_stage = font(F_CAV, max(22, round(30 * k)))

    # ------------------------------------------------------------ lookups
    @staticmethod
    def is_right(a):
        return bool(a.get("bars")) or a.get("side") == "right"

    def bar_id(self, a, key="bar"):
        bid = a.get(key)
        if bid not in self.bars:
            raise LayoutError(f"{a['do']}: there is no bar {bid!r}")
        return bid

    def seg_span(self, a, bid):
        """The [i, j] segments an action covers (all of them when it gives none), checked."""
        n = len(self.bars[bid]["segments"])
        span = a.get("segments")
        if span is None:
            return 0, n - 1
        if (not isinstance(span, list) or len(span) != 2 or not all(isinstance(v, int) for v in span)
                or not 0 <= span[0] <= span[1] < n):
            raise LayoutError(f"{a['do']}: segments {span!r} are out of range for bar {bid!r} ({n} segments)")
        return span[0], span[1]

    def seg_index(self, a, bid, key):
        n, i = len(self.bars[bid]["segments"]), a.get(key)
        if not isinstance(i, int) or not 0 <= i < n:
            raise LayoutError(f"{a['do']}: {key} {i!r} is out of range for bar {bid!r} ({n} segments)")
        return i

    def counted_span(self, bid):
        """x0, x1 of a bar's counted part (removed parts are what left it)."""
        xs = [x for x, s in zip(self.seg_x[bid], self.bars[bid]["segments"]) if s.get("style") != "removed"]
        return (xs[0][0], xs[-1][1]) if xs else (self.seg_x[bid][0][0], self.seg_x[bid][-1][1])

    @staticmethod
    def label_span(text, x0, x1, size):
        """x0, x1 of a label centred on a segment, kept within the bar area."""
        w = font(F_CAV, size).getlength(text) / S
        cx = min(max((x0 + x1) / 2, BX - 4 + w / 2), DIVIDER_X - 12 - w / 2)
        return cx - w / 2, cx + w / 2

    def fit_size(self, text, width, hi, lo):
        """Largest Caveat size from hi down to lo where text fits width, or None."""
        for size in range(hi, lo - 1, -2):
            if font(F_CAV, size).getlength(text) / S <= width:
                return size
        return None

    # ------------------------------------------------------------ drawing
    def elements(self, a):
        """Return list of drawable elements for an action."""
        self.seed += 1
        self.step += 1
        k, sd = a["do"], self.seed * 31
        if k == "write_question":
            return [Text((60, 120 + i * 44), ln, self.f_q) for i, ln in enumerate(self.q_lines)]
        if k == "highlight":
            return self.highlight(a.get("text", ""))
        if k == "pause":
            return []
        if k == "write":
            y = self.work_y
            self.work_y += self.work_step
            return self.write_line(a.get("text", ""), y, sd)
        if k == "answer":
            ts = self.answer_lines(a.get("text", ""))
            x0, y0 = min(t.box[0] for t in ts) / S, min(t.box[1] for t in ts) / S
            x1, y1 = max(t.box[2] for t in ts) / S, max(t.box[3] for t in ts) / S
            return ts + [Stroke(wobbly((x0 - 12, y0 - 10), (x1 + 12, y0 - 10), sd, 1), GREEN, 3),
                    Stroke(wobbly((x1 + 12, y0 - 10), (x1 + 12, y1 + 10), sd + 1, 1), GREEN, 3),
                    Stroke(wobbly((x1 + 12, y1 + 10), (x0 - 12, y1 + 10), sd + 2, 1), GREEN, 3),
                    Stroke(wobbly((x0 - 12, y1 + 10), (x0 - 12, y0 - 10), sd + 3, 1), GREEN, 3)]
        if k == "draw_stage":
            st = self.stages.get(a.get("stage"))
            if st is None:
                raise LayoutError(f"draw_stage: there is no stage {a.get('stage')!r}")
            return [Text((60, self.by[st["bars"][0]["id"]] - 10), st["label"], self.f_stage, GREY)]
        if k == "draw_bar":
            return self.draw_bar(self.bar_id(a), sd)
        if k == "divide_units":
            return self.divide_units(self.bar_id(a), sd)
        if k == "move_part":
            return self.move_part(a)
        if k == "bracket":
            return self.bracket(a, sd)
        if k == "question_mark":
            bid = self.bar_id(a)
            i, j = self.seg_span(a, bid)
            x0, x1 = self.counted_span(bid) if a.get("segments") is None else (self.seg_x[bid][i][0], self.seg_x[bid][j][1])
            yi, r = self.layer[(bid, self.step)]
            h = min(16, r * 0.3)
            size = max(18, min(48, int((r - h - 14) / 0.75)))
            return [Stroke(brace_h(yi + 8, x0 + 2, x1 - 2, h), RED, 4),
                    Text(((x0 + x1) / 2, yi + h + 12 + 0.74 * size), "?", font(F_HAND, size), RED, anchor="ms")]
        if k == "table":
            return self.draw_table(a, sd)
        if k == "fill_cell":
            return self.fill_cell(a)
        if k == "number_line":
            return self.draw_line(a, sd)
        if k == "mark_point":
            return self.mark_point(a)
        if k == "label_unit":
            bid = self.bar_id(a)
            units = self.unit_of[bid]
            i = next((i for i, u in enumerate(units) if u), 0)
            x0, x1 = self.seg_x[bid][i]
            if units[i]:
                x1 = x0 + self.unit_values[units[i][1]] * self.scale
            yi, r = self.layer[(bid, self.step)]
            size = max(18, min(32, int((r - 4) / 0.9)))
            return [Text(((x0 + x1) / 2, yi - 4 - 0.22 * size), a.get("text", ""), font(F_CAV, size), BLUE, anchor="ms")]
        raise LayoutError(f"unknown action {k!r}")

    # ------------------------------------------------------------ table (docs/board-templates.md §4.1)
    def layout_table(self, a, steps):
        """Column widths from the widest text in each column (headers, cells and the values filled in later), one
        font for the whole table, rows sharing the height under the question. Left of the divider, like bars."""
        cols, rows = a.get("columns") or [], a.get("rows") or []
        if not 2 <= len(cols) <= 4 or not 1 <= len(rows) <= 6 or any(len(r) != len(cols) for r in rows):
            raise LayoutError("table: needs 2-4 columns and 1-6 rows of one cell per column")
        grid = [[_cell(v) for v in cols]] + [[_cell(v) for v in r] for r in rows]
        fills = {}
        for f in steps:
            if f["do"] == "fill_cell" and f.get("table") == a.get("id"):
                r, c = f.get("row"), f.get("col")
                if not (isinstance(r, int) and isinstance(c, int) and 0 <= r < len(rows) and 0 <= c < len(cols)):
                    raise LayoutError(f"fill_cell: row {r!r}, col {c!r} is outside table {a.get('id')!r}")
                fills[(r, c)] = _cell(f.get("text", ""))
        x0, x1 = 60, DIVIDER_X - 24
        y0 = self.top + 8
        rh = min(64, (BOARD_BOTTOM - y0) / len(grid))
        if rh < 34:
            raise LayoutError("table: too many rows to fit on the board")
        pad = 16

        def widths(size):
            f = font(F_CAV, size)
            out = []
            for c in range(len(cols)):
                texts = [grid[r][c] for r in range(len(grid))] + [t for (r, cc), t in fills.items() if cc == c]
                out.append(max(f.getlength(t) / S for t in texts) + 2 * pad)
            return out

        size = min(36, int(rh * 0.62))
        while size > 20 and sum(widths(size)) > x1 - x0:
            size -= 2
        ws = widths(size)
        if sum(ws) > x1 - x0:
            raise LayoutError("table: the cells are too wide to fit beside the working; shorten them")
        spare = min(x1 - x0 - sum(ws), 36 * len(cols))
        ws = [w + spare / len(cols) for w in ws]
        xs = [x0]
        for w in ws:
            xs.append(xs[-1] + w)
        return {"id": a.get("id"), "xs": xs, "ys": [y0 + r * rh for r in range(len(grid) + 1)], "size": size,
                "grid": grid}

    def cell_box(self, r, c):
        """Box of a table cell; r = 0 is the header row."""
        t = self.table
        return t["xs"][c], t["ys"][r], t["xs"][c + 1], t["ys"][r + 1]

    def cell_text(self, r, c, text, color):
        x0, y0, x1, y1 = self.cell_box(r, c)
        size = self.fit_size(text, x1 - x0 - 12, self.table["size"], 18) or 18
        t = Text(((x0 + x1) / 2, (y0 + y1) / 2 + 0.3 * size), text, font(F_HAND if text == "?" else F_CAV, size), color,
                 anchor="ms")
        t.cell = (r, c)
        return t

    def draw_table(self, a, sd):
        """A hand-drawn grid, then the headers and the given cells row by row; a "?" cell waits for fill_cell."""
        if self.table is None or a.get("id") != self.table["id"]:
            raise LayoutError("table: the board draws one table")
        xs, ys, grid = self.table["xs"], self.table["ys"], self.table["grid"]
        X0, X1, Y0, Y1 = xs[0], xs[-1], ys[0], ys[-1]
        out = [Stroke(wobbly((X0, Y0), (X1, Y0), sd)), Stroke(wobbly((X1, Y0), (X1, Y1), sd + 1)),
               Stroke(wobbly((X1, Y1), (X0, Y1), sd + 2)), Stroke(wobbly((X0, Y1), (X0, Y0), sd + 3))]
        out.append(Stroke(wobbly((X0, ys[1]), (X1, ys[1]), sd + 4, amp=0.8), INK, 4))  # under the headers
        out += [Stroke(wobbly((X0, y), (X1, y), sd + 5 + n, amp=0.8), INK, 2) for n, y in enumerate(ys[2:-1])]
        out += [Stroke(wobbly((x, Y0), (x, Y1), sd + 20 + n, amp=0.8), INK, 3) for n, x in enumerate(xs[1:-1])]
        for r, row in enumerate(grid):
            for c, text in enumerate(row):
                if text == "?":
                    out.append(self.cell_text(r, c, "?", RED))
                elif text:
                    out.append(self.cell_text(r, c, text, INK))  # filled cells are the blue ones
        return out

    def fill_cell(self, a):
        """Rub out the "?" and write the cell's value in blue."""
        if self.table is None or a.get("table") != self.table["id"]:
            raise LayoutError(f"fill_cell: there is no table {a.get('table')!r}")
        r, c = a.get("row", 0) + 1, a.get("col", 0)
        x0, y0, x1, y1 = self.cell_box(r, c)
        return [Fill((x0 + 4, y0 + 4, x1 - 4, y1 - 4), BG + (255,)),
                self.cell_text(r, c, _cell(a.get("text", "")), BLUE)]

    # ------------------------------------------------------------ number line (docs/board-templates.md §4.2)
    LINE_LABEL, LINE_HOP, LINE_VALUE, LINE_ROW = 28, 44, 84, 38   # label size; jump height; value rows above

    def layout_line(self, a, steps):
        """One axis left of the divider, to scale from start to end, in the middle of the free height. Each mark's
        value (and what mark_point later writes there) gets a slot above the jumps; slots that would touch are
        stepped up a row, so neighbouring values never run together."""
        start, end = a.get("start"), a.get("end")
        if not all(isinstance(v, (int, float)) for v in (start, end)) or not start < end:
            raise LayoutError("number_line: needs numbers start < end")
        x0, x1 = 100, DIVIDER_X - 70
        y = self.top + max(self.LINE_VALUE + 2 * self.LINE_ROW + 30, (BOARD_BOTTOM - self.top) * 0.55)
        if y + 70 > BOARD_BOTTOM:
            raise LayoutError("number_line: no room under the question")
        X = lambda v: x0 + (v - start) / (end - start) * (x1 - x0)  # noqa: E731
        fills = {f.get("at"): _cell(f.get("text", "")) for f in steps
                 if f["do"] == "mark_point" and f.get("line") == a.get("id")}
        slots, rows = {}, []
        for m in sorted((m for m in a.get("marks") or [] if _cell(m.get("value", ""))), key=lambda m: m.get("at", 0)):
            at = m.get("at")
            if not isinstance(at, (int, float)):
                raise LayoutError("number_line: every mark needs a number 'at'")
            texts = [(_cell(m["value"]), self.LINE_LABEL)] + ([(fills[at], self.LINE_LABEL + 2)] if at in fills else [])
            w = max(font(F_CAV, size).getlength(t) / S for t, size in texts)
            cx = min(max(X(at), 28 + w / 2), DIVIDER_X - 14 - w / 2)
            for row in range(3):
                if row == len(rows):
                    rows.append(-1e9)
                if cx - w / 2 > rows[row] + 18:
                    rows[row] = cx + w / 2
                    slots[at] = (cx, y - self.LINE_VALUE - row * self.LINE_ROW)
                    break
            else:
                raise LayoutError("number_line: too many values close together; leave some marks without a value")
        return {"id": a.get("id"), "x": X, "x0": x0, "x1": x1, "y": y, "start": start, "end": end, "slots": slots}

    @staticmethod
    def line_x(text, x, size):
        """A number line text centred on its mark, kept inside the picture's half of the board."""
        w = font(F_CAV, size).getlength(text) / S
        return min(max(x, 28 + w / 2), DIVIDER_X - 14 - w / 2)

    def draw_line(self, a, sd):
        """The axis with an arrow, a tick and a label under each mark, a curved green jump for each span (its label
        above, the arrow at its "to" end), and each mark's value above, joined to its tick by a dotted line
        ("?" waits for mark_point)."""
        L = self.line
        if L is None or a.get("id") != L["id"]:
            raise LayoutError("number_line: the board draws one number line")
        X, y = L["x"], L["y"]
        out = [Stroke(wobbly((L["x0"] - 24, y), (L["x1"] + 30, y), sd)),
               Stroke([(L["x1"] + 18, y - 9), (L["x1"] + 32, y), (L["x1"] + 18, y + 9)], INK, 4)]
        f_lab = font(F_CAV, self.LINE_LABEL)
        for n, m in enumerate(a.get("marks") or []):
            at = m.get("at")
            if not isinstance(at, (int, float)) or not L["start"] <= at <= L["end"]:
                raise LayoutError(f"number_line: mark at {at!r} is off the line")
            x = X(at)
            out.append(Stroke(wobbly((x, y - 12), (x, y + 12), sd + 3 + n, amp=0.5), INK, 4))
            label = _cell(m.get("label", ""))
            if label:
                out.append(Text((self.line_x(label, x, self.LINE_LABEL), y + 20 + 0.75 * self.LINE_LABEL), label, f_lab,
                                INK, anchor="ms"))
            value = _cell(m.get("value", ""))
            if value:
                cx, yv = L["slots"][at]
                out += dashed((x, yv + 10), (x, y - 16), sd + 60 + n, GREY, 2, dash=5, gap=6)
                t = Text((cx, yv), value, font(F_HAND, self.LINE_LABEL) if value == "?" else f_lab,
                         RED if value == "?" else INK, anchor="ms")
                t.cell = ("mark", at)
                out.append(t)
        for n, h in enumerate(a.get("jumps") or []):
            a0, a1 = h.get("from"), h.get("to")
            if not all(isinstance(v, (int, float)) for v in (a0, a1)) or a0 == a1 \
                    or not (L["start"] <= min(a0, a1) and max(a0, a1) <= L["end"]):
                raise LayoutError("number_line: a jump is off the line")
            d = 1 if a1 > a0 else -1
            xa, xb = X(a0) + 3 * d, X(a1) - 3 * d
            span = abs(xb - xa)
            hop = min(self.LINE_HOP, 10 + span * 0.35)
            out.append(Stroke(cubic((xa, y - 6), (xa + (xb - xa) * 0.2, y - hop), (xb - (xb - xa) * 0.2, y - hop),
                                    (xb, y - 6)), GREEN, 3))
            out.append(Stroke([(xb - 9 * d, y - 16), (xb, y - 6), (xb - 13 * d, y - 5)], GREEN, 3))
            label = _cell(h.get("label", ""))
            if label:
                size = self.fit_size(label, max(60, span), 26, 18) or 18
                out.append(Text(((xa + xb) / 2, y - hop * 0.75 - 10), label, font(F_CAV, size), GREEN, anchor="ms"))
        return out

    def mark_point(self, a):
        """Rub out a mark's "?" and write its value in blue, in the same slot."""
        L = self.line
        if L is None or a.get("line") != L["id"] or a.get("at") not in L["slots"]:
            raise LayoutError(f"mark_point: number line {a.get('line')!r} has no value \"?\" at {a.get('at')!r}")
        cx, yv = L["slots"][a["at"]]
        text, size = _cell(a.get("text", "")), self.LINE_LABEL + 2
        t = Text((cx, yv), text, font(F_CAV, size), BLUE, anchor="ms")
        t.cell = ("mark", a["at"])
        q = font(F_CAV, self.LINE_LABEL).getlength("?") / S
        x = L["x"](a["at"])
        return [Fill((min(cx, x) - q - 8, yv - 30, max(cx, x) + q + 8, yv + 9), BG + (255,)), t]

    def answer_lines(self, text):
        """The answer as the live board writes it: one line shrinking to 40, else two lines (46 down to 30)
        rather than tiny writing."""
        maxw, size = self.work_w - 24, 54
        while size > 40 and font(F_CAV, size).getlength(text) / S > maxw:
            size -= 2
        lines = [text]
        if font(F_CAV, size).getlength(text) / S > maxw:
            size = 46
            lines = self.wrap(text, font(F_CAV, size), maxw)
            while size > 30 and len(lines) > 2:
                size -= 2
                lines = self.wrap(text, font(F_CAV, size), maxw)
        y = self.work_y + 20
        if len(lines) > 1 and y + (len(lines) - 1) * size * 1.05 + size * 0.3 + 12 > BOARD_BOTTOM + 10:
            return [Text((self.work_x, y), text, self.f_answer, GREEN)]  # no room for two lines: one smaller line
        return [Text((self.work_x, y + j * size * 1.05), ln, font(F_CAV, size), GREEN) for j, ln in enumerate(lines)]

    def write_line(self, text, y, sd):
        """A line of working. Beat 8's check line ("Check: ... ✓") is green, with the tick drawn by hand
        (the board's fonts have no ✓)."""
        tick = text.rstrip().endswith(("✓", "✔"))
        text = text.rstrip().rstrip("✓✔").rstrip()
        check = tick or text.lower().startswith("check")
        t = Text((self.work_x, y), text, self.f_work, GREEN if check else INK)
        if not tick:
            return [t]
        x, h = t.box[2] / S + 10, self.f_work.size / S
        return [t, Stroke([(x, y - 0.3 * h), (x + 0.18 * h, y - 0.05 * h), (x + 0.5 * h, y - 0.62 * h)], GREEN, 4)]

    def highlight(self, phrase):
        """Yellow highlighter on a phrase of the question, across a line break if it wraps."""
        joined, needle = " ".join(self.q_lines), " ".join(phrase.split())
        j = joined.lower().find(needle.lower()) if needle else -1
        if j < 0:
            raise LayoutError(f"highlight: {phrase!r} is not in the question")
        out, start = [], 0
        for i, ln in enumerate(self.q_lines):
            a0, a1 = max(j, start), min(j + len(needle), start + len(ln))
            if a0 < a1:
                x0 = 60 + self.f_q.getlength(ln[:a0 - start]) / S
                x1 = 60 + self.f_q.getlength(ln[:a1 - start]) / S
                y = 120 + i * 44
                out.append(Highlight((x0 - 4, y - 30, x1 + 4, y + 8), [Text((60, y), ln, self.f_q)]))
            start += len(ln) + 1
        return out

    def draw_bar(self, bid, sd):
        bar, xs, bh = self.bars[bid], self.seg_x[bid], self.bh
        y0, y1 = self.by[bid], self.by[bid] + bh
        mid = (y0 + y1) / 2
        size = min(46, int(bh * 0.9))
        while size > 22 and font(F_CAV, size).getlength(bar["label"]) / S > BX - 20 - 24:
            size -= 2
        if font(F_CAV, size).getlength(bar["label"]) / S > BX - 20 - 16:
            raise LayoutError(f"bar name {bar['label']!r} is too long")
        out = [Text((BX - 20, mid + 0.3 * size), bar["label"], font(F_CAV, size), BLUE, anchor="rs")]
        # Outline: runs of segments drawn alike (plain ink, blue added, dashed removed).
        runs = []
        for i, s in enumerate(bar["segments"]):
            kind = s.get("style") if s.get("style") in ("added", "removed") else "ink"
            if runs and runs[-1][0] == kind:
                runs[-1][2] = i
            else:
                runs.append([kind, i, i])
        inner, marks, labels = [], [], []
        for r, (kind, i0, i1) in enumerate(runs):
            xa, xb, s = xs[i0][0], xs[i1][1], sd + 10 * r
            edges = [((xa, y0), (xb, y0)), ((xb, y0), (xb, y1)), ((xb, y1), (xa, y1))] + ([((xa, y1), (xa, y0))] if r == 0 else [])
            cuts = [((xs[i][1], y0 + 2), (xs[i][1], y1 - 2)) for i in range(i0, i1)]
            if kind == "removed":
                out += [d for n, (p, q) in enumerate(edges) for d in dashed(p, q, s + n)]
                inner += [d for n, (p, q) in enumerate(cuts) for d in dashed(p, q, s + 5 + n)]
            else:
                col = BLUE if kind == "added" else INK
                out += [Stroke(wobbly(p, q, s + n), col) for n, (p, q) in enumerate(edges)]
                inner += [Stroke(wobbly(p, q, s + 5 + n, amp=0.8), col, 3) for n, (p, q) in enumerate(cuts)]
        for i, seg in enumerate(bar["segments"]):
            x0, x1, style = xs[i][0], xs[i][1], seg.get("style", "normal")
            if style == "marked":
                marks += hatch((x0 + 4, y0 + 4, x1 - 4, y1 - 4))
            elif style == "added":
                marks.append(Fill((x0 + 3, y0 + 3, x1 - 3, y1 - 3), TINT))
            elif style == "removed":
                marks += [Stroke(wobbly((x0 + 8, y0 + 8), (x1 - 8, y1 - 8), sd + 40 + i, amp=0.6), RED, 3),
                          Stroke(wobbly((x0 + 8, y1 - 8), (x1 - 8, y0 + 8), sd + 50 + i, amp=0.6), RED, 3)]
            if (bid, i) in self.seg_label:
                labels.append(self.seg_text(bid, i, seg, x0, x1, y0, y1))
        tag = getattr(self, "percent_tags", {}).get(bid)
        if tag:
            labels.append(Text((tag[0], mid + 8), tag[1], font(F_CAV, 24), GREY, anchor="ls"))
        return out + inner + marks + labels

    def seg_text(self, bid, i, seg, x0, x1, y0, y1):
        place, size = self.seg_label[(bid, i)]
        color = BLUE if seg.get("style") == "added" else INK
        cx = (x0 + x1) / 2
        if place == "in":
            size = min(size, int(self.bh * 0.62))
            t = Text((cx, (y0 + y1) / 2 + 0.3 * size), seg["label"], font(F_CAV, size), color, anchor="ms",
                     halo=3 if seg.get("style") == "marked" else 0)
            t.inside = True                     # meant to sit on the bar (check() allows it)
            return t
        yi, r = self.layer[(bid, place)]
        size = max(18, min(LABEL_SIZE, int((r - 4) / 0.9)))
        lx0, lx1 = self.label_span(seg["label"], x0, x1, size)
        y = yi - 4 - 0.22 * size if place == "above" else yi + 4 + 0.68 * size
        return Text(((lx0 + lx1) / 2, y), seg["label"], font(F_CAV, size), color, anchor="ms")

    def divide_units(self, bid, sd):
        """Red dividers, and a small unit name in each box, for every k*u segment of the bar."""
        y0, y1 = self.by[bid], self.by[bid] + self.bh
        lines, names = [], []
        for i, (seg, units) in enumerate(zip(self.bars[bid]["segments"], self.unit_of[bid])):
            if not units:
                continue
            n, name = units
            x0, x1 = self.seg_x[bid][i]
            w = (x1 - x0) / n
            lines += [Stroke(wobbly((x0 + j * w, y0 + 2), (x0 + j * w, y1 - 2), sd + i * 10 + j, amp=0.8), RED, 3)
                      for j in range(1, n)]
            size = min(34, int(self.bh * 0.7), int(w * 0.9))
            if seg.get("style") != "removed" and size >= 16:
                names += [Text((x0 + (j + .5) * w, (y0 + y1) / 2 + 0.3 * size), name, font(F_CAV, size), RED, anchor="ms")
                          for j in range(n)]
        for t in names:
            t.inside = True
        return lines + names

    def move_part(self, a):
        """A curved blue arrow from the removed part to the added part, round the right of the bars."""
        fb, tb = self.bar_id(a, "from"), self.bar_id(a, "to")
        fi, ti = self.seg_index(a, fb, "from_segment"), self.seg_index(a, tb, "to_segment")
        mid = {b: self.by[b] + self.bh / 2 for b in self.bars}
        (fx0, fx1), (tx0, tx1) = self.seg_x[fb][fi], self.seg_x[tb][ti]
        if fb == tb:
            top = self.by[fb] - 4
            pts = cubic(((fx0 + fx1) / 2, top), ((fx0 + fx1) / 2, top - 40), ((tx0 + tx1) / 2, top - 40), ((tx0 + tx1) / 2, top))
        else:
            lo, hi = sorted((mid[fb], mid[tb]))
            reach = max(self.seg_x[b][-1][1] for b in self.bars if lo <= mid[b] <= hi)
            p0, p3 = (fx1 + 6, mid[fb]), (tx1 + 8, mid[tb])
            rx = max(p0[0], p3[0], reach) + 36
            pts = cubic(p0, (rx, p0[1]), (rx, p3[1]), p3)
        (ex, ey), (qx, qy) = pts[-1], pts[-3]
        ang = math.atan2(ey - qy, ex - qx)
        head = [Stroke([(ex - 15 * math.cos(ang + s), ey - 15 * math.sin(ang + s)), (ex, ey)], BLUE, 4) for s in (0.5, -0.5)]
        return [Stroke(pts, BLUE, 4)] + head

    def bracket(self, a, sd):
        text = a.get("text", "")
        if self.is_right(a):
            ids = a.get("bars") or [a.get("bar")]
            for bid in ids:
                self.bar_id({"do": "bracket", "bar": bid})
            top, bot = min(self.by[b] for b in ids), max(self.by[b] for b in ids) + self.bh
            x = max(self.seg_x[b][-1][1] for b in ids) + 22
            return [Stroke(brace_v(x, top, bot, 26), GREEN, 4),
                    Text((x + 40, (top + bot) / 2 + 12), text, self.f_brk, GREEN)]
        bid = self.bar_id(a)
        i, j = self.seg_span(a, bid)
        x0, x1 = self.seg_x[bid][i][0] + 2, self.seg_x[bid][j][1] - 2
        yi, r = self.layer[(bid, self.step)]
        h = min(16, r * 0.33)
        size = max(18, min(34, int((r - h - 13) / 0.62)))
        if a.get("side", "top") == "top":
            return [Stroke(brace_h(yi - 8, x0, x1, -h), GREEN, 4),
                    Text(((x0 + x1) / 2, yi - h - 13), text, font(F_CAV, size), GREEN, anchor="ms")]
        return [Stroke(brace_h(yi + 8, x0, x1, h), GREEN, 4),
                Text(((x0 + x1) / 2, yi + h + 13 + 0.62 * size), text, font(F_CAV, size), GREEN, anchor="ms")]

    def background(self):
        return super().background((self.top, BOARD_BOTTOM) if self.picture else None)

    def check(self, steps):
        """Everything stays on its part of the board, no two labels overlap, and only a segment's own
        label and unit names sit on a bar."""
        bars_box = (16, self.top - 12, DIVIDER_X - 4, BOARD_BOTTOM + 8)
        work_box = (self.work_x - 26, self.top - 12, W - 16, BOARD_BOTTOM + 12)  # the answer box sits 12 left, plus wobble
        bodies = {bid: (BX, self.by[bid], xs[-1][1], self.by[bid] + self.bh) for bid, xs in self.seg_x.items()} if self.bars else {}
        texts = []
        for a, els in steps:
            k = a["do"]
            if k in ("write_question", "highlight", "pause"):
                continue
            bx0, by0, bx1, by1 = work_box if k in ("write", "answer") else bars_box
            for e in els:
                x0, y0, x1, y1 = extent(e)
                if x0 < bx0 or y0 < by0 or x1 > bx1 or y1 > by1:
                    what = f" {e.txt!r}" if isinstance(e, Text) else ""
                    raise LayoutError(f"{k}{what} doesn't fit on the board")
                if isinstance(e, Text) and k not in ("write", "answer"):
                    texts.append((e, (x0 + 3, y0 + 3, x1 - 3, y1 - 3)))
        def meet(p, q):
            return p[0] < q[2] and q[0] < p[2] and p[1] < q[3] and q[1] < p[3]
        for (e, p), (f, q) in itertools.combinations(texts, 2):
            if getattr(e, "cell", 0) == getattr(f, "cell", 1):
                continue  # a filled cell covers its "?"
            if meet(p, q):
                raise LayoutError(f"labels {e.txt!r} and {f.txt!r} overlap on the board")
        for e, p in texts:
            for bid, body in bodies.items():
                if not getattr(e, "inside", False) and meet(p, body):
                    raise LayoutError(f"label {e.txt!r} overlaps bar {bid!r}")


def _cell(v):
    """A table cell as text: 20 -> '20', 1.5 -> '1.5', ' $3 ' -> '$3'."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}"
    return str(v).strip()


def make_board(spec):
    return BoardV2(spec) if spec.get("version") == 2 else Board(spec)


def draw_marker(img, x, y):
    """A marker pen tip resting at the drawing point."""
    d = ImageDraw.Draw(img)
    x, y = x * S, y * S
    ang = math.radians(-50)
    L, w = 110 * S, 16 * S
    ex, ey = x + math.cos(ang) * L, y + math.sin(ang) * L
    tip = (x + math.cos(ang) * 22 * S, y + math.sin(ang) * 22 * S)
    d.line([tip, (ex, ey)], fill=(40, 40, 48, 235), width=w)
    d.line([(x, y), tip], fill=(70, 70, 80, 235), width=int(w * .5))
    d.line([(tip[0] + math.cos(ang) * 6 * S, tip[1] + math.sin(ang) * 6 * S),
            (tip[0] + math.cos(ang) * 16 * S, tip[1] + math.sin(ang) * 16 * S)], fill=(37, 99, 235, 255), width=w)


def caption(img, board, text):
    d = ImageDraw.Draw(img)
    lines = board.wrap(text, board.f_cap, 1100)
    h = 18 + 36 * len(lines)
    y0 = H - 22 - h
    d.rounded_rectangle([90 * S, y0 * S, (W - 90) * S, (H - 22) * S], radius=14 * S, fill=(31, 42, 68, 225))
    for i, ln in enumerate(lines):
        d.text((W / 2 * S, (y0 + 38 + i * 36) * S), ln, font=board.f_cap, fill=(255, 255, 255), anchor="ms")


# ---------------------------------------------------------------- audio
def tts(text):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        subprocess.run([sys.executable, "-m", "piper", "-m", VOICE, "-f", tf.name,
                        "--length-scale", "1.12"], input=text.encode(), capture_output=True, check=True)
        with wave.open(tf.name) as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a, sr


# ---------------------------------------------------------------- verify
def verify(spec):
    """Same gate as the app (agent/verify.py), so the two can never disagree."""
    if spec.get("version") == 2:
        from agent.verify_v2 import verify_spec_v2

        result = verify_spec_v2(spec)
        print(f"verify: {result}")
        if not result.ok:
            sys.exit("bar spec failed verification; send back to the agent to fix")
        return
    result = verify_spec(spec)
    print(f"verify: {result}")
    if not result.ok:
        sys.exit("bar spec failed verification; send back to the agent to fix")


def layout_error(spec):
    """Lay out every step without drawing frames or audio. Returns None, or why the board can't be drawn."""
    try:
        board = make_board(spec)
        steps = [(a, board.elements(a)) for a in spec["timeline"]]
        if isinstance(board, BoardV2):
            board.check(steps)
    except LayoutError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 — any layout crash means the video would fail
        return f"{type(exc).__name__}: {exc}"
    return None


def render_still(spec, png_path):
    """The finished board as a PNG: every step fully drawn, no caption, no audio."""
    board = make_board(spec)
    img = board.background()
    for a in spec["timeline"]:
        for e in board.elements(a):
            e.draw(img, 1)
    img.convert("RGB").resize((W, H), Image.LANCZOS).save(png_path)
    return png_path


# ---------------------------------------------------------------- main
def main(spec_path, out):
    spec = json.load(open(spec_path))
    verify(spec)
    board = make_board(spec)
    base = board.background()

    # plan timing + audio
    plan, clips, t = [], [], 0.6
    for a in spec["timeline"]:
        els = board.elements(a)
        audio, sr = tts(a["say"])
        adur = len(audio) / sr
        ink = sum(e.len for e in els)
        draw_dur = max(0.6, min(4.5, ink / 420)) if els else 0
        if a["do"] == "write_question":
            draw_dur = adur * 0.9
        dur = max(adur, draw_dur) + 0.45 + a.get("seconds", 0)
        plan.append((t, dur, draw_dur, els, a))
        clips.append((t, audio))
        t += dur
    total = t + 2.0

    # mix narration
    track = np.zeros(int(total * sr) + sr, dtype=np.int32)
    for st, a in clips:
        i = int(st * sr)
        track[i:i + len(a)] += a
    wav_path = os.path.splitext(out)[0] + ".wav"
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(np.clip(track, -32768, 32767).astype(np.int16).tobytes())

    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", wav_path,
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "veryfast",
                           "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out],
                          stdin=subprocess.PIPE)
    nframes = int(total * FPS)
    pi, done_base = 0, base.copy()
    held_key, held_bytes = None, b""     # a finished drawing holds still during narration
    for f in range(nframes):
        tt = f / FPS
        while pi < len(plan) - 1 and tt >= plan[pi + 1][0]:
            for e in plan[pi][3]:          # bake finished action into base
                e.draw(done_base, 1)
            pi += 1
        st, dur, dd, els, a = plan[pi]
        prog = 1 if dd == 0 else min(1, max(0, (tt - st) / dd))
        key = (pi, tt < st + dur) if prog >= 1 else None
        if key is not None and key == held_key:
            ff.stdin.write(held_bytes)
            continue
        frame = done_base.copy()
        pen = None
        if els:
            lens = [e.len for e in els]
            target = prog * sum(lens)
            for e, l in zip(els, lens):
                if target <= 0:
                    break
                fr = min(1, target / l) if l else 1
                pen = e.draw(frame, fr)
                target -= l
        if pen and prog < 1:
            draw_marker(frame, *pen)
        if tt < st + dur:
            caption(frame, board, a["say"])
        out_img = frame.convert("RGB").resize((W, H), Image.LANCZOS)
        held_key, held_bytes = key, out_img.tobytes()
        ff.stdin.write(held_bytes)
    ff.stdin.close()
    ff.wait()
    print(f"wrote {out}: {total:.1f}s, {nframes} frames")


if __name__ == "__main__":
    if sys.argv[2].endswith(".png"):
        render_still(json.load(open(sys.argv[1])), sys.argv[2])
    else:
        main(sys.argv[1], sys.argv[2])
