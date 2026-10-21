"""
colab_upload_once.py — Do the whole upload in one connection.

Every previous attempt failed for the same reason: Chrome's approval-mode
debugging tolerates a handful of connect/disconnect cycles and then starts
refusing handshakes. Each script that opened its own connection burned one.

This script opens one connection and performs the entire flow:

    1. attach to the Colab tab
    2. find the upload control
    3. click it
    4. hand the file to the resulting <input type=file> via CDP
    5. wait for the new notebook to appear
    6. report what happened

Run it once, after approving remote debugging in Chrome. If it fails, it prints
the page state instead of retrying blindly.

    python colab_upload_once.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace", line_buffering=True)

import attach
from browser import Session
from zendriver import cdp


# --------------------------------------------------------------------------
# Notebook content
# --------------------------------------------------------------------------

NOTEBOOK = {
    "cells": [
        {"cell_type": "markdown", "metadata": {},
         "source": [
             "# hands upload test\n",
             "\n",
             "Created by the **hands** browser automation server.\n",
             "\n",
             "This proves a file can be handed to Colab through the UI without\n",
             "touching the native file picker.\n",
         ]},
        {"cell_type": "code", "execution_count": None, "metadata": {},
         "outputs": [],
         "source": ["print('uploaded by hands')\n", "1 + 1"]},
    ],
    "metadata": {
        "colab": {"provenance": []},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4,
    "nbformat_minor": 0,
}


def write_notebook() -> Path:
    p = Path(tempfile.gettempdir()) / "hands-upload-test.ipynb"
    p.write_text(json.dumps(NOTEBOOK, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# Page probes
# --------------------------------------------------------------------------

#: Everything clickable or labelled, across shadow roots and frames.
ALL_JS = r"""
(() => {
  const out = [];
  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      const role = el.getAttribute ? el.getAttribute('role') : null;
      const r = el.getBoundingClientRect();
      const interactive =
        tag === 'button' || tag === 'a' || tag === 'input' ||
        role === 'button' || role === 'tab' || role === 'link' ||
        (el.hasAttribute && el.hasAttribute('onclick'));
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if ((interactive || text) && r.width > 20 && r.height > 12) {
        out.push({
          tag, role: role || null,
          text: text.slice(0, 60),
          aria: (el.getAttribute ? (el.getAttribute('aria-label') || '') : '')
            .slice(0, 60),
          x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2),
          w: Math.round(r.width), h: Math.round(r.height),
          depth,
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      if (tag === 'iframe' || tag === 'frame') {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc) walk(doc, depth + 1);
      }
    }
  }
  walk(document, 0);
  return out;
})()
"""

#: File inputs anywhere in the page.
FILE_INPUTS_JS = r"""
(() => {
  const out = [];
  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('input'); } catch (e) { return; }
    for (const el of nodes) {
      if (el.type === 'file') {
        const r = el.getBoundingClientRect();
        out.push({
          accept: el.getAttribute('accept'),
          multiple: !!el.multiple,
          w: Math.round(r.width), h: Math.round(r.height),
        });
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  }
  walk(document, 0);
  return out;
})()
"""

#: Anything mentioning upload, in either language.
UPLOAD_MENTIONS_JS = r"""
(() => {
  const NEEDLES = ['Upload', 'upload', '\u4e0a\u50b3', '\u4e0a\u4f20',
                   'Upload notebook'];
  const out = [];
  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      const aria = el.getAttribute ? (el.getAttribute('aria-label') || '') : '';
      const hay = text + ' ' + aria;
      if (hay && hay.length < 80 && NEEDLES.some(n => hay.includes(n))) {
        const r = el.getBoundingClientRect();
        if (r.width > 20 && r.height > 10) {
          out.push({
            tag: el.tagName.toLowerCase(),
            text: text.slice(0, 50),
            aria: aria.slice(0, 40),
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            w: Math.round(r.width), h: Math.round(r.height),
            depth,
          });
        }
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc) walk(doc, depth + 1);
      }
    }
  }
  walk(document, 0);
  return out;
})()
"""


# --------------------------------------------------------------------------
# CDP file attachment
# --------------------------------------------------------------------------

async def attach_file(session: Session, path: Path) -> dict:
    """Give a file to the first <input type=file> on the page.

    ``DOM.setFileInputFiles`` is the only way to satisfy a file input without a
    native picker. The input is usually hidden, which does not matter: the DOM
    node exists and the browser accepts a path for it.
    """
    tab = session._tab
    doc = await tab.send(cdp.dom.get_document(depth=-1, pierce=True))
    hit: dict = {"node": None, "depth": 0}

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
            hit["depth"] = depth
            return
        for c in (getattr(node, "children", None) or []):
            walk(c, depth + 1)
        for r in (getattr(node, "shadow_roots", None) or []):
            walk(r, depth + 1)
        content = getattr(node, "content_document", None)
        if content is not None:
            walk(content, depth + 1)

    walk(doc)
    if hit["node"] is None:
        return {"found": False}

    await tab.send(cdp.dom.set_file_input_files(
        files=[str(path)], node_id=hit["node"]))
    return {"found": True, "node_id": hit["node"], "depth": hit["depth"]}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main() -> int:
    started = time.perf_counter()

    # One connection for the whole run.
    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print("Could not connect. Chrome's approval may have lapsed.")
        print(f"  {exc}")
        print("\nOpen chrome://inspect/#remote-debugging and approve again,")
        print("then run this script once.")
        return 1

    try:
        version = await sock.send("Browser.getVersion")
        print(f"browser: {version.get('product')}")

        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), None)
        if idx is None:
            print("No Colab tab. Open colab.research.google.com first.")
            return 1

        info = await attach.attach(session, tab_index=idx)
        print(f"tab    : {info.get('active_title')!r}")
        print(f"url    : {info.get('active_url')}\n")

        await asyncio.sleep(1.0)

        # 1. Is there already a file input? Some builds keep one hidden.
        inputs = await session.evaluate(FILE_INPUTS_JS)
        print(f"file inputs before: {inputs}")

        nb = write_notebook()
        print(f"notebook: {nb} ({nb.stat().st_size} bytes)\n")

        if not inputs:
            # 2. Find the upload control.
            mentions = await session.evaluate(UPLOAD_MENTIONS_JS)
            mentions = mentions if isinstance(mentions, list) else []
            print(f"{len(mentions)} element(s) mention upload:")
            for m in mentions[:10]:
                print(f"  {m['tag']:<10} ({m['x']:>5},{m['y']:>4}) "
                      f"w={m['w']:>4} d={m['depth']} "
                      f"text={m['text'][:32]!r} aria={m['aria'][:20]!r}")

            if mentions:
                target = mentions[0]
                print(f"\nclicking at ({target['x']},{target['y']})")
                res = await session.click(x=target["x"], y=target["y"])
                print(f"  url_changed={res.get('url_changed')} "
                      f"blocked={(res.get('hit') or {}).get('blocked')}")
                await asyncio.sleep(2.0)
                inputs = await session.evaluate(FILE_INPUTS_JS)
                print(f"file inputs after click: {inputs}")

        # 3. Attach the file.
        result = await attach_file(session, nb)
        print(f"\nfile attached: {result}")

        if result.get("found"):
            # 4. Wait for navigation to the new notebook.
            changed = await session.assert_changed(
                "location.href.includes('/notebook/') || "
                "location.href.includes('/drive/')",
                timeout=12.0,
            )
            print(f"navigation: {changed}")
            final = await session.evaluate(
                "({url: location.href, title: document.title})")
            print(f"\nfinal: {json.dumps(final, ensure_ascii=False)}")
        else:
            print("\nNo file input found, so nothing was uploaded.")
            state = await session.evaluate(
                "({url: location.href, title: document.title, "
                "body: (document.body.innerText || '').slice(0, 200)})")
            print(json.dumps(state, ensure_ascii=False, indent=2))

        print(f"\nelapsed: {(time.perf_counter() - started):.1f}s")
        return 0

    finally:
        # Close once, at the end: repeated connects are what break the session.
        await sock.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
