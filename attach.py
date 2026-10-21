"""
attach.py — Attach to a running Chrome over its DevTools WebSocket.

How Chrome 144+ remote debugging actually works
-----------------------------------------------
Enabling remote debugging at ``chrome://inspect/#remote-debugging`` and
approving a connection makes Chrome write ``DevToolsActivePort`` into its
user-data directory:

    line 1: 9222
    line 2: /devtools/browser/85038e3e-...

Three facts follow, and each one breaks a common assumption:

1. **HTTP 404s.** ``/json/version``, ``/json/list`` and ``/`` all return 404.
   The port is not a normal debug port; it speaks only the approval protocol.
   Anything that discovers browsers over HTTP (including zendriver's
   ``Browser.create``) fails here.

2. **The browser WebSocket works.** Line 2 is a live CDP endpoint. Opening it
   and sending ``Browser.getVersion`` returns the Chrome version.

3. **Page WebSockets are refused.** ``/devtools/page/<id>`` returns HTTP 403.
   Per-page sockets are disabled in approval mode. Everything must go through
   the *browser* socket using **flat sessions**:
   ``Target.attachToTarget {flatten: true}`` yields a session id, and every
   subsequent command carries that ``sessionId`` on the same socket.

This module implements (3) directly, so no Puppeteer is needed. It exposes a
``RawTab`` whose ``send()`` mirrors zendriver's ``Tab.send()`` signature, which
means ``browser.py``'s observe/click/type code runs unchanged against either a
launched browser or an attached one.

Usage
-----
    session = Session()
    await attach(session)                       # default Chrome profile
    await attach(session, user_data_dir=...)    # explicit profile
    await attach(session, ws_url=...)           # explicit endpoint
    await attach(session, tab_index=1)          # pick a specific tab
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class AttachError(RuntimeError):
    """The running Chrome could not be attached to, with an actionable reason."""


# --------------------------------------------------------------------------
# Locating the endpoint
# --------------------------------------------------------------------------

def chrome_user_data_root() -> Path:
    """Default Chrome user-data directory for this platform."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def read_devtools_port(user_data_dir: Path | str | None = None) -> tuple[int, str]:
    """Parse ``DevToolsActivePort`` into ``(port, ws_path)``."""
    root = Path(user_data_dir).expanduser() if user_data_dir else chrome_user_data_root()
    port_file = root / "DevToolsActivePort"

    if not port_file.exists():
        raise AttachError(
            f"No DevToolsActivePort in {root}.\n"
            "Open Chrome, go to chrome://inspect/#remote-debugging, enable "
            "remote debugging, then approve the incoming connection."
        )

    try:
        raw = port_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AttachError(f"Cannot read {port_file}: {exc}") from exc

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise AttachError(
            f"{port_file} is incomplete: expected a port and a WebSocket path, "
            f"found {len(lines)} non-empty line(s). Re-approve in Chrome."
        )

    port_text, ws_path = lines[0], lines[1]
    try:
        port = int(port_text, 10)
    except ValueError as exc:
        raise AttachError(f"Invalid port {port_text!r} in {port_file}") from exc
    if not (0 < port <= 65535):
        raise AttachError(f"Port {port} is out of range in {port_file}")
    if not ws_path.startswith("/devtools/"):
        raise AttachError(
            f"Unexpected path {ws_path!r} in {port_file}; "
            "expected something like /devtools/browser/<uuid>"
        )

    return port, ws_path


def ws_endpoint(user_data_dir: Path | str | None = None) -> str:
    """Full WebSocket URL for the running Chrome."""
    port, path = read_devtools_port(user_data_dir)
    return f"ws://127.0.0.1:{port}{path}"


def _site(url: str) -> str:
    """Host of ``url``, with a leading ``www.`` removed.

    Used only to decide whether an open tab is already on the site the caller
    wants, so that a second visit reuses it instead of opening another tab.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------
# Flat-session CDP transport
# --------------------------------------------------------------------------

#: Events that must not be dropped when a queue overflows. They are the ones
#: that carry navigation identity or session routing; losing one means a waiter
#: times out for no visible reason.
_CRITICAL_EVENTS = frozenset({
    "Page.frameNavigated",
    "Page.frameStartedNavigating",
    "Page.frameStoppedLoading",
    "Page.lifecycleEvent",
    "Page.loadEventFired",
    "Page.domContentEventFired",
    "Page.navigatedWithinDocument",
    "Runtime.executionContextCreated",
    "Runtime.executionContextDestroyed",
    "Runtime.executionContextsCleared",
    "Inspector.detached",
})


def _is_critical(method: str | None) -> bool:
    return bool(method) and (method in _CRITICAL_EVENTS or method.startswith("Target."))


class BrowserSocket:
    """One WebSocket to Chrome, multiplexing many flat sessions.

    Chrome's approval mode refuses per-page sockets, so every command travels
    on this single connection with a ``sessionId`` field selecting the page.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._closed = False
        # CDP events have no ``id``; they are routed by session id. ``None`` is
        # its own bucket because browser-scoped events (Target.*) omit the
        # session id by design, which is not an error.
        self._events: dict[Any, asyncio.Queue] = {}
        #: How many events were discarded under pressure. A non-zero value here
        #: means an event consumer may have missed something.
        self.dropped = 0

    async def connect(self, timeout: float = 10.0) -> None:
        import websockets
        self._ws = await websockets.connect(self.url, max_size=2 ** 26,
                                            open_timeout=timeout)
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue

                # No id: a CDP event. Dropping these (the previous behaviour)
                # makes Page.frameNavigated and friends unusable, so queue them
                # per session. When the queue is full the oldest *droppable*
                # event goes first: losing a console message is harmless,
                # losing frameNavigated makes a waiter hang with no error.
                sid = msg.get("sessionId")
                queue = self._events.get(sid)
                if queue is None:
                    queue = asyncio.Queue(maxsize=512)
                    self._events[sid] = queue
                if queue.full():
                    kept = []
                    dropped = False
                    while not queue.empty():
                        item = queue.get_nowait()
                        if not dropped and not _is_critical(item.get("method")):
                            dropped = True
                            continue
                        kept.append(item)
                    if not dropped:
                        # Everything queued was critical; drop the oldest.
                        kept.pop(0)
                    for item in kept:
                        queue.put_nowait(item)
                    self.dropped += 1
                queue.put_nowait(msg)
        except Exception:
            pass
        finally:
            self._closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AttachError("Chrome closed the connection"))
            self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send one CDP command and await its result."""
        if self._ws is None or self._closed:
            raise AttachError("The CDP connection is not open.")

        self._id += 1
        mid = self._id
        payload: dict[str, Any] = {"id": mid, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps(payload))

        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(mid, None)
            raise AttachError(f"{method} timed out after {timeout}s") from exc

        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(
                f"{method}: {err.get('message', err)} (code {err.get('code')})"
            )
        return msg.get("result", {})

    async def wait_event(
        self,
        method: str,
        *,
        session_id: str | None = None,
        timeout: float = 10.0,
        predicate: Any = None,
    ) -> dict[str, Any]:
        """Wait for one CDP event, discarding others on the same session.

        Events that arrive before this call are already queued, so a fast
        navigation is not missed simply because the waiter started late.
        """
        queue = self._events.get(session_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=512)
            self._events[session_id] = queue

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AttachError(f"Timed out after {timeout}s waiting for {method}")
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise AttachError(
                    f"Timed out after {timeout}s waiting for {method}"
                ) from exc
            if msg.get("method") != method:
                continue
            params = msg.get("params") or {}
            if predicate is not None and not predicate(params):
                continue
            return params

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None


# --------------------------------------------------------------------------
# Tab shim
# --------------------------------------------------------------------------

#: Document identity plus readiness, read in one round trip. ``timeOrigin`` is
#: the only cheap signal that distinguishes a same-URL reload from no action at
#: all, since neither the URL nor the history length changes in that case.
_NAV_STATE_JS = """(() => ({
  href: location.href,
  ready: document.readyState,
  t0: performance.timeOrigin,
  hist: history.length,
  navType: ((performance.getEntriesByType('navigation')[0]) || {}).type || null,
}))()"""


class RawTab:
    """A page target driven through a flat session.

    ``send()`` accepts zendriver CDP generator objects, exactly like
    ``zendriver.core.tab.Tab.send``, so ``browser.py`` does not need to know
    which transport it is talking to.
    """

    def __init__(self, socket: BrowserSocket, target_id: str,
                 session_id: str, info: dict[str, Any]) -> None:
        self._socket = socket
        self.target_id = target_id
        self.session_id = session_id
        self._info = info
        self.connection = socket  # duck-type: browser.py touches this rarely

    # -- metadata ----------------------------------------------------------

    @property
    def url(self) -> str:
        """The URL from the target list — a snapshot, not the live page.

        This value is captured when targets are enumerated and never updates.
        Use :meth:`live_url` (or read ``location.href``) for anything that has
        to be true now.
        """
        return self._info.get("url") or "about:blank"

    async def live_url(self) -> str:
        """The page's current URL, read from the document itself."""
        value = await self.evaluate("location.href")
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        return self._info.get("title") or ""

    @property
    def target(self) -> Any:
        return self._info

    # -- CDP ---------------------------------------------------------------

    async def send(self, cdp_obj: Generator[dict, dict, Any], *,
                   timeout: float = 30.0) -> Any:
        """Run a zendriver CDP generator and return its typed result.

        The generator expects to be *sent* the reply dict and then returns the
        coerced value via ``StopIteration.value``. Driving it that way gives the
        same ``cdp.dom.Node`` objects a launched browser produces.
        """
        method, params = _request(cdp_obj)
        raw = await self._socket.send(method, params,
                                      session_id=self.session_id,
                                      timeout=timeout)
        try:
            cdp_obj.send(raw)
        except StopIteration as stop:
            return stop.value
        except Exception:
            return raw
        return raw

    async def evaluate(self, expression: str, await_promise: bool = False,
                       return_by_value: bool = True) -> Any:
        result = await self._socket.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": return_by_value,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            session_id=self.session_id,
        )
        if "exceptionDetails" in result:
            desc = result["exceptionDetails"].get("text", "evaluate failed")
            raise RuntimeError(desc)
        return result.get("result", {}).get("value")

    async def wait_for_event(self, method: str, *, timeout: float = 10.0,
                             predicate: Any = None) -> dict[str, Any]:
        """Wait for one event belonging to this tab's session."""
        return await self._socket.wait_event(
            method, session_id=self.session_id, timeout=timeout,
            predicate=predicate,
        )

    async def get(self, url: str, *, timeout: float = 30.0,
                  wait: bool = True) -> "RawTab":
        """Navigate, and by default wait until the new document is ready.

        ``Page.navigate`` only acknowledges that the request was accepted: it
        returns before the navigation commits, so reading ``location.href``
        straight afterwards still sees the old page. That is why a snapshot URL
        and a live URL disagreed. Waiting here means callers never see the
        intermediate state.
        """
        before = await self._nav_state()
        result = await self._socket.send(
            "Page.navigate", {"url": url}, session_id=self.session_id,
            timeout=timeout,
        )
        error = result.get("errorText")
        if error:
            raise AttachError(f"Page.navigate refused {url}: {error}")
        if wait:
            await self._wait_document(before, result.get("loaderId"), timeout)
        return self

    async def _nav_state(self) -> dict[str, Any]:
        """Document identity, readiness and loader id, in as few trips as possible."""
        try:
            state = await self.evaluate(_NAV_STATE_JS)
        except Exception:
            return {}
        if not isinstance(state, dict):
            return {}

        # loaderId is the only value that identifies *which* document is loaded;
        # it changes when a redirect produces a new document. The frame tree is
        # one extra round trip and its failure is not fatal.
        state["loader"] = None
        if self._socket is not None:
            try:
                tree = await self._socket.send(
                    "Page.getFrameTree", session_id=self.session_id, timeout=3.0
                )
                frame = ((tree or {}).get("frameTree") or {}).get("frame") or {}
                state["loader"] = frame.get("loaderId")
            except Exception:
                pass
        return state

    async def _wait_document(self, before: dict[str, Any], loader_id: Any,
                             timeout: float, quiet_ms: float = 240.0) -> None:
        """Poll until the document that replaced ``before`` is stable.

        A settled page means three things at once: the identity changed from
        the pre-navigation snapshot, the identity stopped changing for a quiet
        window, and ``readyState`` left ``loading``.

        The quiet window is what protects against redirect hops. Returning as
        soon as ``readyState === "complete"`` looks correct and is not: a
        client-side redirect can fire 100 ms after an intermediate document
        reports complete, and the caller then reads the URL of a page that is
        already leaving.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        before_key = (before.get("href"), before.get("t0"))

        key: tuple[Any, Any, Any] | None = None
        quiet_since: float | None = None
        changed = False
        last: dict[str, Any] = before

        while loop.time() < deadline:
            await asyncio.sleep(0.04)
            state = await self._nav_state()

            # An unreadable document (context destroyed mid-navigation) is not
            # evidence of anything; it must not count as a settled page.
            if not state or not state.get("href"):
                quiet_since = None
                continue

            last = state
            if (state.get("href"), state.get("t0")) != before_key:
                changed = True

            new_key = (state.get("href"), state.get("t0"), state.get("loader"))
            if new_key != key:
                key = new_key
                quiet_since = loop.time()
                continue

            if state.get("ready") == "loading":
                quiet_since = None
                continue

            if quiet_since is None:
                quiet_since = loop.time()
                continue

            if changed and (loop.time() - quiet_since) * 1000.0 >= quiet_ms:
                return

        raise AttachError(
            f"Navigation did not settle within {timeout}s "
            f"(before={before.get('href')!r} now={last.get('href')!r} "
            f"ready={last.get('ready')!r} loaderId={last.get('loader')!r} "
            f"firstLoaderId={loader_id!r} changed={changed})"
        )

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.send("Target.closeTarget",
                                    {"targetId": self.target_id})

    async def bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            await self._socket.send("Page.bringToFront", {},
                                    session_id=self.session_id)


def _request(cdp_obj: Any) -> tuple[str, dict[str, Any]]:
    """Extract (method, params) from a zendriver CDP command generator.

    The generator yields ``{"method": ..., "params": {...}}`` and expects the
    reply to be *sent back* into it; the coerced value comes out through
    ``StopIteration.value``. Callers must keep the generator alive and send it
    the raw result — that is what produces typed ``cdp.dom.Node`` objects.
    """
    if not hasattr(cdp_obj, "__next__"):
        raise TypeError(f"Expected a CDP command generator, got {type(cdp_obj)!r}")

    payload = next(cdp_obj)
    if not isinstance(payload, dict) or "method" not in payload:
        raise TypeError(f"Unexpected CDP payload: {payload!r}")

    return payload["method"], payload.get("params") or {}


# --------------------------------------------------------------------------
# Attaching
# --------------------------------------------------------------------------

async def probe_ws(url: str, timeout: float = 6.0) -> bool:
    """Confirm the endpoint answers a CDP handshake.

    A port file on disk proves nothing: Chrome leaves it behind after a crash,
    and approval can be revoked while the file still lists a live port. The only
    reliable check is to open a socket and ask for the version.
    """
    sock = BrowserSocket(url)
    try:
        await sock.connect(timeout=timeout)
        version = await sock.send("Browser.getVersion", timeout=timeout)
        return bool(version.get("product"))
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            await sock.close()


async def open_session(
    socket: BrowserSocket,
    target_id: str,
    info: dict[str, Any],
) -> RawTab:
    """Attach a flat session to a target and enable the domains we need.

    Every new session starts with no domains enabled. ``DOM.getDocument`` and
    ``Page.getFrameTree`` fail with -32601 until ``DOM.enable`` / ``Page.enable``
    run on that session, so this must happen per tab, not once per browser.
    """
    attached = await socket.send(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
    )
    session_id = attached["sessionId"]
    tab = RawTab(socket, target_id, session_id, info)

    for domain in ("Page.enable", "DOM.enable", "Runtime.enable"):
        with contextlib.suppress(Exception):
            await socket.send(domain, {}, session_id=session_id)

    return tab


async def list_targets(socket: BrowserSocket) -> list[dict[str, Any]]:
    res = await socket.send("Target.getTargets")
    return [t for t in res.get("targetInfos", []) if t.get("type") == "page"]


async def attach(
    session: Any,
    *,
    user_data_dir: Path | str | None = None,
    ws_url: str | None = None,
    timeout: float = 15.0,
    tab_index: int | None = None,
    target_id: str | None = None,
    own_tab: bool | None = None,
    start_url: str = "about:blank",
) -> dict[str, Any]:
    """Attach ``session`` to a running Chrome and select a tab.

    Tab policy, in order:

    * ``target_id`` / ``tab_index`` — drive that tab, deliberately.
    * ``own_tab=None`` (default) — reuse an open tab on the same site as
      ``start_url``; otherwise open a new one. Reusing keeps the user's reading
      position; opening a tab avoids overwriting a page on some other site.
    * ``own_tab=True`` / ``False`` — force one behaviour.

    Returns a summary safe to hand to an agent: endpoint, tab count, and the
    selected tab's URL and title.
    """
    if ws_url is None:
        ws_url = ws_endpoint(user_data_dir)

    socket = BrowserSocket(ws_url)
    try:
        await socket.connect(timeout=timeout)
    except Exception as exc:
        raise AttachError(
            f"Could not open {ws_url}.\n"
            "Three things cause this, in order of likelihood:\n"
            "  1. Another client already holds the connection. Chrome's "
            "approval-mode debugging serves one client at a time, so stop any "
            "other MCP server or script that is attached.\n"
            "  2. The approval expired. Re-approve at "
            "chrome://inspect/#remote-debugging.\n"
            "  3. Chrome was closed or the port changed."
        ) from exc

    try:
        version = await socket.send("Browser.getVersion")
    except Exception as exc:
        await socket.close()
        raise AttachError(
            f"Connected to {ws_url} but Browser.getVersion failed: {exc}"
        ) from exc

    pages = await list_targets(socket)
    if not pages and own_tab is False:
        await socket.close()
        raise AttachError("Chrome is running but has no page targets open.")

    # Pick a tab: an explicit one, a tab already on the target site, or a fresh
    # one. Reusing keeps the user's place; opening a tab instead of navigating
    # theirs avoids replacing a page they were reading.
    created_tab = False
    if own_tab is None:
        host = _site(start_url)
        match = None
        if host:
            match = next((p for p in pages
                          if _site(p.get("url") or "") == host), None)
        if match is not None:
            chosen = match
            own_tab = False
        else:
            own_tab = True

    if own_tab:
        try:
            made = await socket.send(
                "Target.createTarget", {"url": start_url}
            )
        except Exception as exc:
            await socket.close()
            raise AttachError(f"Could not open a work tab: {exc}") from exc
        chosen = {
            "targetId": made["targetId"],
            "url": start_url,
            "title": "",
            "type": "page",
        }
        pages = pages + [chosen]
        created_tab = True
    elif target_id is not None:
        chosen = next((p for p in pages if p["targetId"] == target_id), None)
        if chosen is None:
            await socket.close()
            raise AttachError(f"No page target with id {target_id!r}")
    elif tab_index is not None:
        try:
            chosen = pages[tab_index]
        except IndexError as exc:
            await socket.close()
            raise AttachError(
                f"No tab at index {tab_index}; {len(pages)} page(s) open."
            ) from exc
    else:
        chosen = pages[0]

    try:
        tab = await open_session(socket, chosen["targetId"], chosen)
    except Exception as exc:
        await socket.close()
        raise AttachError(f"Could not attach to the tab: {exc}") from exc

    session._browser = _BrowserShim(socket, pages)
    session._tab = tab
    session._targets.clear()
    if hasattr(session, "_headless"):
        session._headless = False

    return {
        "attached": True,
        "endpoint": ws_url,
        "browser": version.get("product"),
        "tabs": len(pages),
        "created_tab": created_tab,
        "active_url": chosen.get("url"),
        "active_title": chosen.get("title"),
        "tab_list": [
            {"index": i, "title": p.get("title"), "url": p.get("url")}
            for i, p in enumerate(pages)
        ],
    }


class _BrowserShim:
    """Just enough ``Browser`` surface for ``browser.py``'s tab helpers.

    ``tabs`` must expose ``switch_tab`` and ``tabs`` list-style access. Each
    entry carries its target id; a flat session is opened lazily the first time
    a tab is actually driven, because sessions are per-tab and must not be
    created speculatively.
    """

    def __init__(self, socket: BrowserSocket, pages: list[dict[str, Any]],
                 session: Any = None) -> None:
        self._socket = socket
        self._pages = pages
        self._session = session
        self.tabs: list[RawTab] = [RawTab(socket, p["targetId"], "", p)
                                   for p in pages]

    async def stop(self) -> None:
        """Close the connection, leaving every tab open.

        ``browser.py`` calls ``stop()`` when a session ends. For an attached
        browser that must mean "let go", never "quit Chrome" or "close the
        user's tabs".
        """
        await self._socket.close()

    def _info_for(self, target_id: str) -> dict[str, Any]:
        return next((p for p in self._pages if p["targetId"] == target_id), {})

    async def open(self, index: int) -> RawTab:
        """Attach a live session to the tab at ``index`` and return it."""
        if index < 0 or index >= len(self._pages):
            raise AttachError(f"No tab at index {index}")
        info = self._pages[index]
        return await open_session(self._socket, info["targetId"], info)

    async def get(self, url: str, **_kwargs: Any) -> RawTab:
        """Open a new tab and return its live session.

        ``browser.py``'s new_tab expects the launched-browser API, where
        ``Browser.get`` returns a tab. Attached browsers have to create the
        target first and then attach to it.
        """
        created = await self._socket.send("Target.createTarget", {"url": url})
        target_id = created.get("targetId")
        if not target_id:
            raise AttachError("Target.createTarget returned no targetId")
        info = {"targetId": target_id, "url": url, "title": "", "type": "page"}
        self._pages.append(info)
        self.tabs = [RawTab(self._socket, p["targetId"], "", p)
                     for p in self._pages]
        tab = await open_session(self._socket, target_id, info)

        # A new target starts on about:blank and navigates afterwards, so the
        # URL is not readable for the first few milliseconds.
        if url.startswith(("http://", "https://")):
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 20.0
            while loop.time() < deadline:
                with contextlib.suppress(Exception):
                    if (await tab.live_url()) not in ("", "about:blank"):
                        break
                await asyncio.sleep(0.05)
        return tab

    @property
    def main_tab(self) -> RawTab | None:
        return self.tabs[0] if self.tabs else None

    async def refresh(self) -> list[dict[str, Any]]:
        pages = await list_targets(self._socket)
        self._pages = pages
        self.tabs = [RawTab(self._socket, p["targetId"], "", p) for p in pages]
        return pages
