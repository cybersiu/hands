"""
upload_variants.py — Try uploading different file types to Colab.

Colab's notebook upload accepts `.ipynb` only. This script proves that by
attempting several kinds and reporting what the page does with each:

    .ipynb     should upload and open
    .txt       should be rejected by the accept filter
    .py        should be rejected
    no ext     should be rejected

The point is to characterise the boundary honestly rather than assume it. A
tool that silently "succeeds" at uploading a .txt would be worse than one that
reports the rejection.

    python upload_variants.py
    python upload_variants.py --keep-going   # continue after a failure
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach
from browser import Session
from zendriver import cdp


NOTEBOOK = {
    "cells": [
        {"cell_type": "markdown", "metadata": {},
         "source": ["# variant test\n", "\n",
                    "Uploaded to check file-type handling.\n"]},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [], "source": ["print('variant uploaded')\n"]},
    ],
    "metadata": {
        "colab": {"provenance": []},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}

#: What to try, in order. Each entry is (label, filename, content).
VARIANTS: list[tuple[str, str, str]] = [
    ("notebook", "hands-variant-test.ipynb",
     json.dumps(NOTEBOOK, ensure_ascii=False, indent=1)),
    ("plain text", "hands-test.txt",
     "This is a plain text file.\nColab should reject it.\n"),
    ("python script", "hands-test.py",
     "print('hello from a .py file')\n"),
    ("no extension", "hands-noext",
     "no extension here\n"),
]


#: Every file input on the page, with its accept filter.
INPUTS_JS = r"""
(() => {
  const out = [];
  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('input'); } catch (e) { return; }
    for (const el of nodes) {
      if (el.type === 'file') {
        out.push({
          accept: el.getAttribute('accept'),
          multiple: !!el.multiple,
          value: el.value,
          files: el.files ? el.files.length : 0,
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  }
  walk(document, 0);
  return out;
})()
"""


async def attach_colab(session: Session, sock) -> dict:
    pages = await attach.list_targets(sock)
    idx = next((i for i, p in enumerate(pages)
                if "colab.research.google.com" in (p.get("url") or "")), None)
    if idx is None:
        raise SystemExit("No Colab tab open.")
    return await attach.attach(session, tab_index=idx)


async def find_file_input(session: Session) -> dict:
    """Locate the first <input type=file> in the pierced tree."""
    tab = session._tab
    doc = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
    hit: dict = {"node": None}

    def walk(node, depth=0):
        if depth > 40 or hit["node"] is not None:
            return
        tag = (getattr(node, "local_name", "") or "").lower()
        attrs = getattr(node, "attributes", None) or []
        amap = {}
        for i in range(0, len(attrs) - 1, 2):
            amap[attrs[i]] = attrs[i + 1]
        if tag == "input" and amap.get("type") == "file":
            hit["node"] = node.node_id
            hit["accept"] = amap.get("accept")
            return
        for c in (getattr(node, "children", None) or []):
            walk(c, depth + 1)
        for r in (getattr(node, "shadow_roots", None) or []):
            walk(r, depth + 1)
        content = getattr(node, "content_document", None)
        if content is not None:
            walk(content, depth + 1)

    walk(doc)
    return hit


async def try_upload(session: Session, path: Path) -> dict:
    """Hand a file to the file input and report whether it was accepted."""
    before_url = await session.evaluate("location.href")
    hit = await find_file_input(session)
    if hit["node"] is None:
        return {"ok": False, "reason": "no file input on the page"}

    try:
        await session._tab.send(cdp.dom.set_file_input_files(
            files=[str(path)], node_id=hit["node"]))
    except Exception as exc:
        return {"ok": False, "reason": f"CDP rejected: {str(exc)[:100]}"}

    # Give the page a moment to react, then look for a URL change or an error.
    deadline = time.perf_counter() + 8.0
    while time.perf_counter() < deadline:
        await asyncio.sleep(0.5)
        now = await session.evaluate("location.href")
        if now != before_url:
            return {"ok": True, "url": now, "elapsed": round(
                8.0 - (deadline - time.perf_counter()), 1)}

    # No navigation: check whether an error surfaced.
    err = await session.evaluate(r"""(() => {
      const t = (document.body.innerText || '');
      const m = t.match(/(not a notebook|invalid|error|failed|無法|錯誤|不支援)/i);
      return m ? m[0] : null;
    })()""")
    return {"ok": False, "reason": err or "no navigation and no visible error",
            "accept": hit.get("accept")}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-going", action="store_true")
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())

    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 1

    try:
        info = await attach_colab(session, sock)
        print(f"tab: {info.get('active_url')}\n")

        # Reset to the upload landing page so each attempt starts clean.
        await session.navigate("https://colab.research.google.com/")
        await asyncio.sleep(2.5)

        results = []
        for label, name, content in VARIANTS:
            path = Path(tempfile.gettempdir()) / name
            path.write_text(content, encoding="utf-8")
            print(f"{label:<16} {name:<26} {len(content):>6} bytes")

            # The upload control has to be opened before the input exists.
            hit = await find_file_input(session)
            if hit["node"] is None:
                # Click the upload button to create the input.
                clicked = await session.evaluate(r"""
                (() => {
                  const out = [];
                  function walk(root, d) {
                    if (!root || d > 40) return;
                    let ns; try { ns = root.querySelectorAll('*'); } catch (e) { return; }
                    for (const el of ns) {
                      const t = (el.innerText || '').trim();
                      if (t.includes('\u4e0a\u50b3') || t.includes('upload')) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 40 && r.height > 15)
                          out.push({x: Math.round(r.left + r.width/2),
                                    y: Math.round(r.top + r.height/2)});
                      }
                      if (el.shadowRoot) walk(el.shadowRoot, d + 1);
                    }
                  }
                  walk(document, 0);
                  return out[0] || null;
                })()""")
                if clicked:
                    await session.click(x=clicked["x"], y=clicked["y"],
                                        settle=False)
                    await asyncio.sleep(1.5)

            res = await try_upload(session, path)
            print(f"  -> {json.dumps(res, ensure_ascii=False)}")
            results.append((label, name, res))

            if not res.get("ok") and not args.keep_going:
                print("  (stopping; pass --keep-going to try the rest)")
                break

            # Return to the landing page for the next attempt.
            if res.get("ok"):
                await session.navigate("https://colab.research.google.com/")
                await asyncio.sleep(2.5)

        # ---- summary -----------------------------------------------------
        print(f"\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        for label, name, res in results:
            status = "ACCEPTED" if res.get("ok") else "rejected"
            print(f"  {label:<16} {name:<26} {status}")
            if not res.get("ok"):
                print(f"    {res.get('reason', '')[:70]}")

        print("\nColab's notebook upload accepts .ipynb only. Anything else is")
        print("refused by the input's accept filter or by the page after the")
        print("file is handed over.")
        return 0

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
