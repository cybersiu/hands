"""
fixture_site.py — A local site that reproduces the hostile page shapes.

Real sites are bad test targets: they change, they need logins, they rate-limit
you, and a failure tells you nothing about whether your tool is wrong or the
site is. This server builds the awkward structures deliberately, so a failure
points at the code.

It serves four pages, each modelling a pattern that broke a real attempt:

    /colab     lit-style shadow DOM, slotted dialog buttons, a full-viewport
               scrim that swallows clicks, and a run-all button whose label
               lives in a shadow root.
    /kaggle    deeply nested same-origin iframes, a modal that only appears
               after a delay, and a form inside the third level.
    /yahoo     a frameset-era layout: table markup, no ARIA, links with images
               only, and a cookie banner pinned over the content.
    /canvas    a page whose only interactive surface is a canvas. No DOM
               elements to find; clicks must be positional.

    python fixture_site.py                 # serve on a free port
    python fixture_site.py --port 8080
    python fixture_site.py --port 8080 --open

Each page exposes a small JS API on `window.__test` so a test can assert on
what actually happened rather than on what the DOM looks like:

    window.__test.state()      -> current observable state
    window.__test.reset()      -> back to the initial state
"""

from __future__ import annotations

import argparse
import http.server
import json
import socket
import threading
import webbrowser
from typing import Any


# --------------------------------------------------------------------------
# Shared styling: deliberately plain, so failures are structural not cosmetic
# --------------------------------------------------------------------------

BASE_STYLE = """
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px;
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    color: #202124; background: #fff;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  h2 { font-size: 15px; margin: 20px 0 8px; color: #444; }
  p { margin: 6px 0; line-height: 1.5; }
  button {
    font: inherit; font-size: 14px; padding: 7px 14px;
    border: 1px solid #c4c7c5; border-radius: 6px;
    background: #fff; cursor: pointer;
  }
  button:hover { background: #f1f3f4; }
  button:disabled { opacity: .45; cursor: default; }
  input, textarea {
    font: inherit; font-size: 14px; padding: 8px 10px;
    border: 1px solid #c4c7c5; border-radius: 6px; width: 320px;
  }
  .log {
    margin-top: 18px; padding: 12px;
    background: #f8f9fa; border: 1px solid #e8eaed; border-radius: 6px;
    font-family: ui-monospace, Consolas, monospace; font-size: 12px;
    white-space: pre-wrap; min-height: 56px;
  }
  .hint { color: #5f6368; font-size: 13px; }
  nav { margin-bottom: 20px; }
  nav a {
    display: inline-block; margin-right: 14px; font-size: 13px;
    color: #1a73e8; text-decoration: none;
  }
"""

NAV = """
<nav>
  <a href="/">index</a>
  <a href="/colab">colab</a>
  <a href="/kaggle">kaggle</a>
  <a href="/yahoo">yahoo</a>
  <a href="/canvas">canvas</a>
</nav>
"""


# --------------------------------------------------------------------------
# / — index
# --------------------------------------------------------------------------

INDEX = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>hands fixture</title><style>{BASE_STYLE}</style></head>
<body>
{NAV}
<h1>hands fixture site</h1>
<p class="hint">Four pages, each reproducing a structure that broke a real attempt.</p>

<h2>colab</h2>
<p>lit-style shadow DOM. A full-viewport scrim covers the page until you dismiss
it. The run-all button lives three shadow roots deep. Dialog buttons are slotted
from the light DOM, so a shadow-only search finds the dialog but no way out.</p>

<h2>kaggle</h2>
<p>Three nested same-origin iframes. A modal appears after a delay, so a tool
that checks once at load time will miss it. The real form is in the innermost
frame.</p>

<h2>yahoo</h2>
<p>Frameset-era markup: tables, image-only links, no ARIA, and a cookie banner
pinned over the content. Tests whether observation degrades gracefully when
nothing is semantic.</p>

<h2>canvas</h2>
<p>No DOM elements at all. The only interactive surface is a canvas, so clicks
must be positional. This is the honest limit of DOM-based automation.</p>
</body></html>
"""


# --------------------------------------------------------------------------
# /colab — shadow DOM, scrim, slotted buttons
# --------------------------------------------------------------------------

COLAB = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Fixture Colab</title><style>{BASE_STYLE}
  #toolbar {{
    display: flex; align-items: center; gap: 8px;
    padding: 10px 14px; margin: -24px -24px 20px;
    background: #f8f9fa; border-bottom: 1px solid #e8eaed;
  }}
  #toolbar .spacer {{ flex: 1; }}
  .cell {{
    border: 1px solid #e8eaed; border-radius: 6px;
    margin-bottom: 12px; padding: 12px;
  }}
  .cell.running {{ border-color: #1a73e8; background: #e8f0fe; }}
  .cell.executed {{ border-color: #34a853; }}
  .cell-head {{ display: flex; align-items: center; gap: 10px; }}
  .count {{
    font-family: ui-monospace, monospace; font-size: 12px;
    color: #5f6368; min-width: 28px;
  }}
  .out {{
    margin-top: 8px; font-family: ui-monospace, monospace;
    font-size: 12px; color: #188038; white-space: pre-wrap;
  }}
</style></head>
<body>
{NAV}
<div id="toolbar">
  <span id="status">Ready</span>
  <span class="spacer"></span>
  <span id="runtime-badge">disconnected</span>
</div>

<h1>Fixture notebook</h1>
<p class="hint">Click Run all. Nothing will happen until the blocking dialog is dismissed.</p>

<div id="cells"></div>
<div class="log" id="log">waiting</div>

<script>
// ---------------------------------------------------------------------------
// A shadow-DOM toolbar button, mirroring how Colab renders colab-toolbar-button.
// The visible label lives in the shadow root; the host's innerText is empty.
// ---------------------------------------------------------------------------
class FixtureToolbarButton extends HTMLElement {{
  connectedCallback() {{
    const label = this.getAttribute('label') || 'Button';
    const id = this.getAttribute('data-id') || '';
    const root = this.attachShadow({{ mode: 'open' }});
    root.innerHTML = `
      <style>
        button {{
          font: inherit; font-size: 14px; padding: 7px 14px;
          border: 1px solid #c4c7c5; border-radius: 6px;
          background: #fff; cursor: pointer;
        }}
        button:hover {{ background: #f1f3f4; }}
        button:disabled {{ opacity: .45; cursor: default; }}
      </style>
      <button id="inner" data-id="${{id}}">${{label}}</button>
    `;
    root.getElementById('inner').addEventListener('click', () => {{
      this.dispatchEvent(new CustomEvent('fixture-action', {{
        bubbles: true, composed: true, detail: {{ id }}
      }}));
    }});
  }}
  setDisabled(v) {{
    const b = this.shadowRoot && this.shadowRoot.getElementById('inner');
    if (b) b.disabled = v;
  }}
}}
customElements.define('fixture-toolbar-button', FixtureToolbarButton);

// ---------------------------------------------------------------------------
// A dialog that renders its own scrim, with buttons slotted from light DOM.
// This is the shape that makes clicks silently land on the scrim.
// ---------------------------------------------------------------------------
class FixtureDialog extends HTMLElement {{
  connectedCallback() {{
    const title = this.getAttribute('title') || 'Notice';
    const body = this.getAttribute('body') || '';
    const root = this.attachShadow({{ mode: 'open' }});
    root.innerHTML = `
      <style>
        .scrim {{
          position: fixed; inset: 0; background: rgba(32,33,36,.32);
          z-index: 1000;
        }}
        .box {{
          position: fixed; left: 50%; top: 50%;
          transform: translate(-50%, -50%);
          width: min(460px, 90vw); background: #fff; border-radius: 10px;
          box-shadow: 0 8px 28px rgba(0,0,0,.28); z-index: 1001;
          padding: 22px;
        }}
        h3 {{ margin: 0 0 10px; font-size: 17px; }}
        .content {{ font-size: 14px; line-height: 1.5; color: #444; }}
        .actions {{
          display: flex; justify-content: flex-end; gap: 8px;
          margin-top: 20px;
        }}
        ::slotted(button) {{
          font: inherit; font-size: 14px; padding: 7px 14px;
          border: 1px solid #c4c7c5; border-radius: 6px;
          background: #fff; cursor: pointer;
        }}
        ::slotted(button[slot="primaryAction"]) {{
          background: #1a73e8; color: #fff; border-color: #1a73e8;
        }}
      </style>
      <div class="scrim"></div>
      <div class="box" role="dialog" aria-modal="true">
        <h3>${{title}}</h3>
        <div class="content">${{body}}</div>
        <div class="actions">
          <slot name="secondaryAction"></slot>
          <slot name="primaryAction"></slot>
        </div>
      </div>
    `;
  }}
}}
customElements.define('fixture-dialog', FixtureDialog);

// ---------------------------------------------------------------------------
// Page behaviour
// ---------------------------------------------------------------------------
const CELLS = [
  {{ title: 'pip install', code: 'pip install -q torch transformers' }},
  {{ title: 'download weights', code: 'hf_hub_download("org/model")' }},
  {{ title: 'start server', code: 'llama-server --port 8080' }},
];

const state = {{
  connected: false,
  running: false,
  executed: [null, null, null],
  log: [],
}};

function render() {{
  const host = document.getElementById('cells');
  host.innerHTML = CELLS.map((c, i) => {{
    const cls = ['cell'];
    if (state.running && state.executed[i] === null) cls.push('running');
    if (state.executed[i] !== null) cls.push('executed');
    const count = state.executed[i] === null ? '--' : `[${{state.executed[i]}}]`;
    const out = state.executed[i] === null ? '' :
      `<div class="out">done in ${{state.executed[i]}} step(s)</div>`;
    return `
      <div class="${{cls.join(' ')}}" data-cell="${{i}}">
        <div class="cell-head">
          <span class="count">${{count}}</span>
          <strong>${{c.title}}</strong>
        </div>
        <div class="out" style="color:#5f6368">${{c.code}}</div>
        ${{out}}
      </div>`;
  }}).join('');

  document.getElementById('runtime-badge').textContent =
    state.connected ? 'connected' : 'disconnected';
  document.getElementById('status').textContent =
    state.running ? 'Running' : (state.connected ? 'Ready' : 'Disconnected');
  document.getElementById('log').textContent =
    state.log.length ? state.log.join(' | ') : 'waiting';
}}

function addLog(msg) {{
  state.log.push(msg);
  render();
}}

// Run all: refuses unless connected, which models Colab's real refusal.
function runAll() {{
  if (!state.connected) {{
    addLog('run-all refused: runtime disconnected');
    return;
  }}
  state.running = true;
  render();
  let step = 0;
  const timer = setInterval(() => {{
    step += 1;
    state.executed = state.executed.map((v, i) =>
      v === null && i < step ? step : v);
    if (step >= CELLS.length) {{
      clearInterval(timer);
      state.running = false;
      addLog('run-all finished');
    }}
    render();
  }}, 700);
}}

document.addEventListener('fixture-action', (e) => {{
  const id = e.detail && e.detail.id;
  if (id === 'run-all') runAll();
  if (id === 'connect') {{
    state.connected = true;
    addLog('connected');
  }}
}});

// The blocking dialog. Present from load, exactly like Colab's
// "runtime disconnected" notice.
function showDialog() {{
  if (document.querySelector('fixture-dialog')) return;
  const d = document.createElement('fixture-dialog');
  d.setAttribute('title', 'Runtime disconnected');
  d.setAttribute('body',
    'The runtime is idle or hit its time limit, so the connection was closed. ' +
    'Reconnect to continue running cells.');

  const close = document.createElement('button');
  close.setAttribute('slot', 'secondaryAction');
  close.textContent = 'Close';
  close.addEventListener('click', () => {{ d.remove(); addLog('dialog closed'); }});

  const reconnect = document.createElement('button');
  reconnect.setAttribute('slot', 'primaryAction');
  reconnect.textContent = 'Reconnect';
  reconnect.addEventListener('click', () => {{
    d.remove();
    state.connected = true;
    addLog('reconnected');
  }});

  d.appendChild(close);
  d.appendChild(reconnect);
  document.body.appendChild(d);
}}

// Toolbar
function buildToolbar() {{
  const bar = document.getElementById('toolbar');
  const run = document.createElement('fixture-toolbar-button');
  run.setAttribute('label', 'Run all');
  run.setAttribute('data-id', 'run-all');
  const connect = document.createElement('fixture-toolbar-button');
  connect.setAttribute('label', 'Connect');
  connect.setAttribute('data-id', 'connect');
  bar.insertBefore(run, bar.querySelector('.spacer'));
  bar.insertBefore(connect, bar.querySelector('.spacer'));
}}

window.__test = {{
  state: () => JSON.parse(JSON.stringify(state)),
  reset: () => {{
    state.connected = false;
    state.running = false;
    state.executed = [null, null, null];
    state.log = [];
    const d = document.querySelector('fixture-dialog');
    if (d) d.remove();
    render();
    showDialog();
  }},
  dismiss: () => {{
    const d = document.querySelector('fixture-dialog');
    if (d) d.remove();
  }},
  hasDialog: () => !!document.querySelector('fixture-dialog'),
}};

buildToolbar();
render();
showDialog();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# /kaggle — nested iframes, delayed modal, form in the deepest frame
# --------------------------------------------------------------------------

KAGGLE_FRAME_3 = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>deepest frame</title><style>{BASE_STYLE}
  body {{ padding: 14px; }}
</style></head>
<body>
<h2 style="margin-top:0">Dataset settings</h2>
<p class="hint">This form lives in the third-level iframe.</p>
<form id="f">
  <p><input id="title" placeholder="Dataset title" value=""></p>
  <p><textarea id="desc" rows="2" placeholder="Description"></textarea></p>
  <p><button type="submit" id="save">Save dataset</button></p>
</form>
<div class="log" id="log">idle</div>
<script>
document.getElementById('f').addEventListener('submit', (e) => {{
  e.preventDefault();
  const t = document.getElementById('title').value.trim();
  const d = document.getElementById('desc').value.trim();
  document.getElementById('log').textContent =
    'saved: title=' + JSON.stringify(t) + ' desc=' + JSON.stringify(d);
  // Report upward so the top page can assert without reading cross-frame.
  try {{
    window.top.postMessage({{ kind: 'fixture-saved', title: t, desc: d }}, '*');
  }} catch (err) {{}}
}});
</script>
</body></html>
"""

KAGGLE_FRAME_2 = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>level two</title><style>{BASE_STYLE}
  body {{ padding: 14px; }}
  iframe {{ width: 100%; height: 260px; border: 1px solid #e8eaed; border-radius: 6px; }}
</style></head>
<body>
<p class="hint">Level 2 of 3</p>
<iframe src="/kaggle-inner-3" title="level three"></iframe>
</body></html>
"""

KAGGLE = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Fixture Kaggle</title><style>{BASE_STYLE}
  iframe {{ width: 100%; height: 420px; border: 1px solid #e8eaed; border-radius: 6px; }}
  #late {{
    position: fixed; inset: 0; display: none;
    background: rgba(32,33,36,.32); z-index: 900;
  }}
  #late .box {{
    position: absolute; left: 50%; top: 50%;
    transform: translate(-50%,-50%);
    width: min(420px, 90vw); background: #fff; border-radius: 10px;
    padding: 22px; box-shadow: 0 8px 28px rgba(0,0,0,.28);
  }}
  #late .box h3 {{ margin: 0 0 10px; font-size: 17px; }}
  #late .box p {{ font-size: 14px; color: #444; }}
  #late .row {{ display: flex; gap: 8px; justify-content: flex-end; margin-top: 18px; }}
</style></head>
<body>
{NAV}
<h1>Fixture Kaggle</h1>
<p class="hint">A modal appears 2.5 seconds after load. The real form is three
iframes deep.</p>

<div id="late">
  <div class="box" role="dialog" aria-modal="true">
    <h3>Before you continue</h3>
    <p>This notebook reads a dataset owned by another account. Confirm you have
       permission to use it.</p>
    <div class="row">
      <button id="late-close">Cancel</button>
      <button id="late-ok" style="background:#1a73e8;color:#fff;border-color:#1a73e8">Accept</button>
    </div>
  </div>
</div>

<iframe src="/kaggle-inner-2" title="nested frames"></iframe>
<div class="log" id="log">waiting</div>

<script>
const state = {{ lateShown: false, accepted: false, saved: null }};

window.addEventListener('message', (e) => {{
  if (e.data && e.data.kind === 'fixture-saved') {{
    state.saved = {{ title: e.data.title, desc: e.data.desc }};
    document.getElementById('log').textContent =
      'saved via message: ' + JSON.stringify(state.saved);
  }}
}});

setTimeout(() => {{
  document.getElementById('late').style.display = 'block';
  state.lateShown = true;
}}, 2500);

document.getElementById('late-close').addEventListener('click', () => {{
  document.getElementById('late').style.display = 'none';
  state.accepted = false;
  document.getElementById('log').textContent = 'late modal cancelled';
}});

document.getElementById('late-ok').addEventListener('click', () => {{
  document.getElementById('late').style.display = 'none';
  state.accepted = true;
  document.getElementById('log').textContent = 'late modal accepted';
}});

window.__test = {{
  state: () => JSON.parse(JSON.stringify(state)),
  reset: () => {{
    state.lateShown = false;
    state.accepted = false;
    state.saved = null;
    document.getElementById('late').style.display = 'none';
    document.getElementById('log').textContent = 'waiting';
    setTimeout(() => {{
      document.getElementById('late').style.display = 'block';
      state.lateShown = true;
    }}, 2500);
  }},
  lateVisible: () =>
    document.getElementById('late').style.display === 'block',
}};
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# /yahoo — frameset-era markup, image links, cookie banner
# --------------------------------------------------------------------------

YAHOO = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Fixture Yahoo</title><style>{BASE_STYLE}
  table {{ border-collapse: collapse; width: 100%; }}
  td {{ padding: 6px 10px; border-bottom: 1px solid #eee; font-size: 14px; }}
  .banner {{
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 800;
    background: #202124; color: #fff; padding: 14px 20px;
    display: flex; align-items: center; gap: 16px; font-size: 14px;
  }}
  .banner button {{
    background: #8ab4f8; color: #202124; border: 0;
    padding: 8px 16px; border-radius: 4px; cursor: pointer; font-weight: 600;
  }}
  .imgbtn {{
    display: inline-block; width: 88px; height: 26px;
    background: #1a73e8; border-radius: 3px; cursor: pointer;
    color: #fff; font-size: 12px; text-align: center; line-height: 26px;
    text-decoration: none;
  }}
  .imgbtn.green {{ background: #188038; }}
  .rowhover:hover {{ background: #f8f9fa; }}
</style></head>
<body>
{NAV}
<h1>Fixture Yahoo</h1>
<p class="hint">Table markup, no ARIA, links that are images, and a cookie
banner pinned over the bottom of the page.</p>

<table>
  <tr><td style="width:70px"><a href="#a" class="imgbtn">Buy now</a></td>
      <td>Item A — vintage camera</td><td>$120</td></tr>
  <tr class="rowhover"><td><a href="#b" class="imgbtn green">Bid</a></td>
      <td>Item B — mechanical keyboard</td><td>$45</td></tr>
  <tr><td><a href="#c" class="imgbtn">Buy now</a></td>
      <td>Item C — film scanner</td><td>$210</td></tr>
  <tr class="rowhover"><td><a href="#d" class="imgbtn green">Bid</a></td>
      <td>Item D — desk lamp</td><td>$18</td></tr>
</table>

<div class="log" id="log">waiting</div>

<div class="banner" id="banner" role="dialog" aria-modal="true"
     aria-label="Cookie consent">
  <span>We use cookies to personalise content. By continuing you accept this.</span>
  <button id="accept" slot="primaryAction">Accept all</button>
  <button id="reject" slot="secondaryAction" style="background:#e8eaed">Reject</button>
</div>

<script>
const state = {{ clicks: [], cookies: null }};

document.querySelectorAll('.imgbtn').forEach((el, i) => {{
  el.addEventListener('click', (e) => {{
    e.preventDefault();
    state.clicks.push(i);
    document.getElementById('log').textContent =
      'clicked image link #' + i + ' (' + el.textContent.trim() + ')';
  }});
}});

document.getElementById('accept').addEventListener('click', () => {{
  state.cookies = 'accepted';
  document.getElementById('banner').remove();
  document.getElementById('log').textContent = 'cookies accepted';
}});

document.getElementById('reject').addEventListener('click', () => {{
  state.cookies = 'rejected';
  document.getElementById('banner').remove();
  document.getElementById('log').textContent = 'cookies rejected';
}});

window.__test = {{
  state: () => JSON.parse(JSON.stringify(state)),
  reset: () => {{
    state.clicks = [];
    state.cookies = null;
    document.getElementById('log').textContent = 'waiting';
    if (!document.getElementById('banner')) {{
      location.reload();
    }}
  }},
  bannerVisible: () => !!document.getElementById('banner'),
}};
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# /canvas — no DOM, positional clicks only
# --------------------------------------------------------------------------

CANVAS = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Fixture canvas</title><style>{BASE_STYLE}
  canvas {{
    border: 1px solid #c4c7c5; border-radius: 8px;
    display: block; cursor: crosshair; background: #fff;
  }}
</style></head>
<body>
{NAV}
<h1>Fixture canvas</h1>
<p class="hint">No interactive DOM elements. The three targets below exist only
as pixels, so a click must be positional. This is the honest limit of DOM-based
automation.</p>

<canvas id="c" width="520" height="240"></canvas>
<div class="log" id="log">click a circle</div>

<script>
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');
const targets = [
  {{ x: 100, y: 120, r: 34, label: 'A', hit: false }},
  {{ x: 260, y: 120, r: 34, label: 'B', hit: false }},
  {{ x: 420, y: 120, r: 34, label: 'C', hit: false }},
];
const state = {{ hits: [] }};

function draw() {{
  ctx.clearRect(0, 0, cv.width, cv.height);
  for (const t of targets) {{
    ctx.beginPath();
    ctx.arc(t.x, t.y, t.r, 0, Math.PI * 2);
    ctx.fillStyle = t.hit ? '#34a853' : '#e8f0fe';
    ctx.fill();
    ctx.strokeStyle = t.hit ? '#188038' : '#1a73e8';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.fillStyle = t.hit ? '#fff' : '#1a73e8';
    ctx.font = '600 18px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(t.label, t.x, t.y);
  }}
}}

cv.addEventListener('click', (e) => {{
  const r = cv.getBoundingClientRect();
  // Canvas pixels are not CSS pixels when the element is scaled.
  const x = (e.clientX - r.left) * (cv.width / r.width);
  const y = (e.clientY - r.top) * (cv.height / r.height);
  for (const t of targets) {{
    const dx = x - t.x, dy = y - t.y;
    if (dx * dx + dy * dy <= t.r * t.r) {{
      t.hit = true;
      state.hits.push(t.label);
      document.getElementById('log').textContent =
        'hit ' + t.label + ' at canvas (' + Math.round(x) + ',' + Math.round(y) + ')';
      draw();
      return;
    }}
  }}
  document.getElementById('log').textContent =
    'miss at canvas (' + Math.round(x) + ',' + Math.round(y) + ')';
}});

window.__test = {{
  state: () => JSON.parse(JSON.stringify(state)),
  targets: () => targets.map(t => ({{ label: t.label, x: t.x, y: t.y, r: t.r, hit: t.hit }})),
  reset: () => {{
    targets.forEach(t => (t.hit = false));
    state.hits = [];
    document.getElementById('log').textContent = 'click a circle';
    draw();
  }},
  // CSS-pixel centre of a target, for a positional click.
  centre: (label) => {{
    const t = targets.find(x => x.label === label);
    if (!t) return null;
    const r = cv.getBoundingClientRect();
    return {{
      x: r.left + t.x * (r.width / cv.width),
      y: r.top + t.y * (r.height / cv.height),
    }};
  }},
}};

draw();
</script>
</body></html>
"""


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self.routes.get(path)
        if body is None:
            self.send_error(404, f"No route for {path}")
            return
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        # The fixture must not be cached: tests reload pages constantly.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def build_routes() -> dict[str, str]:
    return {
        "/": INDEX,
        "/colab": COLAB,
        "/kaggle": KAGGLE,
        "/kaggle-inner-2": KAGGLE_FRAME_2,
        "/kaggle-inner-3": KAGGLE_FRAME_3,
        "/yahoo": YAHOO,
        "/canvas": CANVAS,
    }


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int) -> http.server.ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"routes": build_routes()})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--open", action="store_true",
                    help="Open the index in the default browser")
    args = ap.parse_args()

    port = args.port or free_port()
    server = serve(port)
    base = f"http://127.0.0.1:{port}"

    print(f"fixture site listening on {base}")
    print()
    for path, desc in (
        ("/colab", "shadow DOM, scrim, slotted dialog buttons"),
        ("/kaggle", "three nested iframes, delayed modal"),
        ("/yahoo", "table markup, image links, cookie banner"),
        ("/canvas", "no DOM, positional clicks only"),
    ):
        print(f"  {base + path:34} {desc}")
    print()
    print("Each page exposes window.__test.state() for assertions.")
    print("Ctrl-C to stop.")

    if args.open:
        webbrowser.open(base)

    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        print("\nstopping")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
