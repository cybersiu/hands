"""
bench.py — Measure real latency of each operation.

Reports median and p95 over repeated calls against a running browser. Numbers
include the full round trip: Python -> CDP -> Chrome -> DOM -> back.

    python bench.py --iterations 20
    python bench.py --url https://example.com

IMPORTANT: Chrome's approval-mode debugging accepts one client at a time. If an
MCP server (or anything else) already holds the connection, this script's
handshake will time out. Stop the other client first, or accept that the
measurement cannot run while it is attached.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

import attach
from browser import Session


def stats(samples: list[float]) -> str:
    if not samples:
        return "n/a"
    ms = [s * 1000 for s in samples]
    p95 = sorted(ms)[max(0, int(len(ms) * 0.95) - 1)]
    return (f"median {statistics.median(ms):6.1f} ms   "
            f"min {min(ms):6.1f}   p95 {p95:6.1f}   max {max(ms):6.1f}")


async def timeit(label: str, n: int, fn) -> None:
    samples: list[float] = []
    for _ in range(n):
        t0 = time.perf_counter()
        await fn()
        samples.append(time.perf_counter() - t0)
    print(f"  {label:<34} {stats(samples)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iterations", type=int, default=15)
    ap.add_argument("--url", default=None,
                    help="Page to benchmark against (default: current tab)")
    args = ap.parse_args()
    n = args.iterations

    session = Session()

    # Attach to whatever Chrome is running.
    t0 = time.perf_counter()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    await sock.connect()
    pages = await attach.list_targets(sock)
    await sock.close()
    idx = next((i for i, p in enumerate(pages)
                if "colab.research.google.com" in (p.get("url") or "")), None)
    if idx is None:
        idx = 0
    info = await attach.attach(session, tab_index=idx)
    attach_ms = (time.perf_counter() - t0) * 1000

    print(f"tab: {info['active_title']!r}")
    print(f"attach cost: {attach_ms:.0f} ms (one time)\n")

    if args.url:
        t0 = time.perf_counter()
        await session.navigate(args.url)
        print(f"navigate cost: {(time.perf_counter()-t0)*1000:.0f} ms\n")

    tab = session._tab

    print("round trips (no DOM work):")
    await timeit("Browser.getVersion", n,
                 lambda: session._browser._socket.send("Browser.getVersion"))
    await timeit("Runtime.evaluate('1+1')", n,
                 lambda: tab.evaluate("1+1"))
    await timeit("Runtime.evaluate('document.title')", n,
                 lambda: tab.evaluate("document.title"))

    print("\nDOM work:")
    await timeit("DOM.getDocument(depth=-1,pierce)", n,
                 lambda: tab.send(__import__("zendriver").cdp.dom.get_document(
                     depth=-1, pierce=True)))

    print("\nhigh level:")
    await timeit("observe()", max(3, n // 3), lambda: session.observe(max_nodes=200))
    await timeit("scroll(dy=50)", n, lambda: session.scroll(dy=50))

    # Click benchmark: click the same harmless point repeatedly.
    print("\nclick:")
    await timeit("click(x,y) settle=False", n,
                 lambda: session.click(x=640, y=400, settle=False))
    await timeit("click(x,y) settle=True", max(3, n // 3),
                 lambda: session.click(x=640, y=400, settle=True))

    print("\nfor reference:")
    print(f"  a human click takes roughly 150-300 ms")
    print(f"  chrome-devtools-mcp snapshot is typically 1-4 s on a dense page")

    await session._browser._socket.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
