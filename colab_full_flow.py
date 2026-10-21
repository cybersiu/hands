"""
colab_full_flow.py — Upload, connect the runtime, and run every cell.

One connection for the whole run. Chrome's approval-mode debugging tolerates
only a handful of connect/disconnect cycles before it starts refusing
handshakes, so everything happens here.

The flow mirrors what a person does:

    1. open the notebook
    2. dismiss any blocking dialog
    3. click Connect and wait for the runtime to report connected
    4. click Run all
    5. poll cell execution counts until they stop changing
    6. report what ran and what came out

Every step asserts on observable page state. A step that reports success but
changes nothing is treated as a failure, because that is exactly the failure
mode that made earlier attempts misleading.

    python colab_full_flow.py                    # the uploaded test notebook
    python colab_full_flow.py --url <notebook>   # a specific notebook
    python colab_full_flow.py --connect-only     # stop after connecting
    python colab_full_flow.py --no-run           # stop before Run all
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

import attach
from browser import Session


# --------------------------------------------------------------------------
# Page probes
# --------------------------------------------------------------------------

#: Runtime state, read from the connect control's tooltip. Colab puts the
#: state text there because the visible button only shows two sparklines.
RUNTIME_JS = r"""
(() => {
  let connect = null;

  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      if (connect) return;
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      if (tag === 'colab-toolbar-button' && el.id === 'connect') {
        connect = el;
        return;
      }
      if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    }
  }
  walk(document, 0);

  // Fall back to the rightmost toolbar button.
  if (!connect) {
    const all = [];
    (function scan(root, d) {
      if (!root || d > 40) return;
      let ns;
      try { ns = root.querySelectorAll('colab-toolbar-button'); } catch (e) { return; }
      for (const el of ns) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.top < 140) all.push({ el, x: r.left });
      }
      for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) scan(el.shadowRoot, d + 1);
      }
    })(document, 0);
    all.sort((a, b) => a.x - b.x);
    if (all.length) connect = all[all.length - 1].el;
  }

  if (!connect) return { found: false };

  const tip = connect.getAttribute('tooltiptext') || '';
  const r = connect.getBoundingClientRect();
  return {
    found: true,
    tooltip: tip,
    head: tip.split('\n')[0].trim(),
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
  };
})()
"""


#: Cell execution state. The count lives in the run button's shadow root.
CELLS_JS = r"""
(() => {
  const cells = Array.from(document.querySelectorAll('div.cell'))
    .filter(c => (c.className || '').toString().includes('code'));

  const detail = [];
  let running = 0, executed = 0, never = 0;

  for (const c of cells) {
    const btn = c.querySelector('colab-run-button');
    let count = null, stale = null, isRunning = false;

    if (btn && btn.shadowRoot) {
      const sr = btn.shadowRoot;
      const exec = sr.querySelector('.cell-execution');
      if (exec) {
        const ec = exec.className || '';
        stale = ec.includes('stale');
        isRunning = ec.includes('running') || ec.includes('busy');
      }
      const ce = sr.querySelector('.execution-count');
      if (ce) {
        const t = (ce.textContent || '').trim();
        const m = t.match(/\[(\d+)\]/);
        if (m) count = parseInt(m[1], 10);
      }
    }

    if (isRunning) running++;
    if (typeof count === 'number') executed++;
    else if (stale) never++;

    const outEl = c.querySelector('.output, colab-output');
    detail.push({
      count, stale, running: isRunning,
      output: outEl
        ? (outEl.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120)
        : '',
    });
  }

  return { codeCells: cells.length, running, executed, never, detail };
})()
"""


#: Modal that is intercepting the page, if any.
MODAL_JS = r"""
(() => {
  const dialogs = [];
  const seen = new Set();

  function buttonsIn(scope) {
    const found = [];
    const push = (b) => {
      const r = b.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return;
      found.push({
        text: (b.innerText || b.textContent || '')
          .replace(/\s+/g, ' ').trim().slice(0, 30),
        slot: b.getAttribute ? b.getAttribute('slot') : null,
        x: r.left + r.width / 2,
        y: r.top + r.height / 2,
      });
    };
    try {
      scope.querySelectorAll(
        'mwc-button, md-button, md-text-button, md-filled-button, button'
      ).forEach(push);
    } catch (e) {}
    if (scope.shadowRoot) {
      try {
        scope.shadowRoot.querySelectorAll(
          'mwc-button, md-button, md-text-button, md-filled-button, button'
        ).forEach(push);
      } catch (e) {}
    }
    return found;
  }

  function hostOf(el) {
    let cur = el, outer = el;
    while (cur) {
      if (cur.tagName && cur.tagName.includes('-')) outer = cur;
      cur = cur.parentElement ||
            (cur.getRootNode && cur.getRootNode().host) || null;
    }
    return outer;
  }

  function visit(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      const role = el.getAttribute ? el.getAttribute('role') : null;
      const cls = (el.className || '').toString();
      const looksDialog =
        tag === 'mwc-dialog' || tag === 'md-dialog' || tag === 'dialog' ||
        role === 'dialog' || role === 'alertdialog' ||
        cls.includes('mdc-dialog') || cls.includes('modal-dialog');
      if (looksDialog) {
        const host = hostOf(el);
        if (!seen.has(host)) {
          seen.add(host);
          const r = host.getBoundingClientRect();
          const text = (host.innerText || '')
            .replace(/\s+/g, ' ').trim().slice(0, 200);
          const btns = buttonsIn(host);
          if (text || btns.length) {
            dialogs.push({
              tag: host.tagName.toLowerCase(),
              text,
              buttons: btns,
              w: Math.round(r.width),
              h: Math.round(r.height),
            });
          }
        }
      }
      if (el.shadowRoot) visit(el.shadowRoot, depth + 1);
      if (tag === 'iframe' || tag === 'frame') {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc) visit(doc, depth + 1);
      }
    }
  }
  visit(document, 0);
  return { dialogs };
})()
"""


#: Locate the Run-all toolbar button by id, which is stable across locales.
RUNALL_JS = r"""
(() => {
  let found = null;
  (function walk(root, d) {
    if (!root || d > 40 || found) return;
    let ns;
    try { ns = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of ns) {
      if (el.id === 'toolbar-run-button') {
        const r = el.getBoundingClientRect();
        found = {
          x: r.left + r.width / 2,
          y: r.top + r.height / 2,
          text: (el.innerText || '').trim().slice(0, 30),
        };
        return;
      }
      if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    }
  })(document, 0);
  return found;
})()
"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def classify_runtime(head: str) -> str:
    low = (head or "").lower()
    if any(k in low for k in ("\u5df2\u9023\u7dda", "\u5df2\u8fde\u63a5",
                              "connected")):
        return "connected"
    if any(k in low for k in ("\u9023\u7dda\u4e2d", "\u8fde\u63a5\u4e2d",
                              "connecting")):
        return "connecting"
    if any(k in low for k in ("\u672a\u9023\u7dda", "\u672a\u8fde\u63a5",
                              "disconnected")):
        return "disconnected"
    return "unknown"


async def clear_modal(session: Session) -> dict | None:
    """Dismiss a blocking dialog. Returns what was closed, or None."""
    data = await session.evaluate(MODAL_JS) or {}
    dialogs = data.get("dialogs") or []
    if not dialogs:
        return None

    dlg = dialogs[-1]
    print(f"  modal: {dlg['text'][:80]!r}")
    if not dlg.get("buttons"):
        print("  (no buttons found in the dialog)")
        return None

    # Prefer a dismissal action over a primary one.
    for pref in ("\u95dc\u9589", "\u5173\u95ed", "Close",
                 "\u77e5\u9053\u4e86", "Got it", "OK",
                 "\u7a0d\u5f8c\u518d\u8aaa", "\u7a0d\u540e\u518d\u8bf4", "Later"):
        btn = next((b for b in dlg["buttons"]
                    if pref.lower() in (b["text"] or "").lower()), None)
        if btn:
            print(f"  clicking {btn['text']!r}")
            await session.click(x=btn["x"], y=btn["y"], settle=False)
            await asyncio.sleep(1.0)
            return {"closed": btn["text"], "dialog": dlg["text"][:80]}

    btn = dlg["buttons"][0]
    print(f"  clicking {btn['text']!r} (no dismissal label matched)")
    await session.click(x=btn["x"], y=btn["y"], settle=False)
    await asyncio.sleep(1.0)
    return {"closed": btn["text"], "dialog": dlg["text"][:80]}


async def wait_runtime(session: Session, target: str,
                       timeout: float = 90.0) -> dict:
    """Poll the runtime indicator until it matches ``target``."""
    start = time.perf_counter()
    last = None
    while time.perf_counter() - start < timeout:
        rt = await session.evaluate(RUNTIME_JS) or {}
        state = classify_runtime(rt.get("head") or "")
        if state != last:
            print(f"  [{time.strftime('%H:%M:%S')}] runtime: {state}"
                  f"  ({(rt.get('head') or '')[:40]!r})")
            last = state
        if state == target:
            return {"state": state, "elapsed": round(
                time.perf_counter() - start, 1), "ok": True}
        await asyncio.sleep(3.0)

    return {"state": last, "elapsed": round(
        time.perf_counter() - start, 1), "ok": False}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None,
                    help="Notebook URL. Defaults to the current Colab tab.")
    ap.add_argument("--connect-only", action="store_true")
    ap.add_argument("--no-run", action="store_true")
    ap.add_argument("--watch", type=float, default=180.0,
                    help="Seconds to watch cells after Run all")
    args = ap.parse_args()

    started = time.perf_counter()
    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())

    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print("Could not connect to Chrome.")
        print(f"  {exc}")
        print("\nApprove remote debugging at chrome://inspect/#remote-debugging")
        print("and run this script once.")
        return 1

    try:
        version = await sock.send("Browser.getVersion")
        print(f"browser: {version.get('product')}")

        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), None)
        if idx is None:
            print("No Colab tab open.")
            return 1

        info = await attach.attach(session, tab_index=idx)
        print(f"tab    : {info.get('active_title')!r}")
        print(f"url    : {info.get('active_url')}\n")

        if args.url:
            print(f"navigating to {args.url}")
            await session.navigate(args.url)
            await asyncio.sleep(3.0)

        # ---- 1. clear any blocking dialog --------------------------------
        print("step 1: clearing blockers")
        closed = await clear_modal(session)
        if closed:
            print(f"  closed: {closed['closed']!r}")
        else:
            print("  no dialog present")

        # ---- 2. runtime state -------------------------------------------
        print("\nstep 2: runtime")
        rt = await session.evaluate(RUNTIME_JS) or {}
        state = classify_runtime(rt.get("head") or "")
        print(f"  state: {state}  ({(rt.get('head') or '')[:50]!r})")

        if state != "connected":
            if not rt.get("found"):
                print("  connect control not found")
                return 1
            print(f"  clicking connect at ({rt['x']:.0f},{rt['y']:.0f})")
            res = await session.click(x=rt["x"], y=rt["y"], settle=False)
            print(f"  url_changed={res.get('url_changed')} "
                  f"blocked={(res.get('hit') or {}).get('blocked')}")
            await asyncio.sleep(2.0)

            # Connecting can raise a consent dialog; clear it if so.
            await clear_modal(session)

            waited = await wait_runtime(session, "connected", timeout=120.0)
            if not waited["ok"]:
                print(f"\n  runtime did not connect after "
                      f"{waited['elapsed']}s (state={waited['state']})")
                print("  Colab may be out of GPU quota, or the session needs")
                print("  a manual click. Inspect the page before retrying.")
                return 1

        if args.connect_only:
            print("\nconnected; stopping as requested")
            return 0

        # ---- 3. run all --------------------------------------------------
        print("\nstep 3: run all")
        before = await session.evaluate(CELLS_JS) or {}
        print(f"  before: code={before.get('codeCells')} "
              f"executed={before.get('executed')} "
              f"never={before.get('never')}")

        ra = await session.evaluate(RUNALL_JS)
        if not ra:
            print("  run-all button not found")
            return 1

        print(f"  clicking run-all at ({ra['x']:.0f},{ra['y']:.0f})")
        res = await session.click(x=ra["x"], y=ra["y"], settle=False)
        print(f"  url_changed={res.get('url_changed')} "
              f"blocked={(res.get('hit') or {}).get('blocked')}")

        if args.no_run:
            print("\nstopping before execution as requested")
            return 0

        # ---- 4. watch cells ---------------------------------------------
        print(f"\nstep 4: watching cells for {args.watch:.0f}s")
        deadline = time.perf_counter() + args.watch
        last_key = None
        started_running = False

        while time.perf_counter() < deadline:
            await asyncio.sleep(4.0)
            cells = await session.evaluate(CELLS_JS) or {}
            key = (f"run={cells.get('running')} "
                   f"exec={cells.get('executed')} "
                   f"never={cells.get('never')}")
            if key != last_key:
                print(f"  [{time.strftime('%H:%M:%S')}] {key}")
                last_key = key
            if cells.get("running", 0) > 0:
                started_running = True
            if started_running and cells.get("running", 0) == 0:
                print("  all cells finished")
                break

        # ---- 5. report ---------------------------------------------------
        final = await session.evaluate(CELLS_JS) or {}
        print(f"\nfinal: code={final.get('codeCells')} "
              f"executed={final.get('executed')} "
              f"never={final.get('never')}")

        for i, d in enumerate(final.get("detail") or []):
            if d.get("running"):
                flag = "RUN"
            elif isinstance(d.get("count"), int):
                flag = f"#{d['count']}"
            elif d.get("stale"):
                flag = "---"
            else:
                flag = "???"
            out = (d.get("output") or "").replace("\n", " ")[:60]
            print(f"  [{i}] {flag:<4} {out!r}")

        ran = final.get("executed", 0) > 0
        print(f"\n{'SUCCESS' if ran else 'NO CELLS EXECUTED'}")
        if not ran:
            print("The click was delivered but nothing ran. Likely causes:")
            print("  - the runtime refused (quota, or a permission prompt)")
            print("  - a dialog is blocking (run with --no-run to inspect)")
        print(f"\nelapsed: {(time.perf_counter() - started):.1f}s")
        return 0 if ran else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
