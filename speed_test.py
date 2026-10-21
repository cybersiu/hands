"""
speed_test.py — Time the full Colab flow from a cold browser.

Chrome is closed, so this launches its own instance (no approval flow needed)
and measures each phase:

    launch  ->  navigate  ->  connect runtime  ->  run all  ->  first output

The point is to separate the costs a user actually feels:

  * browser launch        (paid once per session)
  * page load             (Colab is a heavy SPA)
  * runtime connect       (Google's backend, outside our control)
  * click-to-execution    (the part our code owns)

    python speed_test.py
    python speed_test.py --url <notebook>
    python speed_test.py --headless
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

from browser import Session
from colab_full_flow import (
    CELLS_JS, MODAL_JS, RUNALL_JS, RUNTIME_JS,
    classify_runtime,
)

DEFAULT_URL = ("https://colab.research.google.com/drive/"
               "12hbaPkNheqTGFi_QA532JtRBdNaDWwFv")


class Clock:
    """Marks phase boundaries so the report reads as a timeline."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: list[tuple[str, float]] = []

    def mark(self, label: str) -> float:
        elapsed = time.perf_counter() - self.t0
        self.marks.append((label, elapsed))
        print(f"  [{elapsed:7.2f}s] {label}")
        return elapsed

    def since(self, label: str) -> float:
        for name, at in reversed(self.marks):
            if name == label:
                return time.perf_counter() - self.t0 - at
        return 0.0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--watch", type=float, default=120.0)
    args = ap.parse_args()

    clock = Clock()
    session = Session()

    try:
        # ---- launch ------------------------------------------------------
        print("phase 1: launch")
        await session.start(headless=args.headless, url="about:blank")
        clock.mark("browser launched")

        # ---- navigate ----------------------------------------------------
        print("\nphase 2: navigate")
        await session.navigate(args.url)
        clock.mark("notebook url loaded")

        # Colab keeps rendering after load; wait for a cell to exist.
        ready = await session.assert_changed(
            "document.querySelectorAll('div.cell').length",
            expect=None, timeout=45.0, interval=0.5,
        )
        cells_now = await session.evaluate(
            "document.querySelectorAll('div.cell').length")
        clock.mark(f"cells rendered (count={cells_now}, "
                   f"waited={ready.get('elapsed')}s)")

        # ---- clear blockers ----------------------------------------------
        print("\nphase 3: clear blockers")
        modal = await session.evaluate(MODAL_JS) or {}
        dialogs = modal.get("dialogs") or []
        print(f"  {len(dialogs)} dialog(s) detected")
        if dialogs:
            d = dialogs[-1]
            print(f"  text: {d.get('text', '')[:60]!r} "
                  f"buttons={[b['text'] for b in (d.get('buttons') or [])]}")
            if d.get("buttons"):
                btn = d["buttons"][0]
                await session.click(x=btn["x"], y=btn["y"], settle=False)
                await asyncio.sleep(0.8)
        clock.mark("blockers handled")

        # ---- runtime -----------------------------------------------------
        print("\nphase 4: runtime")
        rt = await session.evaluate(RUNTIME_JS) or {}
        state = classify_runtime(rt.get("head") or "")
        print(f"  initial state: {state}  ({(rt.get('head') or '')[:40]!r})")

        if state != "connected":
            if not rt.get("found"):
                print("  connect control not found")
                return 1
            t_click = time.perf_counter()
            await session.click(x=rt["x"], y=rt["y"], settle=False)
            clock.mark("connect clicked")

            deadline = time.perf_counter() + 120
            connected = False
            while time.perf_counter() < deadline:
                await asyncio.sleep(2.0)
                cur = await session.evaluate(RUNTIME_JS) or {}
                if classify_runtime(cur.get("head") or "") == "connected":
                    connected = True
                    break
                # A consent dialog may appear mid-connect.
                m = await session.evaluate(MODAL_JS) or {}
                if m.get("dialogs"):
                    dd = m["dialogs"][-1]
                    if dd.get("buttons"):
                        b = dd["buttons"][0]
                        print(f"  clearing mid-connect dialog "
                              f"{b['text']!r}")
                        await session.click(x=b["x"], y=b["y"], settle=False)
                        await asyncio.sleep(1.0)

            if not connected:
                print("  runtime did not connect")
                return 1
            clock.mark(f"runtime connected "
                       f"({time.perf_counter() - t_click:.1f}s after click)")
        else:
            clock.mark("runtime already connected")

        # ---- run all -----------------------------------------------------
        print("\nphase 5: run all")
        before = await session.evaluate(CELLS_JS) or {}
        print(f"  before: code={before.get('codeCells')} "
              f"executed={before.get('executed')}")

        ra = await session.evaluate(RUNALL_JS)
        if not ra:
            print("  run-all not found")
            return 1

        t_run = time.perf_counter()
        await session.click(x=ra["x"], y=ra["y"], settle=False)
        clock.mark("run-all clicked")

        # ---- first output ------------------------------------------------
        print("\nphase 6: wait for execution")
        deadline = time.perf_counter() + args.watch
        first_exec = None
        while time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            cells = await session.evaluate(CELLS_JS) or {}
            if cells.get("executed", 0) > 0 and first_exec is None:
                first_exec = time.perf_counter() - t_run
                clock.mark(f"first cell executed ({first_exec:.1f}s after click)")
            if (cells.get("executed", 0) > 0
                    and cells.get("running", 0) == 0):
                break

        clock.mark("all cells idle")

        final = await session.evaluate(CELLS_JS) or {}
        print(f"\nresult: code={final.get('codeCells')} "
              f"executed={final.get('executed')} "
              f"never={final.get('never')}")
        for i, d in enumerate(final.get("detail") or []):
            flag = "RUN" if d.get("running") else (
                f"#{d['count']}" if isinstance(d.get("count"), int) else "---")
            print(f"  [{i}] {flag:<4} {(d.get('output') or '')[:60]!r}")

        # ---- summary -----------------------------------------------------
        total = time.perf_counter() - clock.t0
        print("\n" + "=" * 62)
        print("TIMELINE")
        print("=" * 62)
        prev = 0.0
        for label, at in clock.marks:
            print(f"  {at:7.2f}s  (+{at - prev:5.2f}s)  {label}")
            prev = at
        print(f"  {total:7.2f}s  total")

        print("\nWhere the time went:")
        launch = clock.since("browser launched") if clock.marks else 0
        print(f"  browser launch + page load : "
              f"{clock.marks[1][1]:6.2f}s" if len(clock.marks) > 1 else "")
        if first_exec is not None:
            print(f"  click -> first output      : {first_exec:6.2f}s")
        print(f"  everything                 : {total:6.2f}s")

        return 0 if final.get("executed", 0) > 0 else 1

    finally:
        await session.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
