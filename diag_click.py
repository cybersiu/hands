"""
diag_click.py — Why do some clicks take seconds?

Hypothesis: the click path does more work than dispatching an event. Each click
currently runs `_hit_at`, which walks the shadow tree from `elementFromPoint`.
On a page with 3000 elements and 200 shadow roots that walk is expensive, and it
happens after every click.

This measures the pieces separately so the cost can be attributed.

    python diag_click.py --url https://www.yahoo.com/
    python diag_click.py --url https://en.wikipedia.org/wiki/Main_Page
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

import attach
from browser import Session


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"connect failed: {exc}")
        return 1

    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        print(f"navigating to {args.url}")
        await session.navigate(args.url)
        await asyncio.sleep(2.0)

        # Baseline: what does a bare round trip cost here?
        bare: list[float] = []
        for _ in range(args.repeats):
            t = time.perf_counter()
            await session.evaluate("1")
            bare.append(time.perf_counter() - t)
        print(f"\nbare Runtime.evaluate  median "
              f"{statistics.median(bare) * 1000:7.1f} ms")

        # Cost of the hit check alone, which runs after every click.
        hit: list[float] = []
        for _ in range(args.repeats):
            t = time.perf_counter()
            await session._hit_at(400, 300)
            hit.append(time.perf_counter() - t)
        print(f"_hit_at(400,300)       median "
              f"{statistics.median(hit) * 1000:7.1f} ms")

        # Cost of a full observe.
        obs: list[float] = []
        for _ in range(max(2, args.repeats // 2)):
            t = time.perf_counter()
            await session.observe(max_nodes=200)
            obs.append(time.perf_counter() - t)
        print(f"observe(200)           median "
              f"{statistics.median(obs) * 1000:7.1f} ms")

        # Cost of the dispatch itself, with no hit check.
        clicks: list[float] = []
        for _ in range(args.repeats):
            t = time.perf_counter()
            await session._dispatch_click(400, 300, "left", 1)
            clicks.append(time.perf_counter() - t)
        print(f"_dispatch_click only   median "
              f"{statistics.median(clicks) * 1000:7.1f} ms")

        # Full click as the tool does it.
        full: list[float] = []
        for _ in range(args.repeats):
            t = time.perf_counter()
            await session.click(x=400, y=300, settle=False)
            full.append(time.perf_counter() - t)
        print(f"click(settle=False)    median "
              f"{statistics.median(full) * 1000:7.1f} ms")

        total = statistics.median(full) * 1000
        parts = (statistics.median(clicks) * 1000
                 + statistics.median(hit) * 1000)
        print(f"\ndispatch + hit check   {parts:7.1f} ms "
              f"of {total:.1f} ms total")

        return 0
    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
