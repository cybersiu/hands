"""
speed_attach.py — Time the Colab flow against a browser the user already has open.

This is the realistic path: the user is signed in, Chrome is running, and the
tool attaches over the approval flow. It measures:

    attach  ->  navigate  ->  connect runtime  ->  run all  ->  first output

Repeated attach/detach cycles destabilise Chrome's approval session, so this
opens one connection and keeps it for the whole run.

    python speed_attach.py
    python speed_attach.py --url <notebook>
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach
from browser import Session
from colab_full_flow import (
    CELLS_JS, MODAL_JS, RUNALL_JS, RUNTIME_JS, classify_runtime,
)

DEFAULT_URL = ("https://colab.research.google.com/drive/"
               "12hbaPkNheqTGFi_QA532JtRBdNaDWwFv")


class Clock:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.marks: list[tuple[str, float]] = []

    def mark(self, label: str) -> float:
        at = time.perf_counter() - self.t0
        self.marks.append((label, at))
        print(f"  [{at:7.2f}s] {label}")
        return at


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--watch", type=float, default=120.0)
    args = ap.parse_args()

    clock = Clock()
    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())

    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print("Could not connect to Chrome.")
        print(f"  {exc}")
        print("\nApprove remote debugging at chrome://inspect/#remote-debugging")
        return 1

    try:
        print("phase 1: attach")
        version = await sock.send("Browser.getVersion")
        print(f"  browser: {version.get('product')}")

        pages = await attach.list_targets(sock)
        print(f"  {len(pages)} tab(s) open")
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), None)
        if idx is None:
            idx = 0

        info = await attach.attach(session, tab_index=idx)
        clock.mark(f"attached to {info.get('active_title', '')[:40]!r}")

        # ---- navigate ----------------------------------------------------
        print("\nphase 2: navigate")
        await session.navigate(args.url)
        clock.mark("notebook url loaded")

        await session.assert_changed(
            "document.querySelectorAll('div.cell').length",
            expect=None, timeout=45.0, interval=0.5,
        )
        cells_now = await session.evaluate(
            "document.querySelectorAll('div.cell').length")
        clock.mark(f"cells rendered (count={cells_now})")

        # ---- blockers ----------------------------------------------------
        print("\nphase 3: blockers")
        modal = await session.evaluate(MODAL_JS) or {}
        dialogs = modal.get("dialogs") or []
        print(f"  {len(dialogs)} dialog(s)")
        for d in dialogs[:3]:
            btns = [b["text"] for b in (d.get("buttons") or [])]
            print(f"    {d.get('tag')}: {d.get('text', '')[:50]!r} buttons={btns}")
        if dialogs and (dialogs[-1].get("buttons")):
            b = dialogs[-1]["buttons"][0]
            print(f"  clearing via {b['text']!r}")
            await session.click(x=b["x"], y=b["y"], settle=False)
            await asyncio.sleep(0.8)
        clock.mark("blockers handled")

        # ---- runtime -----------------------------------------------------
        print("\nphase 4: runtime")
        rt = await session.evaluate(RUNTIME_JS) or {}
        state = classify_runtime(rt.get("head") or "")
        print(f"  initial: {state}  ({(rt.get('head') or '')[:40]!r})")

        if state != "connected":
            if not rt.get("found"):
                print("  connect control not found")
                return 1
            t_click = time.perf_counter()
            await session.click(x=rt["x"], y=rt["y"], settle=False)
            clock.mark("connect clicked")

            deadline = time.perf_counter() + 120
            while time.perf_counter() < deadline:
                await asyncio.sleep(2.0)
                cur = await session.evaluate(RUNTIME_JS) or {}
                if classify_runtime(cur.get("head") or "") == "connected":
                    break
                m = await session.evaluate(MODAL_JS) or {}
                if m.get("dialogs") and (m["dialogs"][-1].get("buttons")):
                    b = m["dialogs"][-1]["buttons"][0]
                    print(f"  mid-connect dialog: {b['text']!r}")
                    await session.click(x=b["x"], y=b["y"], settle=False)
                    await asyncio.sleep(1.0)
            else:
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

        # ---- execution ---------------------------------------------------
        print("\nphase 6: execution")
        deadline = time.perf_counter() + args.watch
        first_exec = None
        while time.perf_counter() < deadline:
            await asyncio.sleep(1.0)
            cells = await session.evaluate(CELLS_JS) or {}
            if cells.get("executed", 0) > 0 and first_exec is None:
                first_exec = time.perf_counter() - t_run
                clock.mark(f"first output ({first_exec:.1f}s after click)")
            if (cells.get("executed", 0) > 0
                    and cells.get("running", 0) == 0):
                break

        clock.mark("all cells idle")

        final = await session.evaluate(CELLS_JS) or {}
        print(f"\nresult: code={final.get('codeCells')} "
              f"executed={final.get('executed')}")
        for i, d in enumerate(final.get("detail") or []):
            flag = "RUN" if d.get("running") else (
                f"#{d['count']}" if isinstance(d.get("count"), int) else "---")
            print(f"  [{i}] {flag:<4} {(d.get('output') or '')[:60]!r}")

        # ---- timeline ----------------------------------------------------
        total = time.perf_counter() - clock.t0
        print("\n" + "=" * 64)
        print("TIMELINE")
        print("=" * 64)
        prev = 0.0
        for label, at in clock.marks:
            print(f"  {at:7.2f}s  (+{at - prev:5.2f}s)  {label}")
            prev = at
        print(f"  {total:7.2f}s  total")

        if first_exec is not None:
            print(f"\nclick -> first output: {first_exec:.2f}s")

        return 0 if final.get("executed", 0) > 0 else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
