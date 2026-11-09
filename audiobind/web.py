"""Status page and controls: watch the daemon, pause it, make it scan now."""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__
from . import overrides as overrides_mod
from .advice import explain
from .config import (
    CONFIG,
    SETTING_KEYS,
    SETTING_MAP,
    effective_val,
    is_custom,
    set_effective,
    setting_bounds,
    setting_choices,
    setting_effective,
    setting_env,
    setting_env_defaults,
)
from .control import CONTROL
from .covers import resolve as resolve_cover
from .covers import valid_asin
from .pipeline import process_unit
from .units import files_under, find_unit, foreign_audio, owned_paths

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
.ab-rail-brand a{color:inherit;text-decoration:none}
.ab-rail-brand a:hover{color:var(--ab-text2)}
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
.ab-rail-foot{padding:10px;font-family:var(--ab-mono);font-size:10.5px;
  color:var(--ab-text4);line-height:1.7;overflow-wrap:anywhere;}
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
.ab-spin{width:12px;height:12px;flex:none;border:2px solid rgba(255,255,255,.25);
  border-top-color:var(--ab-amber);border-radius:50%;display:inline-block;
  animation:ab-rot .8s linear infinite;}
@keyframes ab-rot{to{transform:rotate(360deg)}}
#ab-scanlive{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--ab-amber);}
#ab-scanlive[hidden]{display:none;}

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


.ab-tablewrap{flex:1;padding:0 24px 24px}
.ab-tbl{width:100%;border-collapse:collapse;table-layout:fixed}
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
td.ab-bookcell{display:flex;flex-direction:column;gap:1px;min-width:0;max-width:520px}
.ab-bookcell .ab-btitle{font-weight:500;font-size:13px;color:var(--ab-text);letter-spacing:-.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.ab-bookcell .ab-bauthor{font-size:11.5px;color:var(--ab-text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.ab-bookcell .ab-breason{font-size:11.5px;color:var(--ab-amber);margin-top:2px;
  white-space:normal;overflow-wrap:anywhere;line-height:1.35;}
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

.ab-rowact{display:inline-flex;flex-wrap:wrap;justify-content:flex-end;gap:5px;max-width:100%;
  background:var(--ab-bg);padding:4px 8px;border-radius:6px;
  box-shadow:-12px 0 18px rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .1s}
tr:hover .ab-rowact, tr:focus-within .ab-rowact{opacity:1;pointer-events:auto}
.ab-rowact button{height:26px;padding:0 7px;font-size:11px;border-radius:5px;white-space:nowrap}
.ab-rowact button.warn{color:var(--ab-amber)}
.ab-rowact button.warn:hover{background:var(--ab-amber-soft);border-color:rgba(242,163,60,.3)}
.ab-actcol{padding:0!important;width:auto;position:relative;overflow:hidden;text-align:right}

tr.drow td{background:#11151d;padding:0;border-bottom:1px solid var(--ab-border);white-space:normal}
tr.drow[hidden] td{padding:0}
.dd{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:22px;padding:16px 22px 18px;}
.ab-toprow{grid-column:1/-1;display:flex;gap:26px;align-items:flex-start;}
.ab-topcov{flex:none;}
.ab-topmain{flex:1;min-width:0;}
.explain{margin:0 0 14px;}
.explain h4{margin:0 0 6px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
  color:var(--ab-amber);font-weight:600;}
.explain p{margin:0;color:#d7dce7;}
.explain .todo{margin-top:12px;}
.explain .todo h4{color:var(--ab-amber);}
.ab-actrow{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 14px;}
.ab-actrow button{height:36px;padding:0 18px;font-size:13px;border-radius:8px;}
.ab-actrow .warn{background:var(--ab-amber);border-color:transparent;
  color:#1a1200;font-weight:600;}
.ab-actrow .warn:hover{background:#f6b95d;color:#1a1200;}
.ab-pinrow{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
.ab-pinrow input{font:inherit;font-size:13px;padding:8px 12px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);width:240px;}
.ab-pinlab{font-size:12.5px;color:var(--ab-text3);}
.ab-pinhint{width:100%;font-size:12px;color:var(--ab-text3);}
.ab-meter{display:flex;gap:6px;margin:8px 0 2px;}
.ab-meter span{flex:0 0 92px;text-align:center;font-size:11px;font-weight:700;letter-spacing:.06em;border-radius:6px;padding:4px 0;}
.ab-meter .ab-off{background:var(--ab-surface2);color:var(--ab-text3);}
.ab-meter .ab-on{background:var(--ab-amber);color:#1a1200;}
.ab-meterhint{font-size:12px;color:var(--ab-text3);}
.ab-rebanner{grid-column:1/-1;background:rgba(242,163,60,.12);border:1px solid rgba(242,163,60,.5);
  border-radius:10px;padding:12px 16px;font-size:14px;color:var(--ab-amber);margin-bottom:4px;}
.ab-rebanner b{color:var(--ab-amber);}
.ab-alink{color:var(--ab-blue);border-bottom:1px dotted var(--ab-border);text-decoration:none;}
.acts{grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap;align-items:center;
  border-top:1px solid #1c2230;padding-top:14px;}
.acts button{font-size:12.5px;padding:6px 12px;}
.acts input{font:inherit;font-size:12.5px;padding:6px 10px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);}
.dd h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;text-transform:uppercase;
  color:var(--ab-amber);font-weight:600;}
.dd ul{margin:0;padding-left:16px;}
.dd li{margin:3px 0;color:#c7cddb;}
.dd code{color:#9fd7c8;}

.ab-covbigwrap{position:relative;width:190px;height:190px;border-radius:8px;
  background:#161a22;border:1px solid #232833;overflow:hidden;}
.ab-covbigwrap::after{content:"\\266B";position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;color:#394152;font-size:48px;}
.ab-covbigwrap .ab-thumb{position:relative;z-index:1;display:block;width:100%;height:100%;object-fit:cover;background:#161a22;}
.ab-covpanel{display:flex;align-items:flex-start;}
.ab-editions{grid-column:1/-1;border-top:1px solid #1c2230;padding-top:14px;}
.ab-editions .ab-edition{display:flex;align-items:center;gap:12px;padding:6px 0;
  border-bottom:1px solid var(--ab-border-subtle);}
.ab-editions .ab-edition:last-of-type{border-bottom:none}
.ab-editions .ab-edmain{flex:1;min-width:0;display:flex;flex-direction:column;gap:1px;}
.ab-editions .ab-edmain b{font-size:13px;font-weight:500;color:var(--ab-text)}
.ab-editions .ab-edsub{font-size:11.5px;color:var(--ab-text3)}
.ab-editions button{height:28px;padding:0 12px;font-size:12px;border-radius:6px;flex:none}
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
.ab-modal:not([hidden]){display:flex;position:fixed;inset:0;z-index:100;
  align-items:center;justify-content:center;}
.ab-modal-backdrop{position:absolute;inset:0;background:rgba(0,0,0,.6);}
.ab-modal-dialog{position:relative;background:var(--ab-surface);border:1px solid var(--ab-border);
  border-radius:10px;padding:24px;max-width:420px;width:90%;box-shadow:0 12px 40px rgba(0,0,0,.5);}
.ab-modal-dialog h3{font-size:16px;font-weight:600;color:var(--ab-text);margin:0 0 12px;}
.ab-modal-dialog p{font-size:13px;color:var(--ab-text2);margin:0 0 20px;line-height:1.5;}
.ab-modal-actions{display:flex;gap:8px;justify-content:flex-end;}
.ab-progsub{font-size:12px;color:var(--ab-text3);text-transform:none;letter-spacing:0;}
.ab-statbar{height:8px;border-radius:99px;background:#1c2230;overflow:hidden;}
.ab-statfill{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,#2b5ea8,#12a89b);}
.ab-empty td{color:var(--ab-text3);padding:14px 10px;}
.ab-hintbox{background:var(--ab-surface);border:1px solid var(--ab-border);
  border-radius:8px;padding:12px 16px;margin-bottom:20px;
  font-size:12.5px;color:var(--ab-text3);line-height:1.5;}
.ab-hintbox b{color:var(--ab-text2);font-weight:500;}
.ab-setting{display:flex;flex-direction:column;gap:6px;padding:12px 0;
  border-bottom:1px solid var(--ab-border-subtle);}
.ab-setting:last-of-type{border-bottom:none}
.ab-setting-label{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:500;color:var(--ab-text)}
.ab-setting-hint{font-size:11.5px;color:var(--ab-text3);line-height:1.4;margin-bottom:2px}
.ab-range-wrap{display:flex;align-items:center;gap:10px}
.ab-range-val{font-family:var(--ab-mono);font-size:12px;color:var(--ab-text2);min-width:24px;text-align:right}
input[type=range]{-webkit-appearance:none;appearance:none;height:6px;
  background:rgba(255,255,255,.08);border-radius:3px;outline:none;flex:1;max-width:300px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
  border-radius:50%;background:var(--ab-accent-bright);cursor:pointer;border:none}
input[type=range]::-moz-range-thumb{width:16px;height:16px;
  border-radius:50%;background:var(--ab-accent-bright);cursor:pointer;border:none}
select.ab-in{font:inherit;font-size:12.5px;padding:7px 10px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);max-width:200px}
select.ab-in:focus{outline:none;border-color:#2b5ea8}
input[type=number].ab-in{max-width:100px}
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
// Retry now / Convert anyway / Use this ASIN, from a detail panel, a table row or a
// shelf card: each of those carries data-path, so the nearest one names the book
function abUnitAction(btn, action) {
  const el = btn.closest('[data-path]');
  if (!el) return;
  const body = {action: action, path: el.dataset.path};
  let note = 'queued - re-checking…';
  if (action === 'set_asin') {
    const input = el.querySelector('input[data-asin]');
    body.asin = (input && input.value || '').trim();
    if (!body.asin) { if (input) input.focus(); return; }
    note = 'pinning ' + body.asin + ' - re-checking…';
  } else if (action === 'force') {
    note = 'queued - converting…';
  }
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'working…';
  abToast(note);
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify(body)})
    .then(r => r.json().then(res => ({ok: r.ok, res: res})))
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      setTimeout(() => location.reload(), 700);
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}
function unitDelete(btn) {
  const path = btn.dataset.path;
  const title = btn.dataset.title || path;
  abOpenDeleteModal(path, title, btn);
}
let abDeleteTrigger = null;
function abOpenDeleteModal(path, title, trigger) {
  abDeleteTrigger = trigger;
  document.getElementById('ab-modal-title').textContent = 'Delete this book?';
  document.getElementById('ab-modal-warning').textContent =
    'This permanently deletes ' + title + "'s source folder and its published files. " +
    "This cannot be undone."
  document.getElementById('ab-modal').hidden = false;
  document.getElementById('ab-modal-cancel').focus();
}

function abModalCancel() {
  document.getElementById('ab-modal').hidden = true;
  if (abDeleteTrigger) abDeleteTrigger.focus();
}

function abModalConfirm() {
  const path = abDeleteTrigger ? abDeleteTrigger.dataset.path : '';
  if (!path) return;
  const btn = document.getElementById('ab-modal-confirm');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'deleting\u2026';
  abToast('deleting permanently\u2026');
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({action:'delete', path: path, confirm: true})})
    .then(r => r.json())
    .then(result => {
      if (!result.ok) {
        abToast(result.error || 'delete failed');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      if (result.source_kept) {
        abToast('Deleted from Audiobind and your output library. The source folder stays (' +
                (result.source_kept_reason || 'your library is read-only') +
                ') - it will reappear on the next scan unless you remove it from the share.');
      }
      setTimeout(() => location.reload(), 500);
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}

window.addEventListener('keydown', function(e) {
  const modal = document.getElementById('ab-modal');
  if (e.key === 'Escape' && modal && !modal.hidden) abModalCancel();
});

function abToast(text) {
  const el = document.getElementById('ab-toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(abToast.t);
  abToast.t = setTimeout(() => el.classList.remove('on'), 5000);
}
function abUseEdition(btn) {
  const panel = btn.closest('.dd');
  if (!panel) return;
  const path = panel.dataset.path;
  const asin = btn.dataset.asin;
  if (!path || !asin) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'pinning\\u2026';
  abToast('pinning ' + asin + ' - re-checking\\u2026');
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({action:'set_asin', path: path, asin: asin})})
    .then(r => r.json())
    .then(() => setTimeout(() => location.reload(), 700))
    .catch(() => { btn.disabled = false; btn.textContent = label; });
}
function abFocusAsin(row) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return false;
  panel.hidden = false;
  row.classList.add('ab-sel');
  const input = panel.querySelector('input[data-asin]');
  if (input) {
    input.scrollIntoView({block: 'center', behavior: 'smooth'});
    input.focus();
  }
  return true;
}
function abFindRow(path) {
  return [...document.querySelectorAll('tr.urow')].find(r => r.dataset.path === path) || null;
}
function abEditAsin(btn) {
  const el = btn.closest('[data-path]');
  if (!el) return;
  const path = el.dataset.path;
  const row = abFindRow(path);
  if (row && row.hidden) {
    // hidden by the search box: clear the filter so the panel can show
    const box = abSearchBox();
    if (box) box.value = '';
    try { sessionStorage.setItem(SEARCH_KEY, ''); } catch (e) {}
    abApplyView();
  }
  if (row && abFocusAsin(row)) return;
  // the shelf lists every problem book, but the table may not have this one (another
  // status filter, past the row limit): reload on a view that does, panel open
  try { sessionStorage.setItem(SEARCH_KEY, ''); } catch (e) {}
  location.href = '/?status=' + encodeURIComponent(el.dataset.status || '') +
    '&q=' + encodeURIComponent(path) + '&open=' + encodeURIComponent(path);
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
// Panels start closed on every page load - an open panel only exists because
// the operator opened it this visit, or a re-check asked for it to re-open.
function openSet() {
  const s = new Set();
  document.querySelectorAll('tr.drow:not([hidden])').forEach(p => {
    if (p.dataset.path) s.add(p.dataset.path);
  });
  return s;
}
function saveOpen() {}
function toggleRow(row) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return;
  panel.hidden = !panel.hidden;
  row.classList.toggle('ab-sel', !panel.hidden);
}
document.addEventListener('DOMContentLoaded', () => {
  // a manual Retry now / Use this ASIN that still needs a decision re-opens its panel
  document.querySelectorAll('tr.drow[data-reopen]').forEach(panel => {
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
    if (!box.value) {
      try { box.value = sessionStorage.getItem(SEARCH_KEY) || ''; } catch (e) {}
    }
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
let abScanToastDone = false;
function abScanFinishedToast(d) {
  // a pass can end faster than the first status poll, so the page also
  // announces a scan that finished moments before this page loaded
  if (abScanToastDone) return;
  abScanToastDone = true;
  const lr = d.last_run;
  if (!lr || !lr.finished_at) return;
  if (Date.now() / 1000 - lr.finished_at > 20) return;
  abToast('Scan finished - ' + (lr.seen || 0) + ' checked, '
          + (lr.converted || 0) + ' converted, ' + (lr.held || 0) + ' held, '
          + (lr.failed || 0) + ' failed');
}
function abScanUI(scan) {
  const live = document.getElementById('ab-scanlive');
  const btn = document.getElementById('ab-scanbtn');
  const re = document.getElementById('ab-recheckbtn');
  if (!live) return;
  live.hidden = false;
  const text = document.getElementById('ab-scanlive-text');
  if (text) text.textContent = 'Scanning ' + (scan.done || 0) + '/' + (scan.total || 0) + '\u2026';
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning\u2026'; }
  if (re) re.disabled = true;
}
function abScanIdle() {
  const live = document.getElementById('ab-scanlive');
  const btn = document.getElementById('ab-scanbtn');
  const re = document.getElementById('ab-recheckbtn');
  if (live) live.hidden = true;
  if (btn && btn.disabled) { btn.disabled = false; btn.textContent = 'Scan now'; }
  if (re) re.disabled = false;
}
function tick() {
  fetch('/api/state').then(r => r.json()).then(d => {
    abScanFinishedToast(d);
    const bar = document.getElementById('ab-qfill');
    if (d.scan && d.scan.scanning) { abScanUI(d.scan); return; }
    abScanIdle();
    if (!bar) return;
    if (!d.running) { if (!abBusy()) location.reload(); return; }
    const p = (d.progress && d.progress.percent) || 0;
    bar.style.width = p.toFixed(1) + '%';
    const label = document.getElementById('ab-qpct');
    if (label) label.textContent = p.toFixed(1) + '%';
  }).catch(() => {});
}
document.addEventListener('DOMContentLoaded', () => {
  let want = '';
  try { want = new URLSearchParams(location.search).get('open') || ''; } catch (e) {}
  if (!want) return;
  const row = abFindRow(want);
  if (row) abFocusAsin(row);
});
setInterval(tick, 2000);
let lastCounts = null;
function watch() {
  fetch('/api/state').then(r => r.json()).then(d => {
    // rev moves even when a re-check ends in the same status, so a manual
    // Retry now / Use this ASIN always gets its fresh detail rendered
    const sig = JSON.stringify(d.counts) + '|' + d.running + '|' + (d.rev || '') + '|' + (d.scan && d.scan.scanning ? 'S' : '');
    if (lastCounts === null) { lastCounts = sig; return; }
    if (sig !== lastCounts && !abBusy() && !(d.scan && d.scan.scanning)) { location.reload(); }
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
  <div class="ab-rail-brand"><a href="/" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item ab-active" href="/" aria-current="page">
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
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
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>
  </div>
</aside>

<main class="ab-main">
  <header class="ab-topbar">
    <h1><a href="/" title="Back to the library" style="color:inherit;text-decoration:none">Audiobind</a></h1>
    <span class="ab-status{health_stale}"><span class="ab-dot"></span>{health_text}<small> &middot; last scan {health_scan}</small></span>
    <span class="ab-version">v{version}</span>
    {dry_text}
  </header>

{queue_strip}

{shelf_html}

  <section class="ab-controls" aria-label="Controls">
    {scan_button}
    {recheck_button}
    {pause_button}
    {stop_button}
    {dry_button}
    <span id="ab-scanlive" hidden><span class="ab-spin"></span><span id="ab-scanlive-text">Scanning&hellip;</span></span>
    <span class="ab-spacer"></span>
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

<div id="ab-modal" class="ab-modal" hidden>
  <div class="ab-modal-backdrop" onclick="abModalCancel()"></div>
  <div class="ab-modal-dialog" role="dialog" aria-labelledby="ab-modal-title">
    <h3 id="ab-modal-title"></h3>
    <p id="ab-modal-warning"></p>
    <div class="ab-modal-actions">
      <button class="btn danger" id="ab-modal-confirm" onclick="abModalConfirm()">Delete permanently</button>
      <button class="btn" id="ab-modal-cancel" onclick="abModalCancel()">Cancel</button>
    </div>
  </div>
</div>

<script>
window.AB_SORT_COLUMN = {sort_column_js};
window.AB_SORT_DIR = {sort_dir_js};
{script}
</script>
</body></html>"""

SETTINGS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &middot; Settings</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand"><a href="/" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item{lib_active}" href="/">
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item{set_active}" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
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
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>
  </div>
</aside>
<main class="ab-main">
  <header class="ab-topbar">
    <h1>Settings</h1>
    <span class="ab-version">v{version}</span>
    <div class="ab-right"></div>
  </header>
  <section class="ab-settings" style="padding:24px;max-width:640px;">
    <div class="ab-hintbox">
      <b>Apply later.</b> Changes take effect on the next scan or conversion. Existing m4b files are never touched.</div>
    {controls}
    <div class="ab-editbar" style="margin-top:8px;">
      <button class="ab-save" onclick="abSaveSettings()">Save settings</button>
      <button class="btn" onclick="abResetSettings()">Reset to defaults</button>
      <span class="ab-fb" id="ab-settings-fb"></span>
    </div>
  </section>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>
<script>
{settings_script}
</script>
</body></html>"""

SETTINGS_SCRIPT = """
function abToastSettings(text) {
  const el = document.getElementById('ab-toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(abToastSettings.t);
  abToastSettings.t = setTimeout(() => el.classList.remove('on'), 5000);
}

function abRangeShow(el) {
  const val = el.parentElement.querySelector('.ab-range-val');
  if (val) val.textContent = el.value;
}

// each [data-setting] is a wrapper div; the value lives on the select/input inside it
function abSettingControl(wrap) {
  return wrap.querySelector('select, input');
}

function abSettingValue(el) {
  if (el.tagName === 'SELECT') return el.value;
  if (el.type === 'range' || el.type === 'number') return parseInt(el.value, 10);
  return el.value;
}

function abSaveSettings() {
  const fb = document.getElementById('ab-settings-fb');
  const settings = {};
  // only what you changed: an untouched field keeps following the environment
  document.querySelectorAll('[data-setting]').forEach(wrap => {
    const el = abSettingControl(wrap);
    if (!el) return;
    if (String(el.value) === String(el.dataset.initial)) return;
    settings[wrap.dataset.setting] = abSettingValue(el);
  });
  if (Object.keys(settings).length === 0) {
    fb.className = 'ab-fb';
    fb.textContent = 'nothing changed';
    setTimeout(() => { fb.textContent = ''; }, 4000);
    return;
  }
  fb.className = 'ab-fb';
  fb.textContent = 'saving...';
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({settings: settings})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      if (!res.ok) {
        fb.className = 'ab-fb ab-bad';
        fb.textContent = res.body.error || 'could not save';
        return;
      }
      fb.className = 'ab-fb ab-good';
      fb.textContent = 'saved';
      document.querySelectorAll('[data-setting]').forEach(wrap => {
        const el = abSettingControl(wrap);
        if (el && Object.prototype.hasOwnProperty.call(settings, wrap.dataset.setting)) {
          el.dataset.initial = el.value;
        }
      });
      document.querySelectorAll('[data-setting] .ab-custom').forEach(mark => {
        const key = mark.dataset.field;
        mark.hidden = res.body.custom.indexOf(key) === -1;
      });
      setTimeout(() => { fb.textContent = ''; fb.className = 'ab-fb'; }, 6000);
    })
    .catch(() => {
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
    });
}

function abResetSettings() {
  const fb = document.getElementById('ab-settings-fb');
  fb.className = 'ab-fb';
  fb.textContent = 'resetting...';
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({reset: true})})
    .then(r => r.json())
    .then(body => {
      if (body.ok) {
        document.querySelectorAll('[data-setting]').forEach(wrap => {
          const el = abSettingControl(wrap);
          const env = body.env_defaults[wrap.dataset.setting];
          if (!el || env === undefined) return;
          el.value = String(env);
          if (el.type === 'range') abRangeShow(el);
          el.dataset.initial = el.value;
        });
        document.querySelectorAll('[data-setting] .ab-custom').forEach(mark => {
          mark.hidden = true;
        });
        abSyncProfile();
        fb.className = 'ab-fb ab-good';
        fb.textContent = 'reset to environment defaults';
        setTimeout(() => { fb.textContent = ''; fb.className = 'ab-fb'; }, 6000);
      } else {
        fb.className = 'ab-fb ab-bad';
        fb.textContent = body.error || 'could not reset';
      }
    })
    .catch(() => {
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
    });
}

// ---- quality profiles: named presets for the two conversion settings below ----
const AB_PROFILES = {
  'balanced':     {label: 'Balanced',      auto_convert: 'HIGH', audio_bitrate: '128k'},
  'high':         {label: 'High',          auto_convert: 'HIGH', audio_bitrate: '256k'},
  'source-match': {label: 'Source-match',  auto_convert: 'HIGH', audio_bitrate: '64k'},
};

function abProfileField(key) {
  const wrap = document.querySelector('[data-setting="' + key + '"]');
  return wrap ? wrap.querySelector('select') : null;
}

function abSyncProfile() {
  const sel = document.getElementById('ab-profile');
  if (!sel) return;
  const tier = abProfileField('auto_convert');
  const bitrate = abProfileField('audio_bitrate');
  for (const [name, p] of Object.entries(AB_PROFILES)) {
    if (tier && bitrate && tier.value === p.auto_convert && bitrate.value === p.audio_bitrate) {
      sel.value = name;
      return;
    }
  }
  sel.value = 'custom';
}

function abApplyProfile(name) {
  const preset = AB_PROFILES[name];
  if (!preset) return;
  const tier = abProfileField('auto_convert');
  const bitrate = abProfileField('audio_bitrate');
  if (tier) tier.value = preset.auto_convert;
  if (bitrate) bitrate.value = preset.audio_bitrate;
  abSyncProfile();
}

document.addEventListener('DOMContentLoaded', () => {
  abSyncProfile();
  ['auto_convert', 'audio_bitrate'].forEach(key => {
    const el = abProfileField(key);
    if (el) el.addEventListener('change', abSyncProfile);
  });
});
"""


def _html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(text: str) -> str:
    return _html(text).replace('"', "&quot;")


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
MAX_SHELF_CARDS = 12

COLUMNS = (
    ("title", "Book", "min(560px,30vw)", ""),
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
        return f'<div class="{classes}"></div>'
    alt = _attr(f"cover of {unit.title or unit.path}")
    return (
        f'<div class="{classes}"><img class="ab-thumb" src="{src}" alt="{alt}" '
        'loading="lazy" onerror="abCoverFail(this)"></div>'
    )


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
    tier = str(effective_val("auto_convert") or "high")
    if not problem_units:
        return (
            '<section class="ab-shelf" id="ab-shelf" aria-label="Needs attention">'
            '<div class="ab-shelf-head" id="ab-shelf-head" role="button" tabindex="0" '
            'aria-expanded="false" aria-controls="ab-shelf-body">'
            "<h2>Needs attention</h2>"
            '<span class="ab-scount" id="ab-shelf-count">0</span>'
            '<span class="ab-shint">nothing needs attention</span>'
            '<span class="ab-caret" aria-hidden="true">&#9660;</span>'
            "</div>"
            '<div class="ab-shelf-body" id="ab-shelf-body" hidden></div>'
            '<div class="ab-shelf-empty" id="ab-shelf-empty" style="display:flex">'
            '<span class="ab-sdot"></span> nothing needs attention</div>'
            "</section>"
        )

    visible = problem_units[:MAX_SHELF_CARDS]
    more = len(problem_units) - len(visible)

    cards = []
    for unit in visible:
        why, _todo = explain(
            unit.status, unit.confidence or "", unit.note, json.loads(unit.detail or "{}"), tier=tier
        )
        reason_class = "held" if unit.status == "held" else ("failed" if unit.status == "failed" else "")

        btn = '<button class="btn primary" title="Re-check this book against the catalog and convert it if it passes the rules." onclick="abUnitAction(this,\'retry\')">Retry now</button>'
        force = '<button class="btn warn" title="Convert this book even though the automatic checks flagged it." onclick="abUnitAction(this,\'force\')">Convert anyway</button>'
        asin_btn = '<button class="btn" title="Open this book\'s details and lock an Audible entry." onclick="abEditAsin(this)">Edit ASIN</button>'

        cards.append(
            (
                '<div class="ab-problem" data-path="{dp}" data-status="{st}">'
                '<div class="ab-problem-head">{cov}'
                '<div class="ab-pbook">'
                '<span class="ab-ptitle">{ti}</span>'
                '<span class="ab-pauthor">{au}</span></div>'
                '<span class="pill {st}"><span class="pdot"></span>{st}</span></div>'
                '<div class="ab-preason {rc}">{wh}</div>'
                '<div class="ab-pactions">{btns}</div></div>'
            ).format(
                dp=_attr(unit.path),
                cov=cover_html(unit),
                ti=_html(unit.title or unit.path),
                au=_html(unit.author or ""),
                st=unit.status,
                rc=reason_class,
                wh=_html(why),
                btns=btn + force + asin_btn,
            )
        )
    cards_html = "\n".join(cards)
    if more:
        cards_html += (
            '\n<div class="ab-problem" style="display:flex;align-items:center;'
            'justify-content:center;color:var(--ab-text3);font-size:12.5px">'
            f"+{more} more &mdash; filter in the table below</div>"
        )

    count = len(problem_units)
    return (
        '<section class="ab-shelf" id="ab-shelf" aria-label="Needs attention">'
        '<div class="ab-shelf-head" id="ab-shelf-head" role="button" tabindex="0" '
        'aria-expanded="true" aria-controls="ab-shelf-body">'
        "<h2>Needs attention</h2>"
        '<span class="ab-scount" id="ab-shelf-count">{c}</span>'
        '<span class="ab-shint">action needed on {c} book{suf}</span>'
        '<span class="ab-caret" aria-hidden="true">&#9660;</span>'
        "</div>"
        '<div class="ab-shelf-body" id="ab-shelf-body">{cards}</div>'
        '<div class="ab-shelf-empty" id="ab-shelf-empty">'
        '<span class="ab-sdot"></span> nothing needs attention</div>'
        "</section>"
    ).format(c=count, suf="" if count == 1 else "s", cards=cards_html)


def render_settings(state) -> str:
    """Render the settings page with current effective values."""
    counts = state.counts()
    total = sum(counts.values())
    set_effective(state)

    controls_parts = []

    # 0. Quality profile (client-side preset - not itself a stored setting)
    controls_parts.append(
        '<div class="ab-setting" data-profile>'
        '<span class="ab-setting-label">Quality profile</span>'
        '<div class="ab-setting-hint">Named presets for the two conversion settings '
        "below. Picking one fills them in; edit either field afterwards and the "
        "profile switches to Custom so you can see it is no longer a preset.</div>"
        '<select class="ab-in" id="ab-profile" onchange="abApplyProfile(this.value)">'
        '<option value="balanced">Balanced - HIGH tier, 128k floor</option>'
        '<option value="high">High - HIGH tier, 256k floor</option>'
        '<option value="source-match">Source-match - HIGH tier, 64k floor (keeps '
        "the source bitrate)</option>"
        '<option value="custom">Custom</option>'
        "</select></div>"
    )

    # 1-6: every value shown is the effective one (UI override, else the live environment),
    # and every control carries data-initial so Save posts only what you changed
    def custom(key: str) -> bool:
        return is_custom(SETTING_MAP[key])

    controls_parts.append(
        _setting_select(
            "auto_convert",
            "Auto-convert tier",
            setting_effective("auto_convert"),
            setting_choices("auto_convert"),
            custom("auto_convert"),
            setting_env("auto_convert"),
            hint="What converts without you touching a row. "
            "HIGH: title and author match Audible strongly with a clear margin - safe to "
            "convert automatically. MEDIUM: a good match with weaker evidence (e.g. the "
            "author came from the catalog) - converts when this tier is MEDIUM. LOW: weak "
            "match - always held for review; fix it with Edit ASIN on the book row. "
            "COLLECTION/NONE: the folder looks like several books mixed together, or nothing "
            "in the catalog matched - these are never auto-converted.",
        )
    )
    controls_parts.append(
        _setting_select(
            "audio_bitrate",
            "Output quality floor",
            setting_effective("audio_bitrate"),
            setting_choices("audio_bitrate"),
            custom("audio_bitrate"),
            setting_env("audio_bitrate"),
            hint="A floor, not a target: a richer source keeps its own bitrate (capped at 320k). Applies to future conversions only.",
        )
    )
    lo, hi = setting_bounds("runtime_delta")
    controls_parts.append(
        _setting_range(
            "runtime_delta",
            "Wrong-edition tolerance",
            setting_effective("runtime_delta"),
            lo,
            hi,
            "%",
            custom("runtime_delta"),
            setting_env("runtime_delta"),
            hint="How far the file runtime may differ from the Audible runtime before a book is HELD for review. Applies to future scans.",
        )
    )
    lo, hi = setting_bounds("scan_interval")
    controls_parts.append(
        _setting_number(
            "scan_interval",
            "Scan interval",
            setting_effective("scan_interval"),
            lo,
            hi,
            "min",
            custom("scan_interval"),
            setting_env("scan_interval"),
        )
    )
    lo, hi = setting_bounds("nice")
    controls_parts.append(
        _setting_range(
            "nice",
            "CPU priority (nice)",
            setting_effective("nice"),
            lo,
            hi,
            "",
            custom("nice"),
            setting_env("nice"),
            hint="higher = kinder to the system; ffmpeg runs at this priority",
        )
    )
    controls_parts.append(
        _setting_select(
            "region",
            "Catalog region",
            setting_effective("region"),
            setting_choices("region"),
            custom("region"),
            setting_env("region"),
            hint="Applies to future catalog lookups.",
        )
    )

    rc = rail_counts(state, counts)
    return SETTINGS_PAGE.format(
        styles=STYLES,
        version=__version__,
        lib_active="",
        set_active=" ab-active",
        rail_total=total,
        rail_review_count=rc["review"],
        rail_held_count=rc["held"],
        rail_failed_count=rc["failed"],
        library=CONFIG.host_library or str(CONFIG.library_root),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=CONFIG.host_output or str(CONFIG.output_root),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        controls="\n".join(controls_parts),
        settings_script=SETTINGS_SCRIPT,
    )


def _custom_mark(key: str, custom: bool, env_val) -> str:
    hidden = "" if custom else " hidden"
    return f'<span class="ab-custom" data-field="{key}"{hidden}>custom (was {_html(str(env_val))} from env)</span>'


def _setting_select(
    key: str, label: str, value: str, options: list[str], custom: bool, env_val, hint: str = ""
) -> str:
    mark = _custom_mark(key, custom, env_val)
    selected = next((o for o in options if str(o).upper() == str(value).upper()), options[0])
    opt_html = "".join(
        f'<option value="{_attr(o)}"{" selected" if o == selected else ""}>{_html(o)}</option>' for o in options
    )
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        f"{hint_html}"
        f'<select class="ab-in" data-initial="{_attr(selected)}">{opt_html}</select>'
        "</div>"
    )


def _setting_range(
    key: str, label: str, value: int, vmin: int, vmax: int, suffix: str, custom: bool, env_val, hint: str = ""
) -> str:
    mark = _custom_mark(key, custom, env_val)
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        f"{hint_html}"
        '<div class="ab-range-wrap">'
        f'<input type="range" min="{vmin}" max="{vmax}" value="{value}" step="1" '
        f'data-initial="{value}" oninput="abRangeShow(this)">'
        f'<span class="ab-range-val">{value}{suffix}</span>'
        "</div></div>"
    )


def _setting_number(
    key: str, label: str, value: int, vmin: int, vmax: int, suffix: str, custom: bool, env_val
) -> str:
    mark = _custom_mark(key, custom, env_val)
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        '<div class="ab-range-wrap">'
        f'<input type="number" class="ab-in" min="{vmin}" max="{vmax}" value="{value}" step="1" '
        f'data-initial="{value}">'
        f"<span>{suffix}</span>"
        "</div></div>"
    )


def render(state, status: str = "", query: str = "") -> str:
    counts = state.counts()
    stats = library_stats(state)
    wanted = status if status in counts else ""
    units = state.all(wanted or None)
    total = sum(counts.values())

    # books with a manual conversion request are waiting on the one-at-a-time
    # encoder: they leave every list (the queue strip shows them instead) and
    # the visible counts stay honest
    queued = set(CONTROL.queue_snapshot()["units"])
    queued_by_status: dict[str, int] = {}
    all_units = state.all(None)
    if queued:
        for u in all_units:
            if u.path in queued:
                queued_by_status[u.status] = queued_by_status.get(u.status, 0) + 1
        units = [u for u in units if u.path not in queued]
        total -= queued_by_status.get(wanted, 0) if wanted else len(queued)
    queued_count = sum(queued_by_status.values())

    sort_column, sort_dir = read_sort(state)
    units = sort_units(units, sort_column, sort_dir)
    if query:
        units = [u for u in units if search_matches(u, query)]

    # chips
    chips = [
        '<a class="chip{on}" href="/">All <span class="n">{n}</span></a>'.format(
            on=" on" if not wanted else "", n=total
        )
    ]
    for name in STATUS_ORDER:
        visible = counts.get(name, 0) - queued_by_status.get(name, 0)
        if not visible:
            continue
        chips.append(
            '<a class="chip{on}" href="/?status={st}">{st} <span class="n">{n}</span></a>'.format(
                on=" on" if wanted == name else "", st=name, n=visible
            )
        )
    chips_html = "".join(chips)
    rc = rail_counts(state, counts)

    # problem units for shelf
    shelf_order = {"failed": 0, "held": 1, "review": 2}
    problem_units = [
        u for u in all_units if u.status in ("held", "review", "failed") and u.path not in queued
    ]
    problem_units.sort(key=lambda u: (shelf_order.get(u.status, 99), unit_title_key(u)))

    # table
    shown = len(units)
    units = units[:MAX_ROWS]
    overflow = (
        (
            f'<div class="ab-overflow">showing the first {MAX_ROWS} of {shown} - '
            "filter by status or use <code>/api/units?status=</code></div>"
        )
        if shown > MAX_ROWS
        else ""
    )

    def detail_panel(unit) -> str:
        try:
            data = json.loads(unit.detail or "{}")
        except ValueError:
            data = {}
        src = data.get("source") or {}
        out = data.get("output") or {}
        if not out and unit.output:
            out = {
                "path": unit.output,
                "mb": unit.out_mb,
                "hours": unit.hours,
                "chapters": unit.chapters,
                "chapters_from": "",
            }

        mb = src.get("mb") or unit.src_mb
        size = f"{mb:.0f} MB &middot; " if mb else ""
        codecs = " + ".join(src.get("codecs") or [])
        n_parts = src.get("parts", 1)
        n_files = src.get("files") or unit.files
        before = [
            "{nf} audio file{fs} in {np} part{ps}".format(
                nf=n_files, fs="" if n_files == 1 else "s", np=n_parts, ps="" if n_parts == 1 else "s"
            ),
            "{sz}{hr:.2f} hours of audio".format(sz=size, hr=src.get("hours") or unit.hours),
            ("Audio format: " + codecs if codecs else "Audio format not detected in the files")
            + (" &middot; {} ch".format(", ".join(src.get("channels") or [])) if src.get("channels") else ""),
            f'<span class="dim"><code>{_html(unit.path)}</code></span>',
        ]
        if out:
            after = [
                "<code>{nm}</code>".format(nm=_html(Path(out.get("path", "")).name)),
                "{mb:.0f} MB &middot; {hr:.2f} h".format(mb=out.get("mb") or 0, hr=out.get("hours") or 0),
                "{ch} chapters ({cf})".format(
                    ch=out.get("chapters", 0), cf=_html(str(out.get("chapters_from") or ""))
                ),
                '<span class="dim">{op}</span>'.format(op=_html(str(out.get("path") or ""))),
            ]
        else:
            after = [
                "Nothing converted yet &mdash; still in your source folder.",
                "Once it converts: <b>one M4B</b> with chapters, cover and tags, filed into your library.",
            ]

        match_data = data.get("match") or {}
        mtitle = match_data.get("title") or unit.title
        decision = [f"Audible match: <b>{_html(mtitle)}</b>"]
        authors = match_data.get("authors") or []
        if authors:
            decision.append("Author: <b>{a}</b>".format(a=_html("; ".join(authors))))
        narrators = match_data.get("narrators") or []
        if narrators:
            decision.append("Narrated by <b>{n}</b>".format(n=_html("; ".join(narrators))))
        if unit.confidence:
            decision.append(f"Match confidence: <b>{unit.confidence}</b>")
        if unit.asin:
            decision.append(
                f'ASIN <a class="ab-alink" href="https://www.audible.com/pd?asin={_attr(unit.asin)}" '
                f'target="_blank" rel="noopener">{_attr(unit.asin)}</a>'
            )
        else:
            decision.append("ASIN &mdash;")
        if unit.note:
            decision.append(_html(unit.note))

        tier = str(effective_val("auto_convert") or "high")
        why, todo = explain(unit.status, unit.confidence or "", unit.note, data, tier=tier)

        # status-specific plain-English section titles
        why_headers = {
            "review": "Why it's waiting for you",
            "held": "Why it's paused",
            "failed": "Why it failed",
            "ready": "Why nothing was written",
            "interrupted": "Why it stopped",
            "converted": "What happened",
        }
        why_header = why_headers.get(unit.status, "Status")

        meter = ""
        if unit.confidence in ("LOW", "MEDIUM", "HIGH"):
            cells = "".join(
                '<span class="{cls}">{t}</span>'.format(
                    cls="ab-on" if t == unit.confidence else "ab-off", t=t
                )
                for t in ("LOW", "MEDIUM", "HIGH")
            )
            meter = (
                f'<div class="ab-meter">{cells}</div>'
                '<div class="ab-meterhint">How sure we are this is the '
                "right book</div>"
            )

        # a manual re-check (Retry now / Use this ASIN) that still ended in a
        # decision-needed state says so up top
        banner = ""
        if data.get("reopen") and unit.status in ("review", "held", "failed"):
            tier = str(effective_val("auto_convert") or "high").lower()
            if unit.status == "review":
                if tier == "off":
                    btxt = (
                        "<b>Re-checked &mdash; still needs your decision.</b> "
                        "Auto-convert is off, so every book waits for your OK."
                    )
                else:
                    btxt = (
                        "<b>Re-checked &mdash; still not confident enough.</b> "
                        "ASIN {a} matches at <b>{c}</b>, below your {t} "
                        "auto-convert setting."
                    ).format(a=_html(unit.asin or "the pinned one"), c=unit.confidence or "?", t=tier)
            elif unit.status == "held":
                btxt = (
                    "<b>Re-checked &mdash; still not a runtime match.</b> "
                    "This edition still disagrees with the catalog runtime."
                )
            else:
                btxt = "<b>Re-checked &mdash; the conversion failed again.</b>"
            banner = f'<div class="ab-rebanner">{btxt}</div>'

        def block(title, items):
            return f"<div><h4>{title}</h4><ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul></div>"

        custom = overrides_mod.describe(state.get_overrides(unit.path))
        marked = overrides_mod.custom_fields(state.get_overrides(unit.path))

        def field(key, label_str, catalog_value, width_hint=""):
            mark = ('<span class="ab-custom" data-field="{k}"{h}>custom</span>').format(
                k=key, h="" if key in marked else " hidden"
            )
            placeholder = _attr(catalog_value or "catalog value")
            return (
                f'<div class="ab-field"><span class="ab-lab">{label_str}{mark}</span>'
                f'<input class="ab-in" data-field="{key}" value="{_attr(custom[key])}" '
                f'placeholder="{placeholder}" spellcheck="false"{width_hint}></div>'
            )

        # f-strings only: user text must never sit inside a str.format() template,
        # or one brace in a folder name or override takes the whole page down
        editor = (
            f'<div class="ab-edit" data-path="{_attr(unit.path)}"><h4>Metadata</h4>'
            '<p class="ab-hint">Overrides win over the catalog when this book is '
            "converted - tags, metadata.json and the author/title layout. "
            "Leave a box empty to use the catalog value; "
            "separate several names with a semicolon.</p>"
            '<div class="ab-grid">'
            + field("title", "title", match_data.get("title") or unit.title)
            + field(
                "authors",
                "author(s)",
                "; ".join(match_data.get("authors") or ([unit.author] if unit.author else [])),
            )
            + field("narrator", "narrator(s)", "; ".join(match_data.get("narrators") or []))
            + field("series", "series", "")
            + field("series_index", "series index", "")
            + '</div><div class="ab-editbar">'
            '<button class="ab-save" onclick="abSaveMeta(this)">Save metadata</button>'
            '<span class="ab-fb"></span></div></div>'
        )

        # other editions the catalog search also scored highly, with one-click pins
        editions_html = ""
        try:
            alternates = json.loads(unit.alternates) if getattr(unit, "alternates", "") else []
        except ValueError:
            alternates = []
        others = [a for a in alternates if a.get("asin") and a.get("asin") != unit.asin][:4]
        if others:
            edition_rows = []
            for alt in others:
                edition_rows.append(
                    (
                        '<div class="ab-edition">'
                        '<span class="ab-edmain"><b>{ti}</b>'
                        '<span class="ab-edsub">{au} &middot; '
                        '<a class="ab-alink" href="https://www.audible.com/pd?asin={a}" '
                        'target="_blank" rel="noopener">{a}</a></span></span>'
                        '<button title="Lock this book to this Audible entry, then re-check '
                        'against it." onclick="abUseEdition(this)" data-asin="{a}">'
                        "Use this edition</button></div>"
                    ).format(
                        ti=_html(str(alt.get("title") or "")),
                        au=_html("; ".join(alt.get("authors") or [])),
                        a=_attr(str(alt.get("asin"))),
                    )
                )
            editions_html = (
                '<div class="ab-editions"><h4>Other editions seen</h4>'
                + "".join(edition_rows)
                + '<p class="ab-hint">Audible entries the search also scored highly for '
                "this book - different narrations or editions of the same title. "
                "Pinning one locks the match permanently.</p></div>"
            )

        btn = '<button title="Re-check this book against the catalog and convert it if it passes the rules." onclick="abUnitAction(this,\'retry\')">Retry now</button>'
        force = '<button class="warn" title="Convert this book even though the automatic checks flagged it." onclick="abUnitAction(this,\'force\')">Convert anyway</button>'
        asin_btn = '<button title="Lock this catalog entry to the book, then re-check against it." onclick="abUnitAction(this,\'set_asin\')">Use this ASIN</button>'

        explain_html = (
            '<div class="explain">'
            + f"<div><h4>{why_header}</h4><p>{_html(why)}</p>{meter}</div>"
            + f'<div class="todo"><h4>Your options</h4><p>{_html(todo)}</p></div>'
            + "</div>"
        )
        actions = (
            '<div class="ab-actrow">'
            + force
            + btn
            + "</div>"
            + '<div class="ab-pinrow">'
            + '<span class="ab-pinlab">Lock this book to one exact Audible entry:</span>'
            + '<input data-asin value="{av}" placeholder="ASIN, e.g. B005EHN61O">'.format(
                av=_attr(unit.asin or "")
            )
            + asin_btn
            + '<span class="ab-pinhint">Locks the match permanently - every future '
            "scan checks this book against this exact entry instead of guessing. "
            "Passes your confidence setting &rarr; converts automatically. Still "
            "too unsure &rarr; this panel re-opens with the reason.</span>" + "</div>"
        )
        delete_act = (
            '<div class="acts">'
            '<button class="btn danger" title="Permanently delete this book\'s source folder '
            'and its published files. Cannot be undone." onclick="unitDelete(this)" '
            f'data-path="{_attr(unit.path)}" data-title="{_attr(unit.title or unit.path)}" '
            'style="margin-left:auto">Delete</button></div>'
        )
        reopen_attr = (
            ' data-reopen="1"' if data.get("reopen") and unit.status in ("review", "held", "failed") else ""
        )
        after_title = "What you got" if out else "What you'll get"
        toprow = (
            '<div class="ab-toprow">'
            + f'<div class="ab-topcov">{cover_html(unit, big=True)}</div>'
            + '<div class="ab-topmain">'
            + explain_html
            + actions
            + "</div>"
            + "</div>"
        )
        return (
            f'<tr class="drow" data-path="{_attr(unit.path)}"{reopen_attr} hidden>'
            f'<td colspan="{TOTAL_COLUMNS}">'
            f'<div class="dd" data-path="{_attr(unit.path)}">'
            + banner
            + toprow
            + block("Your source files", before)
            + block(after_title, after)
            + block("The Audible match", decision)
            + editions_html
            + editor
            + delete_act
            + "</div></td></tr>"
        )

    def row(unit) -> str:
        tier = str(effective_val("auto_convert") or "high")
        haystack = " ".join(filter(None, [unit.title, unit.author, unit.path]))
        data = " ".join(f'data-sort-{key}="{_attr(str(sort_value(unit, key)))}"' for key in SORT_COLUMNS)
        has_custom = bool(state.get_overrides(unit.path))
        badge = '<span class="ab-custom"{h}>custom</span>'.format(h="" if has_custom else " hidden")

        act_html = ""
        if unit.status in ("held", "review", "failed"):
            btn = '<button class="btn primary" title="Re-check this book against the catalog and convert it if it passes the rules." onclick="event.stopPropagation();abUnitAction(this,\'retry\')">Retry now</button>'
            force = '<button class="btn warn" title="Convert this book even though the automatic checks flagged it." onclick="event.stopPropagation();abUnitAction(this,\'force\')">Convert anyway</button>'
            asin_btn = '<button class="btn" title="Open this book\'s details and lock an Audible entry." onclick="event.stopPropagation();abEditAsin(this)">Edit ASIN</button>'
            act_html = '<div class="ab-rowact">' + btn + force + asin_btn + "</div>"

        reason_html = ""
        if unit.status in ("held", "review", "failed"):
            try:
                detail_data = json.loads(unit.detail or "{}")
            except ValueError:
                detail_data = {}
            why, _todo = explain(unit.status, unit.confidence or "", unit.note, detail_data, tier=tier)
            rc = " failed" if unit.status == "failed" else ""
            reason_html = f'<span class="ab-breason{rc}">{_html(why)}</span>'

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
            dp=_attr(unit.path),
            hs=_attr(haystack),
            da=data,
            cov=cover_html(unit),
            ti=_html(unit.title or unit.path),
            bd=badge,
            au=_html(unit.author or ""),
            rs=reason_html,
            st=unit.status,
            tc=_tier_class(unit.confidence),
            co=unit.confidence or "-",
            hr=unit.hours or 0,
            ag=_fmt_age(time.time() - (unit.updated_at or 0)),
            ac=act_html,
        )

    rows = "".join(row(u) + detail_panel(u) for u in units)

    # a re-opened panel shows its banner once; the flag is consumed so the next
    # page load starts collapsed again
    for u in units:
        try:
            flagged = bool(json.loads(u.detail or "{}").get("reopen"))
        except ValueError:
            flagged = False
        if flagged and u.status in ("review", "held", "failed"):
            state.clear_reopen(u.path)
    if not units:
        rows = f'<tr class="ab-empty"><td colspan="{TOTAL_COLUMNS}">no books match this filter</td></tr>'

    headers = '<th class="ab-covhead"></th>' + "".join(
        '<th class="ab-sort{on} {kl}" data-column="{k}">{lb}<span class="ab-arrow">{ar}</span></th>'.format(
            on=" ab-on" if key == sort_column else "",
            kl=klass,
            k=key,
            lb=label,
            ar="&#9660;" if (key == sort_column and sort_dir == "desc") else "&#9650;",
        )
        for key, label, _width, klass in COLUMNS
    )
    colgroup = (f'<col style="width:{COVER_WIDTH}">') + "".join(
        (f'<col style="width:{width}">') if width else "<col>" for _k, _l, width, _c in COLUMNS
    )

    # controls
    paused = CONTROL.paused
    scanning = CONTROL.scanning
    prog = CONTROL.scan_progress()
    scan_button = (
        '<button class="btn primary" title="Re-check every book in the library now and '
        'convert the ones that pass the automatic rules." '
        'onclick="act(\'scan\')" id="ab-scanbtn"{di}>{lbl}</button>'
    ).format(
        di=" disabled" if (paused or scanning) else "",
        lbl=("Scanning {} / {}&hellip;".format(prog["done"], prog["total"]) if scanning else "Scan now"),
    )
    recheck_button = (
        '<button class="btn" id="ab-recheckbtn" title="Re-check every book against '
        "Audible, including ones already decided - refreshes the match, the Other "
        'editions list and the chapters. Books still follow the auto-convert rules." '
        "onclick=\"act('recheck')\"{di2}>Re-check all</button>"
    ).format(di2=" disabled" if (paused or scanning) else "")
    pause_button = (
        '<button class="btn" title="Finish the book that is encoding, then stop converting '
        'until you press Resume." onclick="act(\'resume\')">Resume</button>'
        if paused
        else '<button class="btn warn" title="Finish the book that is encoding, then stop '
        'converting until you press Resume." onclick="act(\'pause\')">Pause after this '
        "book</button>"
    )
    dry_button = (
        '<button class="btn dry on" title="Preview mode: reports what it would do but writes '
        'nothing to the output library. Click to turn it off." onclick="dry(false)">'
        'Dry run <span class="ab-switch"></span></button>'
        if CONTROL.dry_run
        else '<button class="btn dry" title="Preview mode: reports what it would do but writes '
        'nothing to the output library. Click to turn it on." onclick="dry(true)">'
        'Dry run <span class="ab-switch"></span></button>'
    )
    dry_note = (
        '<span class="ab-note">Dry run is on &mdash; no files will be written</span>'
        if CONTROL.dry_run
        else ""
    )
    stop_button = (
        '<button class="btn danger" title="Abort the current conversion immediately: the '
        'partial file is discarded and the book is retried on the next scan." '
        'onclick="stop_now()">Stop now</button>'
        if CONTROL.running
        else '<button class="btn danger" disabled title="Abort the current conversion '
        "immediately: the partial file is discarded and the book is retried on the "
        'next scan. Nothing is encoding right now.">'
        "Stop now</button>"
    )

    # encode strip
    queue_strip = ""
    qsnap = CONTROL.queue_snapshot()
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
        if qsnap["count"]:
            meta_parts.append("<b>{}</b> waiting in queue".format(qsnap["count"]))

        title_html = _html(CONTROL.current_unit)
        queue_strip = (
            '<section class="ab-queue" aria-label="Currently encoding">'
            '<div class="ab-label"><span class="ab-pulse"></span> Now encoding</div>'
            '<div class="ab-qtitle">{ti}</div>'
            '<div class="ab-qact">'
            '<button class="btn" onclick="act(\'pause\')">Pause</button>'
            '<button class="btn danger" onclick="stop_now()">Stop</button>'
            "</div>"
            '<div class="ab-qmeta">{mp}</div>'
            '<div class="ab-qbarwrap">'
            '<div class="ab-qbar"><i id="ab-qfill" style="width:{pc:.1f}%"></i></div>'
            '<span class="ab-qpct" id="ab-qpct">{pc:.1f}%</span>'
            "</div></section>"
        ).format(ti=title_html, mp=" &middot; ".join(meta_parts), pc=percent)
    elif qsnap["count"]:
        nxt = state.get(qsnap["units"][0])
        queue_strip = (
            '<section class="ab-queue" aria-label="Conversion queue">'
            '<div class="ab-label"><span class="ab-pulse"></span> Waiting to convert</div>'
            '<div class="ab-qtitle">{ti}</div>'
            '<div class="ab-qmeta"><b>{n}</b> in queue</div>'
            "</section>"
        ).format(ti=_html(nxt.title if nxt and nxt.title else qsnap["units"][0]), n=qsnap["count"])

    # health
    set_effective(state)
    heartbeat_age = state.heartbeat_age()
    eff_interval = effective_val("scan_interval_min")
    healthy = heartbeat_age < eff_interval * 60 * 2.5 + 300
    health_stale = "" if healthy else " ab-stale"
    health_text = "Healthy" if healthy else "Stale"

    # header indicators
    dry_header = (
        ('<span style="font-family:var(--ab-mono);font-size:11px;color:var(--ab-amber)">dry run</span>')
        if CONTROL.dry_run
        else ""
    )

    return PAGE.format(
        styles=STYLES,
        script=SCRIPT,
        version=__version__,
        library=CONFIG.host_library or str(CONFIG.library_root),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=CONFIG.host_output or str(CONFIG.output_root),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        dry_text=dry_header,
        health_stale=health_stale,
        health_text=health_text,
        health_scan=_fmt_age(heartbeat_age),
        rail_total=total,
        rail_review_count=rc["review"],
        rail_held_count=rc["held"],
        rail_failed_count=rc["failed"],
        scan_button=scan_button,
        recheck_button=recheck_button,
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


def rail_counts(state, counts: dict) -> dict:
    """DB counts minus books currently waiting in the manual-conversion queue."""
    adjusted = {k: counts.get(k, 0) for k in ("review", "held", "failed")}
    queued = set(CONTROL.queue_snapshot()["units"])
    if queued:
        for u in state.all(None):
            if u.path in queued and u.status in adjusted:
                adjusted[u.status] -= 1
    return adjusted


def snapshot(state) -> dict:
    set_effective(state)
    return {
        "version": __version__,
        "counts": state.counts(),
        # cheap change signature: moves even when a re-check ends in the same status,
        # so the page notices a manual Retry / ASIN re-check and reloads
        "rev": state.rev(),
        "stats": library_stats(state),
        "last_run": state.last_run() or {},
        "heartbeat_age_seconds": state.heartbeat_age(),
        "library_root": str(CONFIG.library_root),
        "output_root": str(CONFIG.output_root),
        "auto_convert": effective_val("auto_convert"),
        "dry_run": CONTROL.dry_run,
        "settings": {sk: setting_effective(sk) for sk in SETTING_KEYS},
        **CONTROL.describe(),
    }


def run_manual(state, unit, force: bool = False) -> None:
    """A Retry / Use this ASIN / Convert anyway request, run off the request thread.

    The book leaves the queue however it ends - converted, still in review, held, a dry
    run, already published or an exception - or it would vanish from every list until a
    restart (render() hides queued books and shows them in the queue strip instead).
    """
    try:
        process_unit(state=state, unit=unit, log=_log, force=force, manual=True)
    except Exception:  # noqa: BLE001 - a background thread has nobody to raise to
        logging.getLogger("audiobind").exception("manual action for %s failed", unit.rel_path)
    finally:
        CONTROL.dequeue(unit.rel_path)


class Handler(BaseHTTPRequestHandler):
    state = None
    server_version = f"audiobind/{__version__}"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
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
            self._send_cover(params.get("asin", ""), params.get("path", ""))
            return
        if path.startswith("/api/units"):
            units = self.state.all(status or None)
            payload = [
                {
                    "path": u.path,
                    "title": u.title,
                    "author": u.author,
                    "status": u.status,
                    "confidence": u.confidence,
                    "asin": u.asin,
                    "hours": u.hours,
                    "files": u.files,
                    "note": u.note,
                    "output": u.output,
                    "src_mb": u.src_mb,
                    "out_mb": u.out_mb,
                    "chapters": u.chapters,
                }
                for u in units
            ]
            self._send(
                200, json.dumps({"count": len(payload), "units": payload}).encode(), "application/json"
            )
            return
        if path.startswith("/api/state"):
            self._send(200, json.dumps(snapshot(self.state)).encode(), "application/json")
            return
        if path.startswith("/healthz"):
            set_effective(self.state)
            eff_interval = effective_val("scan_interval_min")
            healthy = self.state.heartbeat_age() < eff_interval * 60 * 2.5 + 300
            self._send(200 if healthy else 503, b"ok" if healthy else b"stale", "text/plain")
            return
        if path == "/settings" or path.startswith("/settings"):
            body = render_settings(self.state)
            self._send(200, body.encode(), "text/html; charset=utf-8")
            return
        body = render(self.state, status, params.get("q", ""))
        self._send(200, body.encode(), "text/html; charset=utf-8")

    def _send_cover(self, asin: str, rel_path: str) -> None:
        try:
            found = resolve_cover(self.state, asin=asin, rel_path=rel_path)
        except Exception:  # noqa: BLE001
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
                "metadata overrides saved for %s: %s", rel, sorted(clean) or "none"
            )
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "unit": rel,
                        "custom": sorted(overrides_mod.custom_fields(clean)),
                        "overrides": overrides_mod.describe(clean),
                    }
                ).encode(),
                "application/json",
            )
            return

        if action == "delete":
            if not payload.get("confirm"):
                self._send(400, b'{"error":"confirmation required"}', "application/json")
                return

            # Refuse while encoding
            if CONTROL.running and CONTROL.current_unit == rel:
                self._send(
                    409, b'{"error":"cannot delete a book that is currently encoding"}', "application/json"
                )
                return

            # Validate unit exists in state
            unit_state = self.state.get(rel)
            if unit_state is None:
                self._send(404, b'{"error":"no such unit"}', "application/json")
                return

            # Path traversal guard: resolve source inside LIBRARY_ROOT
            lib_root = Path(CONFIG.library_root).resolve()
            src_path = (lib_root / rel.split("#", 1)[0]).resolve()
            try:
                src_path.relative_to(lib_root)
            except ValueError:
                self._send(400, b'{"error":"source path escapes library root"}', "application/json")
                return

            # What this book owns on disk - its own folder or part folders, never a parent it
            # shares with sibling books. Each target must sit strictly inside LIBRARY_ROOT.
            unit = find_unit(CONFIG.library_root, CONFIG.ignore_globs, rel)
            targets: list[Path] = []
            if unit is not None:
                owned = owned_paths(unit)
                if any(Path(p).resolve() == lib_root for p in owned):
                    owned = [os.path.join(unit.parent, part.name) for part in unit.parts]
                for p in owned:
                    resolved = Path(p).resolve()
                    try:
                        resolved.relative_to(lib_root)
                    except ValueError:
                        self._send(400, b'{"error":"source path escapes library root"}', "application/json")
                        return
                    if resolved == lib_root:
                        self._send(400, b'{"error":"refusing to delete the library root"}', "application/json")
                        return
                    targets.append(Path(p))
                foreign = foreign_audio(unit, [str(t) for t in targets])
                if foreign:
                    self._send(
                        409,
                        json.dumps(
                            {
                                "error": f"this folder also holds {len(foreign)} audio file(s) that belong "
                                "to other books - nothing was deleted. Remove this book's files by hand."
                            }
                        ).encode(),
                        "application/json",
                    )
                    return

            # Path traversal guard: resolve output inside OUTPUT_ROOT
            out_root = Path(CONFIG.output_root).resolve()
            out_trash = None
            if unit_state.output:
                out_path = Path(unit_state.output).resolve()
                try:
                    out_path.relative_to(out_root)
                except ValueError:
                    self._send(400, b'{"error":"output path escapes output root"}', "application/json")
                    return
                out_parent = out_path.parent
                if out_parent != out_root:
                    out_trash = out_parent

            # HARD DELETE: the published output is removed permanently and the book is
            # forgotten. The source folder is removed too when the library mount allows
            # writes; on a read-only library (the documented, recommended setup) the
            # source stays on disk and the book reappears on the next scan until the
            # operator removes the folder from the share. The path guards above are the
            # only thing between this call and the filesystem, so they must stay strict.
            log = logging.getLogger("audiobind")
            source_removed = False
            source_kept_reason = ""
            if targets:
                before = files_under([str(t) for t in targets])
                error: OSError | None = None
                for target in targets:
                    try:
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(str(target))
                        elif target.exists() or target.is_symlink():
                            target.unlink()
                    except OSError as exc:
                        error = exc
                        break
                remaining = {f for f in before if os.path.lexists(f)}
                if error is None:
                    source_removed = True
                    log.info("HARD DELETE %s; source removed: %s", rel, ", ".join(map(str, targets)))
                elif remaining and remaining != before:
                    # some files are already gone: never call that "kept", and keep the row
                    # (and the published output) so the operator can see and finish the job
                    log.error("HARD DELETE %s; source PARTIALLY removed: %s", rel, error)
                    self._send(
                        500,
                        json.dumps(
                            {
                                "error": f"the source was only partly deleted ({len(before) - len(remaining)} "
                                f"of {len(before)} files removed) before this error: {error}. "
                                "Nothing else was touched - fix the permissions and delete again.",
                                "source_partial": True,
                            }
                        ).encode(),
                        "application/json",
                    )
                    return
                elif error.errno == errno.EROFS or "Read-only" in str(error):
                    source_kept_reason = "your source library is mounted read-only"
                    log.info("HARD DELETE %s; source kept: %s", rel, error)
                elif error.errno in (errno.EACCES, errno.EPERM):
                    source_kept_reason = "Audiobind doesn't have permission to delete the source folder"
                    log.info("HARD DELETE %s; source kept: %s", rel, error)
                else:
                    log.error("HARD DELETE %s; source removal failed: %s", rel, error)
                    self._send(
                        500,
                        json.dumps({"error": f"source removal failed: {error}"}).encode(),
                        "application/json",
                    )
                    return
            elif src_path.exists() and "#" not in rel:
                # a row whose folder no longer holds a book we recognise: never rmtree a
                # folder we cannot prove belongs to this one book
                source_kept_reason = "the folder no longer holds this book's audio, so it was left alone"
                log.info("HARD DELETE %s; source left alone - no matching book on disk at %s", rel, src_path)
            else:
                log.info("HARD DELETE %s; source folder not found at %s", rel, src_path)

            output_removed = "absent"
            if out_trash is not None and out_trash.exists():
                try:
                    shutil.rmtree(str(out_trash))
                    output_removed = "removed"
                    log.info("HARD DELETE %s; output removed: %s", rel, out_trash)
                except OSError as exc:
                    output_removed = f"failed: {exc}"
                    log.error("HARD DELETE %s; could not remove output %s: %s", rel, out_trash, exc)
            else:
                log.info("HARD DELETE %s; output -> none", rel)

            # Remove from state
            self.state.delete(rel)
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "deleted": rel,
                        "source_removed": source_removed,
                        "source_kept": bool(source_kept_reason),
                        "source_kept_reason": source_kept_reason,
                        "output": output_removed,
                    }
                ).encode(),
                "application/json",
            )
            return

        unit = find_unit(CONFIG.library_root, CONFIG.ignore_globs, rel)
        if unit is None:
            self._send(404, b'{"error":"no such unit"}', "application/json")
            return

        if action == "set_asin":
            asin = str(payload.get("asin", "")).strip().upper()
            if not valid_asin(asin):
                self._send(
                    400,
                    b'{"error":"that is not an ASIN - it is 8-14 letters and digits, like B005EHN61O"}',
                    "application/json",
                )
                return
            self.state.set_asin_override(rel, asin)
            action = "retry"
        if action not in ("retry", "force"):
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return

        logging.getLogger("audiobind").info("manual %s requested for %s", action, rel)
        CONTROL.enqueue(rel)
        threading.Thread(
            target=run_manual,
            args=(self.state, unit),
            kwargs={"force": action == "force"},
            daemon=True,
            name="manual-" + action,
        ).start()
        self._send(200, json.dumps({"ok": True, "action": action, "unit": rel}).encode(), "application/json")

    def _handle_settings(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        # Reset: clear all six setting keys and hand back the live environment values
        if payload.get("reset"):
            for key in SETTING_KEYS:
                self.state.set_meta(f"setting_{key}", None)
            set_effective(self.state)
            self._send(
                200,
                json.dumps({"ok": True, "env_defaults": setting_env_defaults()}).encode(),
                "application/json",
            )
            return

        settings = payload.get("settings") or {}
        if not isinstance(settings, dict):
            self._send(400, b'{"error":"settings must be an object"}', "application/json")
            return

        errors = []
        validated = {}

        # selects: one of the offered choices (which include an unusual env value)
        for key, fold in (("auto_convert", str.upper), ("audio_bitrate", str.lower), ("region", str.lower)):
            if key not in settings:
                continue
            choices = setting_choices(key)
            v = fold(str(settings[key]))
            match = next((c for c in choices if fold(c) == v), None)
            if match is None:
                errors.append(f"{key}: must be one of {', '.join(choices)}")
            else:
                validated[key] = fold(match)

        # numbers: whole numbers inside the bounds (widened to fit the env value)
        for key in ("runtime_delta", "scan_interval", "nice"):
            if key not in settings:
                continue
            lo, hi = setting_bounds(key)
            try:
                v = int(settings[key])
                if v < lo or v > hi:
                    raise ValueError
                validated[key] = v
            except (ValueError, TypeError):
                errors.append(f"{key}: whole number {lo}-{hi}")

        if errors:
            self._send(400, json.dumps({"error": "; ".join(errors)}).encode(), "application/json")
            return

        for key, value in validated.items():
            self.state.set_meta(f"setting_{key}", value)

        set_effective(self.state)
        # every setting with a saved override, not just the ones in this request: the page
        # posts only the fields you changed
        custom = [key for key in SETTING_KEYS if is_custom(SETTING_MAP[key])]
        self._send(200, json.dumps({"ok": True, "custom": custom}).encode(), "application/json")

    def _post_refusal(self) -> tuple[int, str] | None:
        """Cross-site request guard for every POST: (status, error) to refuse, else None.

        * The body must be declared ``application/json``. A browser can only send that
          cross-origin after a CORS preflight, which this server never answers, so a page
          on another site cannot fire a no-cors ``text/plain`` POST at the API.
        * When the browser says where the request came from (Origin, else Referer), that
          host must be this server's own Host (or X-Forwarded-Host behind a proxy).
          Requests with neither header - curl, scripts - stay allowed.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return 415, "requests must be sent as application/json"
        source = self.headers.get("Origin")
        if source is None:
            source = self.headers.get("Referer")
        if source is None:
            return None
        claimed = urlsplit(source.strip()).netloc.lower() if source.strip() not in ("", "null") else ""
        allowed = {
            h.strip().lower()
            for h in (self.headers.get("Host") or "", *(self.headers.get("X-Forwarded-Host") or "").split(","))
            if h.strip()
        }
        if not claimed or claimed not in allowed:
            return 403, "cross-site request refused"
        return None

    def do_POST(self):  # noqa: N802
        refusal = self._post_refusal()
        if refusal is not None:
            code, message = refusal
            self._send(code, json.dumps({"error": message}).encode(), "application/json")
            return
        if self.path.startswith("/api/unit"):
            self._handle_unit()
            return
        if self.path.startswith("/api/settings"):
            self._handle_settings()
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
        elif action == "recheck":
            CONTROL.request_scan(force=True)
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
        self._send(200, json.dumps({"ok": True, **CONTROL.describe()}).encode(), "application/json")

    def log_message(self, *args):
        return


def serve(state):
    Handler.state = state
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.web_port), Handler)
    if CONFIG.web_port == 0:
        CONFIG.web_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
