"""Status page and controls: watch the daemon, pause it, make it scan now."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import CONFIG
from .control import CONTROL
from .pipeline import process_unit
from .state import State
from .units import discover_units

_log = logging.getLogger("audiobind").info

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind</title>
<style>
 :root {{ color-scheme: dark; }}
 body {{ margin:0; padding:32px; background:#0f1115; color:#e6e9ef;
        font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
 h1 {{ font-size:20px; margin:0 0 4px; letter-spacing:-.2px; }}
 .sub {{ color:#8b93a7; font-size:13px; margin-bottom:22px; }}
 .toolbar {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:26px; }}
 button {{ font:inherit; font-size:14px; font-weight:500; padding:9px 16px; border-radius:9px;
           border:1px solid #2b3240; background:#1b2029; color:#e6e9ef; cursor:pointer; }}
 button:hover {{ background:#232a35; border-color:#3a4353; }}
 button.primary {{ background:#1e4fa0; border-color:#2b5ea8; }}
 button.primary:hover {{ background:#2b5ea8; }}
 button.warn {{ background:#5a4210; border-color:#7a5a16; }}
 button.armed {{ background:#5c1f22; border-color:#7d2a2e; }}
 button.armed:hover {{ background:#7d2a2e; }}
 button.danger {{ background:#3a1416; border-color:#6b2226; color:#ffb4b4; }}
 button.danger:hover {{ background:#6b2226; color:#fff; }}
 .working {{ margin:-14px 0 26px; font-size:13px; color:#8b93a7; }}
 .working b {{ color:#e6e9ef; font-weight:600; }}
 .wline {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:7px; }}
 .pct {{ font-variant-numeric:tabular-nums; font-weight:600; color:#e6e9ef; font-size:13px; }}
 .ptrack {{ height:8px; border-radius:99px; background:#1c2230; overflow:hidden; }}
 .ptrack i {{ display:block; height:100%; border-radius:99px;
              background:linear-gradient(90deg,#2b5ea8,#12a89b); transition:width .6s ease; }}
 .wmeta {{ margin-top:7px; font-size:12px; }}
 button:disabled {{ opacity:.45; cursor:default; }}
 .pill {{ font-size:12px; font-weight:600; letter-spacing:.5px; text-transform:uppercase;
          padding:6px 12px; border-radius:999px; }}
 .pill.run {{ background:#12311f; color:#5fd38d; }}
 .pill.paused {{ background:#3a2a07; color:#e6b450; }}
 .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:12px; margin-bottom:28px; }}
 .card {{ background:#171a21; border:1px solid #232833; border-radius:10px; padding:14px 16px; }}
 .card b {{ display:block; font-size:24px; font-weight:600; }}
 .card span {{ color:#8b93a7; font-size:12px; text-transform:uppercase; letter-spacing:.6px; }}
 table {{ width:100%; border-collapse:collapse; font-size:13px; }}
 th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #232833; }}
 th {{ color:#8b93a7; font-weight:500; text-transform:uppercase; font-size:11px; letter-spacing:.6px; }}
 .ok {{ color:#5fd38d; }} .warn {{ color:#e6b450; }} .err {{ color:#f2686b; }} .dim {{ color:#8b93a7; }}
 tr.urow {{ cursor:pointer; }}
 tr.urow:hover td {{ background:#141821; }}
 tr.urow td:first-child::before {{ content:"+"; display:inline-block; width:13px; margin-right:6px;
                                   color:#7c86a0; font-weight:700; }}
 tr.urow.open td:first-child::before {{ content:"-"; color:#12a89b; }}
 tr.drow td {{ background:#11151d; padding:0; border-bottom:1px solid #232833; }}
 .dd {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:22px; padding:16px 22px 18px; }}
 .acts {{ grid-column:1/-1; display:flex; gap:8px; flex-wrap:wrap; align-items:center;
          border-top:1px solid #1c2230; padding-top:14px; }}
 .acts button {{ font-size:12.5px; padding:6px 12px; }}
 .acts input {{ font:inherit; font-size:12.5px; padding:6px 10px; border-radius:8px;
                border:1px solid #2b3240; background:#0d1118; color:#e6e9ef; }}
 .dd h4 {{ margin:0 0 8px; font-size:11px; letter-spacing:.6px; text-transform:uppercase; color:#8b93a7; font-weight:600; }}
 .dd ul {{ margin:0; padding-left:16px; }}
 .dd li {{ margin:3px 0; color:#c7cddb; }}
 .dd code {{ color:#9fd7c8; }}
 .chips {{ display:flex; gap:8px; flex-wrap:wrap; margin:0 0 14px; }}
 .chip {{ font-size:12.5px; padding:6px 12px; border-radius:999px; text-decoration:none;
          background:#171a21; border:1px solid #232833; color:#c7cddb; }}
 .chip:hover {{ border-color:#3a4353; }}
 .chip.on {{ background:#1e4fa0; border-color:#2b5ea8; color:#fff; }}
 .chip b {{ opacity:.65; font-weight:600; margin-left:5px; }}
</style></head><body>
<h1>Audiobind</h1>
<div class="sub">v{version} &middot; library <code>{library}</code> &rarr; <code>{output}</code>
 &middot; auto: {auto}{dry}</div>

<div class="toolbar">
  <span class="pill {state_class}">{state_text}</span>
  <button class="primary" onclick="act('scan')" {scan_disabled}>Scan now</button>
  {pause_button}
  {stop_button}
  {dry_button}
</div>
{working}

<div class="grid">
 <div class="card"><span>converted</span><b class="ok">{converted}</b></div>
 <div class="card"><span>ready{dry_tag}</span><b class="warn">{ready}</b></div>
 <div class="card"><span>review</span><b class="warn">{review}</b></div>
 <div class="card"><span>held</span><b class="warn">{held}</b></div>
 <div class="card"><span>failed</span><b class="err">{failed}</b></div>
 <div class="card"><span>last scan</span><b>{last_scan}</b></div>
</div>

<h2 style="font-size:14px;color:#8b93a7;font-weight:500;text-transform:uppercase;letter-spacing:.6px">Books</h2>
<div class="chips">{chips}</div>
<table><thead><tr><th>book</th><th>status</th><th>conf</th><th>runtime</th><th>updated</th></tr></thead><tbody>{rows}</tbody></table>
{overflow}

<script>
function act(action) {{
  fetch('/api/control', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                        body: JSON.stringify({{action: action}})}})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 250))
    .catch(() => location.reload());
}}
function stop_now() {{
  if (!confirm('Stop the current conversion? The partial file is discarded and the book is retried later.')) return;
  fetch('/api/control', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                        body: JSON.stringify({{action:'stop'}})}})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 500))
    .catch(() => location.reload());
}}
function dry(on) {{
  fetch('/api/control', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                        body: JSON.stringify({{action:'dry_run', value: on}})}})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 250))
    .catch(() => location.reload());
}}
function unitAction(btn, action) {{
  const panel = btn.closest('.dd');
  const path = panel.dataset.path;
  const body = {{action: action, path: path}};
  if (action === 'set_asin') {{
    const input = panel.querySelector('input[data-asin]');
    body.asin = (input && input.value || '').trim();
    if (!body.asin) {{ input.focus(); return; }}
  }}
  btn.disabled = true;
  fetch('/api/unit', {{method:'POST', headers:{{'Content-Type':'application/json'}},
                     body: JSON.stringify(body)}})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 400))
    .catch(() => {{ btn.disabled = false; }});
}}
const OPEN_KEY = 'audiobind_open';
function openSet() {{
  try {{ return new Set(JSON.parse(sessionStorage.getItem(OPEN_KEY) || '[]')); }}
  catch (e) {{ return new Set(); }}
}}
function saveOpen(set) {{
  try {{ sessionStorage.setItem(OPEN_KEY, JSON.stringify([...set])); }} catch (e) {{}}
}}
function toggleRow(row) {{
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return;
  panel.hidden = !panel.hidden;
  row.classList.toggle('open', !panel.hidden);
  const set = openSet();
  const key = panel.dataset.path || '';
  if (panel.hidden) {{ set.delete(key); }} else {{ set.add(key); }}
  saveOpen(set);
}}
// a refresh must not close what the operator opened
document.addEventListener('DOMContentLoaded', () => {{
  const set = openSet();
  if (!set.size) return;
  document.querySelectorAll('tr.drow').forEach(panel => {{
    if (!set.has(panel.dataset.path)) return;
    panel.hidden = false;
    if (panel.previousElementSibling) panel.previousElementSibling.classList.add('open');
  }});
}});
// poll the encode position so the bar moves smoothly, then reload when the book finishes
function tick() {{
  fetch('/api/state').then(r => r.json()).then(d => {{
    const bar = document.getElementById('prog');
    if (!bar) return;                       // nothing converting here
    if (!d.running) {{ location.reload(); return; }}
    const p = (d.progress && d.progress.percent) || 0;
    bar.style.width = p.toFixed(1) + '%';
    const label = document.getElementById('pct');
    if (label) label.textContent = p.toFixed(1) + '%';
  }}).catch(() => {{}});
}}
setInterval(tick, 2000);

// refresh only when something actually changed, so an open panel is not disrupted
let lastCounts = null;
function watch() {{
  fetch('/api/state').then(r => r.json()).then(d => {{
    const sig = JSON.stringify(d.counts) + '|' + d.running;
    if (lastCounts === null) {{ lastCounts = sig; return; }}
    if (sig !== lastCounts) {{ location.reload(); }}
  }}).catch(() => {{}});
}}
setInterval(watch, 5000);
</script>
</body></html>"""


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _fmt_age(seconds: float) -> str:
    if seconds == float("inf"):
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


STATUS_ORDER = ("converted", "ready", "review", "held", "failed", "interrupted")
MAX_ROWS = 400


def render(state: State, status: str = "") -> str:
    counts = state.counts()
    wanted = status if status in counts else ""
    units = state.all(wanted or None)
    if wanted:
        units.sort(key=lambda u: (u.title or u.path).lower())
    total = sum(counts.values())

    chips = [f'<a class="chip{" on" if not wanted else ""}" href="/">all <b>{total}</b></a>']
    for name in STATUS_ORDER:
        if not counts.get(name):
            continue
        chips.append(
            f'<a class="chip{" on" if wanted == name else ""}" href="/?status={name}">'
            f'{name} <b>{counts[name]}</b></a>'
        )
    chips_html = "".join(chips)
    shown = len(units)
    units = units[:MAX_ROWS]
    overflow = (f'<div class="wmeta">showing the first {MAX_ROWS} of {shown} - '
                f'filter by status or use <code>/api/units?status=</code></div>'
                if shown > MAX_ROWS else "")

    def css(status: str) -> str:
        if status == "converted":
            return "ok"
        if status in ("ready", "review", "held", "interrupted"):
            return "warn"
        if status == "failed":
            return "err"
        return "dim"

    def detail_panel(unit) -> str:
        try:
            data = json.loads(unit.detail or "{}")
        except ValueError:
            data = {}
        src = data.get("source") or {}
        out = data.get("output") or {}
        # rows written before the detail blob existed still carry the flat columns
        if not out and unit.output:
            out = {"path": unit.output, "mb": unit.out_mb, "hours": unit.hours,
                   "chapters": unit.chapters, "chapters_from": ""}

        mb = src.get("mb") or unit.src_mb
        size = f"{mb:.0f} MB &middot; " if mb else ""
        codecs = " + ".join(src.get("codecs") or [])
        before = [
            f"{src.get('parts', 1)} part(s) &middot; {src.get('files') or unit.files} file(s)",
            f"{size}{(src.get('hours') or unit.hours):.2f} h",
            (codecs or "codec not recorded")
            + (f" &middot; {', '.join(src.get('channels') or [])} ch" if src.get("channels") else ""),
            f"<span class=\"dim\"><code>{_html(unit.path)}</code></span>",
        ]
        if out:
            after = [
                f"<code>{_html(Path(out.get('path', '')).name)}</code>",
                f"{(out.get('mb') or 0):.0f} MB &middot; {(out.get('hours') or 0):.2f} h",
                f"{out.get('chapters', 0)} chapters ({_html(str(out.get('chapters_from') or ''))})",
                f"<span class=\"dim\">{_html(str(out.get('path') or ''))}</span>",
            ]
        else:
            after = [f"not converted &mdash; status <b>{unit.status}</b>",
                     f"planned action: {data.get('action') or 'n/a'}"]

        decision = [f"match: <b>{unit.confidence or '-'}</b>",
                    f"asin: {unit.asin or '-'}"]
        if unit.note:
            decision.append(_html(unit.note))

        def block(title, items):
            return (f"<div><h4>{title}</h4><ul>"
                    + "".join(f"<li>{i}</li>" for i in items) + "</ul></div>")

        actions = (
            '<div class="acts">'
            f'<button onclick="unitAction(this,\'retry\')">Retry now</button>'
            f'<button class="warn" onclick="unitAction(this,\'force\')">Convert anyway</button>'
            '<input data-asin placeholder="ASIN, e.g. B005EHN61O" style="width:190px">'
            f'<button onclick="unitAction(this,\'set_asin\')">Use this ASIN</button>'
            "</div>"
        )
        return (f'<tr class="drow" data-path="{_html(unit.path)}" hidden><td colspan="5">'
                f'<div class="dd" data-path="{_html(unit.path)}">'
                + block("Before", before)
                + block("After", after)
                + block("Decision", decision)
                + actions
                + "</div></td></tr>")

    rows = "".join(
        f'<tr class="urow" onclick="toggleRow(this)"><td>{_html(u.title or u.path)}</td>'
        f'<td class="{css(u.status)}">{u.status}</td>'
        f'<td class="dim">{u.confidence or "-"}</td>'
        f'<td class="dim">{(u.hours or 0):.1f}</td>'
        f'<td class="dim">{_fmt_age(time.time() - (u.updated_at or 0))}</td></tr>'
        + detail_panel(u)
        for u in units
    )

    paused = CONTROL.paused
    pause_button = (
        '<button onclick="act(\'resume\')">Resume</button>' if paused
        else '<button class="warn" onclick="act(\'pause\')">Pause after this book</button>'
    )
    dry_button = (
        '<button class="warn" onclick="dry(false)">Dry run: ON &middot; turn off</button>'
        if CONTROL.dry_run
        else '<button class="armed" onclick="dry(true)">Dry run: OFF &middot; turn on</button>'
    )
    # Stop only makes sense while something is actually encoding; pause is for between books
    stop_button = (
        '<button class="danger" onclick="stop_now()">Stop now</button>'
        if CONTROL.running
        else '<button class="danger" disabled>Stop now</button>'
    )
    working = ""
    if CONTROL.running:
        prog = CONTROL.progress or {}
        percent = float(prog.get("percent") or 0)
        meta = []
        if prog.get("speed"):
            meta.append(f"{prog['speed']}x realtime")
        if prog.get("eta"):
            meta.append(f"~{_fmt_duration(prog['eta'])} left")
        if prog.get("elapsed"):
            meta.append(f"{_fmt_duration(prog['elapsed'])} elapsed")
        if not prog:
            meta.append("analysing - the bar appears once encoding starts")
        working = (
            f'<div class="working">'
            f'<div class="wline"><span>converting <b>{_html(CONTROL.current_unit)}</b></span>'
            f'<span class="pct" id="pct">{percent:.1f}%</span></div>'
            f'<div class="ptrack"><i id="prog" style="width:{percent:.1f}%"></i></div>'
            f'<div class="wmeta">{" &middot; ".join(meta)}</div></div>'
        )
    return PAGE.format(
        version=__version__,
        library=CONFIG.library_root,
        output=CONFIG.output_root,
        auto=CONFIG.auto_convert,
        dry=" · dry run" if CONTROL.dry_run else "",
        dry_tag=" (dry run)" if CONTROL.dry_run else "",
        state_class="paused" if paused else "run",
        state_text="paused" if paused else "running",
        scan_disabled="" if not paused else "disabled",
        pause_button=pause_button,
        stop_button=stop_button,
        dry_button=dry_button,
        working=working,
        converted=counts.get("converted", 0),
        ready=counts.get("ready", 0),
        review=counts.get("review", 0),
        held=counts.get("held", 0),
        failed=counts.get("failed", 0),
        last_scan=_fmt_age(state.heartbeat_age()),
        chips=chips_html,
        overflow=overflow,
        rows=rows or '<tr><td colspan=5 class="dim">no books match this filter</td></tr>',
    )


def snapshot(state: State) -> dict:
    return {
        "version": __version__,
        "counts": state.counts(),
        "last_run": state.last_run() or {},
        "heartbeat_age_seconds": state.heartbeat_age(),
        "library_root": str(CONFIG.library_root),
        "output_root": str(CONFIG.output_root),
        "auto_convert": CONFIG.auto_convert,
        "dry_run": CONTROL.dry_run,
        **CONTROL.describe(),
    }


class Handler(BaseHTTPRequestHandler):
    state: State = None                     # set by serve()
    server_version = f"audiobind/{__version__}"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # noqa: N802 - http.server API
        path, _, query = self.path.partition("?")
        params = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
        status = params.get("status", "")
        if path.startswith("/api/units"):
            units = self.state.all(status or None)
            payload = [{"path": u.path, "title": u.title, "author": u.author, "status": u.status,
                        "confidence": u.confidence, "asin": u.asin, "hours": u.hours,
                        "files": u.files, "note": u.note, "output": u.output,
                        "src_mb": u.src_mb, "out_mb": u.out_mb, "chapters": u.chapters}
                       for u in units]
            self._send(200, json.dumps({"count": len(payload), "units": payload}).encode(),
                       "application/json")
            return
        if path.startswith("/api/state"):
            self._send(200, json.dumps(snapshot(self.state)).encode(), "application/json")
            return
        if self.path.startswith("/healthz"):
            healthy = self.state.heartbeat_age() < CONFIG.scan_interval_min * 60 * 2.5 + 300
            self._send(200 if healthy else 503, b"ok" if healthy else b"stale", "text/plain")
            return
        self._send(200, render(self.state, status).encode(), "text/html; charset=utf-8")

    def _handle_unit(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        rel = str(payload.get("path", ""))
        action = str(payload.get("action", "")).lower()
        unit = next((u for u in discover_units(CONFIG.library_root, CONFIG.ignore_globs)
                     if u.rel_path == rel), None)
        if unit is None:
            self._send(404, b'{"error":"no such unit"}', "application/json")
            return

        if action == "set_asin":
            asin = str(payload.get("asin", "")).strip().upper()
            if len(asin) < 8:
                self._send(400, b'{"error":"that is not an ASIN"}', "application/json")
                return
            self.state.set_asin_override(rel, asin)
            action = "retry"
        if action not in ("retry", "force"):
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return

        logging.getLogger("audiobind").info("manual %s requested for %s", action, rel)
        threading.Thread(target=process_unit,
                         kwargs={"state": self.state, "unit": unit, "log": _log,
                                 "force": action == "force"},
                         daemon=True).start()
        self._send(200, json.dumps({"ok": True, "action": action, "unit": rel}).encode(),
                   "application/json")

    def do_POST(self):                      # noqa: N802
        if self.path.startswith("/api/unit"):
            self._handle_unit()
            return
        if not self.path.startswith("/api/control"):
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        action = str(payload.get("action", "")).lower()
        if action == "pause":
            CONTROL.pause()
            self.state.set_meta("paused", True)
        elif action == "resume":
            CONTROL.resume()
            self.state.set_meta("paused", False)
        elif action == "scan":
            CONTROL.request_scan()
        elif action == "stop":
            CONTROL.stop()
            self.state.set_meta("paused", True)
        elif action == "dry_run":
            desired = bool(payload.get("value", not CONTROL.dry_run))
            CONTROL.set_dry_run(desired)
            self.state.set_meta("dry_run", desired)
        else:
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        self._send(200, json.dumps({"ok": True, **CONTROL.describe()}).encode(), "application/json")

    def log_message(self, *args):           # keep container logs clean
        return


def serve(state: State) -> ThreadingHTTPServer:
    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
