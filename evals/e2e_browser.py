"""Full-loop browser test: a real (headless) Chromium plays a whole lesson against a running server.

  pip install playwright && python -m playwright install chromium
  python evals/e2e_browser.py [http://127.0.0.1:8000]

Checks, in order: the marbles lesson starts over the WebSocket; the board draws; at each
question the "child" types (one question to the tutor, one wrong answer, a spelled-out
number); a spoken answer through a fake microphone is heard and checked by code; the
lesson ends with stars. Exits non-zero on any failure. Needs META_API_KEY on the server
for the tutor and voice steps.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TYPED = {"How many units": ["why do we count all the units?", "4"], "1 unit": ["25", "30"], "Now you try": ["ninety"]}


def spoken_answer_wav(path: Path) -> None:
    """'I think it is four units.' in the Piper voice after 2.5 s (the first tap opens the voice stream), then silence,
    as a 48 kHz WAV for Chromium's fake mic."""
    raw = path.with_suffix(".raw.wav")
    subprocess.run([sys.executable, "-m", "piper", "-m", str(ROOT / "renderer" / "voice.onnx"), "-f", str(raw)],
                   input=b"I think it is four units.", check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", "adelay=2500,apad=pad_dur=4", "-ar", "48000", "-ac", "1", str(path)], check=True)


def main(base: str) -> int:
    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        wav = Path(tmp) / "child.wav"
        spoken_answer_wav(wav)
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
                                          "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={wav}"])
        ctx = browser.new_context(permissions=["microphone"])
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # Google Sign-In logs an error on origins not registered for the client id (e.g. 127.0.0.1:<port>);
        # that is a deploy setting, not an app failure.
        page.on("console", lambda m: m.type == "error" and "GSI_LOGGER" not in m.text
                and "origin is not allowed" not in m.text and errors.append(m.text))

        # 1) Typed lesson, start to finish: the landing's "Try an example lesson" (marbles is the first example).
        page.goto(base)
        page.click("#tryExample")
        page.click("#exampleBtn")
        started, answered, last, replied = time.time(), {}, "", True
        while time.time() - started < 240 and not page.is_visible("#after"):
            caption, who = page.inner_text("#captionText"), page.inner_text("#caption .who")
            if caption != last:
                print(f"{time.time() - started:6.1f}s  {who}: {caption}")
                last = caption
                # The teacher has answered the child's last message (a verdict or a tutor reply).
                replied = replied or (who == "Teacher" and "think" not in caption)
            if page.is_visible("#askBox") and replied:
                question = page.inner_text("#askText")
                key = next((k for k in TYPED if k in question), None)
                n = answered.get(key, 0)
                if key and n < len(TYPED[key]):
                    time.sleep(1.2)
                    page.fill("#answerInput", TYPED[key][n])
                    page.press("#answerInput", "Enter")
                    print(f"{time.time() - started:6.1f}s  CHILD TYPES: {TYPED[key][n]}")
                    answered[key], replied = n + 1, False
            time.sleep(0.3)
        if not page.is_visible("#after"):
            failures.append("typed lesson did not reach the end")
        stars = page.eval_on_selector_all("#stars span.won", "e => e.length")
        if stars != 3:
            failures.append(f"expected 3 stars, got {stars}")
        if "We did it together" not in page.inner_text("#captionText"):
            failures.append("no end-of-lesson message")

        # 2) Check my bar model: a correct drawing gets all ticks; one with a wrong answer is caught.
        sys.path.insert(0, str(ROOT / "evals"))
        sys.path.insert(0, str(ROOT))
        import json

        from suites import child_drawing

        spec = json.loads((ROOT / "specs" / "examples" / "marbles.json").read_text(encoding="utf-8"))
        for mistake, want in (("correct", "all correct"), ("wrong_answer", "isn't quite right")):
            photo = Path(tmp) / f"{mistake}.jpg"
            photo.write_bytes(child_drawing(spec, mistake, 5))
            page.set_input_files("#mineInput", str(photo))
            page.wait_for_function("() => document.getElementById('mineChecks').children.length > 0", timeout=60000)
            said = page.inner_text("#mineSay")
            print(f"check my model ({mistake}):", page.inner_text("#mineChecks").replace(chr(10), " "), "|", said)
            if want not in said:
                failures.append(f"check my bar model ({mistake}) said: {said}")
            page.evaluate("document.getElementById('mineChecks').innerHTML = ''")

        # 3) Spoken answer through the fake microphone (the child taps the mic once; it turns itself off after the answer).
        page.keyboard.press("Escape")  # the lesson menu opened with the drawing check
        page.click("#againBtn")
        page.click("#tryExample")
        page.click("#exampleBtn")
        page.wait_for_selector("#askBox.on", timeout=120000)
        page.click("#micBtn")
        heard_ok = False
        t0 = time.time()
        while time.time() - t0 < 40:
            if "Yes!" in page.inner_text("#verdict"):
                heard_ok = True
                break
            time.sleep(0.2)
        print("voice:", page.inner_text("#heard"), "|", page.inner_text("#verdict"))
        if not heard_ok:
            failures.append("spoken 'four units' was not heard and accepted")
        page.wait_for_timeout(1500)
        if page.get_attribute("#micBtn", "data-state") in ("listening", "wait"):
            failures.append("the mic stayed on after the answer was checked")

        if errors:
            failures.append(f"browser errors: {errors[:3]}")
        browser.close()

    print("PASS" if not failures else "FAIL:\n  " + "\n  ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"))
