"""Browser e2e (Playwright): the "build it yourself" board task, on an iPhone 13 and on a desktop.

Not collected by pytest. Start a server first (anonymous: no PUBLIC_DEPLOY), then run:
  python -m uvicorn app.main:app --port 8031
  E2E_URL=http://127.0.0.1:8031/ python tests/e2e_board_browser.py

Plays "How many more" (template B) on an iPhone 13 and a desktop: at the build task the child sets Kavya to 2 units (wrong) and
sees a hint, then 4 (right) and the lesson goes on to draw the bars; the "?" goes on the wrong bar first, then on
Kavya's "more" part. Number questions are skipped with "Show me". At the end the first star (the build task)
is won. Then "stamps-transfer" (C) on the desktop: the bars typed in words (the grammar), the piece and the "?"
dragged with the mouse; and "constant_difference-ages" (D) on the phone: "+ years" on one bar (a hint), then both.
Screenshots go to out/board-*.png, including the builder in all four skins.
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("E2E_OUT", ROOT / "out"))
OUT.mkdir(parents=True, exist_ok=True)
BASE = os.environ.get("E2E_URL", "http://127.0.0.1:8031/")
SKINS = ("notebook", "cinema", "play", "ocean")


def shot(page, name):
    path = OUT / f"board-{name}.png"
    page.screenshot(path=str(path))
    print("  screenshot", path, flush=True)


def no_page_scroll(page, tag):
    over = page.evaluate("document.documentElement.scrollHeight - innerHeight")
    assert over <= 1, f"{tag}: the lesson screen scrolls by {over}px"


def set_units(page, row, n):
    """Tap + or – on that bar's row until it shows n."""
    r = page.locator(f".b-track[data-row='{row}']").locator("xpath=..")
    for _ in range(14):
        have = int(r.locator(".b-n").inner_text())
        if have == n:
            return
        r.locator(".b-pm").nth(1 if have < n else 0).click()
    raise AssertionError(f"could not set {row} to {n}")


def verdict(page, cls):
    page.wait_for_function(f"document.getElementById('verdict').classList.contains('{cls}')", timeout=20000)
    return page.locator("#verdict").inner_text()


def play(page, tag):
    page.goto(BASE)
    page.click("#tryExample")
    page.wait_for_function("[...document.querySelectorAll('#exampleSel option')].some(o => o.value === 'how_many_more-stickers')")
    page.select_option("#exampleSel", "how_many_more-stickers")
    page.click("#exampleBtn")
    page.evaluate("document.querySelector('#speedBox [data-rate=\"2\"]') && document.querySelector('#speedBox [data-rate=\"2\"]').click()")
    seen = []
    while True:
        page.wait_for_function("document.getElementById('askBox').classList.contains('on') || document.getElementById('after').classList.contains('on')",
                               timeout=180000)
        if page.locator("#after.on").count():
            break
        ask = page.locator("#askText").inner_text()
        building = page.locator("#builder:not([hidden])").count() > 0
        print(tag, "ask:", ask, "(board)" if building else "", flush=True)
        if building and page.locator("#builder.op-units").count():
            seen.append("build")
            no_page_scroll(page, tag)
            assert "Tap + and –, then tap Check." in ask
            set_units(page, "lily_bar", 1)
            set_units(page, "kavya_bar", 2)
            shot(page, f"{tag}-1-build")
            page.click("#builderCheck")
            say = verdict(page, "try_again")
            print(tag, "  wrong ->", say, flush=True)
            assert "Give Lily 1 unit" in say
            shot(page, f"{tag}-2-hint")
            if tag == "iphone13":
                for skin in SKINS:
                    page.evaluate(f"document.documentElement.dataset.skin = '{skin}'")
                    page.wait_for_timeout(150)
                    no_page_scroll(page, f"{tag} {skin}")
                    shot(page, f"{tag}-skin-{skin}")
                page.evaluate("document.documentElement.dataset.skin = 'notebook'")
            set_units(page, "kavya_bar", 4)
            page.click("#builderCheck")
            say = verdict(page, "correct")
            print(tag, "  right ->", say, flush=True)
            shot(page, f"{tag}-3-right")
            page.wait_for_function("document.getElementById('builder').hidden", timeout=30000)
            page.wait_for_timeout(2500)  # the teacher draws the bars on the real board
            shot(page, f"{tag}-4-drawn")
        elif building and page.locator("#builder.op-mark").count():
            seen.append("mark")
            no_page_scroll(page, tag)
            page.click(".b-seg[data-bar='lily_bar']")
            page.click("#builderCheck")
            print(tag, "  wrong ? ->", verdict(page, "try_again"), flush=True)
            page.click(".b-seg[data-bar='kavya_bar'][data-seg='1']")
            shot(page, f"{tag}-5-mark")
            page.click("#builderCheck")
            print(tag, "  right ? ->", verdict(page, "correct"), flush=True)
            page.wait_for_function("document.getElementById('builder').hidden", timeout=30000)
        else:
            page.click("#showBtn")
        page.wait_for_function("!document.getElementById('askBox').classList.contains('on')", timeout=30000)
    page.wait_for_timeout(1200)
    shot(page, f"{tag}-6-end")
    stars = page.locator("#endStars span")
    won = [("won" in (stars.nth(i).get_attribute("class") or "")) for i in range(stars.count())]
    print(tag, "board tasks:", seen, "stars:", won, flush=True)
    assert seen == ["build", "mark"], seen
    assert won[0] and won[1], "the build and mark stars should be won (with hints)"


def drag(page, src, dst):
    """A mouse drag (Pointer Events: the same path a finger or a pen takes)."""
    a, b = page.locator(src).first.bounding_box(), page.locator(dst).first.bounding_box()
    page.mouse.move(a["x"] + a["width"] / 2, a["y"] + a["height"] / 2)
    page.mouse.down()
    for k in range(1, 9):
        page.mouse.move(a["x"] + a["width"] / 2 + (b["x"] + b["width"] / 2 - a["x"] - a["width"] / 2) * k / 8,
                        a["y"] + a["height"] / 2 + (b["y"] + b["height"] / 2 - a["y"] - a["height"] / 2) * k / 8)
    page.mouse.up()


def start(page, name):
    page.goto(BASE)
    page.click("#tryExample")
    page.wait_for_function(f"[...document.querySelectorAll('#exampleSel option')].some(o => o.value === '{name}')")
    page.select_option("#exampleSel", name)
    page.click("#exampleBtn")


def run(page, tag, handlers):
    """Answer board tasks with handlers[op](page); number questions get "Show me". Returns the ops seen and stars."""
    seen = []
    while True:
        page.wait_for_function("document.getElementById('askBox').classList.contains('on') || document.getElementById('after').classList.contains('on')",
                               timeout=180000)
        if page.locator("#after.on").count():
            break
        op = page.evaluate("document.getElementById('builder').hidden ? null : document.getElementById('builder').className.match(/op-(\\w+)/)[1]")
        print(tag, "ask:", page.locator("#askText").inner_text(), f"({op})" if op else "", flush=True)
        if op:
            seen.append(op)
            no_page_scroll(page, tag)
            handlers[op](page)
            page.wait_for_function("document.getElementById('builder').hidden", timeout=30000)
        else:
            page.click("#showBtn")
        page.wait_for_function("!document.getElementById('askBox').classList.contains('on')", timeout=30000)
    page.wait_for_timeout(1000)
    stars = page.locator("#endStars span")
    return seen, [("won" in (stars.nth(i).get_attribute("class") or "")) for i in range(stars.count())]


def transfer(page, tag):
    """Template C on stamps-transfer: the bars said in words (the grammar, typed), the move dragged, the ? dragged."""
    start(page, "stamps-transfer")

    def build(page):
        page.fill("#answerInput", "Ben has one unit and Ali has three")
        page.click("#sendBtn")
        print(tag, "  said ->", verdict(page, "correct"), flush=True)
        assert page.locator(".b-n").all_inner_texts() == ["1", "3"]  # the builder shows what was understood
        shot(page, f"{tag}-c1-said")

    def move(page):
        drag(page, ".b-seg.removed[data-bar='ali_a']", ".b-row[data-bar='ben_a'] .b-label")
        assert "Ali → Ben" in page.locator(".b-foot .b-tip").inner_text()
        shot(page, f"{tag}-c2-move")
        page.click("#builderCheck")
        print(tag, "  move ->", verdict(page, "correct"), flush=True)

    def mark(page):
        drag(page, ".b-chip", ".b-seg[data-bar='ben_b']")
        shot(page, f"{tag}-c3-mark")
        page.click("#builderCheck")
        print(tag, "  ? ->", verdict(page, "correct"), flush=True)

    seen, won = run(page, tag, {"units": build, "move": move, "mark": mark})
    print(tag, "board tasks:", seen, "stars:", won, flush=True)
    assert seen == ["units", "move", "mark"] and all(won[:3]), (seen, won)


def ages(page, tag):
    """Template D on constant_difference-ages: "+ years" on one bar is caught, then on both."""
    start(page, "constant_difference-ages")

    def grow(page):
        page.locator(".b-row[data-bar='son_n'] .b-grow").click()
        page.click("#builderCheck")
        say = verdict(page, "try_again")
        print(tag, "  one bar ->", say, flush=True)
        assert "Does only Son get older?" in say
        page.locator(".b-row[data-bar='dad_n'] .b-grow").click()
        shot(page, f"{tag}-d1-grow")
        page.click("#builderCheck")
        print(tag, "  both ->", verdict(page, "correct"), flush=True)

    def mark(page):
        page.click(".b-seg[data-bar='son_l'][data-seg='1']")
        page.click("#builderCheck")
        print(tag, "  ? ->", verdict(page, "correct"), flush=True)

    seen, won = run(page, tag, {"grow": grow, "mark": mark})
    print(tag, "board tasks:", seen, "stars:", won, flush=True)
    assert seen == ["grow", "mark"] and won[1] and won[2], (seen, won)  # star 1 is "41 take away 9" (shown)


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    errors = []
    phone, desk = p.devices["iPhone 13"], {"viewport": {"width": 1366, "height": 800}}
    for tag, opts, fn in (("iphone13", phone, play), ("desktop", desk, play),
                          ("desktop", desk, transfer), ("iphone13", phone, ages)):
        ctx = browser.new_context(**opts)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        fn(page, tag)
        ctx.close()
    browser.close()
    if errors:
        print("page errors:", errors)
        sys.exit(1)
    print("OK")
