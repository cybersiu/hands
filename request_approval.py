"""
request_approval.py — Ask Chrome to show its remote-debugging approval dialog.

Chrome only raises the dialog when a client actually requests a debugging
session, and the request must come over the approval endpoint — not a plain
WebSocket. This reproduces what `chrome-devtools-mcp --autoConnect` sends:

    POST /json/version          (the discovery the approval flow expects)
    then a WebSocket upgrade to the path in DevToolsActivePort

If the port is not listening yet, nothing can be requested: the feature has to
be switched on first at chrome://inspect/#remote-debugging.

    python request_approval.py
    python request_approval.py --open-inspect   # also open the settings page
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach


def http_probe(url: str, timeout: float = 3.0) -> dict:
    """Report what the approval endpoint answers."""
    try:
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "replace")
            return {"status": r.status, "body": body[:300]}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read(200).decode("utf-8", "replace")}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


async def try_ws(port: int, path: str) -> dict:
    """Attempt the WebSocket upgrade that triggers the dialog."""
    import websockets

    url = f"ws://127.0.0.1:{port}{path}"
    try:
        async with websockets.connect(url, open_timeout=8, max_size=2 ** 24) as ws:
            await ws.send('{"id":1,"method":"Browser.getVersion"}')
            raw = await asyncio.wait_for(ws.recv(), timeout=8)
            return {"connected": True, "reply": raw[:200]}
    except Exception as exc:
        return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open-inspect", action="store_true",
                    help="Open chrome://inspect/#remote-debugging in the browser")
    args = ap.parse_args()

    print("Locating Chrome's debugging endpoint...")
    try:
        port, path = attach.read_devtools_port()
    except attach.AttachError as exc:
        print(f"  {exc}")
        print("\nChrome has not written DevToolsActivePort, which means remote")
        print("debugging is not enabled yet.")
        if args.open_inspect:
            webbrowser.open("chrome://inspect/#remote-debugging")
        return 1

    print(f"  port: {port}")
    print(f"  path: {path}")

    # Is anything listening?
    import socket
    with socket.socket() as s:
        s.settimeout(2)
        listening = s.connect_ex(("127.0.0.1", port)) == 0
    print(f"  listening: {listening}")

    if not listening:
        print("\nNothing is listening on that port, so no approval dialog can be")
        print("raised. Enable remote debugging first:")
        print("  1. open chrome://inspect/#remote-debugging")
        print("  2. switch the toggle on")
        if args.open_inspect:
            webbrowser.open("chrome://inspect/#remote-debugging")
        return 1

    # The approval endpoint answers HTTP probes with 404 in this mode; that is
    # expected and proves the port is the approval one, not a normal debug port.
    print("\nHTTP probe (404 is expected here):")
    for p in ("/json/version", "/json/list", "/"):
        print(f"  {p:16} {http_probe(f'http://127.0.0.1:{port}{p}')}")

    # The WebSocket upgrade is what makes Chrome ask the user.
    print("\nRequesting a debugging session over the WebSocket...")
    print("Chrome should now show an approval dialog. Approve it.")
    result = asyncio.run(try_ws(port, path))
    print(f"  {json.dumps(result, ensure_ascii=False)}")

    if result.get("connected"):
        print("\nApproved: the endpoint answered a CDP handshake.")
        print("You can run speed_attach.py now.")
        return 0

    print("\nNot connected yet. If no dialog appeared, the toggle at")
    print("chrome://inspect/#remote-debugging is still off.")
    return 1


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(main())
