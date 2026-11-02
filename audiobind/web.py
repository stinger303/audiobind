"""A tiny status page so you can see what the daemon is doing."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import CONFIG
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
 .sub {{ color:#8b93a7; font-size:13px; margin-bottom:24px; }}
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
<div class="sub">v{version} &middot; library <code>{library}</code> &rarr; <code>{output}</code> &middot; auto: {auto}</div>
<div class="grid">
 <div class="card"><span>converted</span><b class="ok">{converted}</b></div>
 <div class="card"><span>review</span><b class="warn">{review}</b></div>
 <div class="card"><span>held</span><b class="warn">{held}</b></div>
 <div class="card"><span>failed</span><b class="err">{failed}</b></div>
 <div class="card"><span>last scan</span><b>{last_scan}</b></div>
</div>
<h2 style="font-size:14px;color:#8b93a7;font-weight:500;text-transform:uppercase;letter-spacing:.6px">Recent activity</h2>
<table><thead><tr><th>book</th><th>status</th><th>conf</th><th>hours</th><th>updated</th></tr></thead><tbody>{rows}</tbody></table>
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
        if status in ("review", "held"):
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
    return PAGE.format(
        version=__version__,
        library=CONFIG.library_root,
        output=CONFIG.output_root,
        auto=CONFIG.auto_convert,
        converted=counts.get("converted", 0),
        review=counts.get("review", 0),
        held=counts.get("held", 0),
        failed=counts.get("failed", 0),
        last_scan=_fmt_age(state.heartbeat_age()),
        rows=rows or "<tr><td colspan=5 class=dim>nothing yet</td></tr>",
    )


def snapshot(state: State) -> dict:
    last = state.last_run() or {}
    return {
        "version": __version__,
        "counts": state.counts(),
        "last_run": last,
        "heartbeat_age_seconds": state.heartbeat_age(),
        "library_root": str(CONFIG.library_root),
        "output_root": str(CONFIG.output_root),
        "auto_convert": CONFIG.auto_convert,
    }


class Handler(BaseHTTPRequestHandler):
    state: State = None                     # set by serve()
    server_version = f"audiobind/{__version__}"

    def do_GET(self):                       # noqa: N802 - http.server API
        if self.path.startswith("/api/state"):
            body = json.dumps(snapshot(self.state)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/healthz"):
            healthy = self.state.heartbeat_age() < CONFIG.scan_interval_min * 60 * 2.5 + 300
            self.send_response(200 if healthy else 503)
            self.end_headers()
            self.wfile.write(b"ok" if healthy else b"stale")
            return
        body = render(self.state).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):           # keep container logs clean
        return


def serve(state: State) -> ThreadingHTTPServer:
    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.web_port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
