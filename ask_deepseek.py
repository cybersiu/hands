"""ask_deepseek.py — send one message to chat.deepseek.com and read the reply.

This is the script form of what the MCP tools do: attach to the running Chrome,
open a fresh chat, type, submit, then wait until the answer stops growing.
It exists because the MCP server is currently stopped, and because a file is a
much safer way to carry a few thousand characters of Chinese than a shell
argument.

    python ask_deepseek.py message.txt answer.txt
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach
from browser import Session

READ_JS = """(() => {
  const els = document.querySelectorAll('.ds-markdown');
  const text = els.length ? els[els.length - 1].innerText : '';
  const stop = Array.from(document.querySelectorAll('button, [role=button]'))
    .some(b => /停止|Stop/.test((b.getAttribute('aria-label') || '') +
                                (b.innerText || '')));
  return { blocks: els.length, len: text.length, stop, text };
})()"""


async def wait_answer(session: Session, max_rounds: int = 12) -> dict:
    """Poll in 20s slices so no single CDP call hits the 30s transport timeout."""
    prev = -1
    stable = 0
    last: dict = {}
    for _ in range(max_rounds):
        state = await session.evaluate(
            "new Promise(r => setTimeout(() => r(%s), 20000))" % READ_JS,
            await_promise=True,
        )
        if not isinstance(state, dict):
            continue
        last = state
        if state["len"] > 150 and state["len"] == prev and not state["stop"]:
            stable += 1
            if stable >= 1:
                return state
        else:
            stable = 0
        prev = state["len"]
    return last


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python ask_deepseek.py MESSAGE.txt [ANSWER.txt]")
        return 1
    message = Path(sys.argv[1]).read_text(encoding="utf-8")
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=8)
    except Exception as exc:
        print(f"cannot attach: {exc}")
        return 2

    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "deepseek.com" in (p.get("url") or "")), None)
        if idx is None:
            idx = next((i for i, p in enumerate(pages)
                        if not (p.get("url") or "").startswith("devtools://")), 0)
        await attach.attach(session, tab_index=idx)

        await session.navigate("https://chat.deepseek.com/", timeout=30)
        await session.assert_changed(
            "!!document.querySelector('textarea') && "
            "document.querySelector('textarea').offsetWidth > 100",
            timeout=20,
        )

        obs = await session.observe(max_nodes=250)
        field = next((e for e in obs["elements"] if e["tag"] == "textarea"), None)
        if field is None:
            print("no composer found")
            return 3

        typed = await session.type_text(message, ref=field["ref"], clear=True)
        print(f"typed {typed['typed']} chars")
        await session.press("Enter", settle=False)
        await session.assert_changed(
            "(() => { const t = document.querySelector('textarea');"
            " return !!t && t.value === ''; })()",
            timeout=8,
        )

        answer = await wait_answer(session)
        text = answer.get("text", "")
        print(f"answer: {len(text)} chars")

        if out_path is not None:
            out_path.write_text(text, encoding="utf-8")
            print(f"written: {out_path}")
        else:
            print(text)
        return 0 if text else 4
    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
