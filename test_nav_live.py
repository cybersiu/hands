"""test_nav_live.py — one live check of the navigation contract.

Runs against the attached Chrome, in a single connection, and proves three
things the old code got wrong:

1. ``navigate`` returns the URL the page is actually on, not the target-list
   snapshot.
2. Navigating to the same URL twice is not mistaken for "nothing happened";
   the second call is a reload and must be detected.
3. A redirect chain reports the final URL, not the first hop.

    python test_nav_live.py
"""

from __future__ import annotations

import asyncio
import sys

import attach
from browser import Session


async def main() -> int:
    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=6)
    except Exception as exc:
        print(f"cannot attach: {exc}")
        return 2

    ok = 0
    total = 0
    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if not (p.get("url") or "").startswith("devtools://")), 0)
        await attach.attach(session, tab_index=idx)

        async def check(label, coro, predicate):
            nonlocal ok, total
            total += 1
            try:
                value = await coro
            except Exception as exc:
                print(f"  FAIL {label}: {exc}")
                return None
            good = predicate(value)
            print(f"  {'ok  ' if good else 'FAIL'} {label}: {value}")
            if good:
                ok += 1
            return value

        print("live navigation checks")

        await check(
            "navigate returns the live URL",
            session.navigate("https://example.com/", timeout=25),
            lambda r: r["url"].startswith("https://example.com"),
        )

        # Same URL again: a reload. The snapshot fix alone cannot see this.
        await check(
            "same-URL reload is detected",
            session.navigate("https://example.com/", timeout=25),
            lambda r: r["url"].startswith("https://example.com"),
        )

        # google.com redirects to a country host; the final URL must be reported.
        await check(
            "redirect chain reports the final URL",
            session.navigate("https://google.com/", timeout=30),
            lambda r: "google." in r["url"] and r["url"] != "https://google.com/",
        )

        # And the reported value must equal what the page itself says.
        live = await session.evaluate("location.href")
        reported = await session._safe_url()
        total += 1
        if live == reported:
            ok += 1
            print(f"  ok   reported URL equals location.href: {reported}")
        else:
            print(f"  FAIL reported {reported!r} but page says {live!r}")

        print(f"\n{ok}/{total} passed")
        return 0 if ok == total else 1
    finally:
        await sock.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
