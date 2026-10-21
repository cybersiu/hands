"""
watch_ten.py — Open ten YouTube search results and record what each one is.

One connection for the whole run. For each video it captures the title, channel,
view count, duration and a slice of the description, then prints a summary that
can be read as a single paragraph.

Metadata extraction is deliberately redundant: YouTube ships several layouts, so
each field is tried against multiple selectors and the document title is used as
a last resort. A field that cannot be read is reported as null rather than
guessed.

    python watch_ten.py
    python watch_ten.py --query colab --count 10
    python watch_ten.py --query colab --no-open   # read results only
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import attach
from browser import Session


#: Search result list. Prefers ytd-video-renderer (the standard list) and falls
#: back to rich items, which is what the home grid uses.
RESULTS_JS = r"""
(() => {
  const out = [];
  const seen = new Set();

  function push(el, kind) {
    const a = el.querySelector('a#video-title, a#thumbnail, a[href*="/watch"]');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    const m = href.match(/[?&]v=([A-Za-z0-9_-]{6,})/);
    if (!m) return;
    const id = m[1];
    if (seen.has(id)) return;
    seen.add(id);

    const titleEl = el.querySelector('#video-title, #title, h3');
    const title = titleEl
      ? (titleEl.innerText || titleEl.getAttribute('title') || '')
          .replace(/\s+/g, ' ').trim()
      : (a.getAttribute('title') || '').replace(/\s+/g, ' ').trim();

    // Duration badge on the thumbnail.
    const durEl = el.querySelector(
      'ytd-thumbnail-overlay-time-status-renderer #text, '
      + '#overlays span.ytd-thumbnail-overlay-time-status-renderer, '
      + 'span.ytd-thumbnail-overlay-time-status-renderer');
    const duration = durEl ? (durEl.innerText || '').trim() : null;

    // Channel and view count, when the row exposes them.
    const chEl = el.querySelector(
      'ytd-channel-name #text, #channel-name a, .ytd-channel-name a');
    const channel = chEl ? (chEl.innerText || '').trim() : null;

    const metaEl = el.querySelector(
      '#metadata-line, .ytd-video-meta-block #metadata-line');
    let views = null, age = null;
    if (metaEl) {
      const spans = Array.from(metaEl.querySelectorAll('span'))
        .map(s => (s.innerText || '').trim()).filter(Boolean);
      views = spans[0] || null;
      age = spans[1] || null;
    }

    const r = (titleEl || a).getBoundingClientRect();
    out.push({
      id, kind, title: title.slice(0, 120), href,
      channel, views, age, duration,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    });
  }

  document.querySelectorAll('ytd-video-renderer').forEach(e => push(e, 'video'));
  document.querySelectorAll('ytd-rich-item-renderer').forEach(e => push(e, 'rich'));
  return out;
})()
"""


#: Everything worth knowing about the currently open watch page.
WATCH_JS = r"""
(() => {
  const out = { url: location.href };

  // The document title always carries the video title, so it is the reliable
  // fallback when the page's own headings are laid out differently.
  out.docTitle = document.title;

  const h1 = document.querySelector(
    'h1.ytd-watch-metadata yt-formatted-string, '
    + 'h1.ytd-watch-metadata, #title h1 yt-formatted-string, #title h1');
  if (h1) {
    const t = (h1.innerText || '').replace(/\s+/g, ' ').trim();
    if (t) out.title = t;
  }

  // Channel: several wrappers exist depending on the layout generation.
  for (const sel of [
    'ytd-video-owner-renderer #channel-name a',
    '#owner #channel-name a',
    'ytd-channel-name a',
    '#channel-name a',
    '#owner ytd-channel-name a',
  ]) {
    const el = document.querySelector(sel);
    if (el) {
      const t = (el.innerText || '').trim();
      if (t) { out.channel = t; break; }
    }
  }

  // View count and publish date sit in one line.
  for (const sel of ['#info span', '#view-count', 'ytd-view-count',
                     '#info yt-formatted-string']) {
    const el = document.querySelector(sel);
    if (el) {
      const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (t) { out.views = t; break; }
    }
  }

  // A wider block, for anything the specific selectors missed.
  const info = document.querySelector('#info, #info-contents, #above-the-fold');
  if (info) {
    out.infoText = (info.innerText || '')
      .replace(/\s+/g, ' ').trim().slice(0, 300);
  }

  // Description, expanded or collapsed.
  const desc = document.querySelector(
    '#description-inline-expander, #description, ytd-text-inline-expander');
  if (desc) {
    out.description = (desc.innerText || '')
      .replace(/\s+/g, ' ').trim().slice(0, 500);
  }

  // Player facts.
  const v = document.querySelector('video');
  if (v) {
    out.durationSec = Math.round((v.duration || 0) * 10) / 10;
    out.currentTime = Math.round((v.currentTime || 0) * 10) / 10;
    out.resolution = v.videoWidth && v.videoHeight
      ? `${v.videoWidth}x${v.videoHeight}` : null;
    out.paused = v.paused;
    out.muted = v.muted;
  }

  // Transcript availability, without opening the panel.
  out.hasTranscriptButton = !!document.querySelector(
    'ytd-video-description-transcript-section-renderer button, '
    + 'button[aria-label*="transcript"], button[aria-label*="Transcript"]');

  return out;
})()
"""


def clean_title(doc_title: str) -> str | None:
    """Strip YouTube's suffix from the document title."""
    if not doc_title:
        return None
    t = re.sub(r"\s*-\s*YouTube\s*$", "", doc_title).strip()
    return t or None


def human_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "unknown"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


async def find_search(session: Session) -> dict | None:
    """Locate YouTube's search field by what it accepts, not by selector."""
    obs = await session.observe(max_nodes=400)
    best = None
    best_score = 0
    for e in (obs.get("elements") or []):
        tag = (e.get("tag") or "").lower()
        attrs = e.get("attributes") or {}
        if tag not in ("input", "textarea"):
            continue
        if (attrs.get("type") or "text").lower() in (
                "checkbox", "radio", "hidden", "submit", "button", "file"):
            continue
        hay = " ".join([(e.get("text") or ""), (e.get("name") or ""),
                        " ".join(attrs.values())]).lower()
        score = 0
        if attrs.get("name") == "search_query":
            score += 20
        if (attrs.get("type") or "").lower() == "search":
            score += 10
        if "search" in hay or "\u641c" in hay:
            score += 8
        if e.get("y", 9999) < 120:
            score += 4
        if score > best_score:
            best_score = score
            best = e
    return best


async def read_watch(session: Session, timeout: float = 20.0) -> dict:
    """Wait for the watch page to settle, then read its metadata."""
    deadline = time.perf_counter() + timeout
    last: dict = {}
    while time.perf_counter() < deadline:
        data = await session.evaluate(WATCH_JS)
        if isinstance(data, dict):
            last = data
            # A title plus a duration means the page is usable.
            if data.get("title") or data.get("docTitle"):
                if data.get("durationSec") or data.get("views"):
                    return data
        await asyncio.sleep(1.0)
    return last


# --------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="colab")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--no-open", action="store_true",
                    help="List the results without opening each one")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 1

    watched: list[dict] = []
    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "youtube.com" in (p.get("url") or "")), None)
        if idx is None:
            idx = 0
        await attach.attach(session, tab_index=idx)

        # ---- search ------------------------------------------------------
        print(f"searching: {args.query!r}")
        await session.navigate("https://www.youtube.com/")
        await asyncio.sleep(2.5)

        field = await find_search(session)
        if not field:
            print("search box not found")
            return 1

        await session.type_text(args.query, ref=field["ref"], clear=True)
        await session.press("Enter")
        await session.assert_changed("location.href.includes('results')",
                                     timeout=12.0)
        await asyncio.sleep(2.0)

        results = await session.evaluate(RESULTS_JS)
        results = results if isinstance(results, list) else []
        print(f"{len(results)} result(s) found\n")

        if args.no_open:
            for i, r in enumerate(results[:args.count], 1):
                print(f"{i:>2}. {r['title'][:70]}")
                print(f"    {r.get('channel') or '-'} | "
                      f"{r.get('views') or '-'} | {r.get('duration') or '-'}")
            return 0

        # ---- open each result -------------------------------------------
        targets = results[:args.count]
        for i, r in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}] {r['title'][:60]}")
            t0 = time.perf_counter()

            # Navigate by URL rather than clicking: the result list re-renders
            # between visits, so stored coordinates go stale. The URL is stable.
            url = f"https://www.youtube.com/watch?v={r['id']}"
            await session.navigate(url)

            data = await read_watch(session)
            elapsed = (time.perf_counter() - t0) * 1000

            title = (data.get("title") or clean_title(data.get("docTitle"))
                     or r.get("title"))
            entry = {
                "n": i,
                "id": r["id"],
                "title": title,
                "channel": data.get("channel") or r.get("channel"),
                "views": data.get("views") or r.get("views"),
                "age": r.get("age"),
                "duration": human_duration(data.get("durationSec"))
                            if data.get("durationSec") else r.get("duration"),
                "resolution": data.get("resolution"),
                "has_transcript": data.get("hasTranscriptButton"),
                "description": (data.get("description") or "")[:280],
                "url": url,
                "read_ms": round(elapsed),
            }
            watched.append(entry)

            print(f"    title    : {(entry['title'] or '?')[:60]}")
            print(f"    channel  : {entry['channel'] or '?'}")
            print(f"    views    : {entry['views'] or '?'}")
            print(f"    duration : {entry['duration'] or '?'}")
            print(f"    res      : {entry['resolution'] or '?'}  "
                  f"transcript={'yes' if entry['has_transcript'] else 'no'}  "
                  f"({entry['read_ms']}ms)")
            if entry["description"]:
                print(f"    about    : {entry['description'][:150]}")
            print()

            await asyncio.sleep(0.8)

        # ---- summary -----------------------------------------------------
        print("=" * 70)
        print(f"WATCHED {len(watched)} VIDEO(S) FOR {args.query!r}")
        print("=" * 70)
        for e in watched:
            print(f"{e['n']:>2}. {e['title'][:64]}")
            print(f"    {e['channel'] or '?'} · {e['views'] or '?'} · "
                  f"{e['duration'] or '?'}")

        if args.json:
            Path(args.json).write_text(
                json.dumps(watched, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\nwritten: {args.json}")

        return 0

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
