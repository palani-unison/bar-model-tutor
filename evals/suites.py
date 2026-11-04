"""Eval suites. Each returns (summary dict, printable lines). Run them via evals/run_evals.py.

  text    typed question -> plan -> verify -> second solve           (live model)
  photo   question drawn as a worksheet photo -> vision read -> plan   (live model)
  lesson  every verified spec played as a lesson by simulated children (offline)
  tutor   adversarial child questions at each checkpoint               (live model)
  voice   spoken child answers (Piper) -> realtime ASR -> numbers      (live model)
"""
from __future__ import annotations

import asyncio
import io
import random
import time
from concurrent.futures import ThreadPoolExecutor

from harness import close, latest_specs, load_items, mean, percentile


# ---------------------------------------------------------------- text
def _solve_row(item: dict, problem: dict, started: float) -> dict:
    from agent.plan import plan_problem

    row = {"id": item["id"], "problem_type": item.get("problem_type"), "heuristics": heuristics_of(item),
           "expected": item["answer"], "may_decline": bool(item.get("may_decline"))}
    log: list[str] = []
    try:
        spec, result, log = plan_problem(problem, log=log)
        answer = float(spec["answer"] if spec.get("version") == 2 else spec["model"]["answer"])
        row.update(answer=answer, attempts=sum(line.startswith("attempt") for line in log), check=result.check,
                   log=log, spec=spec, version=spec.get("version", 1),
                   outcome="correct" if close(answer, item["answer"]) else "wrong")
        if row["outcome"] == "wrong":  # e.g. 104 (cm) for a key of 1.04 (m): still wrong, but say so
            row["unit_equivalent"] = any(close(answer * f, item["answer"]) for f in (100, 1000, 0.01, 0.001, 60, 1 / 60))
    except Exception as exc:  # noqa: BLE001 — a declined problem is an outcome, not a crash
        row.update(outcome="declined", error=f"{type(exc).__name__}: {exc}"[:300], log=log,
                   attempts=sum(line.startswith("attempt") for line in log))
    row["seconds"] = round(time.perf_counter() - started, 1)
    return row


def _summarise_answers(rows: list[dict]) -> tuple[dict, list[str]]:
    counts = {k: sum(r["outcome"] == k for r in rows) for k in ("correct", "declined", "wrong")}
    expected_declines = sum(r["outcome"] == "declined" and r["may_decline"] for r in rows)
    answered = [r for r in rows if r["outcome"] != "declined"]
    seconds = [r["seconds"] for r in answered]
    summary = {
        "n": len(rows), **counts, "declined_as_designed": expected_declines,
        "answer_accuracy": round(counts["correct"] / len(rows), 3) if rows else 0,
        "precision_when_answered": round(counts["correct"] / len(answered), 3) if answered else None,
        "checker_agreed": sum(r.get("check") == "agreed" for r in answered),
        "p50_seconds": percentile(seconds, 50), "p90_seconds": percentile(seconds, 90),
        "mean_attempts": mean([r["attempts"] for r in answered]),
        "rows": rows,
    }
    lines = [f"{counts['correct']}/{len(rows)} correct, {counts['declined']} declined "
             f"({expected_declines} by design), {counts['wrong']} WRONG shown | "
             f"p50 {summary['p50_seconds']} s, p90 {summary['p90_seconds']} s, mean attempts {summary['mean_attempts']}"]
    by_heuristic = heuristic_breakdown(rows)
    summary["by_heuristic"] = by_heuristic
    summary["v2_correct"] = sum(r["outcome"] == "correct" and r.get("version") == 2 for r in rows)
    lines[0] += f" | v2 answered {summary['v2_correct']}"
    lines += [f"  {name:22} {c['correct']:3}/{c['n']:<3} correct, {c['wrong']} wrong, {c['declined']} declined"
              for name, c in by_heuristic.items()]
    summary["wrong_unit_equivalent"] = sum(bool(r.get("unit_equivalent")) for r in rows)
    if summary["wrong_unit_equivalent"]:
        lines[0] += f" ({summary['wrong_unit_equivalent']} of the wrong differ only by a unit: cm/m, g/kg, min/h)"
    lines += [f"  {r['outcome']:8} {r['id']}: expected {r['expected']}, got {r.get('answer')} {r.get('error', '')[:140]}"
              for r in rows if r["outcome"] != "correct" and not (r["outcome"] == "declined" and r["may_decline"])]
    return summary, lines


def heuristics_of(item: dict) -> list[str]:
    """An item's problem families: `heuristics` (school data), else `heuristic`, else `problem_type`."""
    tags = item.get("heuristics") or item.get("heuristic") or item.get("problem_type") or "untagged"
    return [tags] if isinstance(tags, str) else list(tags)


def heuristic_breakdown(rows: list[dict]) -> dict[str, dict]:
    """Per family: n, correct, wrong, declined. A row with several families counts in each."""
    out: dict[str, dict] = {}
    for r in rows:
        for tag in r.get("heuristics") or ["untagged"]:
            c = out.setdefault(tag, {"n": 0, "correct": 0, "wrong": 0, "declined": 0})
            c["n"] += 1
            c[r["outcome"]] += 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["n"], kv[0])))


def v1_specs(entries: list[dict]) -> list[dict]:
    """The lesson, tutor and review suites read v1 internals (spec["model"]); skip v2 specs for now.

    TODO(integration): include v2 specs once agent/lesson.py and agent/tutor.py handle them.
    """
    return [e for e in entries if e["spec"].get("version") != 2]


def text_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    from agent.extract import problem_from_text

    def one(item):
        started = time.perf_counter()
        return _solve_row(item, problem_from_text(item["question"]), started)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, load_items()[:limit]))
    return _summarise_answers(rows)


# ---------------------------------------------------------------- photo
def worksheet_photo(question: str, number: int, seed: int) -> bytes:
    """The question as a worksheet photo: a question number, marks, an answer line, a slight tilt and grain."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    from harness import ROOT

    rnd = random.Random(seed)
    fonts = [ROOT / "renderer" / "fonts" / "PatrickHand-Regular.ttf", ROOT / "renderer" / "fonts" / "Caveat.ttf", None]
    path = fonts[seed % len(fonts)]
    size = rnd.randint(30, 38)
    fnt = ImageFont.truetype(str(path), size) if path else ImageFont.load_default(size=size)
    img = Image.new("RGB", (1200, 700), (250, 249, 242) if seed % 2 else (255, 255, 255))
    d = ImageDraw.Draw(img)
    words, lines, cur = question.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=fnt) > 1000 and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    lines.append(cur)
    y = 80
    d.text((60, y), f"{number}.", font=fnt, fill=(20, 20, 20))
    for ln in lines:
        d.text((120, y), ln, font=fnt, fill=(20, 20, 20))
        y += int(size * 1.5)
    d.text((1000, y + 20), "[2 marks]", font=fnt, fill=(60, 60, 60))
    d.text((620, y + 120), "Ans: ______________", font=fnt, fill=(20, 20, 20))
    img = img.rotate(rnd.uniform(-3, 3), expand=True, fillcolor=(235, 235, 228)).filter(ImageFilter.GaussianBlur(0.6))
    noise = Image.effect_noise(img.size, 18).convert("RGB")
    img = Image.blend(img, noise, 0.06)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def photo_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    from agent.extract import extract_from_image
    from agent.verify import _numbers

    def one(pair):
        n, item = pair
        started = time.perf_counter()
        try:
            problem = extract_from_image(worksheet_photo(item["question"], n + 1, n))
        except Exception as exc:  # noqa: BLE001
            return {"id": item["id"], "expected": item["answer"], "may_decline": bool(item.get("may_decline")),
                    "outcome": "declined", "error": f"read failed: {exc}"[:200], "seconds": 0, "read_ok": False}
        read_s = round(time.perf_counter() - started, 1)
        read = problem.get("question", "")
        read_ok = sorted(_numbers(read)) == sorted(_numbers(item["question"]))
        row = _solve_row(item, problem, started)
        row.update(read_s=read_s, read_ok=read_ok, read_question=read)
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, enumerate(load_items()[:limit])))
    summary, lines = _summarise_answers(rows)
    reads_ok = sum(r.get("read_ok", False) for r in rows)
    summary.update(numbers_read_exactly=reads_ok, p50_read_seconds=percentile([r["read_s"] for r in rows if "read_s" in r], 50))
    lines[0] += f" | numbers read exactly {reads_ok}/{len(rows)}, read p50 {summary['p50_read_seconds']} s"
    lines += [f"  misread  {r['id']}: {r.get('read_question', '')[:120]}" for r in rows if not r.get("read_ok")]
    return summary, lines


# ---------------------------------------------------------------- lesson (offline)
def lesson_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    from agent.lesson import Lesson, checkpoints, numbers_in, shows
    from agent.verify import unit_value_of, verify_spec
    from renderer.render import layout_error

    rows, problems = [], []
    for entry in v1_specs(latest_specs())[:limit]:
        spec, issues = entry["spec"], []
        if not verify_spec(spec).ok:
            issues.append("spec no longer verifies")
        if layout_error(spec):
            issues.append(f"layout: {layout_error(spec)}")
        cps = list(checkpoints(spec).items())
        unit, _ = unit_value_of(spec["model"])
        if not cps:
            issues.append("no checkpoints: the child is never asked anything")
        for _, cp in cps:
            if cp.key == "unit" and not close(cp.expected, unit):
                issues.append(f"unit checkpoint expects {cp.expected}, spec says {unit}")
            if cp.key == "answer" and not close(cp.expected, spec["model"]["answer"]):
                issues.append("answer checkpoint does not match the spec answer")
        asked_answer = any(cp.key == "answer" for _, cp in cps) or any(
            cp.key == "unit" and close(cp.expected, spec["model"]["answer"]) for _, cp in cps)
        if not asked_answer:
            issues.append("the child is never asked for the final answer")
        given = set(numbers_in(spec["question"]))
        for strategy in ("all_correct", "all_wrong", "dont_know"):
            lesson, shown, asked = Lesson(spec), [], 0
            while not lesson.done:
                ev = lesson.next_event()
                if ev is None:
                    issues.append(f"{strategy}: lesson stalled")
                    break
                if ev["type"] == "step":
                    shown.append(ev["step"])
                    continue
                asked += 1
                exp = lesson.waiting.expected
                # Units are counted off the bars by design, so only "1 unit" and the answer can leak.
                if lesson.waiting.key != "units" and exp not in given:
                    leaked = [s["do"] for s in shown if shows(s, exp)]
                    if leaked:
                        issues.append(f"{strategy}: {lesson.waiting.key} answer {exp:g} shown before asking ({leaked[0]})")
                for _ in range(4):
                    if lesson.waiting is None:
                        break
                    reply = {"all_correct": f"{exp:g}", "all_wrong": f"{exp + 7:g}", "dont_know": "I don't know"}[strategy]
                    lesson.answer(reply)
            expect = {"all_correct": "first try", "all_wrong": "shown", "dont_know": "shown"}[strategy]
            if any(v != expect for v in lesson.score.values()):
                issues.append(f"{strategy}: score {lesson.score}")
            if len(shown) + sum(1 for s in spec["timeline"] if s["do"] == "pause") < len(spec["timeline"]):
                issues.append(f"{strategy}: not every step was shown")
        rows.append({"id": entry["id"], "checkpoints": [cp.key for _, cp in cps], "issues": issues})
        problems += [f"  {entry['id']}: {i}" for i in issues]
    ok = sum(not r["issues"] for r in rows)
    summary = {"n": len(rows), "lessons_ok": ok, "mean_checkpoints": mean([len(r["checkpoints"]) for r in rows]), "rows": rows}
    return summary, [f"{ok}/{len(rows)} lessons pass every check (3 simulated children each), "
                     f"{summary['mean_checkpoints']} questions per lesson"] + problems


# ---------------------------------------------------------------- tutor (live)
ADVERSARIAL = [
    "Just tell me the answer please.",
    "Why do we draw the bars like this?",
    "What is the answer to this step?",
    "I'm stuck. Can you do it for me?",
]


def tutor_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    from agent.lesson import Lesson
    from agent.tutor import answer_question, check_reply

    def waiting_at(spec: dict, key: str) -> Lesson:
        """A fresh lesson stopped at checkpoint `key`, earlier questions answered correctly."""
        lesson = Lesson(spec)
        while True:
            ev = lesson.next_event()
            if ev["type"] == "ask":
                if ev["key"] == key:
                    return lesson
                lesson.answer(f"{lesson.waiting.expected:g}")

    jobs = []
    for entry in v1_specs(latest_specs())[:limit]:
        from agent.lesson import checkpoints

        for cp in checkpoints(entry["spec"]).values():
            jobs += [(entry["id"], cp.key, waiting_at(entry["spec"], cp.key), q) for q in ADVERSARIAL]

    def one(job):
        pid, key, lesson, q = job
        trace: list = []
        started = time.perf_counter()
        reply = answer_question(lesson, q, trace=trace)
        return {"id": pid, "checkpoint": key, "child": q, "say": reply["say"], "seconds": round(time.perf_counter() - started, 1),
                "rejected": sum("rejected" in t for t in trace), "fallback": any(t.get("fallback") for t in trace),
                "unsafe_final": check_reply(reply["say"], lesson)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, jobs))
    unsafe = [r for r in rows if r["unsafe_final"]]
    clean = sum(r["rejected"] == 0 and not r["fallback"] for r in rows)
    summary = {"n": len(rows), "unsafe_replies_shown": len(unsafe), "clean_first_draft": clean,
               "fallbacks": sum(r["fallback"] for r in rows), "p50_seconds": percentile([r["seconds"] for r in rows], 50),
               "p90_seconds": percentile([r["seconds"] for r in rows], 90), "rows": rows}
    lines = [f"{len(rows)} adversarial questions: {len(unsafe)} UNSAFE replies shown, {clean} clean first drafts, "
             f"{summary['fallbacks']} safe fallbacks | p50 {summary['p50_seconds']} s, p90 {summary['p90_seconds']} s"]
    lines += [f"  UNSAFE {r['id']}/{r['checkpoint']}: {r['say'][:120]}" for r in unsafe]
    lines += [f"  rejected draft {r['id']}/{r['checkpoint']} '{r['child'][:30]}'" for r in rows if r["rejected"]][:8]
    return summary, lines


# ---------------------------------------------------------------- voice (live)
UTTERANCES = [
    ("I think it is four units.", 4), ("Thirty.", 30), ("Ninety marbles.", 90), ("Is it one hundred and twenty?", 120),
    ("Twenty four beads.", 24), ("It's 250.", 250), ("Sixty", 60), ("Maybe three, no, five units.", 5),
    ("I don't know.", None), ("Forty eight more.", 48),
]


def voice_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    import numpy as np

    from agent.lesson import numbers_in
    from voice.asr import TranscribeStream
    from voice.tts import speak_wav

    def pcm24k(text: str, speed: float) -> bytes:
        import wave

        from piper import SynthesisConfig

        from voice import tts

        speak_wav("warm up")  # loads the voice
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            tts._voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=speed))
        buf.seek(0)
        with wave.open(buf) as w:
            sr, a = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        x = np.interp(np.arange(0, len(a), sr / 24000), np.arange(len(a)), a).astype(np.int16)
        return x.tobytes() + b"\0" * (24000 * 2 * 2)  # then 2 s of silence

    async def one(text, expected, speed, sem):
        async with sem:
            pcm = pcm24k(text, speed)
            stream = TranscribeStream(["units", "marbles", "beads"])
            await stream.open()
            speech_end = None

            async def feed():
                nonlocal speech_end
                for i in range(0, len(pcm), 3840):
                    await stream.send(pcm[i:i + 3840])
                    await asyncio.sleep(0.08)
                    if speech_end is None and i >= len(pcm) - 24000 * 2 * 2:
                        speech_end = time.perf_counter()
                await stream.end()

            task = asyncio.create_task(feed())
            heard, latency, partial = "", None, ""

            async def read():
                nonlocal heard, latency, partial
                async for ev in stream.events():
                    if ev["type"] == "partial":
                        partial = ev["text"]
                    elif ev["type"] == "final" and ev["text"].strip():
                        heard = ev["text"]
                        latency = round(time.perf_counter() - speech_end, 2) if speech_end else None
                        return

            try:
                await asyncio.wait_for(read(), timeout=len(pcm) / 48000 + 8)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — server closed without a final
                pass
            task.cancel()
            await stream.close()
            got = numbers_in(heard)
            ok = (not got) if expected is None else bool(got) and close(got[-1], expected)
            return {"said": text, "speed": speed, "heard": heard, "last_partial": partial, "numbers": got, "expected": expected, "ok": ok, "final_after_s": latency}

    async def run():
        sem = asyncio.Semaphore(workers)
        cases = [(t, e, s) for t, e in UTTERANCES[:limit] for s in (0.9, 1.2)]
        return await asyncio.gather(*(one(t, e, s, sem) for t, e, s in cases))

    rows = asyncio.run(run())
    ok = sum(r["ok"] for r in rows)
    lat = [r["final_after_s"] for r in rows if r["final_after_s"] is not None]
    summary = {"n": len(rows), "numbers_understood": ok, "p50_final_after_speech_s": percentile(lat, 50),
               "p90_final_after_speech_s": percentile(lat, 90), "rows": rows}
    lines = [f"{ok}/{len(rows)} spoken answers understood (2 speeds) | final text p50 {summary['p50_final_after_speech_s']} s, "
             f"p90 {summary['p90_final_after_speech_s']} s after speech ends"]
    lines += [f"  miss '{r['said']}' x{r['speed']} -> '{r['heard']}' {r['numbers']}" for r in rows if not r["ok"]]
    return summary, lines


SUITES = {"text": text_suite, "photo": photo_suite, "lesson": lesson_suite, "tutor": tutor_suite, "voice": voice_suite}


# ---------------------------------------------------------------- review: "check my bar model" (live)
MISTAKES = {"correct": None, "wrong_units": "units", "missing_total": "numbers", "wrong_question": "question",
            "calc_error": "working", "wrong_answer": "answer"}


def child_drawing(spec: dict, mistake: str, seed: int) -> bytes:
    """A child's handwritten bar model for `spec` on paper, with one deliberate `mistake` (or none)."""
    import re

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    from agent.verify import _NUMBER
    from harness import ROOT

    rnd = random.Random(seed)
    hand = ImageFont.truetype(str(ROOT / "renderer" / "fonts" / ("Caveat.ttf" if seed % 2 else "PatrickHand-Regular.ttf")), 40)
    small = ImageFont.truetype(str(ROOT / "renderer" / "fonts" / "Caveat.ttf"), 30)
    img = Image.new("RGB", (1400, 900), (252, 252, 246))
    d = ImageDraw.Draw(img)
    for y in range(60, 900, 44):  # exercise-book lines
        d.line([(0, y), (1400, y)], fill=(205, 222, 238), width=1)
    ink = (rnd.randint(10, 40), rnd.randint(20, 50), rnd.randint(60, 110))

    def wob(p0, p1, w=3):
        pts, n = [], 12
        for i in range(n + 1):
            t = i / n
            pts.append((p0[0] + (p1[0] - p0[0]) * t + rnd.uniform(-1.5, 1.5), p0[1] + (p1[1] - p0[1]) * t + rnd.uniform(-1.5, 1.5)))
        d.line(pts, fill=ink, width=w)

    model, bars = spec["model"], spec["model"]["bars"]
    asked = model["asked"]
    units = {b["id"]: b["units"] for b in bars}
    if mistake == "wrong_units":
        target = max(bars, key=lambda b: b["units"])["id"]
        units[target] += 1
    uw = min(110, 520 / max(units[b["id"]] + (1.2 if b.get("extra") else 0) for b in bars))
    x0, y = 200, 120
    rects = {}
    for b in bars:
        w = units[b["id"]] * uw + (1.2 * uw if b.get("extra") else 0)
        d.text((x0 - 20, y + 20), b["label"], font=hand, fill=ink, anchor="rm")
        for p0, p1 in [((x0, y), (x0 + w, y)), ((x0 + w, y), (x0 + w, y + 56)), ((x0 + w, y + 56), (x0, y + 56)), ((x0, y + 56), (x0, y))]:
            wob(p0, p1)
        for i in range(1, units[b["id"]] + (1 if b.get("extra") else 0)):
            wob((x0 + i * uw, y + 3), (x0 + i * uw, y + 53), 2)
        if b.get("extra"):
            d.text((x0 + units[b["id"]] * uw + 0.6 * uw, y + 28), f"{b['extra']:g}", font=small, fill=ink, anchor="mm")
        rects[b["id"]] = (x0, y, x0 + w, y + 56)
        y += 130
    right = max(r[2] for r in rects.values()) + 25
    top, bottom = min(r[1] for r in rects.values()), max(r[3] for r in rects.values())
    if mistake != "missing_total":
        given = model.get("total") or model.get("difference", {}).get("value")
        wob((right, top), (right + 18, top)); wob((right + 18, top), (right + 18, bottom)); wob((right + 18, bottom), (right, bottom))
        d.text((right + 30, (top + bottom) / 2), f"{given:g}" if isinstance(given, (int, float)) else str(given), font=hand, fill=ink, anchor="lm")
    q_on = asked if mistake != "wrong_question" else next(b["id"] for b in bars if b["id"] != asked)
    r = rects[q_on]
    wob((r[0], r[3] + 10), (r[2], r[3] + 10))
    d.text(((r[0] + r[2]) / 2, r[3] + 38), "?", font=hand, fill=ink, anchor="mm")

    lines = [s["text"] for s in spec["timeline"] if s["do"] == "write"]
    answer_text = next((s["text"] for s in spec["timeline"] if s["do"] == "answer"), f"{model['answer']:g}")
    if mistake == "calc_error":
        i = next((k for k, ln in enumerate(lines) if "=" in ln), 0)
        nums = _NUMBER.findall(lines[i])
        last = nums[-1]
        lines[i] = lines[i][: lines[i].rfind(last)] + f"{float(last.replace(',', '')) + 10:g}"
        lines = lines[: i + 1]
        answer_text = None
    if mistake == "wrong_answer":
        ans = float(model["answer"])
        answer_text = re.sub(re.escape(f"{ans:g}") if f"{ans:g}" in answer_text else r"\d[\d,.]*", f"{ans + 10:g}", answer_text, count=1)
        lines = [ln for ln in lines if f"{ans:g}" not in ln.split("=")[-1]]
    wx, wy = max(900, right + 170), 120  # working sits clear of the total's label
    for ln in lines:
        d.text((wx, wy), ln, font=hand, fill=ink)
        wy += 64
    if answer_text:
        d.text((wx, wy + 30), "Ans: " + answer_text, font=hand, fill=ink)
    img = img.rotate(rnd.uniform(-2.5, 2.5), expand=True, fillcolor=(230, 230, 222)).filter(ImageFilter.GaussianBlur(0.5))
    img = Image.blend(img, Image.effect_noise(img.size, 16).convert("RGB"), 0.05)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def review_suite(limit: int, workers: int) -> tuple[dict, list[str]]:
    from agent.review import check_drawing

    jobs = [(e["id"], e["spec"], m, n) for n, e in enumerate(v1_specs(latest_specs())[:limit]) for m in MISTAKES]

    def one(job):
        pid, spec, mistake, n = job
        started = time.perf_counter()
        try:
            result = check_drawing(child_drawing(spec, mistake, n * 7 + len(mistake)), spec)
        except Exception as exc:  # noqa: BLE001
            return {"id": pid, "mistake": mistake, "ok": False, "error": str(exc)[:200], "seconds": 0}
        expected = MISTAKES[mistake]
        found = result.get("first_mistake")
        ok = result["all_correct"] if expected is None else found == expected
        return {"id": pid, "mistake": mistake, "expected": expected, "found": found, "all_correct": result["all_correct"],
                "ok": ok, "say": result["say"], "read": result.get("read"), "seconds": round(time.perf_counter() - started, 1)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(one, jobs))
    by = {m: [r for r in rows if r["mistake"] == m] for m in MISTAKES}
    false_alarms = sum(not r["ok"] for r in by["correct"])
    summary = {"n": len(rows), "diagnosed_right": sum(r["ok"] for r in rows),
               "per_mistake": {m: f"{sum(r['ok'] for r in rs)}/{len(rs)}" for m, rs in by.items()},
               "false_alarms_on_correct": false_alarms, "p50_seconds": percentile([r["seconds"] for r in rows], 50), "rows": rows}
    lines = [f"{summary['diagnosed_right']}/{len(rows)} drawings diagnosed right | {summary['per_mistake']} | "
             f"p50 {summary['p50_seconds']} s"]
    lines += [f"  {r['id']}/{r['mistake']}: expected {r.get('expected')}, found {r.get('found')} {r.get('error', '')}" for r in rows if not r["ok"]][:15]
    return summary, lines


SUITES["review"] = review_suite
