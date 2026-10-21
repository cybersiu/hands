"""
server.py — MCP surface for the hands.

Run with stdio transport:

    python server.py

The tool set is deliberately small. An agent should be able to hold the whole
model in its head: look, act, look again. Every tool returns compact JSON that
fits comfortably in a context window.

Design rules
------------
* ``observe`` is the only way to discover elements, and it is cheap. Callers
  never receive raw HTML.
* ``click``/``type`` accept either a ``ref`` from the last ``observe`` or raw
  viewport coordinates. Refs are invalidated by navigation and by any click that
  changes the DOM, so agents are told to re-observe.
* Failures raise ``HandsError``; FastMCP turns that into a clean tool error
  rather than a stack trace the model has to parse.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# Allow `python server.py` from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser import HandsError, Session  # noqa: E402
import attach as _attach  # noqa: E402


def real_chrome_profile() -> Path:
    """Locate the platform's real Chrome profile directory.

    Chrome 136+ blocks remote debugging against this path, so using it requires
    Chrome to be closed. The function exists so the error message can name the
    exact directory the caller asked for.
    """
    return _attach.chrome_user_data_root()


mcp = FastMCP("hands")
_session = Session()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

@mcp.tool()
async def start(
    headless: bool = False,
    url: str = "about:blank",
    profile_dir: str | None = None,
    use_my_profile: bool = False,
    executable: str | None = None,
    width: int = 1280,
    height: int = 900,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Launch Chrome and keep it open for later calls.

    Chrome 136 and later refuse ``--remote-debugging-port`` against the default
    data directory, so this cannot attach to the Chrome you are already using.
    Two options remain:

    * ``use_my_profile=True`` launches against your real Chrome profile
      directory. Requires Chrome to be fully closed first, otherwise Chrome
      hands the request to the running instance and no debug port opens.
    * Leave it False to use the dedicated hands profile at ~/.mcp-hands/profile,
      which persists logins across runs. Seed it from your real profile with
      ``python import_profile.py``.

    Args:
        headless: Run without a visible window. Use False when a human may need
            to log in or solve a challenge.
        url: Page to open first.
        profile_dir: Explicit profile directory. Overrides the two options above.
        use_my_profile: Use the platform's real Chrome profile directory.
        executable: Full path to chrome.exe. Only needed if Chrome is not on PATH.
        width: Viewport width in CSS pixels.
        height: Viewport height in CSS pixels.
        extra_args: Additional Chrome command-line switches.

    Returns the current URL and whether a session was already running.
    """
    if profile_dir is None and use_my_profile:
        profile_dir = str(real_chrome_profile())

    return await _session.start(
        headless=headless,
        url=url,
        profile_dir=profile_dir,
        executable=executable,
        viewport=(width, height),
        extra_args=extra_args,
    )


@mcp.tool()
async def stop() -> dict[str, Any]:
    """Close the browser and release the session.

    When the session was created with ``attach`` this disconnects only; the
    user's Chrome keeps running.
    """
    return await _session.stop()


@mcp.tool()
async def attach(
    user_data_dir: str | None = None,
    ws_url: str | None = None,
    tab_index: int | None = None,
    target_id: str | None = None,
) -> dict[str, Any]:
    """Attach to the Chrome the user is already using.

    This is the fastest path to a logged-in browser: no separate profile, no
    copying cookies, no re-authentication. It works because Chrome 144+ writes
    a ``DevToolsActivePort`` file when remote debugging is approved, and the
    WebSocket endpoint in that file accepts CDP commands.

    Requires, once, in Chrome:

    1. Open ``chrome://inspect/#remote-debugging`` and enable remote debugging.
    2. Approve the incoming connection when Chrome asks.

    Args:
        user_data_dir: Chrome profile root holding ``DevToolsActivePort``.
            Defaults to the platform's standard Chrome profile.
        ws_url: Explicit WebSocket endpoint, overriding ``user_data_dir``.
        tab_index: Which tab to drive, by position. Defaults to the first real
            page (skipping blank tabs).
        target_id: Which tab to drive, by CDP target id. Takes precedence over
            ``tab_index``.

    Returns the browser version, the tab list, and which tab is now active.
    """
    return await _attach.attach(
        _session,
        user_data_dir=user_data_dir,
        ws_url=ws_url,
        tab_index=tab_index,
        target_id=target_id,
    )


@mcp.tool()
async def attach_status(user_data_dir: str | None = None) -> dict[str, Any]:
    """Report whether a running Chrome can be attached to, without attaching.

    Useful for diagnosing the approval flow: it checks that
    ``DevToolsActivePort`` exists, is well formed, and that the WebSocket
    actually answers a CDP handshake.
    """
    try:
        port, path = _attach.read_devtools_port(user_data_dir)
    except _attach.AttachError as exc:
        return {"attachable": False, "reason": str(exc)}

    url = f"ws://127.0.0.1:{port}{path}"
    alive = await _attach.probe_ws(url, timeout=5.0)
    return {
        "attachable": alive,
        "port": port,
        "endpoint": url,
        "reason": None if alive else (
            "DevToolsActivePort exists but the endpoint did not answer. "
            "Chrome may have closed the session; re-approve it at "
            "chrome://inspect/#remote-debugging."
        ),
    }


@mcp.tool()
async def navigate(url: str, settle: bool = True, timeout: float = 30.0) -> dict[str, Any]:
    """Go to a URL in the active tab.

    Args:
        url: Absolute URL, or a bare host such as "example.com".
        settle: Wait for the DOM to stop changing before returning.
        timeout: Seconds to wait for the load event.

    Returns the final URL and page title.
    """
    return await _session.navigate(url, settle=settle, timeout=timeout)


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------

@mcp.tool()
async def observe(
    max_nodes: int = 300,
    min_size: int = 3,
    text: str | None = None,
    include_offscreen: bool = False,
) -> dict[str, Any]:
    """List the interactable elements on the page, with coordinates.

    This is the primary way to see a page. Each element gets a short ``ref``
    you can pass to ``click`` or ``type``. Coordinates are CSS pixels relative
    to the top-left of the viewport, which is also the space ``click(x, y)``
    expects.

    Works through shadow DOM and cross-origin iframes.

    Args:
        max_nodes: Stop after this many elements. Raise it on dense pages.
        min_size: Ignore elements smaller than this many pixels in both axes.
        text: Only return elements whose text, name, tag or role contains this
            substring, case-insensitive.
        include_offscreen: Include elements above/left of the viewport. Useful
            right after scrolling to the bottom of a long page.

    Returns ``elements`` (each with ref, tag, text, x, y, width, height),
    ``count``, and the current scroll position.
    """
    return await _session.observe(
        max_nodes=max_nodes,
        min_size=min_size,
        text=text,
        include_offscreen=include_offscreen,
    )


@mcp.tool()
async def dismiss_blocker(
    prefer: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Close the modal that is intercepting clicks.

    Call this when ``click`` reports ``hit.blocked``, or when ``observe``
    returns elements you cannot interact with. Google properties (Colab, Gmail,
    Drive) and most admin consoles render a full-viewport scrim behind dialogs;
    while it is up, every coordinate resolves to the scrim.

    The button labels are matched across the languages those apps ship, and a
    secondary action (Close, Cancel, Later) is preferred over a primary one, so
    this does not confirm a destructive choice.

    Args:
        prefer: Button labels to try first, in order. Useful when the dialog
            offers something specific like "Reconnect".
        dry_run: Report the dialog and the button that would be clicked,
            without clicking.

    Returns the dialog text, which button was clicked, and whether the dialog
    actually closed.
    """
    return await _session.dismiss_blocker(prefer=prefer, dry_run=dry_run)


@mcp.tool()
async def click(
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    button: str = "left",
    clicks: int = 1,
    settle: bool = True,
    retry: int = 0,
    auto_dismiss: bool = False,
) -> dict[str, Any]:
    """Click an element or a point.

    Prefer ``ref`` from the last ``observe``: it scrolls the element into view
    first, so off-screen clicks do not silently miss. Use ``x``/``y`` when the
    target is inside a cross-origin iframe that has no discoverable node, or
    when you are clicking a canvas.

    Every call reports three proofs of what happened:

    * ``url_changed`` — the cheapest evidence that the click did something.
    * ``hit`` — the element chain under the pointer. ``hit.blocked`` means a
      modal scrim swallowed the click.
    * ``attempts`` — present when ``retry`` was used.

    **A successful return does not prove the click reached its target.** Check
    ``url_changed`` and ``hit``, and use ``assert_changed`` for anything subtler
    than navigation.

    Args:
        ref: Element reference from ``observe``, such as "e12".
        x: Viewport x in CSS pixels.
        y: Viewport y in CSS pixels.
        button: "left", "right" or "middle".
        clicks: 1 for a click, 2 for a double-click.
        settle: Wait for the DOM to settle afterwards.
        retry: Extra attempts when the URL did not change. The final attempt
            reloads the page first, which clears stale overlays.
        auto_dismiss: If a modal intercepted the click, close it and retry once.

    Returns what was clicked, the URL before and after, the hit report, and any
    warning.
    """
    result = await _session.click(
        ref=ref, x=x, y=y, button=button, clicks=clicks, settle=settle,
        retry=retry,
    )

    blocked = bool((result.get("hit") or {}).get("blocked"))
    if blocked and auto_dismiss:
        dismissed = await _session.dismiss_blocker()
        result["auto_dismiss"] = dismissed
        if dismissed.get("dismissed"):
            retry = await _session.click(
                ref=ref, x=x, y=y, button=button, clicks=clicks, settle=settle,
            )
            result["retry"] = retry
            result["hit"] = retry.get("hit")
            result.pop("warning", None)

    return result


@mcp.tool()
async def hover(
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
) -> dict[str, Any]:
    """Move the mouse over an element or point without clicking.

    Use this to open hover menus and tooltips before observing again.
    """
    return await _session.hover(ref=ref, x=x, y=y)


@mcp.tool()
async def type_text(
    text: str,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    clear: bool = True,
    submit: bool = False,
    per_char: bool = False,
    delay_ms: int = 0,
) -> dict[str, Any]:
    """Type into a field.

    The field is focused by clicking it first, so pass the ref of the input,
    textarea or contenteditable element from ``observe``.

    Args:
        text: The text to insert.
        ref: Reference of the field to focus.
        x: Viewport x, if you prefer coordinates over a ref.
        y: Viewport y.
        clear: Select-all and delete before typing.
        submit: Press Enter afterwards.
        per_char: Emit one key event per character instead of a single insert.
            Needed by some autocomplete and masked-input widgets.
        delay_ms: Milliseconds between characters when ``per_char`` is set.

    Returns the character count, the focused element, and the current URL.
    """
    return await _session.type_text(
        text, ref=ref, x=x, y=y, clear=clear, submit=submit,
        per_char=per_char, delay_ms=delay_ms,
    )


@mcp.tool()
async def assert_changed(
    expression: str,
    expect: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Wait for a page condition to become true, and report whether it did.

    This is how you tell a real action from a no-op. A click that returns
    success may have landed on an overlay, or the app may have refused the
    action silently. Assert on observable state instead of assuming.

    Args:
        expression: JavaScript that evaluates to the value to check. It is
            re-evaluated until it matches or ``timeout`` expires.
        expect: If given, the condition is satisfied when the evaluated value
            equals this string (compared as text). If omitted, any truthy value
            satisfies it.
        timeout: Seconds to keep polling.

    Returns ``changed``, the final value, and how long it took.

    Example:
        assert_changed("document.querySelectorAll('.cell.executed').length > 0")
        assert_changed("document.title", expect="Dashboard")
    """
    import time as _time

    start = _time.perf_counter()
    deadline = start + timeout
    last_value: Any = None

    while _time.perf_counter() < deadline:
        try:
            last_value = await _session.evaluate(expression)
        except Exception as exc:
            return {"changed": False, "error": str(exc)[:200],
                    "elapsed": round(_time.perf_counter() - start, 2)}

        if expect is None:
            if last_value:
                return {"changed": True, "value": last_value,
                        "elapsed": round(_time.perf_counter() - start, 2)}
        else:
            if str(last_value) == expect:
                return {"changed": True, "value": last_value,
                        "elapsed": round(_time.perf_counter() - start, 2)}

        await asyncio.sleep(0.25)

    return {
        "changed": False,
        "value": last_value,
        "expected": expect,
        "elapsed": round(_time.perf_counter() - start, 2),
        "hint": (
            "The condition never became true. Either the action did not take "
            "effect, or the condition is wrong. Run observe() and check the "
            "hit report from the last click."
        ),
    }


@mcp.tool()
async def press(keys: str, settle: bool = True) -> dict[str, Any]:
    """Press a key or chord.

    Examples: "Enter", "Escape", "Tab", "ctrl+a", "ctrl+shift+t", "cmd+v".

    Args:
        keys: Keys joined by "+". Modifiers are alt, ctrl, meta/cmd, shift.
        settle: Wait for the DOM to settle afterwards.
    """
    return await _session.press(keys, settle=settle)


@mcp.tool()
async def scroll(
    dy: int = 0,
    dx: int = 0,
    x: float | None = None,
    y: float | None = None,
) -> dict[str, Any]:
    """Scroll the page.

    Args:
        dy: Vertical pixels. Positive scrolls down, negative up.
        dx: Horizontal pixels.
        x: Viewport x to scroll at. Defaults to the horizontal centre.
        y: Viewport y to scroll at. Defaults to the vertical centre.

    Returns the new scroll position and document size.
    """
    return await _session.scroll(dy=dy, dx=dx, x=x, y=y)


# --------------------------------------------------------------------------
# Escape hatches
# --------------------------------------------------------------------------

@mcp.tool()
async def evaluate(expression: str, await_promise: bool = False) -> Any:
    """Evaluate JavaScript in the page and return the result.

    Use sparingly: prefer ``observe`` and ``click`` so actions stay faithful to
    what a user would do. This is for reading values the DOM tools do not
    expose, such as a computed style or a global variable.

    Args:
        expression: JavaScript source. An expression's value is returned; use
            ``await_promise`` for async work.
        await_promise: Resolve a returned Promise before returning.
    """
    return await _session.evaluate(expression, await_promise=await_promise)


@mcp.tool()
async def screenshot(path: str | None = None, full_page: bool = False) -> dict[str, Any]:
    """Save a PNG of the viewport and return its path.

    Only useful if the caller can view images. ``observe`` is cheaper and more
    precise for finding things to click.
    """
    return await _session.screenshot(path=path, full_page=full_page)


# --------------------------------------------------------------------------
# Tabs
# --------------------------------------------------------------------------

@mcp.tool()
async def tabs() -> list[dict[str, Any]]:
    """List open tabs with their index, URL and title."""
    return await _session.tabs()


@mcp.tool()
async def switch_tab(index: int) -> dict[str, Any]:
    """Make the tab at ``index`` the active one."""
    return await _session.switch_tab(index)


@mcp.tool()
async def new_tab(url: str = "about:blank") -> dict[str, Any]:
    """Open a new tab and make it active."""
    return await _session.new_tab(url)


@mcp.tool()
async def close_tab(index: int | None = None) -> dict[str, Any]:
    """Close a tab. Defaults to the last one."""
    return await _session.close_tab(index)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    transport = os.environ.get("HANDS_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
