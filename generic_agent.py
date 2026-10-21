"""
generic_agent.py — Accomplish tasks without a single hardcoded selector.

The earlier scripts failed the same way: each site needed its own CSS selector,
so the tool only worked where someone had already done the work. That is not a
general browser agent, it is a pile of scripts.

This module uses only what ``observe`` reports — tag, text, role, name — and
picks targets the way a person would: read the labels, choose the one that
means "search", type, press Enter, check that something changed.

Nothing here knows that Wikipedia calls its field #searchInput, or that Bing
uses #sb_form_q. If a site labels its controls sensibly, this works. If it does
not, the failure is reported with the labels that were actually present, which
is information a caller can act on.

    python generic_agent.py --site youtube --query "lofi hip hop"
    python generic_agent.py --site wikipedia --query "Alan Turing"
    python generic_agent.py --url https://example.com --query test
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


#: Tags that accept typed text. Chosen by behaviour, not by site.
TEXT_TAGS = {"input", "textarea"}

#: Input types that never accept free text.
NON_TEXT_INPUT = {
    "checkbox", "radio", "hidden", "submit", "button", "reset",
    "file", "image", "range", "color", "date", "time", "week",
    "month", "datetime-local",
}

#: Words that mean "this is the thing you type a query into".
SEARCH_WORDS = (
    "search", "find", "query", "look up", "type to",
    "\u641c\u5c0b", "\u641c\u7d22", "\u641c", "\u5c0b\u627e",
    "\u691c\u7d22",
)


def looks_like_text_field(e: dict) -> bool:
    """Does this element accept typed text?"""
    tag = (e.get("tag") or "").lower()
    attrs = e.get("attributes") or {}
    if tag == "textarea":
        return True
    if tag == "input":
        kind = (attrs.get("type") or "text").lower()
        return kind not in NON_TEXT_INPUT
    if tag == "div" and attrs.get("contenteditable") == "true":
        return True
    return False


def search_score(e: dict) -> int:
    """Rank a text field by how much it looks like a search box.

    Higher is better. The score uses only what the page itself exposes, so it
    transfers across sites without per-site knowledge.
    """
    if not looks_like_text_field(e):
        return -1

    score = 0
    hay = " ".join([
        (e.get("text") or ""),
        (e.get("name") or ""),
        " ".join((e.get("attributes") or {}).values()),
        (e.get("role") or ""),
    ]).lower()

    for word in SEARCH_WORDS:
        if word in hay:
            score += 10
            break

    attrs = e.get("attributes") or {}
    if (attrs.get("type") or "").lower() == "search":
        score += 15
    if attrs.get("name") == "q":
        score += 12
    if attrs.get("role") == "searchbox":
        score += 12
    if "search" in (attrs.get("id") or "").lower():
        score += 8
    if "search" in (attrs.get("class") or "").lower():
        score += 6

    # A field near the top of the page is usually the site-wide one.
    y = e.get("y", 9999)
    if y < 120:
        score += 6
    elif y < 300:
        score += 3

    # Wide fields are more likely to be the main input than narrow ones.
    if e.get("width", 0) > 200:
        score += 4

    return score


def describe(e: dict) -> str:
    """One-line description for logs."""
    label = (e.get("text") or e.get("name") or "")[:34]
    attrs = e.get("attributes") or {}
    return (f"{e.get('tag')} {label!r} "
            f"type={attrs.get('type', '-')} "
            f"({round(e.get('x', 0))},{round(e.get('y', 0))})")


async def find_search_field(session: Session,
                            max_nodes: int = 400) -> tuple[dict | None, list[dict]]:
    """Return the best text field and the ranked candidates."""
    obs = await session.observe(max_nodes=max_nodes)
    els = obs.get("elements") or []
    ranked = [(search_score(e), e) for e in els]
    ranked = [(s, e) for s, e in ranked if s >= 0]
    ranked.sort(key=lambda p: -p[0])
    candidates = [e for _s, e in ranked[:6]]
    best = candidates[0] if candidates else None
    return best, candidates


async def type_and_submit(session: Session, field: dict, query: str) -> dict:
    """Focus the field, type, then submit with Enter.

    Returns a report rather than raising, so a caller can decide what to do with
    a partial failure.
    """
    out: dict = {"field": describe(field)}

    res = await session.type_text(query, ref=field["ref"], clear=True)
    out["typed"] = res.get("typed")

    active = res.get("active") or {}
    out["active_after_typing"] = active
    # The value proves the text landed; active only proves focus.
    value = active.get("value")
    out["value"] = value
    out["text_landed"] = bool(value and query[:6].lower() in str(value).lower())

    if not out["text_landed"]:
        return out

    before = await session.evaluate("location.href")
    await session.press("Enter")
    changed = await session.assert_changed(
        "location.href", expect=None, timeout=8.0, interval=0.4)
    after = await session.evaluate("location.href")

    out["url_before"] = before
    out["url_after"] = after
    out["navigated"] = after != before
    out["waited"] = changed.get("elapsed")

    # If Enter did nothing, try the form's own submit button.
    if not out["navigated"]:
        out["submit_fallback"] = await try_submit_button(session)

    return out


async def try_submit_button(session: Session) -> dict:
    """Click a button whose label means submit, if one exists."""
    obs = await session.observe(max_nodes=300)
    words = ("search", "go", "submit", "\u641c\u5c0b", "\u641c\u7d22",
             "\u641c", "\u9001\u51fa", "\u78ba\u5b9a")
    for e in (obs.get("elements") or []):
        if (e.get("tag") or "") not in ("button", "input"):
            continue
        hay = " ".join([(e.get("text") or ""), (e.get("name") or ""),
                        " ".join((e.get("attributes") or {}).values())]).lower()
        if any(w in hay for w in words):
            res = await session.click(ref=e["ref"], settle=False)
            await asyncio.sleep(2.0)
            return {"clicked": describe(e),
                    "url": res.get("url"),
                    "navigated": res.get("url_changed")}
    return {"clicked": None}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default=None,
                    help="youtube | wikipedia | bing | google | amazon")
    ap.add_argument("--url", default=None)
    ap.add_argument("--query", default="lofi hip hop")
    args = ap.parse_args()

    sites = {
        "youtube": "https://www.youtube.com/",
        "wikipedia": "https://en.wikipedia.org/wiki/Main_Page",
        "bing": "https://www.bing.com/",
        "google": "https://www.google.com/",
        "amazon": "https://www.amazon.com/",
    }
    url = args.url or sites.get(args.site or "")
    if not url:
        print("Pass --site or --url")
        return 1

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect to Chrome: {exc}")
        return 1

    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        print(f"site : {url}")
        print(f"query: {args.query!r}\n")

        t0 = time.perf_counter()
        await session.navigate(url)
        await asyncio.sleep(2.0)
        print(f"load: {(time.perf_counter()-t0)*1000:.0f} ms")

        field, candidates = await find_search_field(session)
        print(f"\n{len(candidates)} text field(s), ranked:")
        for c in candidates:
            print(f"  score={search_score(c):>3}  {describe(c)}")

        if field is None:
            print("\nNo text field found. The page may not have loaded, or its")
            print("inputs are inside a frame this tool cannot reach.")
            return 1

        print(f"\nchosen: {describe(field)}")
        report = await type_and_submit(session, field, args.query)

        print("\nresult:")
        for k, v in report.items():
            print(f"  {k:<20} {v}")

        ok = report.get("navigated") or report.get("text_landed")
        print(f"\n{'PASS' if report.get('navigated') else 'PARTIAL'}: "
              f"text={'landed' if report.get('text_landed') else 'lost'}, "
              f"nav={'yes' if report.get('navigated') else 'no'}")
        return 0 if ok else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
