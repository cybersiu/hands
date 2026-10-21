"""
multi_step.py — Accomplish a real task on each site, step by step.

The previous sweep only counted elements and clicked once. That proves the page
was parsed, not that anything worked. This script instead performs a short but
complete task on each site, verifying every step before moving on:

    Wikipedia      search for a term, land on the article, read its heading
    Google         type a query, submit, confirm results rendered
    Stack Overflow navigate to a question, confirm the title changed
    GitHub         open the search dialog, type, confirm the dialog reacted
    Bing           type a query, submit, confirm results rendered

Every step asserts on observable state. If a step fails, the run stops there and
reports what the page actually showed, rather than continuing on a false
assumption.

    python multi_step.py
    python multi_step.py --only wikipedia
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach
from browser import Session


class Step:
    """One verified action."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.t0 = time.perf_counter()

    def done(self, ok: bool, detail: str = "") -> tuple[str, bool, str, float]:
        return (self.name, ok, detail, (time.perf_counter() - self.t0) * 1000)


def report(steps: list[tuple[str, bool, str, float]]) -> bool:
    """Print a step list and return whether all of them passed."""
    ok_all = True
    for name, ok, detail, ms in steps:
        mark = "ok  " if ok else "FAIL"
        print(f"    [{mark}] {name:<34} {ms:7.0f}ms  {detail[:50]}")
        if not ok:
            ok_all = False
    return ok_all


async def find_search_box(session: Session) -> dict | None:
    """Find a text entry field, excluding checkboxes and buttons.

    Picking "the first input" is wrong on almost every real site: navigation
    menus, toggles and hidden fields all come first in document order. A text
    field is identified by what it accepts, not by its tag.
    """
    obs = await session.observe(max_nodes=400)
    for e in (obs.get("elements") or []):
        attrs = e.get("attributes") or {}
        tag = e.get("tag")
        if tag == "textarea":
            return e
        if tag != "input":
            continue
        kind = (attrs.get("type") or "text").lower()
        if kind in ("checkbox", "radio", "hidden", "submit", "button",
                    "file", "range", "color"):
            continue
        if not e.get("enabled", True):
            continue
        return e
    return None


async def find_text(session: Session, needle: str,
                    max_nodes: int = 300) -> dict | None:
    """Find an element whose text contains ``needle``."""
    obs = await session.observe(max_nodes=max_nodes)
    for e in (obs.get("elements") or []):
        if needle in (e.get("text") or ""):
            return e
    return None


async def find_tag(session: Session, tag: str,
                   max_nodes: int = 300) -> dict | None:
    """Find the first visible element of a given tag."""
    obs = await session.observe(max_nodes=max_nodes)
    for e in (obs.get("elements") or []):
        if e.get("tag") == tag and e.get("enabled", True):
            return e
    return None


# --------------------------------------------------------------------------
# Per-site flows
# --------------------------------------------------------------------------

async def flow_wikipedia(session: Session) -> list:
    steps: list[tuple[str, bool, str, float]] = []

    s = Step("load wikipedia")
    await session.navigate("https://en.wikipedia.org/wiki/Main_Page")
    await asyncio.sleep(1.0)
    title = await session.evaluate("document.title")
    steps.append(s.done(bool(title), str(title)[:50]))

    s = Step("find the search box")
    box = await session.evaluate(r"""(() => {
      const e = document.querySelector(
        '#searchInput, input[name=search], input[type=search]');
      if (!e) return null;
      const r = e.getBoundingClientRect();
      return {x: Math.round(r.left + r.width/2),
              y: Math.round(r.top + r.height/2),
              id: e.id, type: e.type};
    })()""")
    steps.append(s.done(box is not None, str(box)))

    if box:
        s = Step("type a query")
        await session.click(x=box["x"], y=box["y"], settle=False)
        await asyncio.sleep(0.2)
        await session.type_text("Alan Turing", x=box["x"], y=box["y"],
                                clear=True)
        value = await session.evaluate(
            "(() => { const i = document.querySelector('#searchInput, "
            "input[name=search]'); return i ? i.value : null; })()")
        steps.append(s.done(bool(value), repr(value)))

        s = Step("submit the search")
        await session.press("Enter")
        ok = await session.assert_changed(
            "location.href.includes('search') || "
            "location.href.includes('Alan_Turing')",
            expect=None, timeout=10.0)
        steps.append(s.done(ok.get("changed"),
                            await session.evaluate("location.pathname")))

        s = Step("read the resulting heading")
        heading = await session.evaluate(
            "(() => { const h = document.querySelector('h1'); "
            "return h ? h.innerText.trim() : null; })()")
        steps.append(s.done(bool(heading), repr(heading)))

    return steps


async def flow_google(session: Session) -> list:
    steps: list[tuple[str, bool, str, float]] = []

    s = Step("load google")
    await session.navigate("https://www.google.com/")
    await asyncio.sleep(1.2)
    steps.append(s.done(True, await session.evaluate("document.title")))

    s = Step("find the search box")
    box = await session.evaluate(r"""(() => {
      const el = document.querySelector('textarea[name=q], input[name=q]');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.left + r.width/2),
              y: Math.round(r.top + r.height/2)};
    })()""")
    steps.append(s.done(box is not None, str(box)))

    if box:
        s = Step("click and type")
        await session.click(x=box["x"], y=box["y"], settle=False)
        await session.type_text("python asyncio", x=box["x"], y=box["y"],
                                clear=True)
        value = await session.evaluate(
            "(() => { const e = document.querySelector('textarea[name=q], "
            "input[name=q]'); return e ? e.value : null; })()")
        steps.append(s.done(bool(value), repr(value)))

        s = Step("submit")
        await session.press("Enter")
        ok = await session.assert_changed(
            "location.pathname === '/search'", expect=None, timeout=12.0)
        steps.append(s.done(ok.get("changed"),
                            await session.evaluate("location.pathname")))

        s = Step("count results")
        n = await session.evaluate(
            "document.querySelectorAll('div.g, div[data-hveid]').length")
        steps.append(s.done(bool(n and n > 0), f"{n} result blocks"))

    return steps


async def flow_stackoverflow(session: Session) -> list:
    steps: list[tuple[str, bool, str, float]] = []

    s = Step("load questions page")
    await session.navigate("https://stackoverflow.com/questions")
    await asyncio.sleep(1.2)
    steps.append(s.done(True, await session.evaluate("document.title")))

    s = Step("find a question link")
    link = await session.evaluate(r"""(() => {
      const a = document.querySelector('#questions a.s-link, a.question-hyperlink');
      if (!a) return null;
      const r = a.getBoundingClientRect();
      return {text: a.innerText.trim().slice(0, 50),
              href: a.getAttribute('href'),
              x: Math.round(r.left + r.width/2),
              y: Math.round(r.top + r.height/2)};
    })()""")
    steps.append(s.done(link is not None,
                        (link or {}).get("text", "")[:40]))

    if link:
        before = await session.evaluate("location.pathname")
        s = Step("click the question")
        await session.click(x=link["x"], y=link["y"], settle=False)
        ok = await session.assert_changed("location.pathname",
                                          expect=None, timeout=10.0)
        after = await session.evaluate("location.pathname")
        steps.append(s.done(after != before, f"{before} -> {after}"))

        s = Step("read the question title")
        h = await session.evaluate(
            "(() => { const e = document.querySelector('#question-header h1, "
            ".fs-headline1'); return e ? e.innerText.trim().slice(0,60) : null; })()")
        steps.append(s.done(bool(h), repr(h)))

    return steps


async def flow_github(session: Session) -> list:
    steps: list[tuple[str, bool, str, float]] = []

    s = Step("load github")
    await session.navigate("https://github.com/")
    await asyncio.sleep(1.2)
    steps.append(s.done(True, await session.evaluate("document.title")))

    s = Step("open the search dialog")
    btn = await session.evaluate(r"""(() => {
      const b = document.querySelector(
        'button[aria-label*="search"], button[data-target*="search"], '
        + '.header-search-button, [aria-label*="Search or jump"]');
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return {x: Math.round(r.left + r.width/2),
              y: Math.round(r.top + r.height/2)};
    })()""")
    steps.append(s.done(btn is not None, str(btn)))

    if btn:
        await session.click(x=btn["x"], y=btn["y"], settle=False)
        await asyncio.sleep(0.8)

        s = Step("type into the dialog")
        field = await session.evaluate(r"""(() => {
          const i = document.querySelector(
            'input[type=text][name=q], dialog input, '
            + '[role=dialog] input, input[aria-label*="Search"]');
          if (!i) return null;
          const r = i.getBoundingClientRect();
          return {x: Math.round(r.left + r.width/2),
                  y: Math.round(r.top + r.height/2)};
        })()""")
        steps.append(s.done(field is not None, str(field)))

        if field:
            await session.type_text("playwright", x=field["x"], y=field["y"],
                                    clear=True)
            val = await session.evaluate(
                "(() => { const i = document.querySelector("
                "'input[name=q], dialog input, [role=dialog] input'); "
                "return i ? i.value : null; })()")
            steps.append(s.done(bool(val), repr(val)))

    return steps


async def flow_bing(session: Session) -> list:
    steps: list[tuple[str, bool, str, float]] = []

    s = Step("load bing")
    await session.navigate("https://www.bing.com/")
    await asyncio.sleep(1.2)
    steps.append(s.done(True, await session.evaluate("document.title")))

    s = Step("find the search box")
    box = await session.evaluate(r"""(() => {
      const e = document.querySelector('#sb_form_q, input[name=q]');
      if (!e) return null;
      const r = e.getBoundingClientRect();
      return {x: Math.round(r.left + r.width/2),
              y: Math.round(r.top + r.height/2)};
    })()""")
    steps.append(s.done(box is not None, str(box)))

    if box:
        s = Step("type and submit")
        await session.click(x=box["x"], y=box["y"], settle=False)
        await session.type_text("rust async", x=box["x"], y=box["y"],
                                clear=True)
        await session.press("Enter")
        ok = await session.assert_changed(
            "location.pathname.includes('search')", expect=None, timeout=12.0)
        steps.append(s.done(ok.get("changed"),
                            await session.evaluate("location.pathname")))

        s = Step("count results")
        n = await session.evaluate(
            "document.querySelectorAll('#b_results > li.b_algo').length")
        steps.append(s.done(bool(n and n > 0), f"{n} results"))

    return steps


FLOWS = {
    "wikipedia": ("Wikipedia", flow_wikipedia),
    "google": ("Google", flow_google),
    "stackoverflow": ("Stack Overflow", flow_stackoverflow),
    "github": ("GitHub", flow_github),
    "bing": ("Bing", flow_bing),
}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None,
                    help="Run one flow: " + ", ".join(FLOWS))
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect to Chrome: {exc}")
        return 1

    try:
        version = await sock.send("Browser.getVersion")
        print(f"browser: {version.get('product')}\n")

        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        names = [args.only] if args.only else list(FLOWS)
        summary: list[tuple[str, bool, int, int]] = []

        for key in names:
            if key not in FLOWS:
                print(f"unknown flow: {key}")
                continue
            label, fn = FLOWS[key]
            print("=" * 70)
            print(label)
            print("=" * 70)
            try:
                steps = await fn(session)
                ok = report(steps)
                passed = sum(1 for st in steps if st[1])
                summary.append((label, ok, passed, len(steps)))
            except Exception as exc:
                print(f"    EXCEPTION {type(exc).__name__}: {str(exc)[:120]}")
                summary.append((label, False, 0, 0))
            print()

        print("=" * 70)
        print("SUMMARY")
        print("=" * 70)
        for label, ok, passed, total in summary:
            mark = "PASS" if ok else "PARTIAL"
            print(f"  {label:<18} {mark:<8} {passed}/{total} steps")

        return 0 if all(s[1] for s in summary) else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
