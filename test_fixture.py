"""
test_fixture.py — Drive the fixture site with the hands API.

Each test reproduces a page shape that broke a real attempt, and asserts on
observable page state rather than on the click returning success.

    python test_fixture.py
    python test_fixture.py --keep-open      # leave Chrome open afterwards

Requires a desktop session: the browser is visible.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

import fixture_site
from browser import HandsError, Session

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not ok else ""))


async def js(session: Session, expr: str):
    return await session.evaluate(expr)


# --------------------------------------------------------------------------
# /colab — scrim, shadow DOM, slotted buttons
# --------------------------------------------------------------------------

async def test_colab(session: Session, base: str) -> None:
    print("\n== colab: scrim + shadow DOM ==")
    await session.navigate(f"{base}/colab")
    await asyncio.sleep(0.4)

    # The dialog is present from load.
    has = await js(session, "window.__test.hasDialog()")
    check("blocking dialog present on load", has is True, repr(has))

    # A click on the run-all area must be reported as blocked.
    obs = await session.observe(max_nodes=80)
    run_all = next((e for e in obs["elements"]
                    if "Run all" in (e.get("text") or "")), None)
    check("run-all found through shadow DOM", run_all is not None,
          f"{len(obs['elements'])} elements seen")

    if run_all:
        res = await session.click(ref=run_all["ref"])
        blocked = bool((res.get("hit") or {}).get("blocked"))
        check("click reported as blocked by the scrim", blocked,
              str(res.get("hit", {}).get("top")))
        check("blocked click did not change the URL",
              res.get("url_changed") is False, str(res.get("url_changed")))

        # The scrim itself carries no text; the dialog behind it does. What
        # matters is that the interception was detected and named.
        hit = res.get("hit") or {}
        check("blocker detected and named",
              bool(hit.get("blocked")) and bool(hit.get("blocker")),
              f"blocked={hit.get('blocked')} blocker={hit.get('blocker')!r}")

    # Dismissing it makes the page usable.
    dis = await session.dismiss_blocker()
    check("dismiss_blocker closed the dialog", dis.get("dismissed") is True,
          str(dis))
    check("dialog gone from the page",
          await js(session, "window.__test.hasDialog()") is False)

    # Connect, then run all. State must actually advance.
    obs = await session.observe(max_nodes=80)
    connect = next((e for e in obs["elements"]
                    if "Connect" in (e.get("text") or "")), None)
    if connect:
        await session.click(ref=connect["ref"])
        await asyncio.sleep(0.3)
        connected = await js(session, "window.__test.state().connected")
        check("connect took effect", connected is True, repr(connected))
    else:
        check("connect button found", False)

    obs = await session.observe(max_nodes=80)
    run_all = next((e for e in obs["elements"]
                    if "Run all" in (e.get("text") or "")), None)
    if run_all:
        await session.click(ref=run_all["ref"])
        changed = await session.assert_changed(
            "window.__test.state().executed.filter(v => v !== null).length",
            expect="3", timeout=8.0,
        )
        check("run-all executed every cell", changed.get("changed") is True,
              str(changed))
    else:
        check("run-all found after dismiss", False)

    # Refusal path: a disconnected runtime refuses run-all.
    await js(session, "window.__test.reset()")
    await session.dismiss_blocker()
    obs = await session.observe(max_nodes=80)
    run_all = next((e for e in obs["elements"]
                    if "Run all" in (e.get("text") or "")), None)
    if run_all:
        await session.click(ref=run_all["ref"])
        await asyncio.sleep(0.8)
        st = await js(session, "window.__test.state()")
        refused = any("refused" in line for line in (st or {}).get("log", []))
        check("run-all refused while disconnected", refused,
              str((st or {}).get("log")))


# --------------------------------------------------------------------------
# /kaggle — nested iframes, delayed modal, form in the deepest frame
# --------------------------------------------------------------------------

async def test_kaggle(session: Session, base: str) -> None:
    print("\n== kaggle: nested iframes + delayed modal ==")
    await session.navigate(f"{base}/kaggle")
    await asyncio.sleep(0.3)

    # Before the delay the modal is absent.
    visible_now = await js(session, "window.__test.lateVisible()")
    check("modal not yet visible immediately after load",
          visible_now is False, repr(visible_now))

    # The innermost form must be reachable through three frames.
    obs = await session.observe(max_nodes=120)
    title_field = next((e for e in obs["elements"]
                        if e.get("tag") == "input"
                        and "title" in (e.get("attributes") or {}).get("id", "")),
                       None)
    if title_field is None:
        title_field = next((e for e in obs["elements"]
                            if e.get("tag") == "input"), None)
    check("input inside nested iframes discovered", title_field is not None,
          f"{len(obs['elements'])} elements")
    if title_field:
        check("input marked as in_frame",
              title_field.get("in_frame") is True,
              str(title_field.get("in_frame")))

    # Wait for the delayed modal, then confirm it blocks.
    await asyncio.sleep(2.6)
    visible_later = await js(session, "window.__test.lateVisible()")
    check("delayed modal appeared", visible_later is True, repr(visible_later))

    # Dismiss it (it is a plain div overlay, not a custom element).
    obs = await session.observe(max_nodes=120)
    accept = next((e for e in obs["elements"]
                   if (e.get("text") or "").strip() == "Accept"), None)
    check("modal button found", accept is not None,
          str([e.get("text") for e in obs["elements"] if e.get("text")][:10]))
    if accept:
        await session.click(ref=accept["ref"])
        await asyncio.sleep(0.3)
        accepted = await js(session, "window.__test.state().accepted")
        check("accepting the modal took effect", accepted is True, repr(accepted))

    # Fill the form in the deepest frame and submit.
    obs = await session.observe(max_nodes=120)
    field = next((e for e in obs["elements"]
                  if e.get("tag") == "input"), None)
    if field:
        await session.type_text("my dataset", ref=field["ref"])
        obs = await session.observe(max_nodes=120)
        save = next((e for e in obs["elements"]
                     if "Save" in (e.get("text") or "")), None)
        if save:
            await session.click(ref=save["ref"])
            saved = await session.assert_changed(
                "window.__test.state().saved && window.__test.state().saved.title",
                expect="my dataset", timeout=5.0,
            )
            check("form submitted from the deepest frame",
                  saved.get("changed") is True, str(saved))
        else:
            check("save button found", False)
    else:
        check("input found for typing", False)


# --------------------------------------------------------------------------
# /yahoo — table markup, image links, cookie banner
# --------------------------------------------------------------------------

async def test_yahoo(session: Session, base: str) -> None:
    print("\n== yahoo: table markup + cookie banner ==")
    await session.navigate(f"{base}/yahoo")
    await asyncio.sleep(0.3)

    banner = await js(session, "window.__test.bannerVisible()")
    check("cookie banner present", banner is True, repr(banner))

    obs = await session.observe(max_nodes=80)
    buy = next((e for e in obs["elements"]
                if (e.get("text") or "").strip() == "Buy now"), None)
    check("image-styled link found by text", buy is not None,
          str([e.get("text") for e in obs["elements"] if e.get("text")][:10]))

    # The banner is fixed to the bottom; a click near it must be reported.
    if buy:
        await session.click(ref=buy["ref"])
        await asyncio.sleep(0.3)
        st = await js(session, "window.__test.state()")
        clicks = (st or {}).get("clicks") or []
        check("table link click registered", len(clicks) >= 1, str(st))

    # Dismiss the banner via dismiss_blocker, which prefers a secondary action.
    dis = await session.dismiss_blocker()
    check("dismiss_blocker handled the banner",
          dis.get("dismissed") is True, str(dis))
    still = await js(session, "window.__test.bannerVisible()")
    check("banner gone", still is False, repr(still))

    cookies = await js(session, "window.__test.state().cookies")
    check("banner used a dismissal action, not Accept all",
          cookies in ("rejected", None), str(cookies))


# --------------------------------------------------------------------------
# /canvas — no DOM, positional clicks
# --------------------------------------------------------------------------

async def test_canvas(session: Session, base: str) -> None:
    print("\n== canvas: no DOM, positional clicks ==")
    await session.navigate(f"{base}/canvas")
    await asyncio.sleep(0.3)

    obs = await session.observe(max_nodes=80)
    buttons = [e for e in obs["elements"] if e.get("tag") == "button"]
    check("no interactive DOM elements to find", len(buttons) == 0,
          f"{len(buttons)} buttons")

    # Ask the page where a target is, then click that point.
    centre = await js(session, "window.__test.centre('B')")
    check("page can report a canvas target position", centre is not None,
          str(centre))

    if centre:
        await session.click(x=centre["x"], y=centre["y"])
        await asyncio.sleep(0.3)
        st = await js(session, "window.__test.state()")
        hits = (st or {}).get("hits") or []
        check("positional click hit the canvas target", "B" in hits, str(st))

    # A deliberate miss proves the assertion above is meaningful.
    await session.click(x=10, y=10)
    await asyncio.sleep(0.2)
    st = await js(session, "window.__test.state()")
    check("miss did not register a hit",
          len((st or {}).get("hits") or []) == 1, str(st))


# --------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    port = fixture_site.free_port()
    server = fixture_site.serve(port)
    base = f"http://127.0.0.1:{port}"
    print(f"fixture site: {base}")

    session = Session()
    try:
        await session.start(headless=False, url=f"{base}/")
        await test_colab(session, base)
        await test_kaggle(session, base)
        await test_yahoo(session, base)
        await test_canvas(session, base)
    finally:
        if not args.keep_open:
            await session.stop()
        server.shutdown()

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r[0] == PASS)
    failed = len(results) - passed
    print(f"{passed} passed, {failed} failed, {len(results)} total")
    if failed:
        print("\nFailures:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"  - {name}: {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
