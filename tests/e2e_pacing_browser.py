"""Browser e2e (Playwright): dev login, add a child, play marbles twice as the child, check the pacing changes.

Not collected by pytest. Start a fresh server first, then run:
  ALLOW_DEV_LOGIN=1 DB_PATH=/tmp/e2e.db python -m uvicorn app.main:app --port 8013
  E2E_OUT=/tmp/shots python tests/e2e_pacing_browser.py
"""
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

OUT = Path(os.environ.get("E2E_OUT", "out/e2e"))
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.environ.get("E2E_URL", "http://127.0.0.1:8013/")


def answer_for(ask: str) -> str:
    if "Challenge" in ask or "How many marbles" in ask:
        return "90"
    if "1 unit" in ask:
        return "30"
    if "units" in ask:
        return "4"
    raise AssertionError(f"unexpected question: {ask}")


def play_lesson(page, tag):
    page.select_option("#exampleSel", "marbles")
    page.click("#exampleBtn")
    page.wait_for_function("!document.getElementById('after').classList.contains('on')", timeout=15000)
    expect(page.locator("#paceNote")).to_be_visible(timeout=15000)
    pace = page.locator("#paceNote").inner_text()
    print(tag, "pace:", pace)
    asks = []
    shot = False
    while True:
        page.wait_for_function("document.getElementById('askBox').classList.contains('on') || document.getElementById('after').classList.contains('on')", timeout=120000)
        if page.locator("#after.on").count():
            break
        ask = page.locator("#askText").inner_text()
        asks.append(ask)
        print(tag, "ask:", ask, flush=True)
        if not shot:
            page.screenshot(path=str(OUT / f"{tag}-ask.png"))
            shot = True
        page.fill("#answerInput", answer_for(ask))
        page.click("#sendBtn")
        page.wait_for_function("!document.getElementById('askBox').classList.contains('on')", timeout=20000)
    page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / f"{tag}-end.png"), full_page=True)
    nxt = page.locator("#nextPace").inner_text()
    print(tag, "asks:", asks)
    print(tag, "next:", nxt)
    return pace, asks, nxt


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(BASE)
    page.click("#devBtn")
    expect(page.locator("#panelBtn")).to_be_visible()
    page.click("#panelBtn")
    page.fill("#newKidName", "Kai")
    page.select_option("#newKidLevel", "P4")
    page.select_option("#newKidAvatar", "🐯")
    page.click("#addKidBtn")
    expect(page.locator("#panelMsg")).to_contain_text("Code name")
    page.screenshot(path=str(OUT / "1-parent-panel.png"))
    page.click("button.kid")
    expect(page.locator("#childBadge")).to_contain_text("Kai")
    expect(page.locator("#parentPanel")).to_be_hidden()
    page.screenshot(path=str(OUT / "2-child-signed-in.png"))

    pace1, asks1, next1 = play_lesson(page, "3-lesson1")
    assert "Step by step" in pace1 and "every step" in pace1, pace1
    assert len(asks1) == 3 and "Challenge first" in next1, (asks1, next1)

    pace2, asks2, next2 = play_lesson(page, "4-lesson2")
    assert "Challenge first" in pace2 and "1.5×" in pace2, pace2
    assert len(asks2) == 1 and "Challenge!" in asks2[0], asks2
    assert page.locator("#stars span.won").count() == page.locator("#stars span").count()
    assert re.search(r"Auto 1\.5×", page.locator("#speedAuto").inner_text())

    # The parent signs back in on the family device and looks at progress.
    page.click("#switchBtn")
    expect(page.locator("#devBtn")).to_be_visible()
    page.screenshot(path=str(OUT / "5-family-picker.png"))
    page.click("#devBtn")
    page.click("#panelBtn")
    page.click(".kProg")
    expect(page.locator(".progress-box")).to_contain_text("part whole")
    page.screenshot(path=str(OUT / "6-progress.png"))
    print("progress:", page.locator(".progress-box").inner_text())
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(OUT / "7-phone.png"), full_page=True)
    print("page errors:", errors)
    browser.close()
    sys.exit(1 if errors else 0)
