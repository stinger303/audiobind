"""Status page and controls: watch the daemon, pause it, make it scan now."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import CONFIG
from .control import CONTROL
from .state import State

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
 .bar {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:26px; }}
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
 .working {{ margin:-14px 0 24px; font-size:13px; color:#8b93a7; }}
 .working b {{ color:#e6e9ef; font-weight:600; }}
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
</style></head><body>
<h1>Audiobind</h1>
<div class="sub">v{version} &middot; library <code>{library}</code> &rarr; <code>{output}</code>
 &middot; auto: {auto}{dry}</div>

<div class="bar">
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

<h2 style="font-size:14px;color:#8b93a7;font-weight:500;text-transform:uppercase;letter-spacing:.6px">Recent activity</h2>
<table><thead><tr><th>book</th><th>status</th><th>conf</th><th>runtime</th><th>updated</th></tr></thead><tbody>{rows}</tbody></table>

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
setTimeout(() => location.reload(), 10000);
</script>
</body></html>"""


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_age(seconds: float) -> str:
    if seconds == float("inf"):
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def render(state: State) -> str:
    counts = state.counts()
    units = state.all()[:40]

    def css(status: str) -> str:
        if status == "converted":
            return "ok"
        if status in ("ready", "review", "held", "interrupted"):
            return "warn"
        if status == "failed":
            return "err"
        return "dim"

    rows = "".join(
        f"<tr><td>{_html(u.title or u.path)}</td>"
        f"<td class=\"{css(u.status)}\">{u.status}</td>"
        f"<td class=\"dim\">{u.confidence or '-'}</td>"
        f"<td class=\"dim\">{(u.hours or 0):.1f}</td>"
        f"<td class=\"dim\">{_fmt_age(time.time() - (u.updated_at or 0))}</td></tr>"
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
    working = (
        f'<div class="working">converting <b>{_html(CONTROL.current_unit)}</b>'
        f' &middot; stopping discards the partial file and retries it next scan</div>'
        if CONTROL.running else ""
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
        rows=rows or "<tr><td colspan=5 class=dim>nothing yet</td></tr>",
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
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(snapshot(self.state)).encode(), "application/json")
            return
        if self.path.startswith("/healthz"):
            healthy = self.state.heartbeat_age() < CONFIG.scan_interval_min * 60 * 2.5 + 300
            self._send(200 if healthy else 503, b"ok" if healthy else b"stale", "text/plain")
            return
        self._send(200, render(self.state).encode(), "text/html; charset=utf-8")

    def do_POST(self):                      # noqa: N802
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
