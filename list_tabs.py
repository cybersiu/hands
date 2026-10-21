"""list_tabs.py — show the open tabs without touching any of them.

Reads Target.getTargets over the browser socket and prints each page. No tab is
created, attached or navigated, so it is safe to run while the user is working.

    python list_tabs.py
"""

from __future__ import annotations

import asyncio
import io
import sys

import attach


async def main() -> int:
    sock = attach.BrowserSocket(attach.ws_endpoint())
    await sock.connect(timeout=30)
    try:
        pages = await attach.list_targets(sock)
        for i, p in enumerate(pages):
            print(f"{i:>2}  {(p.get('url') or '')[:90]}")
            print(f"    {p.get('title') or ''}")
        print(f"\n{len(pages)} page target(s)")
    finally:
        await sock.close()
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
