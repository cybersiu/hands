"""
speed_matrix.py — Measure click latency across different page shapes.

One connection, several sites, each measured the same way:

    observe  ->  how long to list interactables
    click    ->  how long the CDP event round trip takes
    assert   ->  how long the page takes to show a change

The point is to separate our cost from the site's cost. A heavy SPA will always
feel slower than a static page, and that difference comes from the page, not
from the tool.

    python speed_matrix.py
    python speed_matrix.py --skip-heavy   # leave out Colab
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


#: (label, url, what to click, verification expression)
#:
#: The click target is expressed as a text fragment so the same code works on
#: every site; the verification is a JavaScript expression that must become
#: truthy after the click.
SITES: list[tuple[str, str, str | None, str | None]] = [
    (
        "example.com (static)",
        "https://example.com/",
        "Learn more",
        None,
    ),
    (
        "canto-gen (React SPA)",
        "https://canto-gen.com/playground",
        "\u751f\u6210\u8a9e\u97f3",          # 生成語音
        None,
    ),
    (
        "cantonese.ai (React SPA)",
        "https://cantonese.ai/zh-TC",
        "\u6587\u5b57\u8f49\u8a9e\u97f3",    # 文字轉語音
        None,
    ),
    (
        "colab (lit + shadow DOM)",
        "https://colab.research.google.com/drive/"
        "12hbaPkNheqTGFi_QA532JtRBdNaDWwFv",
        None,                                  # handled separately
        None,
    ),
]


class Stats:
    def __init__(self) -> None:
        self.observe: list[float] = []
        self.click: list[float] = []
        self.verify: list[float] = []
        self.counts: list[int] = []

    def line(self, label: str, samples: list[float], unit: str = "ms") -> str:
        if not samples:
            return f"  {label:<16} n/a"
        ms = [s * 1000 for s in samples]
        med = statistics.median(ms)
        p95 = sorted(ms)[max(0, int(len(ms) * 0.95) - 1)]
        return (f"  {label:<16} median {med:7.1f} {unit}   "
                f"p95 {p95:7.1f}   min {min(ms):6.1f}   max {max(ms):7.1f}")


async def measure_site(session: Session, label: str, url: str,
                       click_text: str | None,
                       verify_expr: str | None,
                       repeats: int = 3) -> Stats:
    st = Stats()
    print(f"\n{'=' * 66}")
    print(label)
    print(f"{'=' * 66}")

    t = time.perf_counter()
    await session.navigate(url)
    load = (time.perf_counter() - t) * 1000
    print(f"  load             {load:7.1f} ms")

    # Let the SPA settle before the first observe, so the measurement is of a
    # stable page rather than of a page still building itself.
    await session.assert_changed(
        "document.querySelectorAll('*').length", expect=None,
        timeout=10.0, interval=0.2,
    )
    await asyncio.sleep(0.5)

    for _ in range(repeats):
        t = time.perf_counter()
        obs = await session.observe(max_nodes=200)
        st.observe.append(time.perf_counter() - t)
        st.counts.append(obs.get("count", 0))

    print(st.line("observe", st.observe))
    print(f"  {'elements':<16} {st.counts}")

    if not click_text:
        return st

    # Find a target by text, then click it repeatedly and time each round trip.
    obs = await session.observe(max_nodes=200)
    target = next((e for e in (obs.get("elements") or [])
                   if click_text in (e.get("text") or "")), None)
    if target is None:
        print(f"  target {click_text!r} not found; skipping click timing")
        return st

    print(f"  {'target':<16} {target['ref']} {target['tag']} "
          f"{(target.get('text') or '')[:30]!r}")

    # settle=False isolates the dispatch cost from the page's own work.
    for _ in range(repeats):
        t = time.perf_counter()
        await session.click(ref=target["ref"], settle=False)
        st.click.append(time.perf_counter() - t)

    print(st.line("click (no wait)", st.click))

    if verify_expr:
        t = time.perf_counter()
        await session.assert_changed(verify_expr, timeout=5.0)
        st.verify.append(time.perf_counter() - t)
        print(st.line("verify", st.verify))

    return st


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-heavy", action="store_true")
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())

    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print("Could not connect to Chrome.")
        print(f"  {exc}")
        return 1

    try:
        version = await sock.send("Browser.getVersion")
        print(f"browser: {version.get('product')}")

        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        all_stats: list[tuple[str, Stats]] = []
        for label, url, click_text, verify in SITES:
            if args.skip_heavy and "colab" in label:
                continue
            try:
                st = await measure_site(session, label, url, click_text,
                                        verify, repeats=args.repeats)
                all_stats.append((label, st))
            except Exception as exc:
                print(f"  FAILED: {type(exc).__name__}: {str(exc)[:120]}")

        # ---- summary -----------------------------------------------------
        print(f"\n{'=' * 66}")
        print("SUMMARY")
        print(f"{'=' * 66}")
        print(f"  {'site':<26} {'observe':>10} {'click':>10} {'elements':>9}")
        for label, st in all_stats:
            obs = (statistics.median([s * 1000 for s in st.observe])
                   if st.observe else 0)
            clk = (statistics.median([s * 1000 for s in st.click])
                   if st.click else 0)
            n = max(st.counts) if st.counts else 0
            print(f"  {label[:26]:<26} {obs:>9.0f}ms {clk:>9.1f}ms {n:>9}")

        print("\nWhat this means:")
        print("  observe cost scales with DOM size — that is the page's doing.")
        print("  click cost is the CDP round trip and is nearly constant.")
        print("  the wait a user notices is the site's own reaction time.")

        return 0

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
