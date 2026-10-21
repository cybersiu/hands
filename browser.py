"""
hands/browser.py — A pair of hands for an agent.

One persistent Chrome session, driven over raw CDP.

Why this exists
---------------
Browser MCP servers usually fall into one of two traps:

1. They expose the accessibility tree or a full DOM dump. The payload is huge,
   the model burns its context on markup it cannot use, and the call times out
   on any page of moderate complexity.
2. They click with ``element.click()``. That produces ``isTrusted === false``,
   which React, Vue, Angular and most payment/checkout widgets silently ignore.
   The click "succeeds" and nothing happens.

This module takes a third path:

* **Observation** returns a compact, indexed list of interactable elements with
  a viewport-centre coordinate each. An agent picks an index; it never parses
  HTML.
* **Action** dispatches raw CDP ``Input.*`` events. The browser treats them as
  real user input, so framework handlers fire.
* **Reach** comes from ``DOM.getDocument(pierce=True)``, which descends through
  shadow roots *and cross-origin iframes*. Same-origin JS traversal cannot do
  the latter; CDP can, because it talks to each frame's own document.

Coordinate contract
-------------------
All coordinates are **CSS pixels relative to the top-level viewport**. That is
the space CDP ``Input.dispatchMouseEvent`` expects, and it is what
``getBoundingClientRect`` reports inside a frame *after* the frame's own offset
is applied. ``DOM.getContentQuads`` returns quads in that same space for
cross-process frames, so both paths agree.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import zendriver as uc
from zendriver import cdp

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------

DEFAULT_VIEWPORT = (1280, 900)
DEFAULT_PROFILE = Path.home() / ".mcp-hands" / "profile"

#: Elements an agent can plausibly act on. Anything else is noise.
INTERACTIVE_TAGS: frozenset[str] = frozenset({
    "a", "button", "input", "select", "textarea", "option",
    "label", "summary", "details", "area", "audio", "video",
    "iframe", "frame", "object", "embed",
})

INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "link", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "switch", "textbox", "searchbox", "combobox",
    "slider", "spinbutton", "treeitem", "gridcell", "columnheader", "rowheader",
})

#: Attributes that usually carry an accessible name.
NAME_ATTRS: tuple[str, ...] = (
    "aria-label", "title", "alt", "placeholder", "name",
    "data-testid", "data-test-id", "data-cy", "data-qa", "data-automation-id",
)

#: CDP node types we care about. 1 == ELEMENT_NODE.
_ELEMENT_NODE = 1

#: How deep to walk the pierced DOM. Guards against pathological trees.
_MAX_DEPTH = 40


class HandsError(RuntimeError):
    """A failure whose message is safe and useful to show an agent."""


# --------------------------------------------------------------------------
# Observation model
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Target:
    """One interactable element, addressed by a stable ``ref``."""

    ref: str
    backend_node_id: Any
    frame_id: str
    tag: str
    text: str
    name: str | None
    role: str | None
    x: float
    y: float
    width: float
    height: float
    enabled: bool
    in_frame: bool
    shadow_depth: int
    attributes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ref": self.ref,
            "tag": self.tag,
            "text": self.text,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "width": round(self.width, 1),
            "height": round(self.height, 1),
            "enabled": self.enabled,
        }
        if self.name:
            d["name"] = self.name
        if self.role:
            d["role"] = self.role
        if self.in_frame:
            d["in_frame"] = True
        if self.shadow_depth:
            d["shadow_depth"] = self.shadow_depth
        if self.attributes:
            d["attributes"] = self.attributes
        return d


# --------------------------------------------------------------------------
# CDP DOM helpers
# --------------------------------------------------------------------------

def _quad_to_rect(quad: Sequence[float]) -> tuple[float, float, float, float]:
    """Convert a content quad (8 floats) to (x, y, w, h).

    ``DOM.getContentQuads`` returns points in viewport space, ordered
    top-left, top-right, bottom-right, bottom-left. For a rotated element the
    bounding box of those points is the only sane axis-aligned approximation.
    """
    xs = quad[0::2]
    ys = quad[1::2]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    return left, top, right - left, bottom - top


def _backend_id(node: Any) -> Any:
    """Return the backend node id in the form CDP calls expect.

    zendriver wraps it in ``BackendNodeId`` (an int subclass whose ``to_json``
    the serialiser needs). Passing a plain int fails at the transport layer, so
    preserve the wrapper when present and fall back to int for dict input.
    """
    raw = _get(node, "backend_node_id")
    if raw is None:
        return None
    return raw


def _attrs_from_cdp(attributes: Sequence[str] | None) -> dict[str, str]:
    """CDP gives attributes as a flat [name, value, name, value, ...] list."""
    out: dict[str, str] = {}
    if not attributes:
        return out
    for i in range(0, len(attributes) - 1, 2):
        name, value = attributes[i], attributes[i + 1]
        if name in ("style", "class"):
            continue
        if value and len(value) <= 200:
            out[name] = value
    return out


def _get(node: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a CDP dataclass or a plain dict.

    ``DOM.getDocument`` returns ``cdp.dom.Node`` dataclasses, not dicts. Older
    zendriver builds and the raw JSON transport return dicts. Accept both so the
    module does not break on an upgrade.
    """
    if isinstance(node, dict):
        return node.get(key, default)
    return getattr(node, key, default)


def _text_of(node: Any, depth: int = 0) -> str:
    """Concatenate descendant text nodes, as ``innerText`` would.

    CDP gives each text node its own ``node_value``; an element node has none.
    Walking children is the only way to recover visible label text without a
    round trip per element.
    """
    if depth > 12:
        return ""
    parts: list[str] = []
    value = _get(node, "node_value")
    if value:
        parts.append(str(value))
    for child in _get(node, "children") or ():
        if len(" ".join(parts)) > 200:
            break
        parts.append(_text_of(child, depth + 1))
    collapsed = " ".join(" ".join(parts).split())
    return collapsed[:117] + "..." if len(collapsed) > 120 else collapsed


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class Session:
    """Owns one Chrome process and one active tab."""

    def __init__(self) -> None:
        self._browser: uc.Browser | None = None
        self._tab: uc.Tab | None = None
        self._lock = asyncio.Lock()
        self._headless = False
        self._targets: dict[str, Target] = {}
        self._ref_seq = 0

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._browser is not None and self._tab is not None

    async def start(
        self,
        *,
        headless: bool = False,
        profile_dir: str | None = None,
        executable: str | None = None,
        viewport: tuple[int, int] | None = None,
        url: str = "about:blank",
        extra_args: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            if self.running:
                return {
                    "already_running": True,
                    "url": await self._safe_url(),
                    "headless": self._headless,
                }

            profile = Path(profile_dir).expanduser() if profile_dir else DEFAULT_PROFILE
            profile.mkdir(parents=True, exist_ok=True)
            self._headless = headless
            width, height = viewport or DEFAULT_VIEWPORT

            config = uc.Config(
                user_data_dir=str(profile),
                headless=headless,
                browser_executable_path=executable,
                sandbox=False,
            )
            config.add_argument(f"--window-size={width},{height}")
            config.add_argument("--disable-blink-features=AutomationControlled")
            config.add_argument("--no-first-run")
            config.add_argument("--no-default-browser-check")
            config.add_argument("--disable-features=Translate,MediaRouter")
            for arg in extra_args or ():
                config.add_argument(arg)

            try:
                browser = await uc.start(config)
            except Exception as exc:
                raise HandsError(
                    f"Could not launch Chrome: {exc}\n"
                    f"Profile: {profile}\n"
                    "If your own Chrome is open, close it or pass a different "
                    "profile_dir. To drive the running Chrome instead, use "
                    "attach() rather than start()."
                ) from exc

            self._browser = browser
            tab = browser.main_tab or await browser.get("about:blank")
            self._tab = tab

            # Confirm Chrome honoured the profile, rather than silently using
            # the default one and colliding with the user's own browser.
            actual = await self._launched_profile(tab)
            if actual and "mcp-hands" not in actual.replace("\\", "/"):
                self._profile_warning = (
                    f"Chrome launched with {actual!r}, not {str(profile)!r}. "
                    "The profile argument was not honoured."
                )
            else:
                self._profile_warning = None

            await self._apply_viewport(width, height)
            if url and url != "about:blank":
                await self.navigate(url)

            result = {
                "already_running": False,
                "url": await self._safe_url(),
                "headless": headless,
                "profile": str(profile),
                "viewport": {"width": width, "height": height},
            }
            warning = getattr(self, "_profile_warning", None)
            if warning:
                result["warning"] = warning
            return result

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            browser, self._browser = self._browser, None
            self._tab = None
            self._targets.clear()
            if browser is None:
                return {"stopped": False, "reason": "not running"}
            with contextlib.suppress(Exception):
                result = browser.stop()
                if asyncio.iscoroutine(result):
                    await result
            return {"stopped": True}

    async def _launched_profile(self, tab: uc.Tab) -> str | None:
        """Read the profile directory Chrome actually used.

        Chrome exposes it through the browser process command line. Reading it
        back is the only way to tell whether ``--user-data-dir`` was honoured.
        """
        try:
            info = await tab.send(cdp.browser.get_browser_command_line())
            for arg in info:
                if arg.startswith("--user-data-dir="):
                    return arg.split("=", 1)[1]
        except Exception:
            pass
        return None

    async def _apply_viewport(self, width: int, height: int) -> None:
        tab = self._tab
        if tab is None:
            return
        with contextlib.suppress(Exception):
            await tab.send(cdp.emulation.set_device_metrics_override(
                width=width, height=height,
                device_scale_factor=1, mobile=False,
            ))

    def _require_tab(self) -> uc.Tab:
        if self._tab is None:
            raise HandsError("Browser is not running. Call 'start' first.")
        return self._tab

    async def _safe_url(self) -> str:
        """The tab's live URL, read from the document.

        The target snapshot that ``Target.getTargets`` returns never updates,
        so using it here silently corrupted every URL comparison: a click
        reported ``chrome://newtab/`` while the page was on grok.com. An
        unreadable URL now returns an empty string rather than a
        plausible-looking stale one, so failures are visible instead of wrong.
        """
        try:
            value = await self._require_tab().evaluate("location.href")
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    # -- navigation --------------------------------------------------------

    async def navigate(self, url: str, *, settle: bool = True,
                       timeout: float = 30.0) -> dict[str, Any]:
        tab = self._require_tab()
        if "://" not in url and not url.startswith("about:"):
            url = "https://" + url

        # A launched browser's Tab.get has no timeout parameter; the attached
        # transport's does, and it waits for the navigation to commit.
        try:
            accepts_timeout = "timeout" in inspect.signature(tab.get).parameters
        except (TypeError, ValueError):
            accepts_timeout = False

        try:
            if accepts_timeout:
                await asyncio.wait_for(tab.get(url, timeout=timeout),
                                       timeout=timeout + 10)
            else:
                await asyncio.wait_for(tab.get(url), timeout=timeout + 10)
        except asyncio.TimeoutError as exc:
            raise HandsError(f"Navigation to {url} timed out after {timeout}s") from exc

        if settle:
            await self._settle()

        live = await self._safe_url()
        if not live:
            raise HandsError(
                f"Navigation to {url} finished but location.href could not be "
                "read; refusing to report the target snapshot as the URL."
            )
        return {"url": live, "title": await self.title()}

    async def title(self) -> str:
        tab = self._require_tab()
        with contextlib.suppress(Exception):
            value = await tab.evaluate("document.title")
            if isinstance(value, str):
                return value
        return ""

    async def _settle(self, quiet_ms: int = 350, max_ms: int = 6000) -> None:
        """Wait until DOM mutations stop, bounded by ``max_ms``."""
        tab = self._tab
        if tab is None:
            return
        js = (
            "new Promise(resolve => {"
            "  let t = null;"
            "  const done = () => { try { obs.disconnect(); } catch(e){} resolve(true); };"
            "  const obs = new MutationObserver(() => { clearTimeout(t); t = setTimeout(done, %d); });"
            "  try { obs.observe(document, {childList:true, subtree:true, attributes:true}); } catch(e) { resolve(false); return; }"
            "  t = setTimeout(done, %d);"
            "  setTimeout(done, %d);"
            "})"
        ) % (quiet_ms, quiet_ms, max_ms)
        with contextlib.suppress(Exception):
            await tab.evaluate(js, await_promise=True)

    # -- observation -------------------------------------------------------

    async def observe(
        self,
        *,
        max_nodes: int = 300,
        min_size: int = 3,
        text: str | None = None,
        include_offscreen: bool = False,
    ) -> dict[str, Any]:
        """Return interactable elements with viewport coordinates.

        Walks the *pierced* DOM: shadow roots and cross-origin iframes are
        included because ``DOM.getDocument(pierce=True)`` returns one node tree
        spanning every frame the renderer knows about.
        """
        tab = self._require_tab()
        self._targets.clear()
        self._ref_seq = 0

        doc = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
        targets: list[Target] = []

        async def visit(node: Any, *, frame_id: str,
                        shadow_depth: int, depth: int) -> None:
            if depth > _MAX_DEPTH or len(targets) >= max_nodes:
                return

            # A node inside a frame reports its own frame id; trust it over the
            # inherited one so cross-origin frames are attributed correctly.
            own_frame = _get(node, "frame_id") or frame_id

            if _get(node, "node_type") == _ELEMENT_NODE:
                maybe = self._classify(node, own_frame, shadow_depth)
                if maybe is not None:
                    rect = await self._rect_for(node, tab, include_offscreen, min_size)
                    if rect is not None:
                        x, y, w, h = rect
                        if text is None or self._matches(maybe, text):
                            self._ref_seq += 1
                            maybe.ref = f"e{self._ref_seq}"
                            maybe.x, maybe.y = x, y
                            maybe.width, maybe.height = w, h
                            targets.append(maybe)

            for child in _get(node, "children") or ():
                await visit(child, frame_id=own_frame,
                            shadow_depth=shadow_depth, depth=depth + 1)

            for root in _get(node, "shadow_roots") or ():
                await visit(root, frame_id=own_frame,
                            shadow_depth=shadow_depth + 1, depth=depth + 1)

            # With pierce=True the renderer inlines frame documents here,
            # including cross-origin ones.
            content = _get(node, "content_document")
            if content is not None:
                await visit(content, frame_id=own_frame,
                            shadow_depth=shadow_depth, depth=depth + 1)

        root_frame = await self._top_frame_id(tab)
        await visit(doc, frame_id=root_frame, shadow_depth=0, depth=0)

        # CDP's DOM.getDocument returns text through node_value, which arrives
        # mangled for non-ASCII in some transports. innerText via Runtime is
        # reliable, so re-read labels for elements whose text came back as
        # question marks or empty.
        await self._repair_texts(tab, targets)

        for t in targets:
            self._targets[t.ref] = t

        info = await self._viewport_info()
        return {
            "url": await self._safe_url(),
            "title": await self.title(),
            "count": len(targets),
            "truncated": len(targets) >= max_nodes,
            "viewport": info,
            "elements": [t.as_dict() for t in targets],
        }

    async def _repair_texts(self, tab: uc.Tab,
                            targets: list[Target]) -> None:
        """Re-read element labels through Runtime.evaluate.

        Why this exists
        ---------------
        ``DOM.getDocument`` returns element text as ``node_value`` strings, and
        those arrive as ``?`` for non-ASCII on some Windows transports. A label
        of "全部執行" comes back as "? ? ? ?", which makes every text-based
        match useless. ``innerText`` read through ``Runtime.evaluate`` travels
        as a JSON string and survives intact.

        Why it matches by id, not position
        ----------------------------------
        The CDP walk and a JavaScript walk do not visit the same set of nodes:
        the CDP side filters on tag, role and attributes while descending, and
        a naive ``querySelectorAll('*')`` sees three times as many. Index N in
        one list is therefore not index N in the other. Each target carries its
        backend node id, so the JS side resolves exactly those ids instead.
        """
        if not targets:
            return

        damaged = [t for t in targets
                   if not (t.text or "").strip()
                   or "?" in (t.text or "")
                   or "\ufffd" in (t.text or "")]
        if not damaged:
            return

        # `DOM.resolveNode` accepts a backend node id directly, so each damaged
        # element becomes a RemoteObject and `Runtime.callFunctionOn` reads its
        # innerText. That path is what decodes CJK correctly; the CDP node tree
        # does not.
        resolved: dict[int, str] = {}
        read_js = (
            "function() {"
            "  const t = this.innerText || this.textContent || '';"
            "  return t.replace(/\\s+/g, ' ').trim().slice(0, 120);"
            "}"
        )

        for target in damaged:
            backend_id = target.backend_node_id
            if not backend_id:
                continue
            key = int(backend_id)
            if key in resolved:
                target.text = resolved[key]
                continue
            try:
                remote = await tab.send(cdp.dom.resolve_node(
                    backend_node_id=backend_id
                ))
                object_id = getattr(remote, "object_id", None)
                if object_id is None:
                    object_id = getattr(remote, "objectId", None)
                if not object_id:
                    continue

                result = await tab.send(cdp.runtime.call_function_on(
                    function_declaration=read_js,
                    object_id=object_id,
                    return_by_value=True,
                ))
                # call_function_on returns (RemoteObject, ExceptionDetails|None)
                value = result[0] if isinstance(result, tuple) else result
                text = getattr(value, "value", None)
                if isinstance(text, str) and text.strip():
                    resolved[key] = text
                    target.text = text
            except Exception:
                continue

    def _classify(self, node: Any, frame_id: str,
                  shadow_depth: int) -> Target | None:
        """Decide whether a CDP node is worth reporting, and prefill it."""
        tag = (_get(node, "local_name") or "").lower()
        attrs = _attrs_from_cdp(_get(node, "attributes"))
        role = attrs.get("role")

        interesting = (
            tag in INTERACTIVE_TAGS
            or (role is not None and role.lower() in INTERACTIVE_ROLES)
            or "onclick" in attrs
            or "tabindex" in attrs
            or attrs.get("contenteditable") == "true"
        )
        if not interesting:
            return None

        name = next((attrs[a] for a in NAME_ATTRS if attrs.get(a)), None)
        text = _text_of(node)

        return Target(
            ref="",
            backend_node_id=_backend_id(node),
            frame_id=frame_id,
            tag=tag or "?",
            text=text,
            name=name,
            role=role,
            x=0.0, y=0.0, width=0.0, height=0.0,
            enabled=("disabled" not in attrs
                     and attrs.get("aria-disabled") != "true"),
            in_frame=bool(frame_id),
            shadow_depth=shadow_depth,
            attributes=attrs,
        )

    @staticmethod
    def _matches(target: Target, needle: str) -> bool:
        low = needle.lower()
        hay = f"{target.text} {target.name or ''} {target.tag} {target.role or ''}".lower()
        return low in hay

    async def _rect_for(self, node: Any, tab: uc.Tab,
                        include_offscreen: bool,
                        min_size: int) -> tuple[float, float, float, float] | None:
        """Resolve a node's viewport rectangle.

        ``DOM.getContentQuads`` is the precise route, but it returns nothing for
        elements in a sticky or fixed container — which is where most sites put
        their search box. When that happens the rectangle is read from the live
        DOM through ``Runtime.evaluate`` instead.

        Both routes return CSS pixels relative to the top-level viewport, so
        callers cannot tell which one answered.
        """
        backend_id = _backend_id(node)
        if not backend_id:
            return None

        rect = None
        try:
            quads = await tab.send(cdp.dom.get_content_quads(
                backend_node_id=backend_id))
        except Exception:
            quads = None

        if quads:
            # An element split across lines yields several quads; use their
            # union so the rectangle covers the whole thing.
            left = top = float("inf")
            right = bottom = float("-inf")
            for quad in quads:
                x, y, w, h = _quad_to_rect(list(quad))
                left = min(left, x)
                top = min(top, y)
                right = max(right, x + w)
                bottom = max(bottom, y + h)
            rect = (left, top, right - left, bottom - top)

        if rect is None:
            rect = await self._rect_from_dom(tab, backend_id)

        if rect is None:
            return None

        left, top, width, height = rect
        if width < min_size and height < min_size:
            return None

        if not include_offscreen:
            info = await self._viewport_info()
            if top + height < 0 or left + width < 0:
                return None
            if top > info["innerHeight"] * 3 or left > info["innerWidth"] * 3:
                return None

        return left, top, width, height

    async def _rect_from_dom(self, tab: uc.Tab, backend_id: Any
                             ) -> tuple[float, float, float, float] | None:
        """Read an element's bounding rectangle through the page.

        ``DOM.resolveNode`` turns the backend id into a JavaScript handle, then
        ``getBoundingClientRect`` reports the geometry the renderer actually
        uses — including sticky and fixed positioning, which the quads API
        sometimes declines to describe.
        """
        try:
            remote = await tab.send(cdp.dom.resolve_node(
                backend_node_id=backend_id))
        except Exception:
            return None

        object_id = (getattr(remote, "object_id", None)
                     or getattr(remote, "objectId", None))
        if not object_id:
            return None

        js = (
            "function() {"
            "  const r = this.getBoundingClientRect();"
            "  return [r.left, r.top, r.width, r.height];"
            "}"
        )
        try:
            result = await tab.send(cdp.runtime.call_function_on(
                function_declaration=js,
                object_id=object_id,
                return_by_value=True,
            ))
        except Exception:
            return None

        value = result[0] if isinstance(result, tuple) else result
        coords = getattr(value, "value", None)
        if (not isinstance(coords, list) or len(coords) != 4
                or not all(isinstance(c, (int, float)) for c in coords)):
            return None

        left, top, width, height = (float(c) for c in coords)
        return left, top, width, height

    async def _top_frame_id(self, tab: uc.Tab) -> str:
        with contextlib.suppress(Exception):
            tree = await tab.send(cdp.page.get_frame_tree())
            frame = _get(tree, "frame")
            if frame is not None:
                fid = _get(frame, "id_") or _get(frame, "id") or ""
                return str(fid)
        return ""

    async def _viewport_info(self) -> dict[str, Any]:
        tab = self._tab
        if tab is None:
            return {}
        js = """(() => {
          const d = document.documentElement;
          return {
            scrollX: window.scrollX, scrollY: window.scrollY,
            innerWidth: window.innerWidth, innerHeight: window.innerHeight,
            scrollWidth: Math.max(d.scrollWidth, document.body ? document.body.scrollWidth : 0),
            scrollHeight: Math.max(d.scrollHeight, document.body ? document.body.scrollHeight : 0),
          };
        })()"""
        with contextlib.suppress(Exception):
            value = await tab.evaluate(js)
            if isinstance(value, dict):
                return value
        return {}

    # -- clicking ----------------------------------------------------------

    async def click(
        self,
        *,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        clicks: int = 1,
        settle: bool = True,
        retry: int = 0,
        expect_change: bool = True,
    ) -> dict[str, Any]:
        """Click a referenced element, or a raw viewport point.

        Two things are reported beyond "the event was dispatched":

        * ``url_before`` / ``url_after`` / ``url_changed`` — the cheapest proof
          that something happened. Navigation is the one observable effect that
          cannot be faked by a handler that ignores the click.
        * ``hit`` — the element chain under the pointer. ``hit.blocked`` means a
          modal scrim swallowed the click.

        ``retry`` re-attempts the click when neither the URL changed nor the DOM
        settled into anything new. On the final attempt the page is reloaded
        first, which clears most stale overlays and frozen tabs.
        """
        tab = self._require_tab()

        if ref is not None:
            target = self._targets.get(ref)
            if target is None:
                raise HandsError(
                    f"Unknown ref {ref!r}. Call 'observe' again; refs are "
                    "invalidated by navigation and page mutation."
                )
            cx = target.x + target.width / 2
            cy = target.y + target.height / 2
            await self._scroll_into_view(target, tab)
            desc = f"{target.tag} {target.text[:40]!r}"
        elif x is not None and y is not None:
            cx, cy = float(x), float(y)
            desc = f"point ({cx:.0f}, {cy:.0f})"
        else:
            raise HandsError("click needs either 'ref' or both 'x' and 'y'")

        url_before = await self._safe_url()
        attempts: list[dict[str, Any]] = []
        total = max(0, retry) + 1
        result: dict[str, Any] = {}

        for attempt in range(total):
            if attempt > 0:
                # A reload clears stale overlays, frozen tabs and half-applied
                # navigations. It is the cheapest recovery that does not need
                # to know why the first click failed.
                if attempt == total - 1:
                    with contextlib.suppress(Exception):
                        await tab.get(url_before)
                        await self._settle(quiet_ms=300, max_ms=5000)
                    # Re-resolve the element: the reload replaced the DOM.
                    if ref is not None and ref in self._targets:
                        t = self._targets[ref]
                        cx = t.x + t.width / 2
                        cy = t.y + t.height / 2
                await asyncio.sleep(0.4)

            await self._dispatch_click(cx, cy, button, clicks)
            if settle:
                await self._settle(quiet_ms=220, max_ms=3500)

            url_after = await self._safe_url()
            hit = await self._hit_at(cx, cy) or {}
            blocked = bool(hit.get("blocked"))
            changed = url_after != url_before

            attempts.append({
                "attempt": attempt + 1,
                "url_after": url_after,
                "url_changed": changed,
                "blocked": blocked,
            })

            result = {
                "clicked": True,
                "target": desc,
                "x": round(cx, 1),
                "y": round(cy, 1),
                "url": url_after,
                "url_before": url_before,
                "url_after": url_after,
                "url_changed": changed,
                "hit": hit,
            }

            if blocked:
                result["warning"] = (
                    f"A modal overlay is intercepting clicks: "
                    f"{hit.get('blocker_text', '')[:80]!r}. "
                    "Call dismiss_blocker, or pass auto_dismiss=True."
                )
                # Retrying a blocked click is pointless: the overlay is still
                # there. Stop and report rather than burn attempts.
                break

            if not expect_change or changed:
                break

        if len(attempts) > 1:
            result["attempts"] = attempts
        if not result.get("url_changed") and not result.get("hit", {}).get("blocked"):
            result.setdefault("note", (
                "The URL did not change. If the click was meant to navigate, "
                "verify with assert_changed; if it was meant to mutate the page, "
                "the handler may have ignored it."
            ))

        return result

    async def dismiss_blocker(self, *, prefer: Sequence[str] | None = None,
                              dry_run: bool = False) -> dict[str, Any]:
        """Find and close whatever modal is intercepting the page.

        Google properties (Colab, Gmail, Drive) render dialogs as ``mwc-dialog``
        with a full-viewport ``mdc-dialog__scrim``. While that scrim is up, every
        coordinate resolves to it and clicks silently do nothing.

        The buttons are slotted in from the light DOM, not from the dialog's
        shadow root, so a shadow-only search finds the dialog but no way out.
        This walks both.

        Args:
            prefer: Button labels to try first, in order. Defaults to a list of
                dismissal words across the languages Colab ships.
            dry_run: Report what would be clicked without clicking.

        Returns the dialog found, the button chosen, and whether it closed.
        """
        tab = self._require_tab()

        discover = r"""(() => {
          const out = [];
          const seen = new Set();

          function buttonsIn(scope) {
            const found = [];
            const push = (b) => {
              const r = b.getBoundingClientRect();
              if (r.width < 8 || r.height < 8) return;
              found.push({
                text: (b.innerText || b.textContent || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 40),
                slot: b.getAttribute ? b.getAttribute('slot') : null,
                x: r.left + r.width / 2,
                y: r.top + r.height / 2,
              });
            };
            try {
              scope.querySelectorAll(
                'mwc-button, md-button, md-text-button, md-filled-button, ' +
                'button, [role="button"]'
              ).forEach(push);
            } catch (e) {}
            if (scope.shadowRoot) {
              try {
                scope.shadowRoot.querySelectorAll(
                  'mwc-button, md-button, md-text-button, md-filled-button, button'
                ).forEach(push);
              } catch (e) {}
            }
            return found;
          }

          // A dialog may be a custom element whose shadow root contains the
          // actual role="dialog" node. Walk up to the outermost custom element
          // so slotted light-DOM buttons are included.
          function hostOf(el) {
            let cur = el;
            let outer = el;
            while (cur) {
              if (cur.tagName && cur.tagName.includes('-')) outer = cur;
              cur = cur.parentElement || (cur.getRootNode && cur.getRootNode().host);
            }
            return outer;
          }

          function consider(el, depth) {
            const tag = el.tagName ? el.tagName.toLowerCase() : '';
            const role = el.getAttribute ? el.getAttribute('role') : null;
            const cls = (el.className || '').toString();
            const looksLikeDialog =
              tag === 'mwc-dialog' || tag === 'md-dialog' ||
              tag === 'dialog' || role === 'dialog' ||
              role === 'alertdialog' || cls.includes('modal-dialog');

            if (!looksLikeDialog) return;

            const host = hostOf(el);
            if (seen.has(host)) return;
            seen.add(host);

            const r = host.getBoundingClientRect();
            const text = (host.innerText || '')
              .replace(/\s+/g, ' ').trim().slice(0, 200);
            if (!text && r.width < 40) return;

            out.push({
              tag: host.tagName.toLowerCase(),
              text,
              x0: Math.round(r.left), y0: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height),
              buttons: buttonsIn(host),
              inShadow: depth > 0,
            });
          }

          function visit(root, depth) {
            if (!root || depth > 30) return;
            let nodes;
            try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
            for (const el of nodes) {
              consider(el, depth);
              if (el.shadowRoot) visit(el.shadowRoot, depth + 1);
              const tag = el.tagName ? el.tagName.toLowerCase() : '';
              if (tag === 'iframe' || tag === 'frame') {
                let doc = null;
                try { doc = el.contentDocument; } catch (e) { doc = null; }
                if (doc) visit(doc, depth + 1);
              }
            }
          }
          visit(document, 0);

          return {
            dialogs: out,
            scrims: document.querySelectorAll('.mdc-dialog__scrim').length,
          };
        })()"""

        found = await tab.evaluate(discover) or {}
        dialogs = found.get("dialogs") or []

        if not dialogs:
            return {
                "dismissed": False,
                "reason": "no dialog found",
                "scrims": found.get("scrims", 0),
            }

        # Prefer a dialog that actually exposes buttons; a nested role="dialog"
        # inside a custom element often has none of its own.
        with_buttons = [d for d in dialogs if d.get("buttons")]
        pool = with_buttons or dialogs
        dialog = pool[-1]
        buttons = dialog.get("buttons") or []

        if not buttons:
            return {
                "dismissed": False,
                "reason": "dialog has no visible buttons",
                "dialog": {"tag": dialog.get("tag"),
                           "text": dialog.get("text", "")[:100]},
            }

        # Dismissal words across the languages Colab and Google ship.
        defaults = [
            "\u95dc\u9589", "\u5173\u95ed", "Close", "close",     # close
            "\u77e5\u9053\u4e86", "\u77e5\u9053", "Got it", "OK", "Ok", "ok",
            "\u78ba\u5b9a", "\u786e\u5b9a", "\u7e7c\u7e8c", "\u7ee7\u7eed", "Continue",
            "\u53d6\u6d88", "Cancel",                              # cancel
            "\u7a0d\u5f8c\u518d\u8aaa", "\u7a0d\u540e\u518d\u8bf4", "Later", "Not now",
        ]
        order = list(prefer or []) + defaults

        chosen = None
        for word in order:
            chosen = next((b for b in buttons
                           if word.lower() in (b.get("text") or "").lower()),
                          None)
            if chosen:
                break

        # Never pick a primary/destructive action when a secondary exists.
        if chosen is None:
            chosen = next((b for b in buttons
                           if (b.get("slot") or "") == "secondaryAction"), None)
        if chosen is None:
            chosen = buttons[0]

        if dry_run:
            return {
                "dismissed": False,
                "dry_run": True,
                "would_click": chosen.get("text"),
                "dialog": {"tag": dialog.get("tag"),
                           "text": dialog.get("text", "")[:100]},
                "buttons": [b.get("text") for b in buttons],
            }

        await self._dispatch_click(chosen["x"], chosen["y"], "left", 1)
        await asyncio.sleep(0.8)

        after = await tab.evaluate(discover) or {}
        remaining = after.get("dialogs") or []

        return {
            "dismissed": len(remaining) < len(dialogs),
            "clicked": chosen.get("text"),
            "dialog": {"tag": dialog.get("tag"),
                       "text": dialog.get("text", "")[:100]},
            "buttons_seen": [b.get("text") for b in buttons],
            "dialogs_before": len(dialogs),
            "dialogs_after": len(remaining),
            "scrims_after": after.get("scrims", 0),
        }

    async def _hit_at(self, x: float, y: float) -> dict[str, Any] | None:
        """Report the deepest element at a point and whether a modal covers it.

        ``document.elementFromPoint`` does not cross shadow boundaries, so the
        walk descends manually. A ``mwc-dialog`` / ``md-dialog`` / role=dialog
        in the chain means the click was intercepted.
        """
        tab = self._tab
        if tab is None:
            return None

        js = """(() => {
          const X = %(x)d, Y = %(y)d;
          let el = document.elementFromPoint(X, Y);
          const chain = [];
          let guard = 0;
          while (el && guard++ < 20) {
            const tag = el.tagName ? el.tagName.toLowerCase() : '?';
            // innerText is empty on a custom element whose content lives in a
            // shadow root, so fall back to the shadow root's text.
            let text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!text && el.shadowRoot) {
              text = (el.shadowRoot.textContent || '')
                .replace(/\\s+/g, ' ').trim();
            }
            const r = el.getBoundingClientRect();
            // How much of the viewport this element covers. A dialog scrim
            // covers nearly all of it; a toolbar button covers almost none.
            const coverage =
              (r.width * r.height) /
              Math.max(1, window.innerWidth * window.innerHeight);

            chain.push({
              tag,
              id: el.id || null,
              cls: (el.className || '').toString().slice(0, 40),
              text: text.slice(0, 120),
              coverage: Math.round(coverage * 100) / 100,
            });
            if (!el.shadowRoot) break;
            const inner = el.shadowRoot.elementFromPoint(X, Y);
            if (!inner || inner === el) break;
            el = inner;
          }
          const modal = chain.find(c =>
            (c.tag === 'mwc-dialog' || c.tag === 'md-dialog' ||
             c.tag === 'dialog' ||
             c.cls.includes('scrim') ||
             c.cls.includes('modal-dialog')) &&
            // A real blocker covers a large part of the viewport. Without this
            // check, an ordinary md-outlined-button whose class happens to
            // contain "dialog" is reported as a blocker, which is a false
            // positive that makes every successful click look intercepted.
            (c.coverage || 0) >= 0.5
          );
          return {
            chain,
            blocked: !!modal,
            blocker: modal ? modal.tag : null,
            blocker_text: modal ? modal.text : null,
            top: chain[chain.length - 1] || null,
          };
        })()""" % {"x": int(x), "y": int(y)}

        try:
            value = await tab.evaluate(js)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    # -- verification ------------------------------------------------------

    async def assert_changed(
        self,
        expression: str,
        expect: str | None = None,
        timeout: float = 5.0,
        interval: float = 0.25,
    ) -> dict[str, Any]:
        """Poll a page expression until it matches, and report what happened.

        This is the only reliable way to know an action took effect. A click
        can be dispatched, accepted, and ignored by the page; only observable
        state settles the question.

        Args:
            expression: JavaScript whose value is checked.
            expect: If given, satisfied when the value's text equals this.
                If omitted, any truthy value satisfies it.
            timeout: Seconds to keep polling.
            interval: Seconds between polls.

        Returns ``changed``, the last value, and elapsed time.
        """
        import time as _time

        start = _time.perf_counter()
        deadline = start + timeout
        last: Any = None

        while _time.perf_counter() < deadline:
            try:
                last = await self.evaluate(expression)
            except Exception as exc:
                return {
                    "changed": False,
                    "error": str(exc)[:200],
                    "elapsed": round(_time.perf_counter() - start, 2),
                }

            satisfied = (str(last) == expect) if expect is not None else bool(last)
            if satisfied:
                return {
                    "changed": True,
                    "value": last,
                    "elapsed": round(_time.perf_counter() - start, 2),
                }
            await asyncio.sleep(interval)

        return {
            "changed": False,
            "value": last,
            "expected": expect,
            "elapsed": round(_time.perf_counter() - start, 2),
            "hint": (
                "The condition never became true. Either the action did not "
                "take effect, or the condition is wrong. Run observe() and "
                "check the hit report from the last click."
            ),
        }

    async def _scroll_into_view(self, target: Target, tab: uc.Tab) -> None:
        """Bring the element on screen; off-screen clicks miss silently."""
        if not target.backend_node_id:
            return
        with contextlib.suppress(Exception):
            await tab.send(cdp.dom.scroll_into_view_if_needed(
                backend_node_id=target.backend_node_id
            ))
            await asyncio.sleep(0.08)
            # Refresh coordinates: scrolling moves the element.
            quads = await tab.send(cdp.dom.get_content_quads(
                backend_node_id=target.backend_node_id
            ))
            if quads:
                x, y, w, h = _quad_to_rect(list(quads[0]))
                target.x, target.y = x, y
                target.width, target.height = w, h

    async def _dispatch_click(self, x: float, y: float, button: str,
                              clicks: int) -> None:
        """Raw CDP mouse events — these are trusted by the page."""
        tab = self._require_tab()
        # CDP wants enum members, not strings.
        btn = cdp.input_.MouseButton(button.lower())
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_="mouseMoved", x=x, y=y,
            button=cdp.input_.MouseButton.NONE, buttons=0,
        ))
        for n in range(max(1, clicks)):
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=x, y=y, button=btn,
                buttons=1, click_count=n + 1,
            ))
            await asyncio.sleep(0.015)
            await tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=x, y=y, button=btn,
                buttons=0, click_count=n + 1,
            ))
            if clicks > 1:
                await asyncio.sleep(0.05)

    async def hover(self, *, ref: str | None = None,
                    x: float | None = None, y: float | None = None) -> dict[str, Any]:
        tab = self._require_tab()
        if ref is not None:
            target = self._targets.get(ref)
            if target is None:
                raise HandsError(f"Unknown ref {ref!r}")
            x = target.x + target.width / 2
            y = target.y + target.height / 2
        if x is None or y is None:
            raise HandsError("hover needs 'ref' or 'x'/'y'")
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_="mouseMoved", x=x, y=y,
            button=cdp.input_.MouseButton.NONE, buttons=0,
        ))
        return {"hovered": True, "x": x, "y": y}

    # -- typing ------------------------------------------------------------

    async def type_text(
        self,
        text: str,
        *,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        clear: bool = True,
        submit: bool = False,
        per_char: bool = False,
        delay_ms: int = 0,
    ) -> dict[str, Any]:
        """Focus a field and insert text.

        ``per_char=True`` emits real keydown/keyup per character, which some
        autocomplete widgets require before they will open. Otherwise a single
        ``Input.insert_text`` is used: one round trip, layout independent.
        """
        tab = self._require_tab()

        if ref is not None:
            target = self._targets.get(ref)
            if target is None:
                raise HandsError(f"Unknown ref {ref!r}. Call 'observe' again.")
            await self._scroll_into_view(target, tab)
            cx = target.x + target.width / 2
            cy = target.y + target.height / 2
        elif x is not None and y is not None:
            cx, cy = float(x), float(y)
        else:
            raise HandsError("type_text needs 'ref' or 'x'/'y' to focus a field")

        # A CDP mouse event at the right coordinates does not always focus an
        # input: the page may move the element on mousedown, or a wrapper may
        # swallow the press. Verify focus and fall back to element.focus(),
        # which is what a user's click would have achieved.
        await self._dispatch_click(cx, cy, "left", 1)
        await asyncio.sleep(0.08)

        focused = await self._focus_verify(tab, ref, cx, cy)
        if not focused:
            await self._focus_at(tab, ref, cx, cy)
            await asyncio.sleep(0.05)

        if clear:
            await self.press("ctrl+a", settle=False)
            await self.press("Backspace", settle=False)
            await asyncio.sleep(0.03)

        if per_char:
            for ch in text:
                await self._send_char(ch)
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000.0)
        else:
            await tab.send(cdp.input_.insert_text(text))

        if submit:
            await asyncio.sleep(0.05)
            await self.press("Enter")

        active = await self._active_element()
        return {
            "typed": len(text),
            "submitted": submit,
            "active": active,
            "url": await self._safe_url(),
        }

    async def _focus_verify(self, tab: uc.Tab, ref: str | None,
                            x: float, y: float) -> bool:
        """Is a text-entry element focused at the click point?

        A point inside an iframe reports the frame element as the active
        element, and the inner document is not reachable from here. That counts
        as success: the click was dispatched into the frame, so refusing it
        would send the caller into the fallback, which can only steal focus
        back out of the frame and break typing.
        """
        js = """(() => {
          const a = document.activeElement;
          if (!a) return false;
          const t = (a.tagName || '').toLowerCase();
          if (t === 'iframe' || t === 'frame') return true;
          if (t === 'textarea') return true;
          if (t === 'input') {
            const kind = (a.getAttribute('type') || 'text').toLowerCase();
            return !['checkbox','radio','hidden','submit','button',
                     'file','range','color'].includes(kind);
          }
          return a.getAttribute && a.getAttribute('contenteditable') === 'true';
        })()"""
        with contextlib.suppress(Exception):
            return bool(await tab.evaluate(js))
        return False

    async def _focus_at(self, tab: uc.Tab, ref: str | None,
                        x: float, y: float) -> None:
        """Focus the element at a point through the DOM.

        This is the fallback when the synthetic mouse press did not focus the
        field. It targets whatever is actually at the coordinates, so it works
        even when the resolved ref has moved.
        """
        js = """(() => {
          const el = document.elementFromPoint(%(x)d, %(y)d);
          if (!el) return false;
          // Never focus an iframe element: it moves focus out of the frame the
          // click already landed in, and the inner field cannot be re-focused
          // from this document.
          if ((el.tagName || '').toLowerCase() === 'iframe') return false;
          let target = el;
          // If the point landed on a child, climb to the field.
          while (target && target !== document.body) {
            const t = (target.tagName || '').toLowerCase();
            if (t === 'input' || t === 'textarea' ||
                (target.getAttribute &&
                 target.getAttribute('contenteditable') === 'true')) {
              target.focus();
              if (t === 'input' || t === 'textarea') {
                target.select && target.select();
              }
              return true;
            }
            target = target.parentElement;
          }
          if (el.focus) { el.focus(); return true; }
          return false;
        })()""" % {"x": int(x), "y": int(y)}
        with contextlib.suppress(Exception):
            await tab.evaluate(js)

    async def _active_element(self) -> dict[str, Any] | None:
        tab = self._tab
        if tab is None:
            return None
        js = """(() => {
          const el = document.activeElement;
          if (!el) return null;
          return {
            tag: el.tagName ? el.tagName.toLowerCase() : null,
            id: el.id || null,
            name: el.getAttribute ? el.getAttribute('name') : null,
            type: el.getAttribute ? el.getAttribute('type') : null,
            value: (el.value !== undefined && el.value !== null)
              ? String(el.value).slice(0, 200) : null,
          };
        })()"""
        with contextlib.suppress(Exception):
            value = await tab.evaluate(js)
            if isinstance(value, dict):
                return value
        return None

    async def _send_char(self, ch: str) -> None:
        tab = self._require_tab()
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyDown", text=ch, key=ch,
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyUp", key=ch,
        ))

    async def press(self, keys: str, *, settle: bool = True) -> dict[str, Any]:
        """Press a chord such as ``Enter``, ``ctrl+a``, ``ctrl+shift+t``."""
        tab = self._require_tab()
        parts = [p for p in keys.replace("+", " ").split() if p]
        if not parts:
            raise HandsError("press needs at least one key")

        *mods, final = parts
        modifiers = 0
        for m in mods:
            modifiers |= _MODIFIERS.get(m.lower(), 0)
        key = _canonical_key(final)
        code, vk = _KEY_TABLE.get(key, ("", 0))

        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyDown", key=key, code=code or None,
            windows_virtual_key_code=vk or None,
            native_virtual_key_code=vk or None,
            modifiers=modifiers,
        ))
        await tab.send(cdp.input_.dispatch_key_event(
            type_="keyUp", key=key, code=code or None,
            windows_virtual_key_code=vk or None,
            native_virtual_key_code=vk or None,
            modifiers=modifiers,
        ))

        if settle:
            await self._settle(quiet_ms=220, max_ms=3500)
        return {"pressed": keys, "url": await self._safe_url()}

    # -- scrolling ---------------------------------------------------------

    async def scroll(self, *, dy: int = 0, dx: int = 0,
                     x: float | None = None, y: float | None = None) -> dict[str, Any]:
        tab = self._require_tab()
        info = await self._viewport_info()
        px = x if x is not None else (info.get("innerWidth", 1280) / 2)
        py = y if y is not None else (info.get("innerHeight", 900) / 2)
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_="mouseWheel", x=px, y=py, delta_x=dx, delta_y=dy,
        ))
        await self._settle(quiet_ms=180, max_ms=2500)
        return {"scrolled": {"dx": dx, "dy": dy},
                "viewport": await self._viewport_info()}

    # -- escape hatch ------------------------------------------------------

    async def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        tab = self._require_tab()
        return await tab.evaluate(expression, await_promise=await_promise)

    # -- screenshots -------------------------------------------------------

    async def screenshot(self, path: str | None = None,
                         *, full_page: bool = False) -> dict[str, Any]:
        tab = self._require_tab()
        if path is None:
            base = Path(os.environ.get("HANDS_SHOT_DIR", Path.cwd()))
            out = base / f"shot-{int(time.time() * 1000)}.png"
        else:
            out = Path(path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)

        data = await tab.send(cdp.page.capture_screenshot(
            format_="png", capture_beyond_viewport=full_page,
        ))
        raw = data if isinstance(data, str) else getattr(data, "data", "")
        out.write_bytes(base64.b64decode(raw))
        return {"path": str(out), "bytes": out.stat().st_size,
                "full_page": full_page}

    # -- tabs --------------------------------------------------------------

    async def tabs(self) -> list[dict[str, Any]]:
        browser = self._browser
        if browser is None:
            raise HandsError("Browser is not running. Call 'start' first.")
        out: list[dict[str, Any]] = []

        # Attached browsers list tabs without live sessions, so titles come from
        # the target info rather than an evaluate round trip.
        refresh = getattr(browser, "refresh", None)
        if refresh is not None:
            pages = await refresh()
            for i, p in enumerate(pages):
                out.append({"index": i, "url": p.get("url"),
                            "title": p.get("title")})
            return out

        for i, t in enumerate(browser.tabs):
            title = ""
            with contextlib.suppress(Exception):
                title = (await t.evaluate("document.title")) or ""
            out.append({"index": i, "url": t.url, "title": title})
        return out

    async def switch_tab(self, index: int) -> dict[str, Any]:
        browser = self._browser
        if browser is None:
            raise HandsError("Browser is not running. Call 'start' first.")

        # An attached browser needs a fresh flat session per tab, because CDP
        # domains are enabled per session and a stale session cannot run
        # DOM.getDocument.
        opener = getattr(browser, "open", None)
        if opener is not None:
            tab = await opener(index)
        else:
            try:
                tab = browser.tabs[index]
            except IndexError as exc:
                raise HandsError(f"No tab at index {index}") from exc

        self._tab = tab
        self._targets.clear()
        with contextlib.suppress(Exception):
            await tab.bring_to_front()
        return {"index": index, "url": await self._safe_url(),
                "title": await self.title()}

    async def new_tab(self, url: str = "about:blank") -> dict[str, Any]:
        browser = self._browser
        if browser is None:
            raise HandsError("Browser is not running. Call 'start' first.")
        tab = await browser.get(url)
        self._tab = tab
        self._targets.clear()
        return {"url": await self._safe_url(), "tabs": len(browser.tabs)}

    async def close_tab(self, index: int | None = None) -> dict[str, Any]:
        browser = self._browser
        if browser is None:
            raise HandsError("Browser is not running. Call 'start' first.")
        try:
            tab = browser.tabs[index if index is not None else len(browser.tabs) - 1]
        except IndexError as exc:
            raise HandsError("No such tab") from exc
        await tab.close()
        if self._tab is tab:
            self._tab = browser.main_tab
        self._targets.clear()
        return {"closed": True, "tabs": len(browser.tabs)}


# --------------------------------------------------------------------------
# Key tables
# --------------------------------------------------------------------------

_MODIFIERS: dict[str, int] = {
    "alt": 1, "ctrl": 2, "control": 2,
    "meta": 4, "cmd": 4, "win": 4, "shift": 8,
}

_KEY_TABLE: dict[str, tuple[str, int]] = {
    "Enter": ("Enter", 13),
    "Tab": ("Tab", 9),
    "Escape": ("Escape", 27),
    "Backspace": ("Backspace", 8),
    "Delete": ("Delete", 46),
    "ArrowUp": ("ArrowUp", 38),
    "ArrowDown": ("ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", 37),
    "ArrowRight": ("ArrowRight", 39),
    "Home": ("Home", 36),
    "End": ("End", 35),
    "PageUp": ("PageUp", 33),
    "PageDown": ("PageDown", 34),
    "Space": ("Space", 32),
    "a": ("KeyA", 65), "c": ("KeyC", 67), "v": ("KeyV", 86),
    "x": ("KeyX", 88), "z": ("KeyZ", 90),
}

_KEY_ALIASES: dict[str, str] = {
    "enter": "Enter", "return": "Enter",
    "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "Backspace", "delete": "Delete",
    "up": "ArrowUp", "down": "ArrowDown",
    "left": "ArrowLeft", "right": "ArrowRight",
    "space": "Space", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
}


def _canonical_key(name: str) -> str:
    return _KEY_ALIASES.get(name.lower(), name)
