"""
fingerprint.py — Measure a live page's structure so the fixture can imitate it.

Cloning a page is not possible: shadow roots are built at runtime and never
appear in serialised HTML, and a cloned page has no backend to answer its
requests. What *is* possible — and more useful — is to record the structural
facts that make a page hard to automate, then reproduce those facts in the
fixture.

Run against a live tab, then paste the numbers into fixture_site.py.

    python fingerprint.py                       # current tab
    python fingerprint.py --url https://...     # navigate first
    python fingerprint.py --json out.json

What it measures
----------------
* depth and count of shadow roots, and which custom elements own them
* how many dialogs exist, what tag they use, and where their buttons live
  (light DOM vs shadow root) — the detail that decides whether dismiss_blocker
  can find a way out
* how deep iframes nest, and whether each level is same-origin
* whether clickable things are semantic (button/a) or decorative (div/span)
* how much of the page is reachable without piercing

That last number is the one that matters: it is the fraction of the interface a
naive `querySelectorAll` would miss.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

import attach
from browser import Session


#: Walks the whole pierced tree and reports structural facts.
ANALYSE_JS = r"""
(() => {
  const facts = {
    url: location.href,
    title: document.title,
    customElements: {},
    shadowRoots: 0,
    maxShadowDepth: 0,
    iframes: 0,
    maxFrameDepth: 0,
    crossOriginFrames: 0,
    dialogs: [],
    interactive: { semantic: 0, decorative: 0 },
    lightDomInteractive: 0,
    totalElements: 0,
    slottedButtons: 0,
  };

  const SEMANTIC = new Set(['a', 'button', 'input', 'select', 'textarea', 'option']);
  const DIALOG_TAGS = ['mwc-dialog', 'md-dialog', 'dialog'];

  function isDialog(el) {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute ? el.getAttribute('role') : null;
    return DIALOG_TAGS.includes(tag) || tag.includes('-dialog') ||
           role === 'dialog' || role === 'alertdialog';
  }

  function isInteractive(el) {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    const role = el.getAttribute ? el.getAttribute('role') : null;
    return SEMANTIC.has(tag) ||
           (role && ['button', 'link', 'checkbox', 'radio', 'tab',
                     'menuitem', 'textbox', 'combobox'].includes(role)) ||
           (el.hasAttribute && (
             el.hasAttribute('onclick') ||
             el.hasAttribute('tabindex') ||
             el.getAttribute('contenteditable') === 'true'
           ));
  }

  function buttonsIn(scope) {
    const out = [];
    const seen = new Set();
    const add = (b, where) => {
      if (seen.has(b)) return;
      seen.add(b);
      const r = b.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) return;
      out.push({
        text: (b.innerText || b.textContent || '')
          .replace(/\s+/g, ' ').trim().slice(0, 30),
        where,
        slot: b.getAttribute ? b.getAttribute('slot') : null,
      });
    };
    try {
      scope.querySelectorAll('button, mwc-button, md-button, md-text-button, [role="button"]')
        .forEach(b => add(b, 'light'));
    } catch (e) {}
    if (scope.shadowRoot) {
      try {
        scope.shadowRoot.querySelectorAll('button, mwc-button, md-button, md-text-button')
          .forEach(b => add(b, 'shadow'));
      } catch (e) {}
    }
    return out;
  }

  function visit(root, depth, frameDepth, inShadow, seen) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }

    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      facts.totalElements++;

      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      const isCustom = tag.includes('-');
      if (isCustom) {
        facts.customElements[tag] = (facts.customElements[tag] || 0) + 1;
      }

      if (isInteractive(el)) {
        if (SEMANTIC.has(tag)) {
          facts.interactive.semantic++;
          if (!inShadow) facts.lightDomInteractive++;
        } else {
          facts.interactive.decorative++;
        }
      }

      if (isDialog(el)) {
        facts.dialogs.push({
          tag,
          inShadow,
          shadowDepth: depth,
          buttons: buttonsIn(el),
          hasScrim: !!document.querySelector('.mdc-dialog__scrim, .scrim'),
          text: (el.innerText || '')
            .replace(/\s+/g, ' ').trim().slice(0, 100),
        });
      }

      if (el.shadowRoot) {
        facts.shadowRoots++;
        facts.maxShadowDepth = Math.max(facts.maxShadowDepth, depth + 1);
        visit(el.shadowRoot, depth + 1, frameDepth, true, seen);
      }

      if (tag === 'iframe' || tag === 'frame') {
        facts.iframes++;
        facts.maxFrameDepth = Math.max(facts.maxFrameDepth, frameDepth + 1);
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc) {
          visit(doc, depth, frameDepth + 1, inShadow, seen);
        } else {
          facts.crossOriginFrames++;
        }
      }
    }
  }

  visit(document, 0, 0, false, new Set());

  // How much a naive querySelectorAll would see.
  let naive = 0;
  try {
    naive = document.querySelectorAll(
      'a, button, input, select, textarea, [role="button"], [onclick]'
    ).length;
  } catch (e) {}

  facts.naiveSelectorCount = naive;
  facts.missedByNaiveSelector =
    Math.max(0, (facts.interactive.semantic + facts.interactive.decorative) - naive);

  return facts;
})()
"""


async def _close(session: Session) -> None:
    """Release the browser regardless of which mode opened it."""
    browser = getattr(session, "_browser", None)
    if browser is None:
        return
    sock = getattr(browser, "_socket", None)
    if sock is not None:
        await sock.close()
    else:
        await session.stop()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=None)
    ap.add_argument("--tab", type=int, default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--own-browser", action="store_true",
                    help="Launch a separate Chrome instead of attaching. Use "
                         "this when the approval flow is unavailable.")
    args = ap.parse_args()

    session = Session()

    if args.own_browser:
        await session.start(headless=False, url="about:blank")
        if args.url:
            await session.navigate(args.url)
            await asyncio.sleep(1.5)
        info = {"active_title": await session.title()}
    elif args.tab is not None:
        info = await attach.attach(session, tab_index=args.tab)
    else:
        sock = attach.BrowserSocket(attach.ws_endpoint())
        await sock.connect()
        pages = await attach.list_targets(sock)
        await sock.close()
        if not pages:
            print("no tabs open")
            return 1
        info = await attach.attach(session, tab_index=0)

    print(f"tab: {info.get('active_title')!r}\n")

    if args.url and not args.own_browser:
        await session.navigate(args.url)
        await asyncio.sleep(2.0)

    # Wait for the page to finish building its shadow DOM: the fixture and any
    # real SPA both attach roots after load.
    await session.assert_changed(
        "document.readyState === 'complete'", timeout=8.0)

    facts = await session.evaluate(ANALYSE_JS)
    if not isinstance(facts, dict):
        print("unexpected result:", str(facts)[:300])
        await _close(session)
        return 1

    # ---- report ----
    print(f"url   : {facts.get('url')}")
    print(f"title : {facts.get('title')}")
    print()
    print(f"elements            : {facts.get('totalElements')}")
    print(f"shadow roots        : {facts.get('shadowRoots')} "
          f"(max depth {facts.get('maxShadowDepth')})")
    print(f"iframes             : {facts.get('iframes')} "
          f"(max depth {facts.get('maxFrameDepth')}, "
          f"cross-origin {facts.get('crossOriginFrames')})")
    print()

    inter = facts.get("interactive") or {}
    naive = facts.get("naiveSelectorCount", 0)
    missed = facts.get("missedByNaiveSelector", 0)
    total = (inter.get("semantic", 0) + inter.get("decorative", 0)) or 1
    print(f"interactive (semantic)   : {inter.get('semantic')}")
    print(f"interactive (decorative) : {inter.get('decorative')}")
    print(f"reachable by plain querySelectorAll : {naive}")
    print(f"missed without piercing             : {missed} "
          f"({100 * missed / total:.0f}%)")
    print()

    dialogs = facts.get("dialogs") or []
    print(f"dialogs: {len(dialogs)}")
    for d in dialogs[:6]:
        where = "shadow" if d.get("inShadow") else "light"
        print(f"  <{d['tag']}> in {where} DOM, scrim={d.get('hasScrim')}")
        print(f"    text: {d.get('text', '')[:70]!r}")
        for b in (d.get("buttons") or [])[:5]:
            print(f"    button {b.get('text')!r} "
                  f"({b.get('where')} DOM, slot={b.get('slot')})")
    if not dialogs:
        print("  (none)")

    customs = facts.get("customElements") or {}
    top = sorted(customs.items(), key=lambda kv: -kv[1])[:12]
    print()
    print(f"custom elements: {len(customs)} distinct")
    for name, n in top:
        print(f"  {name:34} x{n}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten: {args.json}")

    await _close(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
