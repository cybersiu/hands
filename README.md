# hands

A pair of hands for an AI agent: look at a page, click things, type into things.

Not a scraper. Not a test runner. The smallest surface that lets an agent act on
a real browser the way a person would.

```json
{
  "mcp": {
    "hands": {
      "type": "local",
      "command": ["python", "/absolute/path/to/mcp-hands/server.py"],
      "enabled": true
    }
  }
}
```

Replace `/absolute/path/to` with wherever you cloned this repository. On
Windows use forward slashes or escaped backslashes.

## Two ways to get a browser

**Launch a dedicated profile** — simplest, isolated, no approval flow:

```python
start(url="https://example.com")
```

Cookies persist in `~/.mcp-hands/profile` between runs. Seed it from your real
Chrome with `python import_profile.py --close-chrome` to inherit logins.

**Attach to the Chrome you are already using** — no separate profile, no
copying, no re-authentication:

```python
attach()               # reuse a tab on the same site, else open one
attach(tab_index=2)    # a specific tab, deliberately
attach(own_tab=True)   # always open a fresh tab
```

`attach()` never navigates a tab it did not open. If a tab is already on the
site you asked for, it is reused — so a workflow that runs twice does not leave
a trail of duplicate tabs. If no tab matches, a new one is opened rather than
replacing whatever the user was reading. Navigating a reused tab to a different
site is the one thing that cannot be undone, so it is never implicit.

Attaching requires, once, in Chrome:

1. Open `chrome://inspect/#remote-debugging` and enable remote debugging.
2. Approve the connection when Chrome asks.

### The one-client rule

Chrome's approval-mode debugging serves **one client at a time**. While an MCP
server holds the connection, a second client — another MCP server, or a script
that calls `attach()` directly — cannot connect; its handshake times out.

That is why `bench.py` warns before running: it opens its own connection, so it
only works when nothing else is attached. Plan around the constraint rather than
fighting it: run one MCP server and drive everything through its tools.

## Three things that will waste your afternoon

Both of these produce a *successful* tool call that did nothing. Learn them
before debugging anything else.

### A modal can swallow every click

Colab, Gmail and most Google properties render a full-viewport `mdc-dialog__scrim`
for disconnection warnings, permission prompts and announcements. While it is
up, `document.elementFromPoint` returns the scrim for every coordinate. CDP
accepts the click, `click` reports success, and the page does not move.

`click` therefore returns a `hit` report:

```json
{
  "clicked": true,
  "target": "button 'Run all'",
  "url_changed": false,
  "hit": {
    "blocked": true,
    "blocker": "mwc-dialog",
    "blocker_text": "執行階段已中斷連線…",
    "top": { "tag": "div", "cls": "mdc-dialog__scrim" }
  },
  "warning": "A modal overlay is intercepting clicks: '執行階段已中斷連線…'."
}
```

**Check `hit.blocked` before believing a click worked.** To clear the overlay:

```python
dismiss_blocker()                 # closes it, reports which button it chose
dismiss_blocker(dry_run=True)     # see the dialog and its buttons first
click(ref="e19", auto_dismiss=True)  # close then retry, in one call
```

The buttons are usually slotted from the light DOM, not inside the dialog's
shadow root, so a shadow-only search finds the dialog but no way out.
`dismiss_blocker` walks both, and prefers a secondary action (Close, Cancel,
Later) over a primary one so it does not confirm a destructive choice.

### A click that lands is not an action that happened

The button receives the event, the handler runs, and the app still refuses —
because the runtime is disconnected, a permission is missing, or the notebook
is read-only. Nothing in the click path can detect that.

Every `click` therefore reports `url_before`, `url_after` and `url_changed`.
Navigation is the one effect a handler cannot fake by ignoring the event.

```python
click(ref="e19", retry=2)
# attempt 1: url unchanged
# attempt 2: url unchanged
# attempt 3: page reloaded first, then clicked
# {"url_changed": false, "attempts": [...],
#  "note": "The URL did not change. If the click was meant to navigate, …"}
```

For anything subtler than navigation, use `assert_changed`:

```python
assert_changed("document.querySelectorAll('.cell.execution-count').length > 0")
# {"changed": false, "elapsed": 5.0, "hint": "The condition never became true…"}
```

The failure is the useful part: `changed: false` means the action did not take
effect, and you now know to inspect the page rather than retry blindly.

### The URL you report may be a photograph of the past

`Target.getTargets` returns a snapshot. Storing its `url` on the tab object and
answering "where are we?" from it looks correct and is wrong from the first
navigation onwards:

```
click reported      url = "chrome://newtab/"
evaluate location.href = "https://grok.com/"
```

Two values, one moment, no error. Every comparison built on the snapshot —
`url_changed`, `assert_changed`, the value `navigate` returns — inherits the
lie. `Page.navigate` makes it worse: it returns when the *request is accepted*,
before the document commits, so reading `location.href` immediately afterwards
still sees the old page.

The fixes are small but they have to be made together:

* `url` is read from the document (`location.href`), never from `Target.getTargets`.
  If it cannot be read the tool says so; it does not substitute a stale value.
* `navigate` waits for a new document before returning, and checks
  `errorText` from `Page.navigate` (a DNS failure is not a successful trip).
* Waiting uses three signals, because none is sufficient. `location.href`
  changes on navigation but not on reload; `performance.timeOrigin` changes on
  every new document including a same-URL reload; `history.length` catches
  pushState routes. `document.readyState !== "loading"` is the completion
  condition, but only once identity has changed — otherwise a slow page that
  has not started navigating looks ready.

```python
navigate("https://example.com/")
# {"url": "https://example.com/", "title": "Example Domain"}   # live, not a snapshot
```

CDP events are now queued per session instead of being dropped, so
`Page.frameNavigated` and `Page.lifecycleEvent` are available through
`tab.wait_for_event(...)` when polling is not good enough. Browser-scoped
events (`Target.*`) carry no `sessionId` by design; they are not malformed.

### The general rule

> A tool call succeeding proves the transport worked, not that the page did
> what you wanted.

Verify with `observe`, `url_changed`, `assert_changed`, or a read-back
`evaluate`. Never infer success from a return value alone.

### What not to copy from scraper bots

Ticket and sneaker bots ship elaborate CAPTCHA bypass stacks — DOM pierce to
find the widget, then hardcoded pixel offsets (`click_x = iframe_x + 30`), then
template matching. Two reasons not to imitate that:

1. It is tied to one vendor's layout. Cloudflare moves the checkbox and every
   offset is wrong; hCaptcha and reCAPTCHA need entirely different code.
2. It is a bypass, not an interaction. A general-purpose hand should not carry
   a CAPTCHA breaker.

The one idea worth taking is the *verification* step at the end of those
loops — click, wait, then check that the challenge is actually gone. That is
what `assert_changed` and `url_changed` do, without hardcoding anyone's HTML.

## Why this exists

Most browser MCP servers fail in one of two ways.

**Too much data.** They dump the accessibility tree or the DOM. A page of
moderate size produces megabytes of markup, the model spends its context
parsing HTML it cannot act on, and the call times out.

**Clicks that do nothing.** They call `element.click()`. The browser marks the
resulting event `isTrusted: false`. React, Vue, Angular and nearly every payment
or checkout widget ignore those events. The click reports success and the page
does not move.

This server takes a different path.

| Concern | Approach |
|---|---|
| Observation | A compact indexed list. Each element gets a `ref`, a tag, its text and a viewport coordinate. No HTML. |
| Action | Raw CDP `Input.dispatchMouseEvent` and `Input.insertText`. The browser treats these as real input, so framework handlers fire. |
| Reach | `DOM.getDocument(pierce=True)`. Descends through shadow roots **and cross-origin iframes**. |
| Latency | One persistent Chrome. Launch cost is paid once, not per call. |

## Tools

| Tool | Purpose |
|---|---|
| `start` | Launch Chrome, keep it open. |
| `stop` | Close it. |
| `navigate` | Go to a URL. |
| `observe` | List interactable elements with coordinates. **The main way to see a page.** |
| `click` | Click by `ref` or by `x`/`y`. Reports `url_changed`, a `hit` chain, and retries. |
| `dismiss_blocker` | Close the modal intercepting clicks. Prefers a secondary action. |
| `hover` | Move the mouse without clicking. |
| `type_text` | Focus a field and type into it. |
| `assert_changed` | Wait for a page condition and report whether it became true. **How you verify an action worked.** |
| `press` | Press a key or chord: `Enter`, `ctrl+a`, `ctrl+shift+t`. |
| `scroll` | Scroll by pixels. |
| `evaluate` | Run JavaScript. Escape hatch. |
| `screenshot` | Save a PNG. Only useful if the caller can see images. |
| `tabs` / `switch_tab` / `new_tab` / `close_tab` | Tab management. |

## The core loop

```
observe          →  [e1] button "Sign in"  (640, 420)
click(ref="e1")  →  clicked button 'Sign in'
observe          →  [e1] input "Email"  (640, 300)
type_text("me@example.com", ref="e1", submit=True)
```

Two rules keep this reliable:

1. **Refs expire.** Any click that changes the DOM invalidates them. Re-observe
   after every action that navigates or swaps content.
2. **Coordinates are CSS pixels from the top-left of the viewport.** That is
   what `click(x, y)` expects, and what `observe` reports.

## What "pierce" buys you

| Situation | Plain `querySelector` | This server |
|---|---|---|
| Web component with a shadow root | Not found | Found, `shadow_depth` set |
| Login or payment inside an iframe | Not found | Found, `in_frame` set |
| Cross-origin iframe | Unreachable from page JS | Found via CDP |
| React handler checking `isTrusted` | Event silently ignored | Event accepted |

The cross-origin case is the one most tools cannot do. A page's own JavaScript
cannot read another origin's document; CDP can, because it asks each frame
directly. That is why Stripe, Google sign-in and reCAPTCHA become reachable.

## How attaching works

This is the part worth reading if you maintain a browser tool.

Chrome 144+ added an approval flow. Once you approve, Chrome writes
`DevToolsActivePort` into its user-data directory:

```
9222
/devtools/browser/85038e3e-77e0-4cdd-9a3d-94d6c41c9d18
```

Three consequences, each of which breaks a common assumption:

**1. HTTP endpoints 404.** `/json/version`, `/json/list` and `/` all return 404
on that port. It is not a normal debug port; it speaks only the approval
protocol. Any client that discovers browsers over HTTP — including zendriver's
`Browser.create` and Puppeteer's `connect` — fails here.

**2. The browser WebSocket works.** Line 2 is a live CDP endpoint. Open it and
`Browser.getVersion` answers.

**3. Page WebSockets are refused.** `/devtools/page/<id>` returns HTTP 403.
Per-page sockets are disabled in approval mode. Everything must go through the
browser socket using **flat sessions**:

```python
Target.attachToTarget { targetId, flatten: true }  ->  sessionId
# then every command carries that sessionId on the same socket
```

`attach.py` implements this directly, without Puppeteer. A `RawTab` wraps the
flat session and exposes a `send()` with the same signature as zendriver's
`Tab.send()`, so `browser.py`'s observe/click/type code runs unchanged against
either a launched browser or an attached one.

One detail that costs an afternoon if you miss it: **CDP domains are enabled per
session, not per browser.** `DOM.getDocument` fails with `-32601` until
`DOM.enable` has run *on that session*. Switching tabs therefore needs a fresh
`Target.attachToTarget` plus `Page.enable` / `DOM.enable` / `Runtime.enable`,
not a reused session id.

## Install

```bash
pip install zendriver mcp
python test_hands.py        # 20 checks against a local fixture
```

`test_hands.py` starts its own HTTP servers on free ports and drives a real
Chrome. It needs a desktop session; headless mode is not exercised.

## Requirements

- Python 3.11+
- Chrome, Edge or Brave on `PATH`
- A desktop session (the browser is visible by default)

## Notes on state

The profile lives at `~/.mcp-hands/profile`, so cookies and logins survive
between runs. Pass `profile_dir` to `start` to use a different one, or set
`HANDS_PROFILE_DIR`.

Screenshots go to the working directory unless `path` is given.

## Limitations

- One browser, one active tab. Tabs can be switched, but only the active one is
  observed and acted on.
- **Attach mode serves one client.** Chrome's approval flow allows a single
  connection. An MCP server holding it blocks every other client.
- `observe` walks the whole pierced tree. On a very large page, pass `text=` to
  filter, or raise `max_nodes`.
- Elements inside a **closed** shadow root (`mode: "closed"`) are not reachable
  by any tool, by design.
- Canvas-rendered UIs (Figma, Google Maps, games) expose no DOM elements to
  observe. Click them by coordinate instead.
- No automatic waiting for network idle beyond the DOM-settle heuristic. If a
  page loads data slowly, call `observe` again.
- Cross-origin iframes are *observed* through CDP, but a script inside the top
  page still cannot read them. Coordinate clicks work; `evaluate` does not cross
  the origin boundary.
