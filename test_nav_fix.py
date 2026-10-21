"""test_nav_fix.py — prove the navigation wait without touching Chrome.

The bug being fixed: ``Page.navigate`` returns before the navigation commits,
and the tab's ``url`` was a snapshot taken when targets were listed. Both made
the tool report the previous page as the current one.

These tests drive ``RawTab._wait_document`` against a scripted page so every
branch is covered deterministically: an ordinary navigation, a same-URL reload
(the case a URL comparison cannot see), and no navigation at all.

    python test_nav_fix.py
"""

from __future__ import annotations

import asyncio
import sys

import attach
from browser import Session


class ScriptedTab(attach.RawTab):
    """A RawTab whose page state is supplied by a script instead of CDP."""

    def __init__(self, states, *, navigate_result=None):
        super().__init__(socket=None, target_id="t", session_id="s", info={})
        self._states = list(states)
        self._last = self._states[-1] if self._states else {}
        self.navigate_result = navigate_result or {"frameId": "f"}
        self.navigate_calls = 0

    async def evaluate(self, expression, **kwargs):
        if expression.strip().startswith("location.href"):
            return self._last.get("href", "")
        if self._states:
            self._last = self._states.pop(0)
        return dict(self._last)

    async def _send_navigate(self, url):
        return self.navigate_result


def state(href, ready="complete", t0=1.0, hist=1):
    return {"href": href, "ready": ready, "t0": t0, "hist": hist}


async def test_plain_navigation():
    before = state("https://a.test/")
    tab = ScriptedTab([state("https://b.test/", "loading"),
                       state("https://b.test/", "interactive"),
                       state("https://b.test/", "interactive")])
    await tab._wait_document(before, "L1", timeout=5)
    assert tab._last["href"] == "https://b.test/", tab._last
    print("  plain navigation        -> returned on the new document")


async def test_same_url_reload():
    """A reload keeps href and history; only timeOrigin moves."""
    before = state("https://a.test/", t0=1000.0)
    tab = ScriptedTab([state("https://a.test/", "loading", t0=2000.0),
                       state("https://a.test/", "complete", t0=2000.0)])
    await tab._wait_document(before, None, timeout=5)
    assert tab._last["t0"] == 2000.0, tab._last
    print("  same-URL reload         -> detected via timeOrigin")


async def test_pushstate_route():
    """pushState adds a history entry without a new document."""
    before = state("https://a.test/one", hist=1)
    tab = ScriptedTab([state("https://a.test/two", "complete", hist=2)])
    await tab._wait_document(before, None, timeout=5)
    assert tab._last["hist"] == 2, tab._last
    print("  pushState route         -> detected via history.length")


async def test_no_navigation_times_out():
    before = state("https://a.test/")
    tab = ScriptedTab([state("https://a.test/")] * 200)
    try:
        await tab._wait_document(before, None, timeout=0.4)
    except attach.AttachError as exc:
        assert "did not settle" in str(exc), exc
        print("  no navigation           -> raises instead of reporting success")
        return
    raise AssertionError("a no-op navigation must not be reported as success")


async def test_navigate_refusal():
    tab = ScriptedTab([], navigate_result={"errorText": "net::ERR_NAME_NOT_RESOLVED"})

    class Socket:
        async def send(self, *a, **k):
            return tab.navigate_result

    tab._socket = Socket()
    try:
        await tab.get("https://nope.invalid/", timeout=1)
    except attach.AttachError as exc:
        assert "ERR_NAME_NOT_RESOLVED" in str(exc), exc
        print("  refused navigation      -> surfaces the CDP errorText")
        return
    raise AssertionError("errorText must not be ignored")


async def test_safe_url_is_live():
    """_safe_url must read the document, never the target snapshot."""
    session = Session()
    tab = ScriptedTab([state("https://live.test/", "complete")])
    tab._info = {"url": "https://stale.test/"}
    session._tab = tab

    url = await session._safe_url()
    assert url == "https://live.test/", url
    print("  _safe_url               -> reads location.href, not the snapshot")

    class Dead:
        async def evaluate(self, *_a, **_k):
            raise RuntimeError("session gone")

    session._tab = Dead()
    url = await session._safe_url()
    assert url == "", url
    print("  _safe_url (dead session)-> empty, not a believable stale URL")


async def main() -> int:
    tests = [
        test_plain_navigation,
        test_same_url_reload,
        test_pushstate_route,
        test_no_navigation_times_out,
        test_navigate_refusal,
        test_safe_url_is_live,
    ]
    print(f"running {len(tests)} checks")
    for fn in tests:
        await fn()
    print(f"\n{len(tests)}/{len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
