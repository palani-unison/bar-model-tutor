"""Voice latency, end to end: a spoken answer played into a real lesson over the WebSocket.

  python evals/voice_latency.py [ws://127.0.0.1:8012] [--trials 10] [--question]

Each trial opens a lesson on the marbles spec, waits for the first checkpoint, turns the mic
on, then streams "I think it is four units." (Piper voice, 24 kHz s16le, paced in 80 ms frames
like the browser's AudioWorklet) followed by silence. It measures, from the end of speech:
  -> final:   the "heard" final event (or the stable partial the server acted on)
  -> verdict: the "verdict" event
  -> audio:   the first event that carries teacher audio (an acknowledgement or the verdict)
With --question the child asks "why do we count all the units?" instead, and "verdict" means
the tutor's reply. Needs META_API_KEY on the server. Prints p50 / p90 per segment.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FRAME = 1920 * 2  # 80 ms of 24 kHz s16le, as the browser sends
ANSWER = "I think it is four units."
QUESTION = "Why do we count all the units?"


def child_pcm(text: str) -> bytes:
    from piper import SynthesisConfig

    from voice import tts

    tts.speak_wav("warm up")  # loads the voice
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        tts._voice.synthesize_wav(text, w, syn_config=SynthesisConfig(length_scale=1.0))
    buf.seek(0)
    with wave.open(buf) as w:
        sr, a = w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    x = np.interp(np.arange(0, len(a), sr / 24000), np.arange(len(a)), a).astype(np.int16)
    nz = np.nonzero(np.abs(x) > 300)[0]  # trim Piper's trailing silence: speech ends at the last loud sample
    return x[: (nz[-1] + 1) if len(nz) else len(x)].tobytes()


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


async def trial(base: str, spec: dict, speech: bytes, question: bool) -> dict:
    import websockets

    async with websockets.connect(f"{base}/ws/lesson", max_size=2**24) as ws:
        await ws.send(json.dumps({"type": "start", "spec": spec}))
        while True:  # play the lesson up to the first checkpoint
            ev = json.loads(await ws.recv())
            if ev["type"] == "error":
                raise RuntimeError(ev.get("message"))
            if ev["type"] == "step":
                await ws.send(json.dumps({"type": "next"}))
            elif ev["type"] == "ask":
                break
        await ws.send(json.dumps({"type": "mic", "on": True}))
        while json.loads(await ws.recv()).get("type") != "mic":
            pass
        got: dict[str, float] = {}
        texts: dict[str, str] = {}
        end_of_speech: list[float] = []
        want = "tutor" if question else "verdict"

        async def feed():
            for i in range(0, len(speech), FRAME):
                await ws.send(speech[i:i + FRAME])
                await asyncio.sleep(0.08)
            end_of_speech.append(time.perf_counter())
            silence = b"\0" * FRAME
            for _ in range(int(12 / 0.08)):
                await ws.send(silence)
                await asyncio.sleep(0.08)

        feeder = asyncio.create_task(feed())
        try:
            async def read():
                while want not in got:
                    ev = json.loads(await ws.recv())
                    now = time.perf_counter()
                    kind = ev.get("type")
                    if kind == "heard" and ev.get("final") and "final" not in got:
                        got["final"], texts["heard"] = now, ev.get("text", "")
                    if ev.get("audio") and "audio" not in got and end_of_speech:
                        got["audio"] = now
                    if kind in ("verdict", "tutor"):
                        got[kind], texts["say"] = now, ev.get("say", "")
            await asyncio.wait_for(read(), timeout=30)
        finally:
            feeder.cancel()
        t0 = end_of_speech[0]
        return {k: round(v - t0, 3) for k, v in got.items()} | texts


async def main(base: str, trials: int, question: bool) -> int:
    spec = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
    speech = child_pcm(QUESTION if question else ANSWER)
    want = "tutor" if question else "verdict"
    rows = []
    for n in range(trials):
        try:
            row = await trial(base, spec, speech, question)
        except Exception as exc:  # noqa: BLE001 — report and go on
            row = {"error": f"{type(exc).__name__}: {exc}"}
        rows.append(row)
        print(f"trial {n + 1}: " + json.dumps(row), flush=True)
    for seg in ("final", want, "audio"):
        xs = [r[seg] for r in rows if seg in r]
        print(f"end of speech -> {seg:8s} n={len(xs):2d}  p50 {pct(xs, 50)} s  p90 {pct(xs, 90)} s")
    if not question:
        ok = sum(r.get("say", "").startswith("Yes") for r in rows)
        print(f"{ok}/{len(rows)} spoken answers accepted")
    return 0 if all(want in r for r in rows) else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(sys.argv[sys.argv.index("--trials") + 1]) if "--trials" in sys.argv else 10
    if "--trials" in sys.argv:
        args = [a for a in args if a != str(n)]
    raise SystemExit(asyncio.run(main(args[0] if args else "ws://127.0.0.1:8012", n, "--question" in sys.argv)))
