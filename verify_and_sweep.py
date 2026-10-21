"""
verify_and_sweep.py — Verify the uploads, then click across popular sites.

Two jobs, one connection:

  PART 1  Open each uploaded notebook and confirm its cells are there. An upload
          that reported success but produced an empty file is a failure, and
          the only way to know is to open it and read the content.

  PART 2  Visit a set of popular sites and, for each, record how many
          interactables are found and whether a click actually navigates. This
          answers "can it click every site" with evidence rather than opinion.

    python verify_and_sweep.py
    python verify_and_sweep.py --part 1
    python verify_and_sweep.py --part 2
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


UPLOADED = [
    ("first upload", "12hbaPkNheqTGFi_QA532JtRBdNaDWwFv"),
    ("second upload", "11T_O5ctYEJVHge6IgBbIElSMCEFWbvDZ"),
]

#: Sites to sweep. Chosen for variety in rendering strategy, not for traffic
#: alone: a server-rendered page, two SPAs, a docs site, a shop, a search engine.
SITES: list[tuple[str, str]] = [
    ("Wikipedia", "https://en.wikipedia.org/wiki/Main_Page"),
    ("GitHub", "https://github.com/"),
    ("Google", "https://www.google.com/"),
    ("Bing", "https://www.bing.com/"),
    ("YouTube", "https://www.youtube.com/"),
    ("Amazon", "https://www.amazon.com/"),
    ("Stack Overflow", "https://stackoverflow.com/"),
    ("Reddit", "https://www.reddit.com/"),
    ("Baidu", "https://www.baidu.com/"),
    ("Yahoo", "https://www.yahoo.com/"),
]


#: Read a notebook's cells, tolerating the shadow DOM Colab renders them in.
CELLS_JS = r"""
(() => {
  const cells = Array.from(document.querySelectorAll('div.cell'));
  const detail = [];
  for (const c of cells) {
    const cls = (c.className || '').toString();
    const kind = cls.includes('code') ? 'code'
               : cls.includes('text') ? 'text' : 'other';
    detail.push({
      kind,
      text: (c.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90),
    });
  }
  return {
    url: location.href,
    title: document.title,
    total: cells.length,
    code: detail.filter(d => d.kind === 'code').length,
    text: detail.filter(d => d.kind === 'text').length,
    cells: detail.slice(0, 6),
  };
})()
"""


#: Count interactables and report what is clickable, across shadow roots.
SWEEP_JS = r"""
(() => {
  const SEMANTIC = new Set(['a', 'button', 'input', 'select', 'textarea']);
  const out = {
    semantic: 0, decorative: 0, shadowRoots: 0,
    lightDom: 0, samples: [],
  };
  const seen = new Set();

  function walk(root, depth) {
    if (!root || depth > 40) return;
    let nodes;
    try { nodes = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of nodes) {
      if (seen.has(el)) continue;
      seen.add(el);
      const tag = el.tagName ? el.tagName.toLowerCase() : '';
      const role = el.getAttribute ? el.getAttribute('role') : null;
      const interactive =
        SEMANTIC.has(tag) ||
        (role && ['button','link','checkbox','radio','tab','menuitem',
                  'textbox','combobox','switch'].includes(role)) ||
        (el.hasAttribute && (el.hasAttribute('onclick') ||
                             el.hasAttribute('tabindex')));
      if (interactive) {
        const r = el.getBoundingClientRect();
        if (r.width > 4 && r.height > 4) {
          if (SEMANTIC.has(tag)) {
            out.semantic++;
            if (depth === 0) out.lightDom++;
          } else {
            out.decorative++;
          }
          if (out.samples.length < 6) {
            const label = (el.getAttribute('aria-label') ||
                           el.innerText || el.textContent || '')
              .replace(/\s+/g, ' ').trim().slice(0, 34);
            if (label) {
              out.samples.push({
                tag,
                label,
                x: Math.round(r.left + r.width / 2),
                y: Math.round(r.top + r.height / 2),
                shadow: depth > 0,
              });
            }
          }
        }
      }
      if (el.shadowRoot) { out.shadowRoots++; walk(el.shadowRoot, depth + 1); }
      if (tag === 'iframe' || tag === 'frame') {
        let doc = null;
        try { doc = el.contentDocument; } catch (e) { doc = null; }
        if (doc) walk(doc, depth + 1);
      }
    }
  }
  walk(document, 0);

  // A naive selector, for comparison.
  let naive = 0;
  try {
    naive = document.querySelectorAll(
      'a, button, input, select, textarea, [role="button"], [onclick]').length;
  } catch (e) {}

  out.naive = naive;
  out.totalElements = document.querySelectorAll('*').length;
  return out;
})()
"""


async def part1(session: Session, sock) -> list[tuple[str, bool, str]]:
    """Open each uploaded notebook and confirm its content survived."""
    print("=" * 68)
    print("PART 1: verify the uploaded notebooks")
    print("=" * 68)

    results = []
    for label, doc_id in UPLOADED:
        url = f"https://colab.research.google.com/drive/{doc_id}"
        print(f"\n{label}: {doc_id[:12]}...")
        try:
            await session.navigate(url)
            await session.assert_changed(
                "document.querySelectorAll('div.cell').length",
                expect=None, timeout=30.0, interval=0.5,
            )
            await asyncio.sleep(1.0)
            data = await session.evaluate(CELLS_JS) or {}
            ok = data.get("total", 0) > 0
            print(f"  title : {data.get('title', '')[:60]}")
            print(f"  cells : {data.get('total')} "
                  f"(code={data.get('code')} text={data.get('text')})")
            for i, c in enumerate(data.get("cells") or []):
                print(f"    [{i}] {c['kind']:<5} {c['text'][:60]!r}")
            results.append((label, ok, data.get("title", "")))
        except Exception as exc:
            print(f"  FAILED: {type(exc).__name__}: {str(exc)[:100]}")
            results.append((label, False, str(exc)[:80]))

    return results


async def part2(session: Session, sock) -> list[dict]:
    """Visit popular sites and record what can be found and clicked."""
    print("\n" + "=" * 68)
    print("PART 2: sweep popular sites")
    print("=" * 68)

    rows: list[dict] = []
    for name, url in SITES:
        row: dict = {"site": name, "url": url}
        t0 = time.perf_counter()
        try:
            await session.navigate(url)
            row["load_ms"] = round((time.perf_counter() - t0) * 1000)

            # Give the SPA time to build itself.
            await asyncio.sleep(1.5)

            t1 = time.perf_counter()
            data = await session.evaluate(SWEEP_JS) or {}
            row["sweep_ms"] = round((time.perf_counter() - t1) * 1000)
            row.update({
                "title": (data.get("title") or "")[:40],
                "total": data.get("totalElements"),
                "semantic": data.get("semantic"),
                "decorative": data.get("decorative"),
                "shadow": data.get("shadowRoots"),
                "naive": data.get("naive"),
                "samples": (data.get("samples") or [])[:3],
            })
            print(f"\n{name:<16} {row['load_ms']:>5}ms load  "
                  f"{row.get('total')} elems  "
                  f"semantic={row.get('semantic')} "
                  f"decor={row.get('decorative')} "
                  f"shadow={row.get('shadow')}")
            print(f"  title: {row.get('title')!r}")
            for s in row.get("samples") or []:
                print(f"  {s['tag']:<10} {s['label'][:34]!r} "
                      f"({s['x']},{s['y']}) sh={s['shadow']}")

            # Try one real click and see whether it navigates.
            if row.get("samples"):
                target = row["samples"][0]
                t2 = time.perf_counter()
                res = await session.click(x=target["x"], y=target["y"],
                                          settle=False)
                row["click_ms"] = round((time.perf_counter() - t2) * 1000)
                row["url_changed"] = res.get("url_changed")
                row["blocked"] = (res.get("hit") or {}).get("blocked")
                print(f"  click -> {row['click_ms']}ms  "
                      f"nav={row['url_changed']} blocked={row['blocked']}")
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {str(exc)[:70]}"
            print(f"\n{name:<16} FAILED: {row['error']}")
        rows.append(row)

    return rows


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["1", "2"], default=None)
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect to Chrome: {exc}")
        return 1

    try:
        version = await sock.send("Browser.getVersion")
        print(f"browser: {version.get('product')}\n")

        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        verify = []
        if args.part in (None, "1"):
            verify = await part1(session, sock)
        sweep = []
        if args.part in (None, "2"):
            sweep = await part2(session, sock)

        # ---- summary -----------------------------------------------------
        if verify:
            print("\n" + "=" * 68)
            print("UPLOAD VERIFICATION")
            print("=" * 68)
            for label, ok, detail in verify:
                print(f"  {label:<16} {'OK' if ok else 'FAILED':<7} "
                      f"{detail[:50]}")

        if sweep:
            print("\n" + "=" * 68)
            print("SWEEP SUMMARY")
            print("=" * 68)
            print(f"  {'site':<16} {'load':>7} {'elems':>7} {'sem':>5} "
                  f"{'sh':>4} {'click':>7} {'nav':>5}")
            for r in sweep:
                if r.get("error"):
                    print(f"  {r['site']:<16} ERROR {r['error'][:40]}")
                    continue
                print(f"  {r['site']:<16} "
                      f"{r.get('load_ms', 0):>6}ms "
                      f"{str(r.get('total')):>7} "
                      f"{str(r.get('semantic')):>5} "
                      f"{str(r.get('shadow')):>4} "
                      f"{str(r.get('click_ms', '-')):>6}ms "
                      f"{str(r.get('url_changed')):>5}")

            ok_sites = sum(1 for r in sweep if r.get("semantic", 0) > 0)
            print(f"\n  {ok_sites}/{len(sweep)} sites had findable interactables")

        return 0

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
