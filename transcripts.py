"""
transcripts.py — Open YouTube's transcript panel and read the actual text.

The transcript is not in the DOM until the panel is opened. YouTube renders the
button inside the description area, and the segments arrive lazily, so this:

    1. opens the watch page
    2. finds the "Show transcript" button by its label, in any language
    3. clicks it
    4. waits for segment elements to appear
    5. reads every segment and joins them into paragraphs

Only videos that expose a transcript are attempted; the caller gets a clear
"no transcript" result rather than an empty string.

    python transcripts.py --ids FILE.json
    python transcripts.py --ids FILE.json --limit 3
    python transcripts.py --id rFZHOHl-L8A
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


#: Find the transcript button. YouTube labels it "Show transcript" in English
#: and something localised elsewhere, so the match is on several forms plus the
#: stable element name where one exists.
FIND_BUTTON_JS = r"""
(() => {
  const out = [];
  const needles = [
    'show transcript', 'transcript', 'transkript', 'transcripción',
    '\u986f\u793a\u5b57\u5e55', '\u663e\u793a\u5b57\u5e55',
    '\u5b57\u5e55', '\u8f6c\u5199', '\u8f49\u5beb',
  ];

  function visible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 8 && r.height > 8;
  }

  // The canonical renderer, when the layout includes it.
  const canonical = document.querySelector(
    'ytd-video-description-transcript-section-renderer button');
  if (canonical && visible(canonical)) {
    const r = canonical.getBoundingClientRect();
    out.push({ how: 'renderer',
               label: (canonical.innerText || '').trim().slice(0, 40),
               x: Math.round(r.left + r.width / 2),
               y: Math.round(r.top + r.height / 2) });
  }

  for (const el of document.querySelectorAll('button, tp-yt-paper-button, '
                                            + '[role="button"]')) {
    if (!visible(el)) continue;
    const hay = ((el.innerText || '') + ' ' +
                 (el.getAttribute('aria-label') || '')).toLowerCase();
    if (!needles.some(n => hay.includes(n))) continue;
    const r = el.getBoundingClientRect();
    out.push({ how: 'label',
               label: (el.innerText || el.getAttribute('aria-label') || '')
                 .trim().slice(0, 40),
               x: Math.round(r.left + r.width / 2),
               y: Math.round(r.top + r.height / 2) });
  }

  // De-duplicate by position.
  const seen = new Set();
  return out.filter(b => {
    const k = `${b.x},${b.y}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
})()
"""


#: Read transcript segments once the panel is open.
READ_JS = r"""
(() => {
  const segs = document.querySelectorAll(
    'ytd-transcript-segment-renderer, '
    + 'ytd-transcript-body-renderer ytd-transcript-segment-renderer, '
    + '.ytd-transcript-segment-renderer');

  if (!segs.length) {
    // Report what the panel looks like so a failure is diagnosable.
    const panel = document.querySelector(
      'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"], '
      + '#panels ytd-engagement-panel-section-list-renderer');
    return {
      ready: false,
      segCount: 0,
      panelFound: !!panel,
      panelText: panel
        ? (panel.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160)
        : null,
    };
  }

  const out = [];
  for (const s of segs) {
    const timeEl = s.querySelector('.segment-timestamp, #segment-timestamp');
    const textEl = s.querySelector('.segment-text, #segment-text, '
                                   + 'yt-formatted-string.segment-text');
    const time = timeEl ? (timeEl.innerText || '').trim() : '';
    const text = textEl
      ? (textEl.innerText || '').replace(/\s+/g, ' ').trim()
      : (s.innerText || '').replace(/\s+/g, ' ').trim();
    if (text) out.push({ time, text });
  }

  return { ready: out.length > 0, segCount: out.length, segments: out };
})()
"""


def to_seconds(stamp: str) -> int:
    """Convert 1:02:03 or 2:03 to seconds."""
    if not stamp:
        return 0
    parts = [p for p in re.split(r"[:.]", stamp) if p.isdigit()]
    total = 0
    for p in parts:
        total = total * 60 + int(p)
    return total


def paragraphs(segments: list[dict], gap: int = 25,
               max_chars: int = 700) -> list[str]:
    """Join segments into readable paragraphs.

    A new paragraph starts when the timestamp jumps by more than ``gap``
    seconds, which usually means a topic change or a pause.
    """
    out: list[str] = []
    buf: list[str] = []
    buf_len = 0
    last_t = None

    for seg in segments:
        t = to_seconds(seg.get("time", ""))
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if (last_t is not None and t - last_t > gap) or buf_len > max_chars:
            if buf:
                out.append(" ".join(buf))
            buf, buf_len = [], 0
        buf.append(text)
        buf_len += len(text)
        last_t = t

    if buf:
        out.append(" ".join(buf))
    return out


async def open_transcript(session: Session, verbose: bool = True) -> dict:
    """Open the transcript panel. Returns what happened."""
    buttons = await session.evaluate(FIND_BUTTON_JS)
    buttons = buttons if isinstance(buttons, list) else []

    if verbose:
        print(f"    transcript buttons found: {len(buttons)}")
        for b in buttons[:3]:
            print(f"      {b['how']:<9} {b['label']!r} ({b['x']},{b['y']})")

    if not buttons:
        # The button lives under the description, which may be collapsed.
        expanded = await session.evaluate(r"""(() => {
          const btn = document.querySelector(
            '#expand, tp-yt-paper-button#expand, '
            + 'ytd-text-inline-expander #expand');
          if (btn) { btn.click(); return true; }
          return false;
        })()""")
        if expanded:
            await asyncio.sleep(1.0)
            buttons = await session.evaluate(FIND_BUTTON_JS)
            buttons = buttons if isinstance(buttons, list) else []
            if verbose:
                print(f"    after expanding description: {len(buttons)}")

    if not buttons:
        return {"opened": False, "reason": "no transcript button"}

    target = buttons[0]
    await session.click(x=target["x"], y=target["y"], settle=False)
    await asyncio.sleep(2.0)

    # Wait for segments.
    deadline = time.perf_counter() + 15.0
    last: dict = {}
    while time.perf_counter() < deadline:
        data = await session.evaluate(READ_JS)
        if isinstance(data, dict):
            last = data
            if data.get("ready"):
                return {"opened": True, **data}
        await asyncio.sleep(1.0)

    return {"opened": False, "reason": "panel opened but no segments",
            **last}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=None, help="JSON file from watch_ten.py")
    ap.add_argument("--id", default=None, help="A single video id")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--out", default=None, help="Write results to this file")
    args = ap.parse_args()

    if not args.ids and not args.id:
        print("Pass --ids FILE or --id VIDEOID")
        return 1

    if args.id:
        videos = [{"id": args.id, "title": args.id}]
    else:
        videos = json.loads(Path(args.ids).read_text(encoding="utf-8"))
    videos = videos[:args.limit]

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 1

    results: list[dict] = []
    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "youtube.com" in (p.get("url") or "")), None)
        if idx is None:
            idx = 0
        await attach.attach(session, tab_index=idx)

        for i, v in enumerate(videos, 1):
            vid = v.get("id") or v.get("video_id")
            title = (v.get("title") or "")[:56]
            print(f"\n[{i}/{len(videos)}] {title}")

            url = f"https://www.youtube.com/watch?v={vid}"
            await session.navigate(url)
            await asyncio.sleep(2.0)

            got = await open_transcript(session)
            entry = {
                "id": vid,
                "title": v.get("title"),
                "channel": v.get("channel"),
                "duration": v.get("duration"),
                "url": url,
                "opened": got.get("opened"),
                "reason": got.get("reason"),
                "segments": got.get("segments") or [],
            }

            if got.get("opened"):
                segs = entry["segments"]
                text = " ".join(s["text"] for s in segs)
                entry["word_count"] = len(text.split())
                entry["paragraphs"] = paragraphs(segs)
                print(f"    transcript: {len(segs)} segments, "
                      f"{entry['word_count']} words, "
                      f"{len(entry['paragraphs'])} paragraphs")
                preview = (entry["paragraphs"][0] if entry["paragraphs"]
                           else "")[:180]
                if preview:
                    print(f"    starts: {preview}")
            else:
                print(f"    no transcript: {got.get('reason')}")
                if got.get("panelText"):
                    print(f"    panel says: {str(got['panelText'])[:100]}")

            results.append(entry)
            await asyncio.sleep(1.0)

        # ---- summary -----------------------------------------------------
        have = [r for r in results if r.get("opened")]
        print(f"\n{'=' * 70}")
        print(f"TRANSCRIPTS: {len(have)}/{len(results)}")
        print(f"{'=' * 70}")
        for r in results:
            mark = "yes" if r.get("opened") else "no "
            wc = r.get("word_count", 0)
            print(f"  [{mark}] {wc:>5} words  {(r.get('title') or '')[:52]}")

        if args.out:
            Path(args.out).write_text(
                json.dumps(results, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"\nwritten: {args.out}")

        return 0 if have else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
