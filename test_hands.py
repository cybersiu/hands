"""
test_hands.py — end-to-end checks against a local fixture.

Runs a real Chrome, so it needs a desktop session. Each test asserts on
observable state rather than screenshots.

    python test_hands.py
"""

from __future__ import annotations

import asyncio
import http.server
import json
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from browser import HandsError, Session

# --------------------------------------------------------------------------
# Fixture pages
# --------------------------------------------------------------------------

PAGE_MAIN = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hands fixture</title>
<style>
  body { font-family: system-ui, sans-serif; padding: 24px; }
  button, input { font-size: 16px; padding: 8px 12px; margin: 4px; }
  #log { margin-top: 16px; padding: 12px; background: #f0f0f0; white-space: pre; }
</style></head>
<body>
  <h1>Hands fixture</h1>

  <button id="plain" onclick="record('plain')">Plain button</button>
  <input id="name" placeholder="Your name">

  <!-- Web component with a closed-ish shadow root -->
  <my-widget id="widget"></my-widget>

  <!-- Same-origin iframe -->
  <iframe id="same" src="/inner" width="400" height="160"></iframe>

  <!-- Cross-origin iframe: different port, so a different origin -->
  <iframe id="cross" src="CROSS_URL" width="400" height="160"></iframe>

  <div id="log">ready</div>

<script>
  const events = [];
  function record(what) {
    events.push(what);
    document.getElementById('log').textContent = events.join(' | ');
  }
  window.__events = () => events;

  // A widget whose button only exists inside a shadow root.
  class MyWidget extends HTMLElement {
    connectedCallback() {
      const root = this.attachShadow({mode: 'open'});
      root.innerHTML = `
        <style>button { background: #4a7; color: #fff; border: 0; padding: 10px 16px; }</style>
        <button id="shadow-btn">Shadow button</button>
      `;
      root.getElementById('shadow-btn')
          .addEventListener('click', () => record('shadow'));
    }
  }
  customElements.define('my-widget', MyWidget);

  // Trusted-event check: a button that ignores untrusted clicks.
  const trust = document.createElement('button');
  trust.id = 'trust-check';
  trust.textContent = 'Trusted only';
  trust.addEventListener('click', (e) => {
    record('trust:' + (e.isTrusted ? 'trusted' : 'untrusted'));
  });
  document.body.appendChild(trust);
</script>

  <!-- Tall spacer so the page can actually scroll -->
  <div style="height: 2400px; background: linear-gradient(#fff, #eef);">
    <p style="padding-top: 1800px;">Bottom marker</p>
  </div>
</body></html>
"""

PAGE_INNER = """<!doctype html>
<html><head><meta charset="utf-8"><title>inner</title></head>
<body style="font-family: system-ui; padding: 12px;">
  <button id="inner-btn" onclick="
    window.parent.document.getElementById('log').textContent += ' | inner';
    this.textContent = 'inner clicked';
  ">Inner button</button>
</body></html>
"""


# --------------------------------------------------------------------------
# Tiny static server
# --------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def serve(routes: dict[str, str], port: int) -> http.server.ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"routes": routes})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    mark = "ok  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))


async def run() -> int:
    port_main = _free_port()
    port_cross = _free_port()

    main_routes = {
        "/": PAGE_MAIN.replace("CROSS_URL", f"http://127.0.0.1:{port_cross}/"),
        "/inner": PAGE_INNER,
    }
    cross_routes = {"/": """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="font-family: system-ui; padding: 12px;">
  <button id="cross-btn" onclick="this.textContent='cross clicked'">Cross-origin button</button>
</body></html>"""}

    s1 = serve(main_routes, port_main)
    s2 = serve(cross_routes, port_cross)
    base = f"http://127.0.0.1:{port_main}/"

    session = Session()
    try:
        print("\n== start ==")
        info = await session.start(headless=False, url=base)
        check("browser launched", info["url"].startswith(base), info["url"])

        print("\n== observe ==")
        obs = await session.observe()
        check("observe returned elements", obs["count"] > 0, f"count={obs['count']}")

        tags = [e["tag"] for e in obs["elements"]]
        check("plain button visible", "button" in tags)
        check("input visible", "input" in tags)

        by_text = {e["text"]: e for e in obs["elements"]}

        # -- plain click ---------------------------------------------------
        print("\n== plain click ==")
        plain = next((e for e in obs["elements"] if e["text"] == "Plain button"), None)
        if plain:
            await session.click(ref=plain["ref"])
            events = await session.evaluate("window.__events()")
            check("plain button fired", "plain" in (events or []), str(events))
        else:
            check("plain button found", False, str(list(by_text)[:10]))

        # -- trusted event -------------------------------------------------
        print("\n== trusted event ==")
        await session.navigate(base, settle=True)
        obs = await session.observe()
        trust = next((e for e in obs["elements"] if e["text"] == "Trusted only"), None)
        if trust:
            await session.click(ref=trust["ref"])
            events = await session.evaluate("window.__events()") or []
            check("click is isTrusted", "trust:trusted" in events, str(events))
        else:
            check("trust button found", False)

        # -- shadow DOM ----------------------------------------------------
        print("\n== shadow DOM ==")
        await session.navigate(base, settle=True)
        obs = await session.observe()
        shadow = next((e for e in obs["elements"] if e["text"] == "Shadow button"), None)
        check("shadow element discovered", shadow is not None)
        if shadow:
            check("shadow depth recorded", shadow.get("shadow_depth", 0) >= 1,
                  str(shadow.get("shadow_depth")))
            await session.click(ref=shadow["ref"])
            events = await session.evaluate("window.__events()") or []
            check("shadow button fired", "shadow" in events, str(events))

        # -- same-origin iframe -------------------------------------------
        print("\n== same-origin iframe ==")
        await session.navigate(base, settle=True)
        obs = await session.observe()
        inner = next((e for e in obs["elements"] if e["text"] == "Inner button"), None)
        check("iframe element discovered", inner is not None)
        if inner:
            check("iframe flag set", inner.get("in_frame") is True, str(inner.get("in_frame")))
            await session.click(ref=inner["ref"])
            await asyncio.sleep(0.3)
            log = await session.evaluate("document.getElementById('log').textContent")
            check("iframe button fired", "inner" in (log or ""), str(log))

        # -- cross-origin iframe ------------------------------------------
        print("\n== cross-origin iframe ==")
        await session.navigate(base, settle=True)
        await asyncio.sleep(0.6)
        obs = await session.observe()
        cross = next((e for e in obs["elements"] if e["text"] == "Cross-origin button"), None)
        check("cross-origin element discovered", cross is not None,
              f"found {[e['text'] for e in obs['elements']][:12]}")
        if cross:
            await session.click(ref=cross["ref"])
            await asyncio.sleep(0.3)
            # The cross-origin frame cannot be read from the parent; verify by
            # observing the frame's own button text through CDP instead.
            obs2 = await session.observe()
            cross2 = next((e for e in obs2["elements"]
                           if "cross clicked" in e["text"]), None)
            check("cross-origin button fired", cross2 is not None,
                  str([e["text"] for e in obs2["elements"]][:12]))

        # -- typing --------------------------------------------------------
        print("\n== typing ==")
        await session.navigate(base, settle=True)
        obs = await session.observe()
        field = next((e for e in obs["elements"] if e["tag"] == "input"), None)
        if field:
            await session.type_text("Wing Lee", ref=field["ref"])
            value = await session.evaluate(
                "document.getElementById('name').value"
            )
            check("text typed", value == "Wing Lee", repr(value))

            await session.type_text("replaced", ref=field["ref"], clear=True)
            value = await session.evaluate(
                "document.getElementById('name').value"
            )
            check("field cleared then typed", value == "replaced", repr(value))
        else:
            check("input found", False)

        # -- keyboard ------------------------------------------------------
        print("\n== keyboard ==")
        await session.press("ctrl+a")
        await session.press("Backspace")
        value = await session.evaluate("document.getElementById('name').value")
        check("ctrl+a then backspace clears", value == "", repr(value))

        # -- scroll --------------------------------------------------------
        print("\n== scroll ==")
        before = await session.evaluate("window.scrollY")
        await session.scroll(dy=400)
        after = await session.evaluate("window.scrollY")
        check("scroll moved", after != before, f"{before} -> {after}")

        # -- error handling ------------------------------------------------
        print("\n== errors ==")
        try:
            await session.click(ref="e9999")
            check("unknown ref rejected", False)
        except HandsError as exc:
            check("unknown ref rejected", "Unknown ref" in str(exc), str(exc))

        try:
            await session.click()
            check("missing target rejected", False)
        except HandsError:
            check("missing target rejected", True)

    finally:
        await session.stop()
        s1.shutdown()
        s2.shutdown()

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
    raise SystemExit(asyncio.run(run()))
