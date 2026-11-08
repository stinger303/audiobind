"""Status page and controls: watch the daemon, pause it, make it scan now."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote

from . import __version__
from .advice import explain
from .config import CONFIG
from .control import CONTROL
from .covers import resolve as resolve_cover
from . import overrides as overrides_mod
from .pipeline import process_unit
from .state import State
from .units import discover_units

_log = logging.getLogger("audiobind").info
ICON_PATH = Path(__file__).resolve().parent.parent / "icon.svg"

STYLES = """
:root {
  --ab-bg:#08090a;
  --ab-panel:#0f1011;
  --ab-surface:#191a1b;
  --ab-surface2:#1c1d1f;
  --ab-border:rgba(255,255,255,.08);
  --ab-border-subtle:rgba(255,255,255,.05);
  --ab-text:#f7f8f8;
  --ab-text2:#d0d6e0;
  --ab-text3:#8a8f98;
  --ab-text4:#62666d;
  --ab-accent:#5e6ad2;
  --ab-accent-bright:#7170ff;
  --ab-green:#27a644;
  --ab-green-soft:rgba(39,166,68,.12);
  --ab-amber:#f2a33c;
  --ab-amber-soft:rgba(242,163,60,.12);
  --ab-red:#f24e6b;
  --ab-red-soft:rgba(242,78,107,.12);
  --ab-blue:#4f9cf7;
  --ab-blue-soft:rgba(79,156,247,.12);
  --ab-sans:system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --ab-mono:ui-monospace,'Cascadia Code','Source Code Pro',Menlo,Consolas,'DejaVu Sans Mono',monospace;
  color-scheme:dark;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  font-family:var(--ab-sans);
  font-size:13.5px;line-height:1.5;
  background:var(--ab-bg);color:var(--ab-text);
  display:flex;overflow:hidden;
  -webkit-font-smoothing:antialiased;
}
::selection{background:rgba(94,106,210,.45)}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#23252a;border-radius:6px;border:2px solid var(--ab-bg)}
::-webkit-scrollbar-track{background:transparent}

.ab-rail{width:212px;flex:none;background:var(--ab-panel);
  border-right:1px solid var(--ab-border-subtle);
  display:flex;flex-direction:column;padding:12px 8px;}
.ab-rail-brand{display:flex;align-items:center;gap:8px;padding:8px 10px 16px;
  font-weight:600;font-size:14px;letter-spacing:-.2px;}
.ab-rail-section{margin-top:6px;display:flex;flex-direction:column;gap:5px}
.ab-rail-label{font-size:10px;font-weight:500;color:var(--ab-text4);
  text-transform:uppercase;letter-spacing:.8px;padding:8px 10px 4px;}
.ab-rail-item{display:flex;align-items:center;gap:9px;position:relative;
  padding:0 10px;height:34px;color:var(--ab-text3);text-decoration:none;
  border-radius:6px;font-size:13px;font-weight:500;border:1px solid transparent;
  transition:background .12s,color .12s;}
.ab-rail-item:hover{background:rgba(255,255,255,.04);color:var(--ab-text2)}
.ab-rail-item:focus-visible{outline:none;border-color:var(--ab-accent-bright)}
.ab-rail-item.ab-active{background:rgba(94,106,210,.14);color:var(--ab-text)}
.ab-rail-item .ab-glyph{width:16px;text-align:center;color:var(--ab-text4);font-size:12px}
.ab-rail-item.ab-active .ab-glyph{color:var(--ab-accent-bright)}
.ab-rail-item .ab-count{margin-left:auto;font-family:var(--ab-mono);font-size:11px;
  color:var(--ab-text4);background:rgba(255,255,255,.04);
  border:1px solid var(--ab-border-subtle);border-radius:4px;
  padding:0 5px;min-width:20px;text-align:center;}
.ab-rail-item.ab-alert .ab-count{color:var(--ab-amber);border-color:rgba(242,163,60,.25)}
.ab-rail-spacer{flex:1}
.ab-rail-foot{padding:10px;font-family:var(--ab-mono);font-size:11px;
  color:var(--ab-text4);line-height:1.7;}
.ab-rail-foot b{color:var(--ab-text3);font-weight:500}

.ab-main{flex:1;min-width:0;display:flex;flex-direction:column;overflow-y:auto}

.ab-topbar{display:flex;align-items:center;gap:14px;
  padding:14px 24px 12px;border-bottom:1px solid var(--ab-border-subtle);flex:none;}
.ab-topbar h1{font-size:16px;font-weight:600;letter-spacing:-.2px}
.ab-status{display:inline-flex;align-items:center;gap:6px;
  font-size:12px;font-weight:500;color:var(--ab-text2);
  padding:3px 9px 3px 7px;border-radius:999px;
  background:var(--ab-green-soft);border:1px solid rgba(39,166,68,.28);}
.ab-status.ab-stale{background:var(--ab-amber-soft);border-color:rgba(242,163,60,.28);color:var(--ab-amber)}
.ab-status.ab-stale .ab-dot{background:var(--ab-amber);box-shadow:0 0 6px rgba(242,163,60,.7)}
.ab-status .ab-dot{width:6px;height:6px;border-radius:50%;
  background:var(--ab-green);box-shadow:0 0 6px rgba(39,166,68,.7)}
.ab-status small{font-weight:400;color:var(--ab-text3)}
.ab-version{font-family:var(--ab-mono);font-size:11px;color:var(--ab-text3);
  background:rgba(255,255,255,.04);border:1px solid var(--ab-border-subtle);
  border-radius:4px;padding:2px 7px;}
.ab-topbar .ab-right{margin-left:auto;display:flex;align-items:center;gap:12px}

.ab-controls{flex:none;display:flex;align-items:center;gap:8px;padding:12px 24px 4px;}
.ab-controls .ab-spacer{flex:1}
.ab-controls .ab-note{font-size:11.5px;color:var(--ab-text4)}

button, .btn{display:inline-flex;align-items:center;gap:6px;position:relative;
  height:30px;padding:0 12px;background:rgba(255,255,255,.02);
  color:var(--ab-text2);border:1px solid rgba(255,255,255,.08);
  border-radius:6px;font:inherit;font-size:12.5px;font-weight:500;
  cursor:pointer;white-space:nowrap;
  transition:background .12s,border-color .12s,color .12s;}
button:hover, .btn:hover{background:rgba(255,255,255,.05);color:var(--ab-text)}
button:focus-visible, .btn:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:1px}
button.primary, .btn.primary{background:var(--ab-accent);border-color:transparent;color:#fff}
button.primary:hover, .btn.primary:hover{background:#828fff}
button.danger, .btn.danger{color:var(--ab-red)}
button.danger:hover, .btn.danger:hover{background:var(--ab-red-soft);border-color:rgba(242,78,107,.3)}
button:disabled, .btn:disabled{opacity:.38;cursor:not-allowed}
button:disabled:hover, .btn:disabled:hover{background:rgba(255,255,255,.02);color:var(--ab-text2)}
button.primary:disabled:hover, .btn.primary:disabled:hover{background:var(--ab-accent)}
button.warn{color:var(--ab-amber)}
button.warn:hover{background:var(--ab-amber-soft);border-color:rgba(242,163,60,.3)}
button.dry{border-color:rgba(242,163,60,.35);color:var(--ab-amber);background:var(--ab-amber-soft)}
button.dry.on{border-color:rgba(242,163,60,.55);color:var(--ab-amber);background:var(--ab-amber-soft)}
button.dry .ab-switch{width:22px;height:12px;border-radius:6px;background:rgba(242,163,60,.4);position:relative;flex:none}
button.dry .ab-switch::after{content:"";position:absolute;top:2px;left:2px;width:8px;height:8px;border-radius:50%;background:var(--ab-amber);transition:left .15s}
button.dry.on .ab-switch::after{left:12px}

.ab-queue{flex:none;margin:12px 24px 0;
  background:var(--ab-surface);border:1px solid var(--ab-border);
  border-radius:10px;padding:12px 16px;
  display:grid;grid-template-columns:1fr auto;gap:4px 16px;align-items:center;}
.ab-queue .ab-label{grid-column:1/-1;font-size:10px;font-weight:500;
  text-transform:uppercase;letter-spacing:.8px;color:var(--ab-text4);
  display:flex;align-items:center;gap:6px;}
.ab-queue .ab-pulse{width:6px;height:6px;border-radius:50%;
  background:var(--ab-accent-bright);animation:ab-pulse 1.6s ease-in-out infinite}
@keyframes ab-pulse{0%,100%{opacity:1}50%{opacity:.25}}
.ab-queue .ab-qtitle{font-size:13.5px;font-weight:600;letter-spacing:-.1px}
.ab-queue .ab-qmeta{display:flex;align-items:center;gap:14px;
  font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text3)}
.ab-queue .ab-qmeta b{color:var(--ab-text2);font-weight:500}
.ab-queue .ab-qact{display:flex;gap:8px}
.ab-queue .ab-qbarwrap{grid-column:1/-1;display:flex;align-items:center;gap:14px}
.ab-queue .ab-qbar{flex:1;height:6px;border-radius:4px;background:rgba(255,255,255,.06);overflow:hidden}
.ab-queue .ab-qbar i{display:block;height:100%;
  background:linear-gradient(90deg,var(--ab-accent),var(--ab-accent-bright));border-radius:4px}
.ab-queue .ab-qpct{font-family:var(--ab-mono);font-size:11.5px;
  color:var(--ab-accent-bright);font-weight:500;min-width:36px}

.ab-shelf{flex:none;margin:12px 24px 0;
  background:var(--ab-surface);border:1px solid var(--ab-border);border-radius:10px;}
.ab-shelf-head{display:flex;align-items:center;gap:10px;padding:10px 14px;
  cursor:pointer;user-select:none;}
.ab-shelf-head:hover{background:rgba(255,255,255,.03)}
.ab-shelf-head h2{font-size:10.5px;font-weight:500;text-transform:uppercase;
  letter-spacing:.7px;color:var(--ab-text4);}
.ab-shelf-head .ab-scount{font-family:var(--ab-mono);font-size:11px;color:var(--ab-amber);
  background:var(--ab-amber-soft);border:1px solid rgba(242,163,60,.28);
  border-radius:4px;padding:0 6px;min-width:20px;text-align:center;}
.ab-shelf-head .ab-shint{font-size:11.5px;color:var(--ab-text4)}
.ab-shelf-head .ab-caret{margin-left:auto;color:var(--ab-text4);font-size:10px;
  transition:transform .15s;flex:none;}
.ab-shelf.ab-collapsed .ab-caret{transform:rotate(-90deg)}
.ab-shelf.ab-collapsed .ab-shelf-body{display:none}
.ab-shelf-body{display:flex;gap:10px;overflow-x:auto;
  padding:12px 14px;border-top:1px solid var(--ab-border-subtle);scrollbar-width:thin;}
.ab-shelf-empty{display:none;align-items:center;gap:8px;
  padding:14px;font-size:12.5px;color:var(--ab-text3);}
.ab-shelf-empty .ab-sdot{width:6px;height:6px;border-radius:50%;background:var(--ab-green);flex:none}
.ab-problem{flex:0 0 300px;display:flex;flex-direction:column;gap:8px;
  background:var(--ab-surface2);border:1px solid var(--ab-border);
  border-radius:8px;padding:10px;}
.ab-problem-head{display:flex;align-items:center;gap:8px;min-width:0}
.ab-problem-head .pill{margin-left:auto;flex:none}
.ab-problem .ab-pbook{display:flex;flex-direction:column;gap:1px;min-width:0}
.ab-problem .ab-ptitle{font-weight:500;font-size:13px;color:var(--ab-text);
  letter-spacing:-.1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ab-problem .ab-pauthor{font-size:11px;color:var(--ab-text3)}
.ab-problem .ab-preason{font-size:11.5px;color:var(--ab-text3);line-height:1.4}
.ab-problem .ab-preason b{font-family:var(--ab-mono);font-size:11px;font-weight:500;color:var(--ab-text2)}
.ab-problem .ab-preason.held{color:var(--ab-amber)}
.ab-problem .ab-preason.failed{color:var(--ab-red)}
.ab-problem .ab-pactions{display:flex;gap:6px;margin-top:auto;flex-wrap:wrap}
.ab-problem .ab-pactions button{height:26px;padding:0 9px;font-size:11.5px;border-radius:5px}

.ab-statsline{flex:none;display:flex;align-items:center;gap:0;
  padding:10px 24px 12px;border-bottom:1px solid var(--ab-border-subtle);
  font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text3);}
.ab-stat{display:flex;align-items:baseline;gap:7px;padding:0 18px}
.ab-stat:first-child{padding-left:0}
.ab-stat+.ab-stat{border-left:1px solid var(--ab-border-subtle)}
.ab-stat b{color:var(--ab-text);font-weight:500;font-size:13px}
.ab-stat .ab-unit{color:var(--ab-text4)}
.ab-stat label{color:var(--ab-text4);font-family:var(--ab-sans);font-size:11px}
.ab-libprog{display:flex;align-items:center;gap:10px;margin-left:18px;
  padding-left:18px;border-left:1px solid var(--ab-border-subtle)}
.ab-libprog .ab-libbar{width:120px;height:4px;border-radius:3px;
  background:rgba(255,255,255,.06);overflow:hidden}
.ab-libprog .ab-libbar i{display:block;height:100%;
  background:var(--ab-accent-bright);border-radius:3px}
.ab-libprog .ab-libnum{font-family:var(--ab-mono);font-size:11px;color:var(--ab-text2)}

.ab-toolbar{flex:none;display:flex;align-items:center;gap:14px;padding:14px 24px 10px;}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;position:relative;
  height:28px;padding:0 10px;background:transparent;color:var(--ab-text3);
  border:1px solid rgba(255,255,255,.08);border-radius:999px;
  font:inherit;font-size:12px;font-weight:500;cursor:pointer;
  transition:background .12s,color .12s,border-color .12s;text-decoration:none;}
.chip:hover{background:rgba(255,255,255,.04);color:var(--ab-text2)}
.chip:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:1px}
.chip .n{font-family:var(--ab-mono);font-size:11px;color:var(--ab-text4)}
.chip.on, .chip.active{background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.16);color:var(--ab-text)}
.chip.on .n, .chip.active .n{color:var(--ab-text2)}

.ab-searchwrap{margin-left:auto;display:flex;align-items:center;gap:8px}
.ab-search{font:inherit;font-size:12.5px;height:30px;width:240px;
  padding:0 10px;background:rgba(255,255,255,.02);
  border:1px solid rgba(255,255,255,.08);border-radius:6px;
  color:var(--ab-text);transition:border-color .12s,background .12s;}
.ab-search::placeholder{color:var(--ab-text4)}
.ab-search:focus{outline:none;border-color:var(--ab-accent-bright);background:rgba(255,255,255,.04)}
.ab-clear{font-size:12px;padding:0 10px;height:28px;}
.ab-shown{font-size:12px;color:var(--ab-text4);font-variant-numeric:tabular-nums;white-space:nowrap;}

.ab-tablewrap{flex:1;padding:0 24px 24px}
.ab-tbl{width:100%;border-collapse:collapse}
.ab-tbl thead th{position:sticky;top:0;background:var(--ab-bg);z-index:2;cursor:pointer;user-select:none;white-space:nowrap;
  text-align:left;font-size:10.5px;font-weight:500;text-transform:uppercase;letter-spacing:.7px;
  color:var(--ab-text4);padding:6px 10px;border-bottom:1px solid var(--ab-border);}
.ab-tbl thead th:hover{color:var(--ab-text2)}
.ab-tbl thead th.ab-on{color:var(--ab-text2)}
.ab-tbl tbody td{padding:0 10px;border-bottom:1px solid var(--ab-border-subtle);vertical-align:middle;white-space:nowrap;}
.ab-tbl tbody tr{height:auto;min-height:44px;transition:background .1s}
.ab-tbl tbody tr:hover{background:rgba(255,255,255,.025)}
.ab-tbl tbody tr.ab-sel{background:rgba(94,106,210,.08)}
.ab-covcell{padding:0 6px 0 10px!important}
.ab-covwrap{position:relative;width:30px;height:30px;border-radius:6px;
  background:var(--ab-surface);border:1px solid rgba(255,255,255,.12);overflow:hidden;}
.ab-covwrap::after{content:"\\266B";position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;color:var(--ab-text4);font-size:12px;}
.ab-thumb{position:relative;z-index:1;display:block;width:100%;height:100%;
  object-fit:cover;background:var(--ab-surface);}
.ab-thumb.ab-gone{display:none}
td.ab-bookcell{display:flex;flex-direction:column;gap:1px;min-width:0}
.ab-bookcell .ab-btitle{font-weight:500;font-size:13px;color:var(--ab-text);letter-spacing:-.1px}
.ab-bookcell .ab-bauthor{font-size:11.5px;color:var(--ab-text3)}
.ab-bookcell .ab-breason{font-size:11.5px;color:var(--ab-amber);margin-top:2px;
  white-space:normal;max-width:420px;line-height:1.35;}
.ab-bookcell .ab-breason b{font-weight:500;font-family:var(--ab-mono);font-size:11px;color:var(--ab-text2)}
.ab-bookcell .ab-breason.failed{color:var(--ab-red)}

.pill{display:inline-flex;align-items:center;gap:6px;
  height:22px;padding:0 9px;border-radius:999px;
  font-size:11px;font-weight:500;border:1px solid transparent;}
.pill .pdot{width:5px;height:5px;border-radius:50%;flex:none}
.pill.converted, .pill.ok{color:#7fd99a;background:var(--ab-green-soft);border-color:rgba(39,166,68,.25)}
.pill.converted .pdot, .pill.ok .pdot{background:var(--ab-green)}
.pill.encoding{color:#a9b4ff;background:rgba(94,106,210,.14);border-color:rgba(94,106,210,.35)}
.pill.encoding .pdot{background:var(--ab-accent-bright)}
.pill.held{color:var(--ab-amber);background:var(--ab-amber-soft);border-color:rgba(242,163,60,.28)}
.pill.held .pdot{background:var(--ab-amber)}
.pill.review, .pill.ready, .pill.interrupted{color:var(--ab-text2);background:rgba(255,255,255,.05);border-color:rgba(255,255,255,.12)}
.pill.review .pdot, .pill.ready .pdot, .pill.interrupted .pdot{background:var(--ab-text4)}
.pill.failed{color:var(--ab-red);background:var(--ab-red-soft);border-color:rgba(242,78,107,.28)}
.pill.failed .pdot{background:var(--ab-red)}

.ok{color:#7fd99a}.warn{color:var(--ab-amber)}.err{color:var(--ab-red)}.dim{color:var(--ab-text3)}

.ab-tier{font-family:var(--ab-mono);font-size:11px;font-weight:500}
.ab-tier.ab-high{color:var(--ab-green)}
.ab-tier.ab-medium{color:var(--ab-text3)}
.ab-tier.ab-low{color:var(--ab-text4)}

.ab-num{font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text2)}
.ab-scan{font-family:var(--ab-mono);font-size:11px;color:var(--ab-text4)}

.ab-rowact{display:flex;gap:6px;position:absolute;right:14px;top:50%;transform:translateY(-50%);z-index:3;
  background:var(--ab-bg);padding:4px 10px;border-radius:6px;
  box-shadow:-12px 0 18px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .1s}
tr:hover .ab-rowact, tr:focus-within .ab-rowact{opacity:1;pointer-events:auto}
.ab-rowact button{height:26px;padding:0 9px;font-size:11.5px;border-radius:5px}
.ab-rowact button.warn{color:var(--ab-amber)}
.ab-rowact button.warn:hover{background:var(--ab-amber-soft);border-color:rgba(242,163,60,.3)}
.ab-actcol{padding:0!important;width:0;position:relative;overflow:visible}

tr.drow td{background:#11151d;padding:0;border-bottom:1px solid var(--ab-border);}
tr.drow[hidden] td{padding:0}
.dd{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:22px;padding:16px 22px 18px;}
.explain{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:22px;
  background:#141a24;border:1px solid #232b38;border-radius:10px;padding:14px 16px;}
.explain h4{margin:0 0 6px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
  color:var(--ab-text3);font-weight:600;}
.explain p{margin:0;color:#d7dce7;}
.explain .todo h4{color:var(--ab-amber);}
.acts{grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  border-top:1px solid #1c2230;padding-top:14px;}
.acts button{font-size:12.5px;padding:6px 12px;}
.acts input{font:inherit;font-size:12.5px;padding:6px 10px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);}
.dd h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
  color:var(--ab-text3);font-weight:600;}
.dd ul{margin:0;padding-left:16px;}
.dd li{margin:3px 0;color:#c7cddb;}
.dd code{color:#9fd7c8;}

.ab-covbigwrap{position:relative;width:150px;height:150px;border-radius:8px;
  background:#161a22;border:1px solid #232833;overflow:hidden;}
.ab-covbigwrap::after{content:"\\266B";position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;color:#394152;font-size:40px;}
.ab-covbigwrap .ab-thumb{position:relative;z-index:1;display:block;width:100%;height:100%;object-fit:cover;background:#161a22;}
.ab-covpanel{display:flex;align-items:flex-start;}
.ab-edit{grid-column:1/-1;border-top:1px solid #1c2230;padding-top:14px;}
.ab-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 18px;}
.ab-field{display:flex;flex-direction:column;gap:4px;}
.ab-lab{font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--ab-text3);
  display:flex;align-items:center;gap:6px;}
.ab-in{font:inherit;font-size:12.5px;padding:7px 10px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);width:100%;box-sizing:border-box;}
.ab-in:focus{outline:none;border-color:#2b5ea8;}
.ab-in::placeholder{color:#5d6479;font-style:italic;}
.ab-editbar{display:flex;align-items:center;gap:10px;margin-top:12px;}
.ab-save{font-size:12.5px;padding:6px 12px;background:#1e4fa0;border-color:#2b5ea8;}
.ab-fb{font-size:12.5px;color:var(--ab-text3);}
.ab-fb.ab-good{color:#5fd38d;}
.ab-fb.ab-bad{color:#f2686b;}
.ab-custom{font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
  color:#12a89b;background:#10302c;border:1px solid #1b4a44;border-radius:999px;
  padding:1px 7px;margin-left:8px;vertical-align:1px;}
.ab-custom[hidden]{display:none;}
.ab-hint{color:var(--ab-text3);font-size:11.5px;margin:2px 0 10px;}
.ab-progsub{font-size:12px;color:var(--ab-text3);text-transform:none;letter-spacing:0;}
.ab-statbar{height:8px;border-radius:99px;background:#1c2230;overflow:hidden;}
.ab-statfill{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#2b5ea8,#12a89b);}
.ab-empty td{color:var(--ab-text3);padding:14px 10px;}
.ab-overflow{font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text4);padding:8px 24px;flex:none;}
#ab-toast{position:fixed;right:18px;bottom:18px;z-index:50;max-width:420px;
  background:var(--ab-surface2);border:1px solid rgba(242,163,60,.4);border-radius:8px;
  padding:10px 14px;font-size:12.5px;color:var(--ab-amber);
  opacity:0;transform:translateY(6px);pointer-events:none;
  transition:opacity .15s,transform .15s;}
#ab-toast.on{opacity:1;transform:none}

@media(max-width:1080px){.ab-rail{display:none}}
@media(max-width:820px){.ab-hidemd{display:none}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

SCRIPT = """
function act(action) {
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action: action})})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 250))
    .catch(() => location.reload());
}
function stop_now() {
  if (!confirm('Stop the current conversion? The partial file is discarded and the book is retried later.')) return;
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action:'stop'})})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 500))
    .catch(() => location.reload());
}
function dry(on) {
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action:'dry_run', value: on})})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 250))
    .catch(() => location.reload());
}
function unitAction(btn, action) {
  const panel = btn.closest('.dd');
  const path = panel.dataset.path;
  const body = {action: action, path: path};
  let note = 'queued - re-checking\u2026';
  if (action === 'set_asin') {
    const input = panel.querySelector('input[data-asin]');
    body.asin = (input && input.value || '').trim();
    if (!body.asin) { input.focus(); return; }
    note = 'pinning ' + body.asin + ' - re-checking\u2026';
  } else if (action === 'force') {
    note = 'queued - converting\u2026';
  }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'working\u2026';
  abToast(note);
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify(body)})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 700))
    .catch(() => { btn.disabled = false; btn.textContent = label; });
}
function rowAction(btn, action) {
  const el = btn.closest('[data-path]');
  if (!el) return;
  const path = el.dataset.path;
  const body = {action: action, path: path};
  let note = 'queued - re-checking\u2026';
  if (action === 'set_asin') {
    const input = el.querySelector('input[data-asin]');
    body.asin = (input && input.value || '').trim();
    if (!body.asin) { input.focus(); return; }
    note = 'pinning ' + body.asin + ' - re-checking\u2026';
  } else if (action === 'force') {
    note = 'queued - converting\u2026';
  }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'working\u2026';
  abToast(note);
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify(body)})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 700))
    .catch(() => { btn.disabled = false; btn.textContent = label; });
}
function abToast(text) {
  const el = document.getElementById('ab-toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(abToast.t);
  abToast.t = setTimeout(() => el.classList.remove('on'), 5000);
}
function abCoverFail(img) {
  img.classList.add('ab-gone');
}
function abSaveMeta(btn) {
  const panel = btn.closest('.ab-edit');
  const path = panel.dataset.path;
  const feedback = panel.querySelector('.ab-fb');
  const fields = {};
  panel.querySelectorAll('input.ab-in').forEach(input => {
    fields[input.dataset.field] = input.value.trim();
  });
  btn.disabled = true;
  feedback.className = 'ab-fb';
  feedback.textContent = 'saving...';
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({action:'set_meta', path: path, fields: fields})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      btn.disabled = false;
      if (!res.ok) {
        feedback.className = 'ab-fb ab-bad';
        feedback.textContent = res.body.error || 'could not save';
        return;
      }
      feedback.className = 'ab-fb ab-good';
      feedback.textContent = res.body.custom.length
        ? 'saved - applied on the next conversion'
        : 'saved - all fields back to the catalog';
      abMarkCustom(panel, res.body.custom);
      setTimeout(() => { feedback.textContent = ''; feedback.className = 'ab-fb'; }, 6000);
    })
    .catch(() => {
      btn.disabled = false;
      feedback.className = 'ab-fb ab-bad';
      feedback.textContent = 'could not reach the daemon';
    });
}
function abMarkCustom(panel, custom) {
  panel.querySelectorAll('.ab-lab .ab-custom').forEach(mark => {
    mark.hidden = custom.indexOf(mark.dataset.field) === -1;
  });
  const row = [...document.querySelectorAll('tr.urow')]
    .find(r => r.dataset.path === panel.dataset.path);
  const badge = row && row.querySelector('.ab-custom');
  if (badge) badge.hidden = custom.length === 0;
}
const OPEN_KEY = 'audiobind_open';
function openSet() {
  try { return new Set(JSON.parse(sessionStorage.getItem(OPEN_KEY) || '[]')); }
  catch (e) { return new Set(); }
}
function saveOpen(set) {
  try { sessionStorage.setItem(OPEN_KEY, JSON.stringify([...set])); } catch (e) {}
}
function toggleRow(row) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return;
  panel.hidden = !panel.hidden;
  row.classList.toggle('ab-sel', !panel.hidden);
  const set = openSet();
  const key = panel.dataset.path || '';
  if (panel.hidden) { set.delete(key); } else { set.add(key); }
  saveOpen(set);
}
document.addEventListener('DOMContentLoaded', () => {
  const set = openSet();
  if (!set.size) return;
  document.querySelectorAll('tr.drow').forEach(panel => {
    if (!set.has(panel.dataset.path)) return;
    panel.hidden = false;
    if (panel.previousElementSibling) panel.previousElementSibling.classList.add('ab-sel');
  });
});
const SEARCH_KEY = 'audiobind_query';
const AB_NUMERIC = {status: true, tier: true, runtime: true, scan: true, title: false};
let abSort = {column: window.AB_SORT_COLUMN || 'scan', dir: window.AB_SORT_DIR || 'desc'};
function abSearchBox() { return document.getElementById('ab-search'); }
function abRowMatches(row, terms) {
  if (!terms.length) return true;
  const hay = (row.dataset.search || '').toLowerCase();
  return terms.every(t => hay.indexOf(t) !== -1);
}
function abCompare(a, b, column, dir) {
  const key = 'sort' + column.charAt(0).toUpperCase() + column.slice(1);
  let x = a.dataset[key], y = b.dataset[key];
  let result;
  if (AB_NUMERIC[column]) {
    result = (parseFloat(x) || 0) - (parseFloat(y) || 0);
  } else {
    result = String(x || '').localeCompare(String(y || ''));
  }
  if (result === 0) {
    result = String(a.dataset.sortTitle || '').localeCompare(String(b.dataset.sortTitle || ''));
    return result;
  }
  return dir === 'desc' ? -result : result;
}
function abApplyView() {
  const body = document.getElementById('ab-body');
  if (!body) return;
  const box = abSearchBox();
  const terms = ((box && box.value) || '').trim().toLowerCase().split(/\\s+/).filter(Boolean);
  const rows = [...body.querySelectorAll('tr.urow')];
  const pairs = rows.map(row => {
    const panel = row.nextElementSibling;
    return [row, (panel && panel.classList.contains('drow')) ? panel : null];
  });
  let shown = 0;
  pairs.forEach(([row, panel]) => {
    const hit = abRowMatches(row, terms);
    row.hidden = !hit;
    if (panel) panel.hidden = !hit ? true : panel.hidden;
    if (hit) shown += 1;
  });
  pairs.sort((p, q) => abCompare(p[0], q[0], abSort.column, abSort.dir));
  pairs.forEach(([row, panel]) => {
    body.appendChild(row);
    if (panel) body.appendChild(panel);
  });
  const open = openSet();
  pairs.forEach(([row, panel]) => {
    if (!panel) return;
    const wanted = open.has(panel.dataset.path) && !row.hidden;
    panel.hidden = !wanted;
    row.classList.toggle('ab-sel', wanted);
  });
  const label = document.getElementById('ab-shown');
  if (label) {
    label.textContent = terms.length ? (shown + ' of ' + pairs.length + ' shown')
                                     : (pairs.length + ' book' + (pairs.length === 1 ? '' : 's'));
  }
  const empty = document.getElementById('ab-empty');
  if (empty) empty.hidden = shown !== 0 || pairs.length === 0;
  document.querySelectorAll('th.ab-sort').forEach(th => {
    const on = th.dataset.column === abSort.column;
    th.classList.toggle('ab-on', on);
    const arrow = th.querySelector('.ab-arrow');
    if (arrow) arrow.textContent = (on && abSort.dir === 'desc') ? '\\u25bc' : '\\u25b2';
  });
}
function abSortBy(column) {
  if (abSort.column === column) {
    abSort.dir = abSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    abSort.column = column;
    abSort.dir = (column === 'scan' || column === 'runtime') ? 'desc' : 'asc';
  }
  abApplyView();
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action:'sort', column: abSort.column,
                                              direction: abSort.dir})}).catch(() => {});
}
function abSearchChanged() {
  const box = abSearchBox();
  try { sessionStorage.setItem(SEARCH_KEY, (box && box.value) || ''); } catch (e) {}
  abApplyView();
}
function abClearSearch() {
  const box = abSearchBox();
  if (box) { box.value = ''; box.focus(); }
  abSearchChanged();
}
document.addEventListener('DOMContentLoaded', () => {
  const box = abSearchBox();
  if (box) {
    try { box.value = sessionStorage.getItem(SEARCH_KEY) || ''; } catch (e) {}
    box.addEventListener('input', abSearchChanged);
  }
  document.querySelectorAll('th.ab-sort').forEach(th => {
    th.addEventListener('click', () => abSortBy(th.dataset.column));
  });
  abApplyView();
});
const SHELF_KEY = 'audiobind_shelf_collapsed';
function abShelfInit() {
  const shelf = document.getElementById('ab-shelf');
  if (!shelf) return;
  const head = document.getElementById('ab-shelf-head');
  if (!head) return;
  let collapsed = false;
  try { collapsed = sessionStorage.getItem(SHELF_KEY) === '1'; } catch (e) {}
  if (collapsed) shelf.classList.add('ab-collapsed');
  head.addEventListener('click', () => {
    shelf.classList.toggle('ab-collapsed');
    try { sessionStorage.setItem(SHELF_KEY, shelf.classList.contains('ab-collapsed') ? '1' : '0'); }
    catch (e) {}
  });
}
document.addEventListener('DOMContentLoaded', abShelfInit);
function abBusy() {
  const box = abSearchBox();
  if (box && (document.activeElement === box || box.value.trim() !== '')) return true;
  if (document.querySelector('tr.drow:not([hidden])')) return true;
  const el = document.activeElement;
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) return true;
  return false;
}
function tick() {
  fetch('/api/state').then(r => r.json()).then(d => {
    const bar = document.getElementById('ab-qfill');
    if (!bar) return;
    if (!d.running) { if (!abBusy()) location.reload(); return; }
    const p = (d.progress && d.progress.percent) || 0;
    bar.style.width = p.toFixed(1) + '%';
    const label = document.getElementById('ab-qpct');
    if (label) label.textContent = p.toFixed(1) + '%';
  }).catch(() => {});
}
setInterval(tick, 2000);
let lastCounts = null;
function watch() {
  fetch('/api/state').then(r => r.json()).then(d => {
    const sig = JSON.stringify(d.counts) + '|' + d.running;
    if (lastCounts === null) { lastCounts = sig; return; }
    if (sig !== lastCounts && !abBusy()) { location.reload(); }
  }).catch(() => {});
}
setInterval(watch, 5000);
"""

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand">Audiobind</div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item ab-active" href="/" aria-current="page">
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
  </nav>
  <div class="ab-rail-label">Attention</div>
  <nav class="ab-rail-section" aria-label="Attention">
    <a class="ab-rail-item ab-alert" href="/?status=review">
      <span class="ab-glyph">&#9873;</span> Review
      <span class="ab-count">{rail_review_count}</span></a>
    <a class="ab-rail-item ab-alert" href="/?status=held">
      <span class="ab-glyph">&#x23F8;</span> Held
      <span class="ab-count">{rail_held_count}</span></a>
    <a class="ab-rail-item ab-alert" href="/?status=failed">
      <span class="ab-glyph">&times;</span> Failed
      <span class="ab-count">{rail_failed_count}</span></a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> {library} <span class="dim">(read-only)</span><br>
    <b>output</b> &rarr; {output}
  </div>
</aside>

<main class="ab-main">
  <header class="ab-topbar">
    <h1>Audiobind</h1>
    <span class="ab-status{health_stale}"><span class="ab-dot"></span>{health_text}<small> &middot; last scan {health_scan}</small></span>
    <span class="ab-version">v{version}</span>
    <div class="ab-right">
      {auto_text}{dry_text}
    </div>
  </header>

{queue_strip}

{shelf_html}

  <section class="ab-controls" aria-label="Controls">
    {scan_button}
    {pause_button}
    {stop_button}
    <span class="ab-spacer"></span>
    {dry_button}
    {dry_note}
  </section>

  <section class="ab-statsline" aria-label="Library statistics">
    <div class="ab-stat"><b>{stat_hours}</b><span class="ab-unit">h</span><label>converted</label></div>
    <div class="ab-stat"><b>{stat_gb}</b><span class="ab-unit">GB</span><label>written</label></div>
    <div class="ab-stat"><b>{stat_pct}%</b><label>library progress</label></div>
    <div class="ab-libprog">
      <div class="ab-libbar"><i style="width:{stat_pct_width}%"></i></div>
      <span class="ab-libnum"><b>{stat_conv}</b> of {stat_total}</span>
    </div>
  </section>

  <section class="ab-toolbar" aria-label="Filters">
    <div class="chips">{chips}</div>
    <div class="ab-searchwrap">
      <input id="ab-search" class="ab-search" type="search" autocomplete="off" spellcheck="false"
             placeholder="Filter books&hellip;" value="{query}">
      <button class="ab-clear btn" onclick="abClearSearch()">Clear</button>
      <span id="ab-shown" class="ab-shown">{shown_label}</span>
    </div>
  </section>

  <div class="ab-tablewrap">
    <table class="ab-tbl">
    <colgroup>{colgroup}<col class="ab-actcol"></colgroup>
    <thead><tr>{headers}<th class="ab-actcol"></th></tr></thead>
    <tbody id="ab-body">{rows}
    <tr id="ab-empty" class="ab-empty" hidden><td colspan="{columns}">no books match this search</td></tr>
    </tbody></table>
  </div>
{overflow}
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>

<script>
window.AB_SORT_COLUMN = {sort_column_js};
window.AB_SORT_DIR = {sort_dir_js};
{script}
</script>
</body></html>"""


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(text: str) -> str:
    return _html(text).replace('"', "&quot;")


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m".format(seconds // 60)
    return "{}h {:02d}m".format(seconds // 3600, (seconds % 3600) // 60)


def _fmt_age(seconds: float) -> str:
    if seconds == float("inf"):
        return "never"
    if seconds < 90:
        return "{:.0f}s ago".format(seconds)
    if seconds < 5400:
        return "{:.0f}m ago".format(seconds / 60)
    return "{:.1f}h ago".format(seconds / 3600)


STATUS_ORDER = ("converted", "ready", "review", "held", "failed", "interrupted")
MAX_ROWS = 400
MAX_SHELF_CARDS = 12

COLUMNS = (
    ("title", "Book", "", ""),
    ("status", "Status", "110px", ""),
    ("tier", "Tier", "90px", "ab-hidemd"),
    ("runtime", "Runtime", "90px", ""),
    ("scan", "Last scan", "110px", "ab-hidemd"),
)
COVER_WIDTH = "50px"
TOTAL_COLUMNS = len(COLUMNS) + 1 + 1
SORT_COLUMNS = tuple(key for key, *_ in COLUMNS)
NUMERIC_COLUMNS = {"status", "tier", "runtime", "scan"}
TIER_ORDER = ("HIGH", "MEDIUM", "LOW", "COLLECTION", "NONE")


def search_matches(unit, query: str) -> bool:
    terms = (query or "").strip().lower().split()
    if not terms:
        return True
    haystack = " ".join([unit.title or "", unit.author or "", unit.path or ""]).lower()
    return all(term in haystack for term in terms)


def tier_rank(confidence: str) -> int:
    name = (confidence or "").upper()
    return TIER_ORDER.index(name) if name in TIER_ORDER else len(TIER_ORDER)


def status_rank(status: str) -> int:
    name = (status or "").lower()
    return STATUS_ORDER.index(name) if name in STATUS_ORDER else len(STATUS_ORDER)


def sort_value(unit, column: str):
    if column == "status":
        return status_rank(unit.status)
    if column == "tier":
        return tier_rank(unit.confidence)
    if column == "runtime":
        return float(unit.hours or 0)
    if column == "scan":
        return float(unit.updated_at or 0)
    return (unit.title or unit.path or "").lower()


def sort_units(units: list, column: str, direction: str) -> list:
    if column not in SORT_COLUMNS:
        column = "scan"
    reverse = str(direction).lower() == "desc"
    ordered = sorted(units, key=lambda u: (unit_title_key(u),))
    return sorted(ordered, key=lambda u: sort_value(u, column), reverse=reverse)


def unit_title_key(unit) -> str:
    return (unit.title or unit.path or "").lower()


def cover_src(unit) -> str:
    if unit.output:
        return "/cover?path=" + quote(unit.path, safe="")
    if unit.asin:
        return "/cover?asin=" + quote(unit.asin, safe="")
    return ""


def cover_html(unit, big: bool = False) -> str:
    classes = "ab-covbigwrap" if big else "ab-covwrap"
    src = cover_src(unit)
    if not src:
        return '<div class="{}"></div>'.format(classes)
    alt = _attr("cover of {}".format(unit.title or unit.path))
    return ('<div class="{c}"><img class="ab-thumb" src="{s}" alt="{a}" '
            'loading="lazy" onerror="abCoverFail(this)"></div>').format(
                c=classes, s=src, a=alt)


def read_sort(state) -> tuple:
    column = state.get_meta("sort_column", "scan")
    direction = state.get_meta("sort_dir", "desc")
    if column not in SORT_COLUMNS:
        column = "scan"
    if direction not in ("asc", "desc"):
        direction = "desc"
    return column, direction


def _tier_class(confidence: str) -> str:
    t = (confidence or "").upper()
    if t == "HIGH":
        return "ab-high"
    if t == "MEDIUM":
        return "ab-medium"
    return "ab-low"


def _render_shelf(problem_units: list) -> str:
    if not problem_units:
        return (
            '<section class="ab-shelf" id="ab-shelf" aria-label="Needs attention">'
            '<div class="ab-shelf-head" id="ab-shelf-head" role="button" tabindex="0" '
            'aria-expanded="false" aria-controls="ab-shelf-body">'
            '<h2>Needs attention</h2>'
            '<span class="ab-scount" id="ab-shelf-count">0</span>'
            '<span class="ab-shint">nothing needs attention</span>'
            '<span class="ab-caret" aria-hidden="true">&#9660;</span>'
            '</div>'
            '<div class="ab-shelf-body" id="ab-shelf-body" hidden></div>'
            '<div class="ab-shelf-empty" id="ab-shelf-empty" style="display:flex">'
            '<span class="ab-sdot"></span> nothing needs attention</div>'
            '</section>'
        )

    visible = problem_units[:MAX_SHELF_CARDS]
    more = len(problem_units) - len(visible)

    cards = []
    for unit in visible:
        why, _todo = explain(unit.status, unit.confidence or "", unit.note,
                             json.loads(unit.detail or "{}"))
        reason_class = ("held" if unit.status == "held"
                        else ("failed" if unit.status == "failed" else ""))

        btn = '<button class="btn primary" onclick="rowAction(this,\'retry\')">Retry now</button>'
        force = '<button class="btn warn" onclick="rowAction(this,\'force\')">Convert anyway</button>'
        asin_btn = '<button class="btn" onclick="rowAction(this,\'set_asin\')">Use this ASIN</button>'

        cards.append(
            ('<div class="ab-problem" data-path="{dp}">'
             '<div class="ab-problem-head">{cov}'
             '<div class="ab-pbook">'
             '<span class="ab-ptitle">{ti}</span>'
             '<span class="ab-pauthor">{au}</span></div>'
             '<span class="pill {st}"><span class="pdot"></span>{st}</span></div>'
             '<div class="ab-preason {rc}">{wh}</div>'
             '<div class="ab-pactions">{btns}</div></div>'
             ).format(
                dp=_attr(unit.path), cov=cover_html(unit),
                ti=_html(unit.title or unit.path),
                au=_html(unit.author or ""),
                st=unit.status, rc=reason_class, wh=_html(why),
                btns=btn + force + asin_btn))
    cards_html = "\n".join(cards)
    if more:
        cards_html += (
            '\n<div class="ab-problem" style="display:flex;align-items:center;'
            'justify-content:center;color:var(--ab-text3);font-size:12.5px">'
            '+{} more &mdash; filter in the table below</div>').format(more)

    count = len(problem_units)
    return (
        '<section class="ab-shelf" id="ab-shelf" aria-label="Needs attention">'
        '<div class="ab-shelf-head" id="ab-shelf-head" role="button" tabindex="0" '
        'aria-expanded="true" aria-controls="ab-shelf-body">'
        '<h2>Needs attention</h2>'
        '<span class="ab-scount" id="ab-shelf-count">{c}</span>'
        '<span class="ab-shint">action needed on {c} book{suf}</span>'
        '<span class="ab-caret" aria-hidden="true">&#9660;</span>'
        '</div>'
        '<div class="ab-shelf-body" id="ab-shelf-body">{cards}</div>'
        '<div class="ab-shelf-empty" id="ab-shelf-empty">'
        '<span class="ab-sdot"></span> nothing needs attention</div>'
        '</section>'
    ).format(c=count, suf="" if count == 1 else "s", cards=cards_html)


def render(state, status: str = "", query: str = "") -> str:
    counts = state.counts()
    stats = library_stats(state)
    wanted = status if status in counts else ""
    units = state.all(wanted or None)
    total = sum(counts.values())

    sort_column, sort_dir = read_sort(state)
    units = sort_units(units, sort_column, sort_dir)
    if query:
        units = [u for u in units if search_matches(u, query)]

    # chips
    chips = ['<a class="chip{on}" href="/">All <span class="n">{n}</span></a>'.format(
        on=" on" if not wanted else "", n=total)]
    for name in STATUS_ORDER:
        if not counts.get(name):
            continue
        chips.append(
            '<a class="chip{on}" href="/?status={st}">{st} <span class="n">{n}</span></a>'.format(
                on=" on" if wanted == name else "", st=name, n=counts[name])
        )
    chips_html = "".join(chips)

    # problem units for shelf
    shelf_order = {"failed": 0, "held": 1, "review": 2}
    problem_units = [u for u in state.all(None) if u.status in ("held", "review", "failed")]
    problem_units.sort(key=lambda u: (shelf_order.get(u.status, 99), unit_title_key(u)))

    # table
    shown = len(units)
    units = units[:MAX_ROWS]
    overflow = (
        '<div class="ab-overflow">showing the first {max} of {sh} - '
        'filter by status or use <code>/api/units?status=</code></div>'
    ).format(max=MAX_ROWS, sh=shown) if shown > MAX_ROWS else ""

    def detail_panel(unit) -> str:
        try:
            data = json.loads(unit.detail or "{}")
        except ValueError:
            data = {}
        src = data.get("source") or {}
        out = data.get("output") or {}
        if not out and unit.output:
            out = {"path": unit.output, "mb": unit.out_mb, "hours": unit.hours,
                   "chapters": unit.chapters, "chapters_from": ""}

        mb = src.get("mb") or unit.src_mb
        size = "{:.0f} MB &middot; ".format(mb) if mb else ""
        codecs = " + ".join(src.get("codecs") or [])
        before = [
            "{} part(s) &middot; {} file(s)".format(
                src.get("parts", 1), src.get("files") or unit.files),
            "{sz}{hr:.2f} h".format(sz=size, hr=src.get("hours") or unit.hours),
            (codecs or "codec not recorded")
            + (" &middot; {} ch".format(", ".join(src.get("channels") or []))
               if src.get("channels") else ""),
            '<span class="dim"><code>{pa}</code></span>'.format(pa=_html(unit.path)),
        ]
        if out:
            after = [
                "<code>{nm}</code>".format(nm=_html(Path(out.get("path", "")).name)),
                "{mb:.0f} MB &middot; {hr:.2f} h".format(
                    mb=out.get("mb") or 0, hr=out.get("hours") or 0),
                "{ch} chapters ({cf})".format(
                    ch=out.get("chapters", 0),
                    cf=_html(str(out.get("chapters_from") or ""))),
                '<span class="dim">{op}</span>'.format(
                    op=_html(str(out.get("path") or ""))),
            ]
        else:
            after = ["not converted &mdash; status <b>{st}</b>".format(st=unit.status),
                     "planned action: {ac}".format(ac=data.get("action") or "n/a")]

        decision = ["match: <b>{co}</b>".format(co=unit.confidence or "-"),
                    "asin: {av}".format(av=unit.asin or "-")]
        if unit.note:
            decision.append(_html(unit.note))

        why, todo = explain(unit.status, unit.confidence or "", unit.note, data)

        def block(title, items):
            return ("<div><h4>{t}</h4><ul>".format(t=title)
                    + "".join("<li>{i}</li>".format(i=i) for i in items)
                    + "</ul></div>")

        custom = overrides_mod.describe(state.get_overrides(unit.path))
        marked = overrides_mod.custom_fields(state.get_overrides(unit.path))

        def field(key, label_str, catalog_value, width_hint=""):
            mark = ('<span class="ab-custom" data-field="{k}"{h}>custom</span>').format(
                k=key, h="" if key in marked else " hidden")
            placeholder = _attr(catalog_value or "catalog value")
            return (
                '<div class="ab-field"><span class="ab-lab">{lb}{mk}</span>'
                '<input class="ab-in" data-field="{k}" value="{cv}" '
                'placeholder="{ph}" spellcheck="false"{wh}></div>'
            ).format(lb=label_str, mk=mark, k=key, cv=_attr(custom[key]),
                     ph=placeholder, wh=width_hint)

        match_data = data.get("match") or {}
        editor = (
            '<div class="ab-edit" data-path="{dp}"><h4>Metadata</h4>'
            '<p class="ab-hint">Overrides win over the catalog when this book is '
            'converted - tags, metadata.json and the author/title layout. '
            'Leave a box empty to use the catalog value; '
            'separate several names with a semicolon.</p>'
            '<div class="ab-grid">'
            + field("title", "title", match_data.get("title") or unit.title)
            + field("authors", "author(s)",
                    "; ".join(match_data.get("authors") or
                              ([unit.author] if unit.author else [])))
            + field("narrator", "narrator(s)", "")
            + field("series", "series", "")
            + field("series_index", "series index", "")
            + '</div><div class="ab-editbar">'
            '<button class="ab-save" onclick="abSaveMeta(this)">Save metadata</button>'
            '<span class="ab-fb"></span></div></div>'
        ).format(dp=_attr(unit.path))

        btn = '<button onclick="unitAction(this,\'retry\')">Retry now</button>'
        force = '<button class="warn" onclick="unitAction(this,\'force\')">Convert anyway</button>'
        asin_btn = '<button onclick="unitAction(this,\'set_asin\')">Use this ASIN</button>'

        explain_html = (
            '<div class="explain">'
            + '<div><h4>Why it is {st}</h4><p>{wh}</p></div>'.format(
                st=unit.status, wh=_html(why))
            + '<div class="todo"><h4>What to do</h4><p>{td}</p></div>'.format(
                td=_html(todo))
            + "</div>"
        )
        actions = (
            '<div class="acts">'
            + btn + force
            + '<input data-asin placeholder="ASIN, e.g. B005EHN61O" style="width:190px">'
            + asin_btn
            + "</div>"
        )
        return (
            '<tr class="drow" data-path="{dp}" hidden>'
            '<td colspan="{tc}">'
            '<div class="dd" data-path="{dp}">'
            + explain_html
            + '<div class="ab-covpanel">{cov}</div>'.format(cov=cover_html(unit, big=True))
            + block("Before", before)
            + block("After", after)
            + block("Decision", decision)
            + editor
            + actions
            + "</div></td></tr>"
        ).format(dp=_attr(unit.path), tc=TOTAL_COLUMNS)

    def row(unit) -> str:
        haystack = " ".join(filter(None, [unit.title, unit.author, unit.path]))
        data = " ".join(
            'data-sort-{k}="{v}"'.format(k=key, v=_attr(str(sort_value(unit, key))))
            for key in SORT_COLUMNS
        )
        has_custom = bool(state.get_overrides(unit.path))
        badge = '<span class="ab-custom"{h}>custom</span>'.format(
            h="" if has_custom else " hidden")

        act_html = ""
        if unit.status in ("held", "review", "failed"):
            btn = '<button class="btn primary" onclick="event.stopPropagation();rowAction(this,\'retry\')">Retry now</button>'
            force = '<button class="btn warn" onclick="event.stopPropagation();rowAction(this,\'force\')">Convert anyway</button>'
            asin_btn = '<button class="btn" onclick="event.stopPropagation();rowAction(this,\'set_asin\')">Use this ASIN</button>'
            act_html = '<div class="ab-rowact">' + btn + force + asin_btn + '</div>'

        reason_html = ""
        if unit.status in ("held", "review", "failed"):
            try:
                data = json.loads(unit.detail or "{}")
            except ValueError:
                data = {}
            why, _todo = explain(unit.status, unit.confidence or "", unit.note, data)
            rc = " failed" if unit.status == "failed" else ""
            reason_html = '<span class="ab-breason{rc}">{wh}</span>'.format(
                rc=rc, wh=_html(why))

        return (
            '<tr class="urow" onclick="toggleRow(this)" data-path="{dp}" '
            'data-search="{hs}" {da}>'
            '<td class="ab-covcell">{cov}</td>'
            '<td class="ab-bookcell"><span class="ab-btitle">{ti}{bd}</span>'
            '<span class="ab-bauthor">{au}</span>{rs}</td>'
            '<td><span class="pill {st}"><span class="pdot"></span>{st}</span></td>'
            '<td class="ab-hidemd"><span class="ab-tier {tc}">{co}</span></td>'
            '<td class="ab-num">{hr:.1f}h</td>'
            '<td class="ab-hidemd ab-scan">{ag}</td>'
            '<td class="ab-actcol">{ac}</td></tr>'
        ).format(
            dp=_attr(unit.path), hs=_attr(haystack), da=data,
            cov=cover_html(unit),
            ti=_html(unit.title or unit.path), bd=badge,
            au=_html(unit.author or ""), rs=reason_html,
            st=unit.status,
            tc=_tier_class(unit.confidence), co=unit.confidence or "-",
            hr=unit.hours or 0,
            ag=_fmt_age(time.time() - (unit.updated_at or 0)),
            ac=act_html)

    rows = "".join(row(u) + detail_panel(u) for u in units)
    if not units:
        rows = ('<tr class="ab-empty"><td colspan="{tc}">'
                'no books match this filter</td></tr>').format(tc=TOTAL_COLUMNS)

    headers = '<th class="ab-covhead"></th>' + "".join(
        '<th class="ab-sort{on} {kl}" data-column="{k}">'
        '{lb}<span class="ab-arrow">{ar}</span></th>'.format(
            on=" ab-on" if key == sort_column else "", kl=klass, k=key,
            lb=label,
            ar="&#9660;" if (key == sort_column and sort_dir == "desc") else "&#9650;")
        for key, label, _width, klass in COLUMNS
    )
    colgroup = ('<col style="width:{cw}">'.format(cw=COVER_WIDTH)) + "".join(
        ('<col style="width:{w}">'.format(w=width)) if width else "<col>"
        for _k, _l, width, _c in COLUMNS
    )
    shown_label = ("{sn} of {tt} shown".format(sn=shown, tt=total) if query
                   else "{sn} book{su}".format(sn=shown, su="" if shown == 1 else "s"))

    # controls
    paused = CONTROL.paused
    scan_button = '<button class="btn primary" onclick="act(\'scan\')"{di}>Scan now</button>'.format(
        di="" if not paused else " disabled")
    pause_button = (
        '<button class="btn" onclick="act(\'resume\')">Resume</button>' if paused
        else '<button class="btn warn" onclick="act(\'pause\')">Pause after this book</button>'
    )
    dry_button = (
        '<button class="btn dry on" onclick="dry(false)">Dry run <span class="ab-switch"></span></button>'
        if CONTROL.dry_run
        else '<button class="btn dry" onclick="dry(true)">Dry run <span class="ab-switch"></span></button>'
    )
    dry_note = (
        '<span class="ab-note">Dry run is on &mdash; no files will be written</span>'
        if CONTROL.dry_run else "")
    stop_button = (
        '<button class="btn danger" onclick="stop_now()">Stop now</button>'
        if CONTROL.running
        else '<button class="btn danger" disabled>Stop now</button>'
    )

    # encode strip
    queue_strip = ""
    if CONTROL.running:
        prog = CONTROL.progress or {}
        percent = float(prog.get("percent") or 0)
        meta_parts = []
        if prog.get("speed"):
            meta_parts.append("<b>{}x</b> realtime".format(prog["speed"]))
        if prog.get("eta"):
            meta_parts.append("<b>{}</b> left".format(_fmt_duration(prog["eta"])))
        if prog.get("elapsed"):
            meta_parts.append("{} elapsed".format(_fmt_duration(prog["elapsed"])))
        if not prog:
            meta_parts.append("analysing - the bar appears once encoding starts")

        title_html = _html(CONTROL.current_unit)
        queue_strip = (
            '<section class="ab-queue" aria-label="Currently encoding">'
            '<div class="ab-label"><span class="ab-pulse"></span> Now encoding</div>'
            '<div class="ab-qtitle">{ti}</div>'
            '<div class="ab-qact">'
            '<button class="btn" onclick="act(\'pause\')">Pause</button>'
            '<button class="btn danger" onclick="stop_now()">Stop</button>'
            '</div>'
            '<div class="ab-qmeta">{mp}</div>'
            '<div class="ab-qbarwrap">'
            '<div class="ab-qbar"><i id="ab-qfill" style="width:{pc:.1f}%"></i></div>'
            '<span class="ab-qpct" id="ab-qpct">{pc:.1f}%</span>'
            '</div></section>'
        ).format(ti=title_html, mp=" &middot; ".join(meta_parts), pc=percent)

    # health
    heartbeat_age = state.heartbeat_age()
    healthy = heartbeat_age < CONFIG.scan_interval_min * 60 * 2.5 + 300
    health_stale = "" if healthy else " ab-stale"
    health_text = "Healthy" if healthy else "Stale"

    # header indicators
    auto_text = (
        '<span style="font-family:var(--ab-mono);font-size:11px;color:var(--ab-text4)">'
        'auto: {ac}</span>').format(ac=CONFIG.auto_convert) if CONFIG.auto_convert else ""
    dry_header = (
        '<span style="font-family:var(--ab-mono);font-size:11px;color:var(--ab-amber)">'
        'dry run</span>') if CONTROL.dry_run else ""

    return PAGE.format(
        styles=STYLES,
        script=SCRIPT,
        version=__version__,
        library=CONFIG.host_library or str(CONFIG.library_root),
        output=CONFIG.host_output or str(CONFIG.output_root),
        auto_text=auto_text,
        dry_text=dry_header,
        health_stale=health_stale,
        health_text=health_text,
        health_scan=_fmt_age(heartbeat_age),
        rail_total=total,
        rail_review_count=counts.get("review", 0),
        rail_held_count=counts.get("held", 0),
        rail_failed_count=counts.get("failed", 0),
        scan_button=scan_button,
        pause_button=pause_button,
        stop_button=stop_button,
        dry_button=dry_button,
        dry_note=dry_note,
        queue_strip=queue_strip,
        shelf_html=_render_shelf(problem_units),
        stat_hours="{}".format(stats["hours"]),
        stat_gb="{}".format(stats["gb"]),
        stat_pct="{}".format(stats["progress"]),
        stat_pct_width=max(0.0, min(100.0, stats["progress"])),
        stat_conv=stats["converted"],
        stat_total=stats["total"],
        chips=chips_html,
        overflow=overflow,
        rows=rows,
        headers=headers,
        colgroup=colgroup,
        columns=TOTAL_COLUMNS,
        query=_attr(query),
        shown_label=shown_label,
        sort_column_js=json.dumps(sort_column),
        sort_dir_js=json.dumps(sort_dir),
    )


def library_stats(state) -> dict:
    units = state.all(None)
    converted = [u for u in units if u.status == "converted"]
    hours = sum(u.hours or 0.0 for u in converted)
    out_mb = sum(u.out_mb or 0.0 for u in converted)
    total = len(units)
    progress = round(100.0 * len(converted) / total, 1) if total else 0.0
    return {
        "hours": round(hours, 1),
        "gb": round(out_mb / 1000.0, 2),
        "converted": len(converted),
        "total": total,
        "progress": progress,
    }


def snapshot(state) -> dict:
    return {
        "version": __version__,
        "counts": state.counts(),
        "stats": library_stats(state),
        "last_run": state.last_run() or {},
        "heartbeat_age_seconds": state.heartbeat_age(),
        "library_root": str(CONFIG.library_root),
        "output_root": str(CONFIG.output_root),
        "auto_convert": CONFIG.auto_convert,
        "dry_run": CONTROL.dry_run,
        **CONTROL.describe(),
    }


class Handler(BaseHTTPRequestHandler):
    state = None
    server_version = "audiobind/{}".format(__version__)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        path, _, query = self.path.partition("?")
        params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
        status = params.get("status", "")
        if path in ("/favicon.svg", "/icon.svg"):
            icon = ICON_PATH
            if icon.exists():
                self._send(200, icon.read_bytes(), "image/svg+xml")
            else:
                self._send(404, b"no icon", "text/plain")
            return
        if path.startswith("/cover"):
            self._send_cover(params.get("asin", ""), unquote(params.get("path", "")))
            return
        if path.startswith("/api/units"):
            units = self.state.all(status or None)
            payload = [{"path": u.path, "title": u.title, "author": u.author,
                        "status": u.status, "confidence": u.confidence,
                        "asin": u.asin, "hours": u.hours, "files": u.files,
                        "note": u.note, "output": u.output,
                        "src_mb": u.src_mb, "out_mb": u.out_mb,
                        "chapters": u.chapters}
                       for u in units]
            self._send(200, json.dumps({"count": len(payload), "units": payload}).encode(),
                       "application/json")
            return
        if path.startswith("/api/state"):
            self._send(200, json.dumps(snapshot(self.state)).encode(), "application/json")
            return
        if path.startswith("/healthz"):
            healthy = self.state.heartbeat_age() < CONFIG.scan_interval_min * 60 * 2.5 + 300
            self._send(200 if healthy else 503, b"ok" if healthy else b"stale", "text/plain")
            return
        body = render(self.state, status, unquote(params.get("q", "")))
        self._send(200, body.encode(), "text/html; charset=utf-8")

    def _send_cover(self, asin: str, rel_path: str) -> None:
        try:
            found = resolve_cover(self.state, asin=asin, rel_path=rel_path)
        except Exception:                   # noqa: BLE001
            found = None
        if found is None:
            self._send(404, b"no cover", "text/plain")
            return
        try:
            body = found.read_bytes()
        except OSError:
            self._send(404, b"no cover", "text/plain")
            return
        kind = "image/png" if found.suffix.lower() == ".png" else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _handle_unit(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        rel = str(payload.get("path", ""))
        action = str(payload.get("action", "")).lower()

        if action == "set_meta":
            if self.state.get(rel) is None:
                self._send(404, b'{"error":"no such unit"}', "application/json")
                return
            clean = overrides_mod.normalise(payload.get("fields") or {})
            self.state.set_overrides(rel, clean)
            logging.getLogger("audiobind").info(
                "metadata overrides saved for %s: %s", rel, sorted(clean) or "none")
            self._send(200, json.dumps({
                "ok": True, "unit": rel,
                "custom": sorted(overrides_mod.custom_fields(clean)),
                "overrides": overrides_mod.describe(clean),
            }).encode(), "application/json")
            return

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
        elif action == "sort":
            column = str(payload.get("column", "scan"))
            direction = str(payload.get("direction", "desc")).lower()
            if column not in SORT_COLUMNS or direction not in ("asc", "desc"):
                self._send(400, b'{"error":"bad sort"}', "application/json")
                return
            self.state.set_meta("sort_column", column)
            self.state.set_meta("sort_dir", direction)
        else:
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        self._send(200, json.dumps({"ok": True, **CONTROL.describe()}).encode(),
                   "application/json")

    def log_message(self, *args):
        return


def serve(state):
    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.web_port), Handler)
    if CONFIG.web_port == 0:
        CONFIG.web_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server