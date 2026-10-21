"""
youtube_task.py — Watch a video, seek, switch videos, and read the metadata.

One connection for the whole run. Chrome's approval-mode debugging tolerates
only a handful of connect/disconnect cycles, so everything happens here.

The task, in order:

    1. search YouTube for a video and open the first result
    2. read its title, channel, view count and duration from the page
    3. seek to a specific time by driving the <video> element's currentTime
    4. confirm the seek took effect by reading currentTime back
    5. pause, then resume, confirming the paused state flips each time
    6. go back to the results and open a different video
    7. read the second video's metadata

Every step is verified against the page's own state. A step that reports
success without the page changing is treated as a failure.

    python youtube_task.py
    python youtube_task.py --query "lofi hip hop" --seek 90
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


#: Read the player's state and the video's metadata.
#:
#: YouTube puts the title in several places depending on layout, so the script
#: reads what the player itself knows first and falls back to the document
#: title. currentTime and duration come from the <video> element, which is the
#: same source the player UI uses.
PLAYER_JS = r"""
(() => {
  const v = document.querySelector('video');
  const out = {
    url: location.href,
    docTitle: document.title,
    hasVideo: !!v,
  };

  if (v) {
    out.currentTime = Math.round(v.currentTime * 10) / 10;
    out.duration = Math.round(v.duration * 10) / 10;
    out.paused = v.paused;
    out.readyState = v.readyState;
    out.videoWidth = v.videoWidth;
    out.videoHeight = v.videoHeight;
    out.muted = v.muted;
    out.playbackRate = v.playbackRate;
  }

  // Title from the player's own heading, which survives layout changes better
  // than a class name.
  const h1 = document.querySelector('h1.ytd-watch-metadata, h1.title');
  if (h1) out.title = (h1.innerText || '').replace(/\s+/g, ' ').trim();

  // Channel name.
  const ch = document.querySelector(
    '#owner #channel-name a, ytd-channel-name a, #owner a');
  if (ch) out.channel = (ch.innerText || '').trim();

  // View count and age, which live in a single info line.
  const info = document.querySelector('#info span, #view-count, '
    + 'ytd-view-count #view-count');
  if (info) out.views = (info.innerText || '').replace(/\s+/g, ' ').trim();

  // The metadata block as one string, for anything the selectors above miss.
  const meta = document.querySelector('#info-contents, #above-the-fold');
  if (meta) {
    out.metaText = (meta.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 220);
  }

  return out;
})()
"""


#: Find search results, preferring the video renderer over shorts or shelves.
RESULTS_JS = r"""
(() => {
  const out = [];
  const seen = new Set();

  function push(el, kind) {
    const a = el.querySelector('a#video-title, a#thumbnail, a[href*="/watch"]');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    const m = href.match(/v=([A-Za-z0-9_-]{6,})/);
    if (!m) return;
    const id = m[1];
    if (seen.has(id)) return;
    seen.add(id);

    const titleEl = el.querySelector('#video-title, #title, h3');
    const title = titleEl
      ? (titleEl.innerText || titleEl.getAttribute('title') || '')
          .replace(/\s+/g, ' ').trim()
      : (a.getAttribute('title') || '').trim();

    const r = (titleEl || a).getBoundingClientRect();
    out.push({
      id,
      kind,
      title: title.slice(0, 90),
      href,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    });
  }

  document.querySelectorAll('ytd-video-renderer').forEach(e => push(e, 'video'));
  document.querySelectorAll('ytd-rich-item-renderer').forEach(e => push(e, 'rich'));
  document.querySelectorAll('ytd-compact-video-renderer').forEach(e => push(e, 'compact'));

  return out;
})()
"""


#: The generic search field ranking, shared with generic_agent.
NON_TEXT_INPUT = {
    "checkbox", "radio", "hidden", "submit", "button", "reset",
    "file", "image", "range", "color",
}
SEARCH_WORDS = ("search", "find", "query", "\u641c\u5c0b", "\u641c\u7d22",
                "\u641c")


def is_text_field(e: dict) -> bool:
    tag = (e.get("tag") or "").lower()
    attrs = e.get("attributes") or {}
    if tag == "textarea":
        return True
    if tag == "input":
        return (attrs.get("type") or "text").lower() not in NON_TEXT_INPUT
    if tag == "div" and attrs.get("contenteditable") == "true":
        return True
    return False


def search_score(e: dict) -> int:
    if not is_text_field(e):
        return -1
    score = 0
    hay = " ".join([
        (e.get("text") or ""), (e.get("name") or ""),
        " ".join((e.get("attributes") or {}).values()),
    ]).lower()
    if any(w in hay for w in SEARCH_WORDS):
        score += 10
    attrs = e.get("attributes") or {}
    if (attrs.get("type") or "").lower() == "search":
        score += 15
    if attrs.get("name") == "search_query":
        score += 15
    if attrs.get("role") == "searchbox":
        score += 12
    if e.get("y", 9999) < 120:
        score += 6
    if e.get("width", 0) > 200:
        score += 4
    return score


async def find_search(session: Session) -> dict | None:
    obs = await session.observe(max_nodes=400)
    ranked = sorted(((search_score(e), e) for e in (obs.get("elements") or [])),
                    key=lambda p: -p[0])
    for score, e in ranked:
        if score > 0:
            return e
    return None


async def player_state(session: Session) -> dict:
    data = await session.evaluate(PLAYER_JS)
    return data if isinstance(data, dict) else {}


async def wait_video(session: Session, timeout: float = 25.0) -> dict:
    """Wait until a <video> element exists and has metadata."""
    deadline = time.perf_counter() + timeout
    last = {}
    while time.perf_counter() < deadline:
        last = await player_state(session)
        if last.get("hasVideo") and (last.get("duration") or 0) > 0:
            return last
        await asyncio.sleep(1.0)
    return last


# --------------------------------------------------------------------------

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="lofi hip hop")
    ap.add_argument("--seek", type=float, default=90.0,
                    help="Seconds to seek to")
    args = ap.parse_args()

    session = Session()
    sock = attach.BrowserSocket(attach.ws_endpoint())
    try:
        await sock.connect(timeout=10)
    except Exception as exc:
        print(f"Could not connect: {exc}")
        return 1

    steps: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name:<38} {detail[:56]}")

    try:
        pages = await attach.list_targets(sock)
        idx = next((i for i, p in enumerate(pages)
                    if "youtube.com" in (p.get("url") or "")), None)
        if idx is None:
            idx = next((i for i, p in enumerate(pages)
                        if "colab.research.google.com" in (p.get("url") or "")), 0)
        await attach.attach(session, tab_index=idx)

        # ---- 1. search ---------------------------------------------------
        print("step 1: search")
        t0 = time.perf_counter()
        await session.navigate("https://www.youtube.com/")
        await asyncio.sleep(2.5)

        field = await find_search(session)
        step("found the search box", field is not None,
             f"{field.get('tag')} {field.get('name')!r}" if field else "none")

        if not field:
            return 1

        await session.type_text(args.query, ref=field["ref"], clear=True)
        await session.press("Enter")
        ok = await session.assert_changed(
            "location.href.includes('results')", timeout=12.0)
        step("search submitted", ok.get("changed"),
             await session.evaluate("location.pathname"))
        print(f"  ({(time.perf_counter()-t0)*1000:.0f} ms)")

        # ---- 2. open a result -------------------------------------------
        print("\nstep 2: open the first video")
        await asyncio.sleep(1.5)
        results = await session.evaluate(RESULTS_JS)
        results = results if isinstance(results, list) else []
        step("found results", len(results) > 0, f"{len(results)} videos")
        for r in results[:4]:
            print(f"       {r['kind']:<8} {r['title'][:52]!r}")

        if not results:
            return 1

        first = results[0]
        t0 = time.perf_counter()
        await session.click(x=first["x"], y=first["y"], settle=False)
        ok = await session.assert_changed(
            "location.href.includes('/watch')", timeout=15.0)
        watch_url = await session.evaluate("location.href")
        step("opened a watch page", ok.get("changed"), str(watch_url)[:60])
        print(f"  ({(time.perf_counter()-t0)*1000:.0f} ms)")

        # ---- 3. read metadata -------------------------------------------
        print("\nstep 3: read metadata")
        state = await wait_video(session)
        step("video element ready", bool(state.get("hasVideo")),
             f"duration={state.get('duration')}s")
        step("title readable", bool(state.get("title")),
             (state.get("title") or "")[:50])
        step("channel readable", bool(state.get("channel")),
             (state.get("channel") or "")[:40])
        step("view count readable", bool(state.get("views")),
             (state.get("views") or "")[:40])
        print(f"       duration: {state.get('duration')}s  "
              f"resolution: {state.get('videoWidth')}x{state.get('videoHeight')}")

        # ---- 4. seek -----------------------------------------------------
        print(f"\nstep 4: seek to {args.seek:.0f}s")
        target = min(args.seek, max(1.0, (state.get("duration") or 60) - 5))
        seeked = await session.evaluate(
            "(() => { const v = document.querySelector('video'); "
            "if (!v) return null; "
            f"v.currentTime = {target}; "
            "return Math.round(v.currentTime * 10) / 10; })()")
        await asyncio.sleep(2.0)
        after = await player_state(session)
        moved = abs((after.get("currentTime") or 0) - target) < 3.0
        step("seek took effect", moved,
             f"asked {target}s, now {after.get('currentTime')}s")

        # ---- 5. pause and resume ----------------------------------------
        print("\nstep 5: pause and resume")
        was_paused = after.get("paused")
        await session.evaluate(
            "(() => { const v = document.querySelector('video'); "
            "if (v && !v.paused) v.pause(); return true; })()")
        await asyncio.sleep(0.8)
        paused = await player_state(session)
        step("paused", paused.get("paused") is True,
             f"paused={paused.get('paused')}")

        await session.evaluate(
            "(() => { const v = document.querySelector('video'); "
            "if (v) { const p = v.play(); if (p) p.catch(()=>{}); } "
            "return true; })()")
        await asyncio.sleep(1.5)
        resumed = await player_state(session)
        step("resumed", resumed.get("paused") is False,
             f"paused={resumed.get('paused')}")

        # ---- 6. switch video --------------------------------------------
        print("\nstep 6: switch to another video")
        await session.navigate("https://www.youtube.com/results?search_query="
                               + args.query.replace(" ", "+"))
        await asyncio.sleep(2.5)
        results2 = await session.evaluate(RESULTS_JS)
        results2 = results2 if isinstance(results2, list) else []

        second = None
        for r in results2:
            if r["id"] != first["id"]:
                second = r
                break

        step("found a different video", second is not None,
             (second or {}).get("title", "")[:50])

        if second:
            before_id = first["id"]
            await session.click(x=second["x"], y=second["y"], settle=False)
            ok = await session.assert_changed(
                f"location.href.includes('{second['id']}')", timeout=15.0)
            step("switched video", ok.get("changed"),
                 f"{before_id[:11]} -> {second['id'][:11]}")

            state2 = await wait_video(session)
            step("second video metadata", bool(state2.get("title")),
                 (state2.get("title") or "")[:50])
            step("second video duration", bool(state2.get("duration")),
                 f"{state2.get('duration')}s")

            print(f"\n       second title   : {state2.get('title')}")
            print(f"       second channel : {state2.get('channel')}")
            print(f"       second views   : {state2.get('views')}")
            print(f"       second duration: {state2.get('duration')}s")

        # ---- summary -----------------------------------------------------
        passed = sum(1 for _n, ok, _d in steps if ok)
        print(f"\n{'=' * 66}")
        print(f"{passed}/{len(steps)} steps passed")
        if passed < len(steps):
            print("failed:")
            for n, ok, d in steps:
                if not ok:
                    print(f"  - {n}: {d}")
        return 0 if passed == len(steps) else 1

    finally:
        await sock.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace", line_buffering=True)
    raise SystemExit(asyncio.run(main()))
