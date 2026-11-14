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

from . import __version__, matching
from . import ingest as ingest_mod
from . import auth as auth_mod
from . import discover as discover_mod
from . import overrides as overrides_mod
from . import series as series_mod
from . import watcher as watcher_mod
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
    valid_ip_or_empty,
)
from .control import CONTROL
from .covers import resolve as resolve_cover
from .covers import valid_asin
from .pipeline import process_unit
from .state import LOCKABLE_FIELDS, SOURCE_INGEST, key_source
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


.ab-tablewrap{flex:1;min-width:0;padding:0 24px 24px}
.ab-tbl{width:100%;border-collapse:collapse;table-layout:fixed}
.ab-tbl thead th{position:sticky;top:0;background:var(--ab-bg);z-index:2;cursor:pointer;user-select:none;white-space:nowrap;
  text-align:left;font-size:10.5px;font-weight:500;text-transform:uppercase;letter-spacing:.7px;
  color:var(--ab-text4);padding:6px 8px;border-bottom:1px solid var(--ab-border);}
.ab-tbl thead th:hover{color:var(--ab-text2)}
.ab-tbl thead th.ab-on{color:var(--ab-text2)}
.ab-tbl thead th button.ab-sortbtn{height:auto;padding:0;border:none;background:none;border-radius:3px;
  font:inherit;font-size:inherit;font-weight:inherit;text-transform:inherit;letter-spacing:inherit;
  color:inherit;gap:4px;cursor:pointer;}
.ab-tbl thead th button.ab-sortbtn:hover{background:none;color:var(--ab-text2)}
.ab-sortbtn .ab-arrow{color:var(--ab-accent-bright)}
.ab-tbl tbody td{padding:0 8px;overflow:hidden;border-bottom:1px solid var(--ab-border-subtle);vertical-align:middle;white-space:nowrap;}
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

.ab-num{font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text2);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.ab-scan{font-family:var(--ab-mono);font-size:11px;color:var(--ab-text4);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.ab-dash{color:var(--ab-text4)}

/* one labeled status dot per row: color + word (+ age); only a moving state pulses */
.ab-st{display:inline-flex;align-items:center;max-width:100%;overflow:hidden;
  color:var(--ab-text2);font-size:12px;white-space:nowrap}
.ab-st-dot{width:8px;height:8px;border-radius:50%;flex:none;margin-right:7px;background:var(--ab-text4)}
.ab-st-ok .ab-st-dot{background:var(--ab-green)}
.ab-st-hold .ab-st-dot{background:var(--ab-blue)}
.ab-st-rev .ab-st-dot{background:var(--ab-amber)}
.ab-st-err .ab-st-dot{background:var(--ab-red)}
.ab-st-enc .ab-st-dot{background:var(--ab-accent-bright)}
.ab-st-moving .ab-st-dot{animation:ab-st-pulse 1.6s ease-in-out infinite}
@keyframes ab-st-pulse{0%,100%{opacity:1}50%{opacity:.45}}
.ab-st-age{color:var(--ab-text4);font-size:10.5px;margin-left:5px;font-variant-numeric:tabular-nums;
  overflow:hidden;text-overflow:ellipsis}
.ab-match{min-width:0;font-size:12px;color:var(--ab-text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-match-cur{color:var(--ab-blue)}
.ab-conf-high{color:var(--ab-green)}
.ab-conf-medium{color:var(--ab-text2)}
.ab-conf-low{color:var(--ab-amber)}
.ab-conf-none{color:var(--ab-text4)}

.ab-actcol{padding:0 8px 0 0!important;width:48px;position:relative;overflow:visible!important;text-align:right}
.ab-kebabwrap{position:relative;display:inline-block;vertical-align:middle}
button.ab-kebab{width:28px;height:28px;padding:0;justify-content:center;border-radius:6px;
  border:1px solid var(--ab-border);background:var(--ab-surface);color:var(--ab-text3);
  font-size:14px;line-height:1}
button.ab-kebab:hover, button.ab-kebab[aria-expanded="true"]{color:var(--ab-text);
  border-color:rgba(255,255,255,.2);background:var(--ab-surface)}
.ab-menu{position:absolute;right:0;top:calc(100% + 4px);z-index:30;width:200px;padding:6px;
  background:var(--ab-surface2);border:1px solid var(--ab-border);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.5);text-align:left;display:flex;flex-direction:column;gap:1px}
.ab-menu[hidden]{display:none}
.ab-menu button{display:flex;width:100%;height:auto;padding:6px 8px;border:none;border-radius:5px;
  background:none;color:var(--ab-text2);font-size:12.5px;font-weight:400;text-align:left}
.ab-menu button:hover, .ab-menu button:focus-visible{background:rgba(255,255,255,.05);color:var(--ab-text);
  outline:none}
.ab-menu button.ab-menu-danger{color:var(--ab-red)}
.ab-menu button.ab-menu-danger:hover, .ab-menu button.ab-menu-danger:focus-visible{background:var(--ab-red-soft)}

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
.ab-ev{grid-column:1/-1;border-top:1px solid #1c2230;padding-top:14px;max-width:860px;}
.ab-ev h4.ab-evh2{margin-top:18px;}
.ab-evwin{display:flex;gap:14px;align-items:center;background:var(--ab-surface);
  border:1px solid rgba(94,106,210,.45);border-radius:8px;padding:12px 14px;}
.ab-evwin .ab-covwrap{width:56px;height:56px;flex:none;}
.ab-evmeta{flex:1;min-width:0;}
.ab-evt{font-size:13.5px;font-weight:600;color:var(--ab-text);}
.ab-eva{color:var(--ab-text2);font-size:12.5px;margin-top:2px;}
.ab-evm{color:var(--ab-text3);font-size:11.5px;margin-top:4px;font-family:var(--ab-mono);}
.ab-evscore{flex:none;text-align:right;}
.ab-evv{font-family:var(--ab-mono);font-size:20px;font-weight:600;color:var(--ab-green);}
.ab-evv.ab-mid{color:var(--ab-amber);}
.ab-evl{font-size:10px;letter-spacing:1px;color:var(--ab-text3);text-transform:uppercase;}
.ab-evbars{margin-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:10px 22px;}
.ab-evbar{min-width:0;}
.ab-evlbl{display:flex;justify-content:space-between;font-size:12px;color:var(--ab-text2);margin-bottom:4px;}
.ab-evlbl em{font-style:normal;font-family:var(--ab-mono);color:var(--ab-text3);font-size:11.5px;}
.ab-evtrack{height:5px;border-radius:3px;background:rgba(255,255,255,.06);overflow:hidden;}
.ab-evtrack i{display:block;height:100%;border-radius:3px;background:var(--ab-green);}
.ab-evtrack i.ab-mid{background:var(--ab-amber);}
.ab-evtrack i.ab-bad{background:var(--ab-red);}
.ab-evtrack i.ab-acc{background:var(--ab-accent);}
.ab-evnote{font-size:11.5px;color:var(--ab-text4);margin-top:4px;}
.ab-evnote b{color:var(--ab-amber);font-weight:600;}
.ab-evrival{display:flex;gap:12px;align-items:center;background:var(--ab-surface2);
  border:1px solid var(--ab-border-subtle);border-radius:8px;padding:10px 12px;margin-top:8px;}
.ab-evrival .ab-evmeta b.ab-evrt{display:block;font-size:12.5px;font-weight:600;color:var(--ab-text2);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ab-evwhy{color:var(--ab-text3);font-size:11.5px;margin-top:2px;}
.ab-evwhy b{color:var(--ab-amber);font-weight:600;}
.ab-evsc{font-family:var(--ab-mono);font-size:12px;color:var(--ab-text3);flex:none;}
.ab-evrival button{height:28px;padding:0 12px;font-size:12px;border-radius:6px;flex:none;}
.ab-ko{display:inline-block;margin-right:6px;padding:0 5px;border:1px solid currentColor;border-radius:4px;
  font-size:10px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--ab-amber);
  vertical-align:1px;}
@media (max-width:700px){.ab-evbars{grid-template-columns:1fr;}}
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
.ab-ingest-badge{font-size:10px;font-weight:700;letter-spacing:.4px;white-space:nowrap;
  color:#b39af2;background:#231c3a;border:1px solid #3b2f63;border-radius:999px;
  padding:1px 7px;margin-left:8px;vertical-align:1px;}
button.ab-lock{margin-left:auto;height:auto;flex:none;display:inline-flex;align-items:center;gap:6px;
  font:600 10.5px var(--ab-sans);letter-spacing:.4px;text-transform:none;
  padding:3px 9px;border-radius:10px;border:none;cursor:pointer;}
button.ab-lock.ab-lock-on{color:var(--ab-green);background:var(--ab-green-soft)}
button.ab-lock.ab-lock-on:hover{color:var(--ab-green);background:rgba(39,166,68,.22)}
button.ab-lock.ab-lock-off{color:var(--ab-text4);background:rgba(255,255,255,.04)}
button.ab-lock.ab-lock-off:hover{color:var(--ab-text3);background:rgba(255,255,255,.07)}
.ab-pinrow button.ab-lock{margin-left:0}
.ab-field.ab-locked .ab-in{border-color:rgba(39,166,68,.35)}
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
.ab-acct{margin-top:28px;padding-top:4px;border-top:1px solid var(--ab-border)}
.ab-acct-head{font-size:15px;font-weight:600;margin:14px 0 4px}
.ab-acct-who{font-size:12.5px;color:var(--ab-text3);margin-bottom:6px}
.ab-acct-who b{color:var(--ab-text2);font-weight:500}
.ab-acct-form{display:flex;flex-direction:column;gap:8px;padding:14px 0;
  border-bottom:1px solid var(--ab-border-subtle)}
.ab-acct-form:last-of-type{border-bottom:none}
.ab-acct-title{font-size:13px;font-weight:500;color:var(--ab-text)}
.ab-acct-lbl{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--ab-text3)}
.ab-acct-lbl .ab-in{max-width:300px}
.ab-acct-row{display:flex;align-items:center;gap:10px;margin-top:2px}
.ab-overflow{font-family:var(--ab-mono);font-size:11.5px;color:var(--ab-text4);padding:8px 24px;flex:none;}
#ab-toast{position:fixed;right:18px;bottom:18px;z-index:50;max-width:420px;
  background:var(--ab-surface2);border:1px solid rgba(242,163,60,.4);border-radius:8px;
  padding:10px 14px;font-size:12.5px;color:var(--ab-amber);
  opacity:0;transform:translateY(6px);pointer-events:none;
  transition:opacity .15s,transform .15s;}
#ab-toast.on{opacity:1;transform:none}

.ab-series-wrap{padding:18px 24px 24px;display:flex;flex-direction:column;gap:18px}
.ab-series-intro{font-size:12.5px;color:var(--ab-text3);max-width:1180px;line-height:1.5}
.ab-series-shelf{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;max-width:1180px}
.ab-series-card{background:var(--ab-panel);border:1px solid var(--ab-border);border-radius:10px;
  padding:14px 16px;transition:border-color .3s,box-shadow .3s;scroll-margin:24px}
.ab-series-card.ab-series-flash{border-color:var(--ab-accent-bright);box-shadow:0 0 0 3px rgba(94,106,210,.35)}
.ab-series-name{font-size:14px;font-weight:600;letter-spacing:-.1px;color:var(--ab-text)}
a.ab-series-name{display:block;text-decoration:none;cursor:pointer;border-radius:3px}
a.ab-series-name:hover{color:var(--ab-accent-bright);text-decoration:underline}
a.ab-series-name:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:2px}
.ab-series-sub{color:var(--ab-text3);font-size:11.5px;margin-top:2px}
.ab-series-count{display:flex;align-items:baseline;gap:8px;margin:10px 0 12px}
.ab-series-count b{font-family:var(--ab-mono);font-size:18px;font-weight:600;color:var(--ab-text)}
.ab-series-of{color:var(--ab-text4);font-size:12px}
.ab-series-missing{color:var(--ab-amber);background:var(--ab-amber-soft);font-size:10.5px;font-weight:600;
  letter-spacing:.6px;padding:2px 8px;border-radius:4px}
.ab-series-complete{color:var(--ab-green);background:var(--ab-green-soft);font-size:10.5px;font-weight:600;
  letter-spacing:.6px;padding:2px 8px;border-radius:4px}
.ab-series-strip{display:flex;flex-wrap:wrap;gap:5px}
.ab-series-tile{width:34px;height:51px;border-radius:3px;overflow:hidden;position:relative;
  background:var(--ab-surface2);flex:none}
.ab-series-tile img{width:100%;height:100%;object-fit:cover;display:block}
.ab-series-tile img.ab-gone, .ab-series-row img.ab-gone{visibility:hidden}
.ab-series-n{position:absolute;left:0;right:0;bottom:0;font-family:var(--ab-mono);font-size:8.5px;
  color:var(--ab-text);background:rgba(0,0,0,.55);text-align:center;padding:1px 0}
.ab-series-tile.ab-series-gap{border:1px dashed rgba(242,163,60,.5);background:var(--ab-amber-soft);
  display:flex;align-items:center;justify-content:center}
.ab-series-qn{font-family:var(--ab-mono);font-size:9.5px;color:var(--ab-amber)}
.ab-series-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-top:12px}
.ab-series-link{color:var(--ab-accent-bright);font-size:12px;font-weight:600;text-decoration:none}
.ab-series-link:hover{text-decoration:underline}
.ab-series-dim{color:var(--ab-text4);font-size:11px;font-weight:600}
.ab-series-panel{max-width:1180px;background:var(--ab-panel);border:1px solid var(--ab-border);
  border-radius:10px;padding:16px 18px;scroll-margin:24px}
.ab-series-panel h3{font-size:12px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;
  color:var(--ab-text3);margin:0 0 10px}
.ab-series-row{display:flex;gap:12px;align-items:center;padding:8px 10px;border-radius:8px}
.ab-series-row:nth-child(even){background:var(--ab-surface2)}
.ab-series-row img{width:30px;height:45px;border-radius:3px;object-fit:cover;flex:none;background:var(--ab-surface2)}
.ab-series-seq{font-family:var(--ab-mono);font-size:12px;color:var(--ab-text3);width:34px;flex:none;text-align:right}
.ab-series-meta{flex:1;min-width:0}
.ab-series-t{font-size:13px;font-weight:600;letter-spacing:-.1px;color:var(--ab-text)}
.ab-series-m{color:var(--ab-text4);font-size:11px;font-family:var(--ab-mono);margin-top:2px}
.ab-series-pill{font-size:10px;font-weight:600;letter-spacing:.5px;color:var(--ab-amber);
  background:var(--ab-amber-soft);padding:2px 8px;border-radius:4px;flex:none}
.ab-series-pill.ab-series-have{color:var(--ab-green);background:var(--ab-green-soft)}
.ab-series-pill.ab-series-wanted{color:var(--ab-accent-bright);background:rgba(94,106,210,.14)}
.ab-series-src{color:var(--ab-text4);font-size:11px;margin:-6px 0 10px}
.ab-series-unk{color:var(--ab-text3);background:var(--ab-surface2);font-size:10.5px;font-weight:600;
  letter-spacing:.5px;padding:2px 8px;border-radius:4px;text-transform:uppercase}
.ab-series-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11.5px;color:var(--ab-text4);
  margin:0 0 8px}
.ab-series-bar .ab-spacer{flex:1}
.ab-series-bar button{height:28px;padding:0 12px;font-size:11.5px;font-weight:600}
.ab-series-row button{height:28px;padding:0 11px;font-size:11.5px;font-weight:600;flex:none}
.ab-series-kpi{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 4px}
.ab-series-kpi b{font-family:var(--ab-mono);font-size:16px;color:var(--ab-amber)}
.ab-series-kpi span{display:block;font-size:10.5px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--ab-text4);margin-top:2px}
.ab-series-extra{margin-top:10px}
.ab-series-extra summary{cursor:pointer;font-size:11.5px;color:var(--ab-text4);padding:4px 0}
.ab-series-extra summary:hover{color:var(--ab-text2)}
.ab-series-extra .ab-series-row{opacity:.6}
.ab-series-marks{margin-top:12px;font-size:11.5px;color:var(--ab-text4);display:flex;flex-wrap:wrap;
  align-items:center;gap:6px 10px}
.ab-series-marks button{height:22px;padding:0 8px;font-size:11px}
.ab-series-empty{background:var(--ab-surface);border:1px solid var(--ab-border);border-radius:10px;
  padding:16px 18px;font-size:12.5px;color:var(--ab-text3);max-width:1180px}
.ab-series-hook{grid-column:1/-1;background:var(--ab-surface);border:1px solid rgba(94,106,210,.45);
  border-radius:8px;padding:12px 14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.ab-series-hooktxt{flex:1;min-width:200px}
.ab-series-hookname{font-size:13px;font-weight:600;color:var(--ab-text)}
.ab-series-hooksub{color:var(--ab-text3);font-size:11.5px;margin-top:2px}
.ab-series-hook .ab-series-strip{max-width:420px}
.ab-series-hook .ab-series-tile{width:30px;height:45px}
.ab-series-hook .ab-series-n{font-size:8px}
.ab-series-hook .ab-series-qn{font-size:9px}
.ab-series-hook .btn{flex:none;text-decoration:none}

.ab-disc-wrap{padding:14px 24px 24px;display:flex;flex-direction:column;gap:14px;max-width:1180px}
.ab-disc-tabs .chip{text-decoration:none}
.ab-disc-intro{font-size:12.5px;color:var(--ab-text3);line-height:1.5}
.ab-disc-intro b{color:var(--ab-text2);font-weight:500}
.ab-disc-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ab-disc-form .ab-search{width:280px}
.ab-disc-form .ab-search.ab-disc-author{width:220px}
.ab-disc-panel{background:var(--ab-panel);border:1px solid var(--ab-border);border-radius:10px;padding:14px 16px}
.ab-disc-panel h3{font-size:12px;font-weight:600;letter-spacing:.4px;text-transform:uppercase;
  color:var(--ab-text3);margin:0 0 8px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.ab-disc-panel h3 .ab-disc-why{font-size:11.5px;font-weight:400;letter-spacing:0;text-transform:none;color:var(--ab-text4)}
.ab-disc-panel h4{font-size:12.5px;font-weight:600;color:var(--ab-text2);margin:12px 0 4px}
.ab-disc-panel h4 span{font-weight:400;color:var(--ab-text4);font-size:11.5px;margin-left:6px}
.ab-disc-row{display:flex;gap:12px;align-items:center;padding:7px 10px;border-radius:8px}
.ab-disc-row:nth-child(even){background:var(--ab-surface2)}
.ab-disc-row img{width:30px;height:45px;border-radius:3px;object-fit:cover;flex:none;background:var(--ab-surface2)}
.ab-disc-row img.ab-gone{visibility:hidden}
.ab-disc-meta{flex:1;min-width:0}
.ab-disc-t{font-size:13px;font-weight:600;letter-spacing:-.1px;color:var(--ab-text);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-disc-a{font-size:11.5px;color:var(--ab-text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-disc-m{color:var(--ab-text4);font-size:11px;font-family:var(--ab-mono);margin-top:1px}
.ab-disc-seq{font-family:var(--ab-mono);font-size:12px;color:var(--ab-text3);width:34px;flex:none;text-align:right}
.ab-disc-pill{font-size:10px;font-weight:600;letter-spacing:.5px;padding:2px 8px;border-radius:4px;flex:none;
  color:var(--ab-text3);background:rgba(255,255,255,.05)}
.ab-disc-pill.ab-disc-have{color:var(--ab-green);background:var(--ab-green-soft)}
.ab-disc-pill.ab-disc-want{color:var(--ab-accent-bright);background:rgba(94,106,210,.14)}
.ab-disc-row button{height:28px;padding:0 11px;font-size:11.5px;font-weight:600;flex:none}
.ab-disc-empty{font-size:12.5px;color:var(--ab-text3);padding:6px 0}
.ab-disc-empty.ab-disc-err{color:var(--ab-amber)}
.ab-disc-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11.5px;color:var(--ab-text4)}
.ab-disc-bar b{color:var(--ab-text2);font-weight:500}
.ab-disc-bar .ab-spacer{flex:1}
.ab-disc-bar a{color:var(--ab-accent-bright);text-decoration:none}
.ab-disc-pill.ab-wl-downloading{color:var(--ab-accent-bright);background:rgba(94,106,210,.14)}
.ab-disc-pill.ab-wl-in_library{color:var(--ab-green);background:var(--ab-green-soft)}
.ab-disc-pill.ab-wl-not_found{color:var(--ab-amber);background:var(--ab-amber-soft)}
.ab-disc-pill.ab-wl-error{color:var(--ab-red);background:var(--ab-red-soft)}
.ab-wl-note{font-size:11px;color:var(--ab-text3);margin-top:2px;line-height:1.4;overflow-wrap:anywhere}
.ab-wl-note.ab-wl-bad{color:var(--ab-amber)}
.ab-wl-acts{display:flex;gap:6px;flex:none}
.ab-watch-grid{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:12px 0;font-size:12.5px}
.ab-watch-grid dt{color:var(--ab-text3)}
.ab-watch-grid dd{margin:0;color:var(--ab-text2);overflow-wrap:anywhere}
.ab-watch-warn{border-color:rgba(242,163,60,.35);background:var(--ab-amber-soft);color:var(--ab-amber);margin:10px 0}
.ab-watch-warn b{color:var(--ab-amber)}

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
function abWatcherDry(on) {
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action:'watcher_dry_run', value: on})})
    .then(r => r.json())
    .then(() => location.reload())
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
    note = 'setting ASIN ' + body.asin + ' - re-checking…';
  } else if (action === 'force') {
    note = 'queued - converting…';
  } else if (action === 'recheck') {
    note = 're-checking against Audible…';
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
  if (trigger && trigger.dataset.source === 'ingest') {
    // a seeding torrent is never deleted: no "permanently deletes" modal, just the server's answer
    fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                       body: JSON.stringify({action:'delete', path: path})})
      .then(r => r.json())
      .then(result => abToast(result.error || 'delete is for library sources; remove the torrent in qBittorrent'))
      .catch(() => abToast('delete is for library sources; remove the torrent in qBittorrent'));
    return;
  }
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
                ') - it will reappear on the next scan unless you remove it from the share.' +
                (result.output === 'kept' ? ' Output kept: ' + result.output_kept_reason + '.' : ''));
      } else if (result.output === 'kept') {
        abToast('Deleted. Output kept: ' + result.output_kept_reason + '.');
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
  btn.textContent = 'setting\\u2026';
  abToast('setting ASIN ' + asin + ' - re-checking\\u2026');
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
        ? 'saved and locked - applied on the next conversion'
        : 'saved - all fields back to the catalog';
      abMarkLocks(panel.closest('.dd') || panel, res.body.locked || res.body.custom);
      setTimeout(() => { feedback.textContent = ''; feedback.className = 'ab-fb'; }, 6000);
    })
    .catch(() => {
      btn.disabled = false;
      feedback.className = 'ab-fb ab-bad';
      feedback.textContent = 'could not reach the daemon';
    });
}
// the lock chips (editor fields and the ASIN row) and the row's lock badge follow the server
function abSetLockChip(chip, on) {
  chip.classList.toggle('ab-lock-on', on);
  chip.classList.toggle('ab-lock-off', !on);
  chip.textContent = on ? '\\ud83d\\udd12 Locked by you' : '\\ud83d\\udd13 Unlocked';
  const asin = chip.dataset.field === 'asin';
  chip.title = on
    ? 'A rescan, re-check or re-rip will never overwrite this. ' +
      (asin ? 'Click to unlock: the next re-check matches this book on its own.'
            : 'Click to unlock and hand it back to the catalog.')
    : (asin ? 'Matched automatically. Use this ASIN to lock one exact entry.'
            : 'Follows the catalog. Edit and save it to lock it.');
  const field = chip.closest('.ab-field');
  if (field) field.classList.toggle('ab-locked', on);
}
function abMarkLocks(scope, locked) {
  scope.querySelectorAll('button.ab-lock').forEach(chip => {
    abSetLockChip(chip, locked.indexOf(chip.dataset.field) !== -1);
  });
  const path = scope.dataset.path;
  const row = abFindRow(path);
  const badge = row && row.querySelector('.ab-custom');
  if (badge) badge.hidden = locked.length === 0;
}
function abLockChip(chip) {
  const scope = chip.closest('.dd');
  if (!scope) return;
  const field = chip.dataset.field;
  if (!chip.classList.contains('ab-lock-on')) {
    // unlocked: editing is how a field gets locked, so take the operator to the box
    const input = field === 'asin'
      ? scope.querySelector('input[data-asin]')
      : scope.querySelector('input.ab-in[data-field="' + field + '"]');
    if (input) input.focus();
    return;
  }
  chip.disabled = true;
  fetch('/api/unit', {method:'POST', headers:{'Content-Type':'application/json'},
                     body: JSON.stringify({action:'unlock', path: scope.dataset.path, field: field})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      chip.disabled = false;
      if (!res.ok) { abToast(res.body.error || 'could not unlock'); return; }
      if (field !== 'asin') {
        const input = scope.querySelector('input.ab-in[data-field="' + field + '"]');
        if (input) input.value = '';
      }
      abMarkLocks(scope, res.body.locked || []);
      abToast(field === 'asin'
        ? 'Unlocked - Re-check against Audible to match this book on its own again'
        : 'Unlocked - this field follows the catalog again');
    })
    .catch(() => { chip.disabled = false; abToast('could not reach the daemon'); });
}
// the row's visible kebab menu: Open details / Edit ASIN / Re-check against Audible / Delete...
function abMenuOpen() { return document.querySelector('.ab-menu:not([hidden])'); }
function abMenuClose(restoreFocus) {
  const menu = abMenuOpen();
  if (!menu) return;
  menu.hidden = true;
  const kebab = menu.parentElement.querySelector('.ab-kebab');
  if (kebab) {
    kebab.setAttribute('aria-expanded', 'false');
    if (restoreFocus) kebab.focus();
  }
}
function abMenuToggle(kebab) {
  const menu = kebab.parentElement.querySelector('.ab-menu');
  if (!menu) return;
  const wasOpen = !menu.hidden;
  abMenuClose(false);
  if (wasOpen) return;
  menu.hidden = false;
  kebab.setAttribute('aria-expanded', 'true');
  const first = menu.querySelector('[role="menuitem"]');
  if (first) first.focus();
}
function abMenuPick(item) {
  const wrap = item.closest('.ab-kebabwrap');
  const kebab = wrap && wrap.querySelector('.ab-kebab');
  const row = item.closest('tr.urow');
  const act = item.dataset.act;
  abMenuClose(false);
  if (!row || !kebab) return;
  if (act === 'open') {
    const panel = row.nextElementSibling;
    if (!panel || !panel.classList.contains('drow')) return;
    panel.hidden = false;
    row.classList.add('ab-sel');
    panel.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  } else if (act === 'asin') {
    abEditAsin(row);
  } else if (act === 'recheck') {
    abUnitAction(item, 'recheck');
  } else if (act === 'delete') {
    // the kebab is the trigger: Cancel returns focus to something that is still visible
    abOpenDeleteModal(kebab.dataset.path, kebab.dataset.title || kebab.dataset.path, kebab);
  }
}
document.addEventListener('click', function(e) {
  if (!e.target.closest || !e.target.closest('.ab-kebabwrap')) abMenuClose(false);
});
document.addEventListener('keydown', function(e) {
  const menu = abMenuOpen();
  if (!menu) {
    if (e.key === 'ArrowDown' && e.target.classList && e.target.classList.contains('ab-kebab')) {
      e.preventDefault();
      abMenuToggle(e.target);
    }
    return;
  }
  const items = [...menu.querySelectorAll('[role="menuitem"]')];
  const at = items.indexOf(document.activeElement);
  if (e.key === 'Escape') {
    e.preventDefault();
    abMenuClose(true);
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    items[(at + 1) % items.length].focus();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    items[(at - 1 + items.length) % items.length].focus();
  } else if (e.key === 'Home') {
    e.preventDefault();
    items[0].focus();
  } else if (e.key === 'End') {
    e.preventDefault();
    items[items.length - 1].focus();
  } else if (e.key === 'Tab') {
    abMenuClose(false);
  }
});
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
    th.setAttribute('aria-sort', on ? (abSort.dir === 'desc' ? 'descending' : 'ascending') : 'none');
    // the arrow marks the active sort only
    const arrow = th.querySelector('.ab-arrow');
    if (arrow) arrow.textContent = on ? (abSort.dir === 'desc' ? '\\u25bc' : '\\u25b2') : '';
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
  // each sortable header holds a real <button>: mouse, Enter and Space all land here
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
  if (abMenuOpen()) return true;
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
function abAuthed(r) {
  if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
  return r.json();
}
function tick() {
  fetch('/api/state').then(abAuthed).then(d => {
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
  fetch('/api/state').then(abAuthed).then(d => {
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
    {series_item}
    {discover_item}
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
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{ingest_line}{signout}
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
    <colgroup>{colgroup}<col class="ab-actcol" style="width:48px"></colgroup>
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
    {series_item}
    {discover_item}
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
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{signout}
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
    {watcher_section}
    {account_section}
  </section>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>
<script>
{settings_script}
{account_script}
{watcher_script}
</script>
</body></html>"""

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &middot; Sign in</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}
body{{align-items:center;justify-content:center;overflow:auto}}
.ab-login{{width:340px;max-width:calc(100vw - 32px);background:var(--ab-panel);
  border:1px solid var(--ab-border);border-radius:12px;padding:28px 24px}}
.ab-login h1{{font-size:17px;font-weight:600;margin-bottom:4px}}
.ab-login p{{color:var(--ab-text3);font-size:12.5px;margin-bottom:18px}}
.ab-login label{{display:block;color:var(--ab-text3);font-size:11.5px;margin-bottom:6px}}
.ab-login .ab-in{{width:100%;margin-bottom:14px}}
.ab-login .ab-save{{width:100%;justify-content:center}}
.ab-login .ab-err{{color:var(--ab-red);font-size:12.5px;margin:-4px 0 12px}}
</style></head><body>
<main class="ab-login">
  <h1>Audiobind</h1>
  <p>{login_hint}</p>
  <form method="post" action="/login">
    <label for="ab-username">Username</label>
    <input class="ab-in" id="ab-username" name="username" type="text" value="{login_username}"
           autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="64"
           autofocus required>
    <label for="ab-password">Password</label>
    <input class="ab-in" id="ab-password" name="password" type="password"
           autocomplete="current-password" required>
    {login_error}
    <button class="ab-save" type="submit">Sign in</button>
  </form>
</main>
</body></html>"""

SETUP_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &#183; Create your account</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}
body{{align-items:center;justify-content:center;overflow:auto}}
.ab-setup{{width:380px;max-width:calc(100vw - 32px);background:var(--ab-panel);
  border:1px solid var(--ab-border);border-radius:12px;padding:28px 24px}}
.ab-setup h1{{font-size:17px;font-weight:600;margin-bottom:4px}}
.ab-setup p{{color:var(--ab-text3);font-size:12.5px;line-height:1.5;margin-bottom:18px}}
.ab-setup label{{display:block;color:var(--ab-text3);font-size:11.5px;margin-bottom:6px}}
.ab-setup .ab-in{{width:100%;margin-bottom:14px}}
.ab-setup .ab-save{{width:100%;justify-content:center}}
.ab-setup .ab-setup-err{{color:var(--ab-red);font-size:12.5px;margin:-4px 0 12px}}
</style></head><body>
<main class="ab-setup">
  <h1>Welcome to Audiobind</h1>
  <p>Create the account that signs in to this server. There is only one account; you can
  change its username and password later in Settings.</p>
  <form method="post" action="/setup">
    <label for="ab-setup-username">Username</label>
    <input class="ab-in" id="ab-setup-username" name="username" type="text" value="{setup_username}"
           autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="64"
           autofocus required>
    <label for="ab-setup-password">Password (at least {setup_min} characters)</label>
    <input class="ab-in" id="ab-setup-password" name="password" type="password"
           autocomplete="new-password" minlength="{setup_min}" required>
    <label for="ab-setup-confirm">Confirm password</label>
    <input class="ab-in" id="ab-setup-confirm" name="confirm" type="password"
           autocomplete="new-password" minlength="{setup_min}" required>
    {setup_error}
    <button class="ab-save" type="submit">Create account</button>
  </form>
</main>
</body></html>"""


def render_login(error: str = "", username: str = "", legacy: bool = False) -> str:
    err = f'<div class="ab-err" role="alert">{_html(error)}</div>' if error else ""
    hint = (
        "Sign in with any username and this server's access token (AUTH_TOKEN) as the password."
        if legacy
        else "Sign in to continue."
    )
    return LOGIN_PAGE.format(
        styles=STYLES, login_error=err, login_username=_attr(username), login_hint=_html(hint)
    )


def render_setup(error: str = "", username: str = "") -> str:
    err = f'<div class="ab-setup-err" role="alert">{_html(error)}</div>' if error else ""
    return SETUP_PAGE.format(
        styles=STYLES, setup_error=err, setup_username=_attr(username), setup_min=auth_mod.PASSWORD_MIN
    )


def signout_html(state=None) -> str:
    """The rail's sign-out link - shown whenever signing in is in force."""
    if not CONFIG.auth_token and not (state is not None and state.has_credentials()):
        return ""
    return '<br><a href="/logout" style="color:var(--ab-text3)">sign out</a>'


def render_account_section(state) -> str:
    """Settings > Account: change password / change username (both need the current
    password). Before an account exists - an install still signing in with AUTH_TOKEN -
    it offers to create one instead, confirmed with the token."""
    creds = state.get_credentials()
    min_len = auth_mod.PASSWORD_MIN
    if creds is None:
        if not CONFIG.auth_token:
            return ""
        return ACCOUNT_LEGACY_HTML.format(acct_min=min_len)
    return ACCOUNT_HTML.format(acct_user=_html(creds.username), acct_min=min_len)


ACCOUNT_HTML = """<section class="ab-acct" aria-labelledby="ab-acct-head">
  <h2 class="ab-acct-head" id="ab-acct-head">Account</h2>
  <div class="ab-acct-who">Signed in as <b id="ab-acct-user">{acct_user}</b>.
    Changing the password or username signs out every other browser; this one stays signed in.</div>
  <form class="ab-acct-form" data-acct="password" onsubmit="return abAccountSubmit(this)">
    <span class="ab-acct-title">Change password</span>
    <label class="ab-acct-lbl">Current password
      <input class="ab-in" name="current" type="password" autocomplete="current-password" required></label>
    <label class="ab-acct-lbl">New password (at least {acct_min} characters)
      <input class="ab-in" name="new_password" type="password" autocomplete="new-password"
             minlength="{acct_min}" required></label>
    <label class="ab-acct-lbl">Confirm new password
      <input class="ab-in" name="confirm" type="password" autocomplete="new-password"
             minlength="{acct_min}" required></label>
    <div class="ab-acct-row"><button class="ab-save" type="submit">Change password</button>
      <span class="ab-fb" data-acct-fb></span></div>
  </form>
  <form class="ab-acct-form" data-acct="username" onsubmit="return abAccountSubmit(this)">
    <span class="ab-acct-title">Change username</span>
    <label class="ab-acct-lbl">New username
      <input class="ab-in" name="username" type="text" autocomplete="username" autocapitalize="none"
             spellcheck="false" maxlength="64" required></label>
    <label class="ab-acct-lbl">Current password
      <input class="ab-in" name="current" type="password" autocomplete="current-password" required></label>
    <div class="ab-acct-row"><button class="ab-save" type="submit">Change username</button>
      <span class="ab-fb" data-acct-fb></span></div>
  </form>
  <div class="ab-setting-hint">Forgot the password? Run <code>audiobind reset-password</code>
    inside the container (<code>docker exec -it audiobind audiobind reset-password</code>).</div>
</section>"""

ACCOUNT_LEGACY_HTML = """<section class="ab-acct" aria-labelledby="ab-acct-head">
  <h2 class="ab-acct-head" id="ab-acct-head">Account</h2>
  <div class="ab-acct-who">You are signed in with the access token (AUTH_TOKEN), which is
    deprecated. Create an account to sign in with a username and password instead. Once it
    exists, AUTH_TOKEN stops working and every other browser is signed out.</div>
  <form class="ab-acct-form" data-acct="create" onsubmit="return abAccountSubmit(this)">
    <span class="ab-acct-title">Create account</span>
    <label class="ab-acct-lbl">Username
      <input class="ab-in" name="username" type="text" autocomplete="username" autocapitalize="none"
             spellcheck="false" maxlength="64" required></label>
    <label class="ab-acct-lbl">Password (at least {acct_min} characters)
      <input class="ab-in" name="new_password" type="password" autocomplete="new-password"
             minlength="{acct_min}" required></label>
    <label class="ab-acct-lbl">Confirm password
      <input class="ab-in" name="confirm" type="password" autocomplete="new-password"
             minlength="{acct_min}" required></label>
    <label class="ab-acct-lbl">Current access token
      <input class="ab-in" name="current" type="password" autocomplete="current-password" required></label>
    <div class="ab-acct-row"><button class="ab-save" type="submit">Create account</button>
      <span class="ab-fb" data-acct-fb></span></div>
  </form>
</section>"""

ACCOUNT_SCRIPT = """
// ---- Settings > Account: each form posts JSON to /api/account; passwords never leave the form otherwise
function abAccountSubmit(form) {
  const fb = form.querySelector('[data-acct-fb]');
  const body = {action: form.dataset.acct};
  form.querySelectorAll('input').forEach(el => { body[el.name] = el.value; });
  fb.className = 'ab-fb';
  fb.textContent = 'saving...';
  fetch('/api/account', {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify(body)})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    })
    .then(out => {
      if (!out.ok) {
        fb.className = 'ab-fb ab-bad';
        fb.textContent = out.res.error || 'could not save';
        return;
      }
      form.reset();
      if (form.dataset.acct === 'create') { location.reload(); return; }
      const who = document.getElementById('ab-acct-user');
      if (who && out.res.username) who.textContent = out.res.username;
      fb.className = 'ab-fb ab-good';
      fb.textContent = form.dataset.acct === 'username' ? 'username changed' : 'password changed';
      setTimeout(() => { fb.textContent = ''; fb.className = 'ab-fb'; }, 6000);
    })
    .catch(err => {
      if (String(err && err.message) === 'signed out') return;
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
    });
  return false;
}
"""


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

SERIES_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &middot; Series</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand"><a href="/" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item" href="/">
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    {series_item}
    {discover_item}
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
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{signout}
  </div>
</aside>
<main class="ab-main">
  <header class="ab-topbar">
    <h1>Series</h1>
    <span class="ab-version">v{version}</span>
    <div class="ab-right"></div>
  </header>
  <div class="ab-series-wrap">
{body}
  </div>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>
<script>
{series_script}
</script>
</body></html>"""

SERIES_SCRIPT = """
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
// Copy Audible link: the clipboard API only exists on https or localhost, and this page is
// usually opened over plain http on the LAN - so fall back to a hidden textarea copy
function abSeriesCopy(btn) {
  const row = btn.closest('[data-link]');
  if (!row) return;
  const link = row.dataset.link;
  const done = () => abToast('Audible link copied - paste it into your browser');
  const fallback = () => {
    const box = document.createElement('textarea');
    box.value = link;
    box.setAttribute('readonly', '');
    box.style.position = 'fixed';
    box.style.opacity = '0';
    document.body.appendChild(box);
    box.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(box);
    if (ok) done(); else abToast('Copy this link: ' + link);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(link).then(done, fallback);
  } else {
    fallback();
  }
}
// I have this / Undo: a per-ASIN "owned without a rip" marker kept by the server
function abSeriesMark(btn, on) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'saving…';
  fetch('/api/series', {method:'POST', headers:{'Content-Type':'application/json'},
                       body: JSON.stringify({action: on ? 'mark_owned' : 'unmark_owned',
                                             asin: row.dataset.asin})})
    .then(r => r.json().then(res => ({ok: r.ok, res: res})))
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      abToast(on ? 'Marked as yours - it no longer counts as missing' : 'Back on your missing list');
      setTimeout(() => location.reload(), 700);
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}
// Want / Want all missing: onto the wishlist (Discover > Wishlist); nothing is downloaded
function abSeriesPost(body) {
  return fetch('/api/wishlist', {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify(body)})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    });
}
function abSeriesWanted(count) {
  const el = document.getElementById('ab-rail-wanted');
  if (el && typeof count === 'number') { el.textContent = count; el.hidden = count < 1; }
}
function abSeriesWant(btn) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'adding\\u2026';
  abSeriesPost({asin: row.dataset.asin, origin: 'series'})
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      // a marker, not a disabled button: a disabled one would hold off the auto-reload
      const pill = document.createElement('span');
      pill.className = 'ab-series-pill ab-series-wanted';
      pill.textContent = 'On your wishlist';
      btn.replaceWith(pill);
      abSeriesWanted(out.res.count);
      abToast(out.res.added ? 'Added to your wishlist' : 'Already on your wishlist');
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}
function abSeriesWantAll(btn) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'adding\\u2026';
  abSeriesPost({series: btn.dataset.series, missing: 'all'})
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      const n = (out.res.added || []).length;
      abSeriesWanted(out.res.count);
      abToast(n ? (n === 1 ? 'Added 1 book to your wishlist' : 'Added ' + n + ' books to your wishlist')
                : 'They are all on your wishlist already');
      setTimeout(() => location.reload(), 700);
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}
// /series#jack-reacher (from a book's "See what you're missing"): scroll to that card and
// light it up for a moment; the hash is then dropped so a later refresh does not jump back
function abSeriesFocus() {
  let slug = '';
  try { slug = decodeURIComponent((location.hash || '').slice(1)); } catch (e) { slug = ''; }
  if (!slug || slug.indexOf('ab-series-') === 0) return;
  const card = document.getElementById('ab-series-' + slug);
  if (!card) return;
  card.scrollIntoView({block: 'center', behavior: 'smooth'});
  card.classList.remove('ab-series-flash');
  void card.offsetWidth;
  card.classList.add('ab-series-flash');
  setTimeout(() => card.classList.remove('ab-series-flash'), 2400);
  try { history.replaceState(null, '', location.pathname); } catch (e) {}
}
document.addEventListener('DOMContentLoaded', abSeriesFocus);
window.addEventListener('hashchange', abSeriesFocus);
// reload when the server-side picture moves (a series list arrived, a book was marked,
// a scan changed the library) - but never while you have something open or in progress
function abSeriesBusy() {
  if (document.querySelector('details.ab-series-extra[open]')) return true;
  if (document.querySelector('.ab-series-row button:disabled, .ab-series-marks button:disabled')) return true;
  const el = document.activeElement;
  if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT')) return true;
  return false;
}
// stale series listings are fetched in the background; asking is a POST, never a GET
document.addEventListener('DOMContentLoaded', () => {
  fetch('/api/series', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({action:'refresh'})}).catch(() => {});
});
let abSeriesSig = null;
function abSeriesWatch() {
  fetch('/api/state').then(r => {
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    return r.json();
  }).then(d => {
    const sig = JSON.stringify(d.counts) + '|' + (d.rev || '');
    if (abSeriesSig === null) { abSeriesSig = sig; return; }
    if (sig !== abSeriesSig && !abSeriesBusy()) location.reload();
  }).catch(() => {});
}
setInterval(abSeriesWatch, 5000);
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


# --- table semantics kit (queue hygiene, 0.15.0-dev3) -------------------------------------
# Numbers that align, empty cells that admit it, ages that read like a person wrote them.

#: an unknown or empty value is an explicit em-dash - never blank, "N/A" or "null"
DASH = '<span class="ab-dash">&mdash;</span>'
#: ages read relative ("5h ago") up to this many days old, then as the date itself
AGE_CUTOFF_DAYS = 7
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def rel_age(timestamp, now: float | None = None, waiting: bool = False) -> str:
    """"2d ago" / "5h ago" / "waiting 3h" within the 7-day cutoff; the date after it.

    Plain text (no HTML). Empty when the time is unknown - the caller renders the dash.
    """
    try:
        ts = float(timestamp or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    now = time.time() if now is None else float(now)
    seconds = max(0.0, now - ts)
    if seconds > AGE_CUTOFF_DAYS * 86400:
        then = time.localtime(ts)
        date = f"{_MONTHS[then.tm_mon - 1]} {then.tm_mday}"
        if then.tm_year != time.localtime(now).tm_year:
            date += f", {then.tm_year}"
        return f"waiting since {date}" if waiting else date
    if seconds < 60:
        return "waiting <1m" if waiting else "just now"
    if seconds < 3600:
        span = f"{int(seconds // 60)}m"
    elif seconds < 86400:
        span = f"{int(seconds // 3600)}h"
    else:
        span = f"{int(seconds // 86400)}d"
    return f"waiting {span}" if waiting else f"{span} ago"


def fmt_hours(hours) -> str:
    """"13.18 h" (HTML); the dash when the length is unknown."""
    value = _num(hours)
    return f"{value:.2f} h" if value > 0 else DASH


def fmt_mb(mb) -> str:
    """"1,856 MB" (HTML); the dash when the size is unknown."""
    value = _num(mb)
    return f"{value:,.0f} MB" if value > 0 else DASH


#: status -> (word, dot family, pulses). The word is always printed beside the dot (color is
#: never the only signal); only a state that is still moving pulses, settled ones hold steady.
STATUS_DOTS = {
    "converted": ("Converted", "ok", False),
    "held": ("Held", "hold", False),
    "review": ("Review", "rev", True),
    "failed": ("Failed", "err", False),
    "ready": ("Ready", "enc", False),
    "interrupted": ("Interrupted", "rev", False),
}
#: states that wait on the operator read "waiting 3h" instead of "3h ago"
WAITING_STATUSES = {"review"}


def status_dot(status: str, updated_at=0.0, now: float | None = None) -> str:
    """One labeled dot: color + word + age. Replaces the old per-row status pill."""
    name = (status or "").lower()
    label, family, moving = STATUS_DOTS.get(name, (name.capitalize() or "Unknown", "idle", False))
    age = rel_age(updated_at, now, waiting=name in WAITING_STATUSES)
    classes = f"ab-st ab-st-{family}" + (" ab-st-moving" if moving else "")
    title = label + (f" · {age}" if age else "")
    age_html = f'<span class="ab-st-age">{_html(age)}</span>' if age else ""
    return (
        f'<span class="{classes}" title="{_attr(title)}">'
        f'<span class="ab-st-dot" aria-hidden="true"></span>{_html(label)}{age_html}</span>'
    )


#: confidence is always spelled out, capital C - never a bare LOW/MEDIUM/HIGH in the table
CONFIDENCE_WORDS = {
    "HIGH": ("High Confidence", "ab-conf-high"),
    "MEDIUM": ("Medium Confidence", "ab-conf-medium"),
    "LOW": ("Low Confidence", "ab-conf-low"),
}


def series_label(detail: dict, overrides: dict) -> str:
    """"Jack Reacher #14" - the operator's series wins over the catalog's. Plain text."""
    match = (detail or {}).get("match") or {}
    overrides = overrides or {}
    name = str(overrides.get("series") or match.get("series") or "").strip()
    if not name:
        return ""
    seq = str(overrides.get("series_index") or match.get("sequence") or "").strip()
    return f"{name} #{seq}" if seq else name


def match_cell(unit, detail: dict, overrides: dict, asin_override: str = "") -> tuple[str, str]:
    """(HTML, plain-text title) for the Match column.

    A hand-set ASIN reads "Current ASIN · <n> Confidence"; an automatic match reads
    "<series #n> · <n> Confidence". No match at all is the dash.
    """
    conf = (unit.confidence or "").upper()
    match = (detail or {}).get("match") or {}
    conf_html = conf_text = ""
    if conf in CONFIDENCE_WORDS:
        conf_text, cls = CONFIDENCE_WORDS[conf]
        conf_html = f'<span class="{cls}">{conf_text}</span>'
    elif conf == "COLLECTION":
        conf_text = "Looks like a collection"
        conf_html = f'<span class="ab-conf-none">{conf_text}</span>'
    elif asin_override and match.get("asin_pin_failed"):
        conf_text = "Not in Audible's catalog"
        conf_html = f'<span class="ab-conf-low">{_html(conf_text)}</span>'

    if asin_override:
        lead_html = '<span class="ab-match-cur">Current ASIN</span>'
        lead_text = f"Current ASIN {asin_override} (you set it)"
    else:
        lead_text = series_label(detail, overrides) if conf_html else ""
        lead_html = _html(lead_text)

    parts_html = [p for p in (lead_html, conf_html) if p]
    if not parts_html:
        return DASH, ""
    return " &middot; ".join(parts_html), " · ".join(p for p in (lead_text, conf_text) if p)


LOCK_ON_TEXT = "\U0001f512 Locked by you"
LOCK_OFF_TEXT = "\U0001f513 Unlocked"


def lock_chip(field: str, locked: bool) -> str:
    """The per-field chip: green "Locked by you" (click to unlock) or muted "Unlocked"."""
    if locked:
        cls, text = "ab-lock-on", LOCK_ON_TEXT
        title = "A rescan, re-check or re-rip will never overwrite this. " + (
            "Click to unlock: the next re-check matches this book on its own."
            if field == "asin"
            else "Click to unlock and hand it back to the catalog."
        )
    else:
        cls, text = "ab-lock-off", LOCK_OFF_TEXT
        title = (
            "Matched automatically. Use this ASIN to lock one exact entry."
            if field == "asin"
            else "Follows the catalog. Edit and save it to lock it."
        )
    return (
        f'<button type="button" class="ab-lock {cls}" data-field="{_attr(field)}" '
        f'title="{title}" onclick="abLockChip(this)">{text}</button>'
    )


def is_ingest_row(unit) -> bool:
    """A row whose source is a torrent in INGEST_DIR (the key prefix says so even if the column lags)."""
    return getattr(unit, "source", "") == SOURCE_INGEST or key_source(unit.path) == SOURCE_INGEST


#: on the table row and the shelf card: an ingest book never blurs with a library one
INGEST_BADGE = (
    '<span class="ab-ingest-badge" title="From the ingest folder: a downloaded torrent that is '
    'still seeding. Audiobind only reads it.">&#8675; ingest</span>'
)


def kebab_menu(unit) -> str:
    """The visible row menu that replaced the hover-only buttons."""
    path = _attr(unit.path)
    title = _attr(unit.title or unit.path)
    # the delete item reads this: an ingest row's Delete asks the server, which refuses
    source_attr = f' data-source="{SOURCE_INGEST}"' if is_ingest_row(unit) else ""
    return (
        '<div class="ab-kebabwrap" onclick="event.stopPropagation()">'
        f'<button type="button" class="ab-kebab" aria-haspopup="menu" aria-expanded="false" '
        f'aria-label="Actions for {title}" title="Actions" data-path="{path}" data-title="{title}"{source_attr} '
        'onclick="abMenuToggle(this)">&#8942;</button>'
        '<div class="ab-menu" role="menu" hidden>'
        '<button type="button" role="menuitem" data-act="open" onclick="abMenuPick(this)">Open details</button>'
        '<button type="button" role="menuitem" data-act="asin" onclick="abMenuPick(this)" '
        'title="Open this book\'s details and set its exact Audible entry.">Edit ASIN</button>'
        '<button type="button" role="menuitem" data-act="recheck" onclick="abMenuPick(this)" '
        'title="Ask Audible about this one book again: refreshes the match, the other editions and '
        "the confidence. An ASIN you set is kept. It still follows your auto-convert rules.\">"
        "Re-check against Audible</button>"
        '<button type="button" role="menuitem" class="ab-menu-danger" data-act="delete" '
        'onclick="abMenuPick(this)">Delete&hellip;</button>'
        "</div></div>"
    )


STATUS_ORDER =("converted", "ready", "review", "held", "failed", "interrupted")
MAX_ROWS = 400
MAX_SHELF_CARDS = 12

COLUMNS = (
    ("title", "Book", "min(560px,30vw)", ""),
    # fixed widths only where the content is short and must stay on one line ("13.18 h",
    # "Converted 2d ago"); Match has no width so it absorbs whatever room is left and
    # ellipsizes (full text on hover) - the table must never outgrow the page
    ("status", "Status", "140px", ""),
    ("tier", "Match", "", "ab-hidemd"),
    ("runtime", "Length", "84px", ""),
    ("scan", "Last scan", "90px", "ab-hidemd"),
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


# --- "Why this match?" evidence panel ---------------------------------------------------
# Display only: every number here was computed by the matcher before it decided. Copy is
# operator-facing (approved mockup, 0.15.0) - plain, second person, no developer shorthand.

#: the score multiplier the matcher applies to non-English products, as a whole percent
PENALTY_PCT = int(round((1 - matching.LANGUAGE_PENALTY) * 100))


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value) -> int:
    return int(round(_num(value) * 100))


def _signed(n: int) -> str:
    """+13 / −22 (a real minus sign, like the approved copy)."""
    return f"+{n}" if n >= 0 else f"−{abs(n)}"


def _hrs(hours) -> str:
    return f"{_num(hours):.1f}"


def _tolerance() -> float:
    return _num(effective_val("max_runtime_delta"), 0.25)


def _over_tolerance(local_hours, catalog_hours, tolerance: float) -> bool:
    """Out of tolerance by the exact rule the pipeline holds books on (not the rounded %)."""
    local, cat = _num(local_hours), _num(catalog_hours)
    if local <= 0 or cat <= 0:
        return False
    return not matching.runtime_ok(local, cat, tolerance)


def _has_evidence(data: dict) -> bool:
    """Rows recorded before 0.15.0 carry no evidence; they keep the old panel until re-checked."""
    return isinstance(data, dict) and "title_sim" in data


#: the badge on a catalog entry the matcher flagged as a knockoff (fixed copy, no escaping needed)
KNOCKOFF_BADGE = (
    '<span class="ab-ko" title="A summary, review, study guide or zero-runtime catalog entry - '
    'not an edition of the book. It never counts against the match margin.">Knockoff</span>'
)


def knockoff_badge(entry: dict) -> str:
    return KNOCKOFF_BADGE if isinstance(entry, dict) and entry.get("knockoff") else ""


def rival_reason(
    alt: dict,
    local_hours: float,
    tolerance: float,
    winner_score: float = 1.0,
    author_known: bool = True,
    winner_corroborated: bool = False,
) -> str:
    """One plain-English line (HTML) saying why this candidate lost to the pick.

    Holds only numbers and fixed copy - never catalog or folder text - so it needs no escaping.
    Empty for a pre-0.15.0 row with no evidence.
    """
    if not _has_evidence(alt):
        return ""
    title, author = _num(alt.get("title_sim")), _num(alt.get("author_sim"))
    clauses: list[str] = []
    if alt.get("knockoff"):
        clauses.append("A summary, review or catalog stub, not an edition — it never counts against the pick")
    elif winner_corroborated and alt.get("corroborated") is False:
        clauses.append("Audnex doesn&#8217;t back this entry, and it does back the pick")
    if author_known and author >= 0.9 and title < 0.6:
        clauses.append(f"Title match only <b>{_pct(title)}%</b> — a different book by the same author")
    if alt.get("language_penalized"):
        penalty = f"<b>−{PENALTY_PCT}% penalty</b>"
        if clauses:
            clauses.append(f"wrong language — {penalty}")
        elif title >= 0.6:
            clauses.append(f"Right title, wrong language — {penalty}")
        else:
            clauses.append(f"Wrong language — {penalty}")
    delta = alt.get("runtime_delta_pct")
    if delta is not None and _over_tolerance(local_hours, alt.get("hours"), tolerance):
        # same measure as the hold rule and the Runtime bar: your files against this edition
        longer = "longer" if int(delta) >= 0 else "shorter"
        runtime = (
            f"untime {_hrs(alt.get('hours'))} hrs — your files are "
            f"<b>{abs(int(delta))}% {longer}</b>, over the {int(round(tolerance * 100))}% tolerance"
        )
        clauses.append(("r" if clauses else "R") + runtime)
    if clauses:
        return ", ".join(clauses)
    if _num(alt.get("score")) >= _num(winner_score):
        return "Same score as the pick — most likely another edition of the same book"
    if not author_known:
        return f"Weaker title match — title <b>{_pct(title)}%</b>"
    return f"Weaker title and author match — title <b>{_pct(title)}%</b>, author <b>{_pct(author)}%</b>"


def _ev_bar(label: str, value: str, width: int, fill: str, note: str) -> str:
    """One labeled bar. ``note`` is HTML the caller has already escaped."""
    cls = f' class="{fill}"' if fill else ""
    width = max(0, min(100, int(width)))
    return (
        f'<div class="ab-evbar"><div class="ab-evlbl">{label} <em>{value}</em></div>'
        f'<div class="ab-evtrack"><i{cls} style="width:{width}%"></i></div>'
        f'<div class="ab-evnote">{note}</div></div>'
    )


def _sim_fill(value: float) -> str:
    return "" if value >= 0.85 else ("ab-mid" if value >= 0.6 else "ab-bad")


def evidence_html(unit, data: dict, alternates: list[dict]) -> str:
    """The "why this match" block: winner card, four evidence bars, rivals with reasons.

    Empty when the row has no match or was recorded before the evidence existed.
    """
    match = (data or {}).get("match") or {}
    if not unit.asin or not match.get("asin") or not _has_evidence(match):
        return ""
    tolerance = _tolerance()
    tol_pct = int(round(tolerance * 100))
    local_hours = _num((data.get("source") or {}).get("hours")) or _num(data.get("hours"))
    cat_hours = _num(match.get("hours"))
    pinned = bool(match.get("pinned"))
    author_known = bool(match.get("author_known", True))
    score = _num(match.get("score"))
    title_sim, author_sim = _num(match.get("title_sim")), _num(match.get("author_sim"))
    authors = [str(a) for a in (match.get("authors") or [])]
    narrators = [str(n) for n in (match.get("narrators") or [])]

    # winner card
    byline = _html("; ".join(authors)) if authors else "Author not listed"
    if narrators:
        byline += " &middot; Narrated by " + _html("; ".join(narrators))
    facts = []
    if cat_hours > 0:
        facts.append(f"{_hrs(cat_hours)} hrs")
    asin = _attr(str(match.get("asin")))
    facts.append(
        f'ASIN <a class="ab-alink" href="https://www.audible.com/pd?asin={asin}" '
        f'target="_blank" rel="noopener">{asin}</a>'
    )
    winner_corroborated = match.get("corroborated") is True
    if winner_corroborated:
        facts.append("Audnex agrees")
    if pinned:
        score_html = '<div class="ab-evv">&mdash;</div><div class="ab-evl">you picked this edition</div>'
    else:
        mid = "" if score >= 0.85 else " ab-mid"
        score_html = f'<div class="ab-evv{mid}">{score:.2f}</div><div class="ab-evl">match score</div>'
    win = (
        '<div class="ab-evwin">'
        + cover_html(unit)
        + '<div class="ab-evmeta">'
        + f'<div class="ab-evt">{knockoff_badge(match)}{_html(str(match.get("title") or unit.title))}</div>'
        + f'<div class="ab-eva">{byline}</div>'
        + f'<div class="ab-evm">{" &middot; ".join(facts)}</div>'
        + "</div>"
        + f'<div class="ab-evscore">{score_html}</div>'
        + "</div>"
    )

    bars = []
    if pinned:
        pin_note = "You set this ASIN yourself, so titles and authors weren't scored"
        bars.append(_ev_bar("Title match", "you picked this edition", 100, "ab-acc", pin_note))
        bars.append(_ev_bar("Author match", "you picked this edition", 100, "ab-acc", pin_note))
    else:
        q_title = str(match.get("query_title") or "")
        cat_title = str(match.get("title") or "")
        title_note = (
            f"&ldquo;{_html(q_title)}&rdquo; vs &ldquo;{_html(cat_title)}&rdquo;"
            if q_title
            else "Your folder gave no title to compare"
        )
        if match.get("name_source") == "embedded":
            # an ingest unit whose torrent name found nothing, matched on its own audio tags
            title_note += " &#183; from the audio file&#8217;s embedded tags, not the torrent name"
        bars.append(
            _ev_bar("Title match", f"{_pct(title_sim)}%", _pct(title_sim), _sim_fill(title_sim), title_note)
        )
        if author_known:
            q_author = str(match.get("query_author") or "")
            if authors and q_author and q_author.strip().lower() != authors[0].strip().lower():
                author_note = f"&ldquo;{_html(q_author)}&rdquo; vs &ldquo;{_html(authors[0])}&rdquo;"
            elif authors:
                author_note = f"&ldquo;{_html(authors[0])}&rdquo; listed as the author"
            else:
                author_note = "The catalog lists no author for this edition"
            author_pct = _pct(author_sim)
            bars.append(
                _ev_bar("Author match", f"{author_pct}%", author_pct, _sim_fill(author_sim), author_note)
            )
        else:
            bars.append(
                _ev_bar(
                    "Author match",
                    "&mdash;",
                    0,
                    "",
                    "Your folder names no author, so only the title was scored",
                )
            )

    delta = match.get("runtime_delta_pct")
    if delta is None:
        if local_hours <= 0:
            rt_note = "We couldn't read the length of your files, so runtime wasn't compared"
        else:
            rt_note = "The catalog lists no runtime for this edition, so runtime wasn't compared"
        bars.append(_ev_bar("Runtime", "&mdash;", 0, "", rt_note))
    else:
        delta = int(delta)
        over = _over_tolerance(local_hours, cat_hours, tolerance)
        where = "outside" if over else "inside"
        rt_note = (
            f"Your files {_hrs(local_hours)} hrs vs catalog {_hrs(cat_hours)} hrs "
            f"&mdash; {where} the {tol_pct}% tolerance"
        )
        width = 100 if over else (abs(delta) * 100 // tol_pct if tol_pct else 0)
        bars.append(_ev_bar("Runtime", f"{_signed(delta)}%", width, "ab-mid" if over else "ab-acc", rt_note))

    language = str(match.get("language") or "")
    lang_label = _html(language.capitalize()) if language else "Not listed"
    if match.get("language_penalized"):
        bars.append(
            _ev_bar(
                "Language",
                lang_label,
                100,
                "ab-mid",
                f"Not listed as English &mdash; <b>&minus;{PENALTY_PCT}% penalty</b> on the score",
            )
        )
    else:
        bars.append(_ev_bar("Language", lang_label, 100, "", "No language penalty applied"))

    # the runners-up, each with the reason it lost and a one-click "Use this edition"
    others = [
        a for a in alternates if isinstance(a, dict) and a.get("asin") and a.get("asin") != unit.asin
    ][:4]
    rivals_html = ""
    if others:
        rows = []
        for alt in others:
            reason = rival_reason(
                alt,
                local_hours,
                tolerance,
                winner_score=score,
                author_known=author_known,
                winner_corroborated=winner_corroborated,
            )
            if not reason:
                reason = _html("; ".join(str(a) for a in (alt.get("authors") or [])))
            alt_asin = _attr(str(alt.get("asin")))
            rows.append(
                '<div class="ab-evrival"><div class="ab-evmeta">'
                f'<b class="ab-evrt">{knockoff_badge(alt)}{_html(str(alt.get("title") or ""))}</b>'
                f'<div class="ab-evwhy">{reason}</div></div>'
                f'<span class="ab-evsc">{_num(alt.get("score")):.2f}</span>'
                '<button title="Set this Audible entry as the book\'s ASIN (locks it), then re-check against it." '
                f'onclick="abUseEdition(this)" data-asin="{alt_asin}">Use this edition</button></div>'
            )
        rivals_html = (
            '<h4 class="ab-evh2">The other candidates &mdash; and why they lost</h4>'
            + "".join(rows)
            + '<p class="ab-hint">Think a runner-up is the right book? Use this edition '
            "sets it as this book's ASIN, locks it, and re-checks.</p>"
        )

    note = f'<p class="ab-hint">{_html(unit.note)}</p>' if unit.note else ""
    return (
        '<div class="ab-ev"><h4>The Audible match</h4>'
        + win
        + '<div class="ab-evbars">'
        + "".join(bars)
        + "</div>"
        + note
        + rivals_html
        + "</div>"
    )


def read_sort(state) -> tuple:
    column = state.get_meta("sort_column", "scan")
    direction = state.get_meta("sort_dir", "desc")
    if column not in SORT_COLUMNS:
        column = "scan"
    if direction not in ("asc", "desc"):
        direction = "desc"
    return column, direction


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
        asin_btn = '<button class="btn" title="Open this book\'s details and set its exact Audible entry." onclick="abEditAsin(this)">Edit ASIN</button>'

        cards.append(
            (
                '<div class="ab-problem" data-path="{dp}" data-status="{st}">'
                '<div class="ab-problem-head">{cov}'
                '<div class="ab-pbook">'
                '<span class="ab-ptitle">{ti}{shelf_src_badge}</span>'
                '<span class="ab-pauthor">{au}</span></div>'
                '<span class="pill {st}"><span class="pdot"></span>{st}</span></div>'
                '<div class="ab-preason {rc}">{wh}</div>'
                '<div class="ab-pactions">{btns}</div></div>'
            ).format(
                dp=_attr(unit.path),
                cov=cover_html(unit),
                ti=_html(unit.title or unit.path),
                shelf_src_badge=INGEST_BADGE if is_ingest_row(unit) else "",
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


# --- Series shelf (approved mockups 3 and 3b, 0.15.0) -------------------------------------
# Everything here reads the stored series listings; nothing on a render path touches the
# network (series.aggregate queues stale listings for its background refresher instead).
# f-strings only: series and book titles come from the catalog and from folder names.

AUDIBLE_PD = "https://www.audible.com/pd?asin="
_EMPTY_SHELF = {"series": [], "by_path": {}, "missing_total": 0}


def _series_shelf(state, refresh: bool = False) -> dict:
    """The aggregation, or an empty shelf - a series problem must never blank a page."""
    try:
        return series_mod.aggregate(state, refresh=refresh)
    except Exception:  # noqa: BLE001
        logging.getLogger("audiobind").exception("series shelf could not be built")
        return dict(_EMPTY_SHELF)


def _series_rail_item(missing: int, active: bool = False) -> str:
    """The rail's Series tab, under Library; the amber count is books missing across series."""
    classes = "ab-rail-item" + (" ab-active" if active else "") + (" ab-alert" if missing else "")
    current = ' aria-current="page"' if active else ""
    count = (
        f'<span class="ab-count" title="books missing from your series">{int(missing)}</span>'
        if missing
        else ""
    )
    return (
        f'<a class="{classes}" href="/series"{current}>\n'
        f'      <span class="ab-glyph">&#9878;</span> Series\n      {count}</a>'
    )


def _series_cover_src(tile: dict) -> str:
    if tile.get("kind") == "owned" and tile.get("has_output") and tile.get("path"):
        return "/cover?path=" + quote(tile["path"], safe="")
    if tile.get("asin"):
        return "/cover?asin=" + quote(tile["asin"], safe="")
    return ""


def _series_tile(tile: dict) -> str:
    n = int(tile["n"])
    label = f"{n:02d}"
    if tile["kind"] == "gap":
        title = _attr(f"Missing #{n} {tile.get('title') or ''}".strip())
        return f'<div class="ab-series-tile ab-series-gap" title="{title}"><span class="ab-series-qn">{label}</span></div>'
    words = f"#{n} {tile.get('title') or ''}".strip()
    if tile["kind"] == "marked":
        words += " (you marked this as yours)"
    src = _series_cover_src(tile)
    img = f'<img src="{_attr(src)}" alt="" loading="lazy" onerror="abCoverFail(this)">' if src else ""
    return f'<div class="ab-series-tile" title="{_attr(words)}">{img}<span class="ab-series-n">{label}</span></div>'


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


#: why a series has no total - the card and the detail-panel hook say which, never a number
SERIES_UNKNOWN_WHY = {
    series_mod.SOURCE_PENDING: "fetching series size&hellip;",
    series_mod.SOURCE_NOT_FOUND: "Audible has no listing for this series",
    series_mod.SOURCE_UNREACHABLE: "the Audible catalog could not be reached &#183; trying again later",
}


def _series_source(view: dict) -> str:
    """Where the total came from: 'total from the Audible catalog · checked 3h ago'."""
    age = rel_age(view.get("fetched_at"))
    text = "total from the Audible catalog" + (f" &#183; checked {_html(age)}" if age else "")
    if view.get("last_error"):
        text += " &#183; the last check failed, so this is the earlier listing"
    return text


def _series_card(view: dict) -> str:
    slug = _attr(view["slug"])
    sub_bits = [_html(view["author"])] if view.get("author") else []
    if view["known"] and (view["unnumbered"] or view["between"] or view["other"]):
        sub_bits.append("mainline numbering")
    sub = f'<div class="ab-series-sub">{" &middot; ".join(sub_bits)}</div>' if sub_bits else ""
    if view["known"]:
        missing = len(view["missing"])
        badge = (
            f'<span class="ab-series-missing">{missing} MISSING</span>'
            if missing
            else '<span class="ab-series-complete">COMPLETE</span>'
        )
        count = f'<b>{view["have"]}</b><span class="ab-series-of">of {view["total"]} owned</span>{badge}'
        strip = (
            f'<div class="ab-series-src">{_series_source(view)}</div>'
            '<div class="ab-series-strip">' + "".join(_series_tile(t) for t in view["tiles"]) + "</div>"
        )
        if missing:
            nxt = view["next_gap"] or {}
            foot = (
                f'<a class="ab-series-link" href="#ab-series-missing-{slug}">Show the {missing} missing &darr;</a>'
                f'<span class="ab-series-dim">next gap: #{int(nxt.get("n") or 0)} {_html(nxt.get("title") or "")}</span>'
            )
        else:
            foot = '<span class="ab-series-dim">nothing to chase</span>'
    else:
        # never a guessed total: just what you have, and why the size is unknown
        count = (
            f'<b>{view["owned"]}</b><span class="ab-series-of">owned</span>'
            '<span class="ab-series-unk" title="No series total: Audiobind only shows one the '
            'Audible catalog gave it.">total unknown</span>'
        )
        strip = ""
        why = SERIES_UNKNOWN_WHY.get(view.get("source"), SERIES_UNKNOWN_WHY[series_mod.SOURCE_PENDING])
        foot = f'<span class="ab-series-dim">{why}</span>'
    if view["known"] and view["missing"]:
        # same target as the "Show the N missing" link: one anchor, one panel, no extra state
        name_html = (
            f'<a class="ab-series-name" href="#ab-series-missing-{slug}" '
            f'title="Show the {len(view["missing"])} missing">{_html(view["name"])}</a>'
        )
    else:
        name_html = f'<div class="ab-series-name">{_html(view["name"])}</div>'
    return (
        f'<article class="ab-series-card" id="ab-series-{slug}" data-slug="{slug}">'
        f'{name_html}{sub}'
        f'<div class="ab-series-count">{count}</div>{strip}'
        f'<div class="ab-series-foot">{foot}</div></article>'
    )


def _series_row(entry: dict, seq_label: str, actions: bool = True, wanted: frozenset | set = frozenset()) -> str:
    """One listing entry. ``actions`` (the missing list): I have this, and Want - or the
    "On your wishlist" marker when it is already there."""
    asin = str(entry.get("asin") or "")
    facts = []
    if _num(entry.get("hours")) > 0:
        facts.append(f"{_num(entry.get('hours')):.1f} hrs")
    facts.append(f"ASIN {_html(asin)}")
    facts.append("Audible.com")
    src = "/cover?asin=" + quote(asin, safe="") if asin else ""
    img = f'<img src="{_attr(src)}" alt="" loading="lazy" onerror="abCoverFail(this)">' if src else ""
    if entry.get("owned"):
        pill = '<span class="ab-series-pill ab-series-have">In your library</span>'
        buttons = ""
    else:
        pill = '<span class="ab-series-pill">Not in your library</span>'
        if not actions or not watcher_mod.configured():
            want = ""
        elif asin in wanted:
            want = '<span class="ab-series-pill ab-series-wanted">On your wishlist</span>'
        else:
            want = (
                '<button class="btn primary" title="Put this book on your wishlist. Nothing is downloaded." '
                'onclick="abSeriesWant(this)">Want</button>'
            )
        buttons = (
            (
                '<button title="You own this book without a rip - stop counting it as missing." '
                'onclick="abSeriesMark(this,true)">I have this</button>'
                if actions
                else ""
            )
            + '<button title="Copy this book\'s Audible page address." '
            'onclick="abSeriesCopy(this)">Copy Audible link</button>'
            + want
        )
    return (
        f'<div class="ab-series-row" data-asin="{_attr(asin)}" data-link="{_attr(AUDIBLE_PD + quote(asin, safe=""))}">'
        f'<span class="ab-series-seq">{seq_label}</span>{img}'
        f'<div class="ab-series-meta"><div class="ab-series-t">{_html(str(entry.get("title") or ""))}</div>'
        f'<div class="ab-series-m">{" &middot; ".join(facts)}</div></div>'
        f"{pill}{buttons}</div>"
    )


def _series_want_bar(view: dict, wanted: frozenset | set) -> str:
    """'Want all 13 missing' - one click, every missing entry onto the wishlist - and where
    the list came from."""
    missing = view["missing"]
    open_ = [m for m in missing if m.get("asin") not in wanted]
    owned, total = view["have"], view["total"]
    source = f"You own {owned} of {total} &#183; {_series_source(view)}"
    if not missing or not watcher_mod.configured():
        action = ""
    elif not open_:
        action = f'<span class="ab-series-pill ab-series-wanted">All {len(missing)} on your wishlist</span>'
    else:
        n = len(open_)
        label = f"Want all {n} missing" if n == len(missing) else f"Want the other {n} missing"
        action = (
            f'<button class="btn primary" data-series="{_attr(view["slug"])}" '
            f'title="Put every missing book in this series on your wishlist. Nothing is downloaded." '
            f'onclick="abSeriesWantAll(this)">{label}</button>'
        )
    return f'<div class="ab-series-bar"><span>{source}</span><span class="ab-spacer"></span>{action}</div>'


def _series_panel(view: dict, wanted: frozenset | set = frozenset()) -> str:
    """'Missing from Jack Reacher — 13 books', with the hidden not-numbered entries below."""
    if not view["known"] or not (view["missing"] or view["marked"]):
        return ""
    slug = _attr(view["slug"])
    name = _html(view["name"])
    missing = len(view["missing"])
    if missing:
        head = f"Missing from {name} &mdash; {missing} {_plural(missing, 'book', 'books')}"
    else:
        head = f"Nothing missing from {name}"
    rows = _series_want_bar(view, wanted) + "".join(
        _series_row(m, str(int(m["n"])), wanted=wanted) for m in view["missing"]
    )

    kpis = []
    if missing:
        avg = f" &middot; {view['avg_hours']:.1f} hrs avg" if view.get("avg_hours") else ""
        kpis.append(f"<div><b>{missing}</b><span>missing{avg}</span></div>")
    if view["unnumbered"]:
        u = view["unnumbered"]
        kpis.append(f"<div><b>{u}</b><span>{_plural(u, 'novella', 'novellas')}, unnumbered &middot; hidden</span></div>")
    if view["between"]:
        b = len(view["between"])
        labels = ", ".join("#" + _html(s) for s in view["between"])
        kpis.append(
            f"<div><b>{b}</b><span>between-the-numbers {_plural(b, 'entry', 'entries')} "
            f"({labels}) &middot; hidden</span></div>"
        )
    if view["other"]:
        o = view["other"]
        kpis.append(f"<div><b>{o}</b><span>{_plural(o, 'box set', 'box sets')} and collections &middot; hidden</span></div>")
    kpi_html = f'<div class="ab-series-kpi">{"".join(kpis)}</div>' if kpis else ""

    extras_html = ""
    if view["extras"]:
        k = len(view["extras"])
        extra_rows = "".join(
            _series_row(e, "#" + _html(e["sequence"]) if e.get("sequence") else "&mdash;", actions=False)
            for e in view["extras"]
        )
        extras_html = (
            f'<details class="ab-series-extra"><summary>Show the {k} not-numbered '
            f"{_plural(k, 'entry', 'entries')}</summary>{extra_rows}</details>"
        )

    marks_html = ""
    if view["marked"]:
        items = "".join(
            f'<span data-asin="{_attr(m["asin"])}">#{int(m["n"])} {_html(m.get("title") or "")} '
            '<button title="Put this book back on your missing list." '
            'onclick="abSeriesMark(this,false)">Undo</button></span>'
            for m in view["marked"]
        )
        marks_html = f'<div class="ab-series-marks"><span>You said you have:</span>{items}</div>'

    return (
        f'<section class="ab-series-panel" id="ab-series-missing-{slug}" aria-label="Missing from {_attr(view["name"])}">'
        f"<h3>{head}</h3>{rows}{kpi_html}{extras_html}{marks_html}</section>"
    )


def series_hook_html(view: dict | None) -> str:
    """The detail panel's "See what you're missing" block (mockup 3b) for a book in a shelf series."""
    if not view:
        return ""
    name = _html(view["name"])
    missing = len(view["missing"])
    complete = view["known"] and not missing
    if view["known"]:
        head = f"{name} &mdash; you're {view['have']} of {view['total']}"
        if missing:
            nxt = view["next_gap"] or {}
            first = f"#{int(nxt.get('n') or 0)} {_html(nxt.get('title') or '')}"
            sub = (
                f"{missing} books missing from this series, starting with {first}"
                if missing > 1
                else f"1 book missing from this series: {first}"
            )
        else:
            sub = "You have every numbered book in this series &mdash; nothing to chase"
    else:
        head = f"{name} &mdash; you have {view['owned']}"
        sub = "total unknown &#183; " + SERIES_UNKNOWN_WHY.get(
            view.get("source"), SERIES_UNKNOWN_WHY[series_mod.SOURCE_PENDING]
        )
    owned_tiles = [t for t in view["tiles"] if t["kind"] != "gap"][:8]
    gap_tiles = [t for t in view["tiles"] if t["kind"] == "gap"][:5]
    tiles = owned_tiles + gap_tiles
    strip = (
        '<div class="ab-series-strip">' + "".join(_series_tile(t) for t in tiles) + "</div>" if tiles else ""
    )
    label = "See the series" if complete else "See what you're missing"
    return (
        '<div class="ab-series-hook">'
        f'<div class="ab-series-hooktxt"><div class="ab-series-hookname">{head}</div>'
        f'<div class="ab-series-hooksub">{sub}</div></div>'
        f"{strip}"
        f'<a class="btn primary" href="/series#{_attr(view["slug"])}">{label}</a>'
        "</div>"
    )


def render_series(state) -> str:
    """The Series page: the shelf of series cards, then what's missing from each."""
    counts = state.counts()
    rc = rail_counts(state, counts)
    shelf = _series_shelf(state)
    views = shelf["series"]
    if views:
        want_help = (
            "<b>Want</b> puts a missing book on your wishlist "
            "(<b>Want all missing</b> does the whole list in one click); nothing is downloaded. "
            if watcher_mod.configured() else ""
        )
        intro = (
            '<p class="ab-series-intro">Every series with two or more of your books, checked '
            "against the Audible series order. " + want_help +
            "<b>I have this</b> marks a book you own without a rip, so it stops counting as "
            "missing. A series total only ever comes from the Audible catalog &#8212; when it "
            "has no listing, the total says unknown.</p>"
        )
        try:
            wanted = frozenset(state.wishlist_asins())
        except Exception:  # noqa: BLE001 - the wishlist must never blank the Series page
            wanted = frozenset()
        cards = "".join(_series_card(v) for v in views)
        panels = "".join(_series_panel(v, wanted) for v in views)
        body = f'{intro}<section class="ab-series-shelf" aria-label="Your series">{cards}</section>{panels}'
    else:
        body = (
            '<div class="ab-series-empty">No series on your shelf yet. Once two or more of your '
            "books belong to the same series, it shows up here &mdash; with what you have and "
            "what you're missing.</div>"
        )
    return SERIES_PAGE.format(
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        rail_total=sum(counts.values()),
        rail_review_count=rc["review"],
        rail_held_count=rc["held"],
        rail_failed_count=rc["failed"],
        series_item=_series_rail_item(shelf["missing_total"], active=True),
        discover_item=_discover_rail_item(state),
        library=_html(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_html(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        body=body,
        series_script=SERIES_SCRIPT,
    )


# ---------------------------------------------------------------------------- Discover
# Search the catalog, suggestions from what you own, the wishlist. A "Get this" / "Want"
# click is the only thing that ever adds a wishlist row. f-strings only for anything that
# carries a catalog title; numeric character references in new markup.

DISCOVER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &#183; Discover</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand"><a href="/" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item" href="/">
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    {series_item}
    {discover_item}
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
      <span class="ab-glyph">&#215;</span> Failed
      <span class="ab-count">{rail_failed_count}</span></a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &#8594; <span title="{output_attr}">{output}</span>{signout}
  </div>
</aside>
<main class="ab-main">
  <header class="ab-topbar">
    <h1>Discover</h1>
    <span class="ab-version">v{version}</span>
    <div class="ab-right"></div>
  </header>
  <div class="ab-disc-wrap">
    <nav class="chips ab-disc-tabs" aria-label="Discover">{disc_tabs}</nav>
{disc_body}
  </div>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>
<script>
{discover_script}
</script>
</body></html>"""

DISCOVER_SCRIPT = """
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
function abPost(url, body) {
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    });
}
function abRailWanted(count) {
  const el = document.getElementById('ab-rail-wanted');
  if (el && typeof count === 'number') { el.textContent = count; el.hidden = count < 1; }
}
// Get this / Want: one click, one wishlist row - nothing is downloaded
function abWant(btn, origin) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'adding\\u2026';
  abPost('/api/wishlist', {asin: row.dataset.asin, origin: origin})
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        return;
      }
      btn.textContent = 'On your wishlist';
      btn.classList.remove('primary');
      abRailWanted(out.res.count);
      abToast(out.res.added ? 'Added to your wishlist' : 'Already on your wishlist');
    })
    .catch(() => {
      abToast('could not reach the daemon');
      btn.disabled = false;
      btn.textContent = label;
    });
}
function abUnwant(btn) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  btn.disabled = true;
  abPost('/api/wishlist/remove', {asin: row.dataset.asin})
    .then(out => {
      if (!out.ok) { abToast(out.res.error || 'that did not work'); btn.disabled = false; return; }
      row.remove();
      abRailWanted(out.res.count);
      abToast('Taken off your wishlist');
    })
    .catch(() => { abToast('could not reach the daemon'); btn.disabled = false; });
}
// Retry: a not-found / failed wishlist book goes back to wanted for the next watcher cycle
function abRetry(btn) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  btn.disabled = true;
  abPost('/api/wishlist/retry', {asin: row.dataset.asin})
    .then(out => {
      if (!out.ok) { abToast(out.res.error || 'that did not work'); btn.disabled = false; return; }
      const pill = row.querySelector('.ab-disc-pill');
      if (pill) { pill.textContent = 'WANTED'; pill.className = 'ab-disc-pill ab-wl-wanted'; pill.title = ''; }
      const note = row.querySelector('.ab-wl-note');
      if (note) { note.textContent = (out.res.item && out.res.item.note) || ''; note.classList.remove('ab-wl-bad'); }
      row.dataset.status = 'wanted';
      btn.remove();
      abToast('Back to wanted - the watcher searches for it in its next cycle');
    })
    .catch(() => { abToast('could not reach the daemon'); btn.disabled = false; });
}
// For you: the page shows the stored suggestions only. Opening it asks for a refresh, which
// the server runs only when nothing is stored or it is over a week old; Refresh always asks.
function abRecsLive(on) {
  const live = document.getElementById('ab-recs-live');
  const btn = document.getElementById('ab-recs-btn');
  if (live) live.hidden = !on;
  if (btn) btn.disabled = on;
}
function abRecsPoll(before) {
  fetch('/api/discover/recommendations').then(r => {
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    return r.json();
  }).then(d => {
    const now = d.fetched_at ? String(Math.round(d.fetched_at * 1000)) : '';
    if (!d.refreshing) {
      if (now !== before) { location.reload(); return; }
      abRecsLive(false);
      return;
    }
    setTimeout(() => abRecsPoll(before), 3000);
  }).catch(() => setTimeout(() => abRecsPoll(before), 6000));
}
function abRecsRefresh(force) {
  const box = document.getElementById('ab-recs');
  if (!box) return;
  const before = box.dataset.fetched || '';
  abPost('/api/discover/recommendations', {action: 'refresh', force: !!force})
    .then(out => {
      if (!out.ok) { abToast(out.res.error || 'that did not work'); return; }
      if (out.res.refreshing) { abRecsLive(true); abRecsPoll(before); }
      else if (force) abToast('Suggestions are up to date');
    })
    .catch(() => abToast('could not reach the daemon'));
}
document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('ab-recs')) abRecsRefresh(false);
});
"""

DISCOVER_TABS = (
    ("search", "/discover", "Search"),
    ("for_you", "/discover/for-you", "For you"),
    ("wishlist", "/discover/wishlist", "Wishlist"),
)


def _discover_rail_item(state, active: bool = False) -> str:
    """The rail's Discover tab, under Series; the count is books on your wishlist."""
    classes = "ab-rail-item" + (" ab-active" if active else "")
    current = ' aria-current="page"' if active else ""
    try:
        wanted = state.wishlist_count()
    except Exception:  # noqa: BLE001 - the rail must never blank a page
        wanted = 0
    hidden = "" if wanted else " hidden"
    badge = (
        f'\n      <span class="ab-count" id="ab-rail-wanted" title="books on your wishlist"{hidden}>{int(wanted)}</span>'
        if watcher_mod.configured() else ""
    )
    return (
        f'<a class="{classes}" href="/discover"{current}>\n'
        f'      <span class="ab-glyph">&#8981;</span> Discover{badge}</a>'
    )


def _discover_tabs(active: str, wanted: int) -> str:
    out = []
    for key, href, label in DISCOVER_TABS:
        if key == "wishlist" and not watcher_mod.configured():
            continue  # a MAM-only surface: nothing consumes the wishlist without the watcher
        on = key == active
        count = f' <span class="n">{int(wanted)}</span>' if key == "wishlist" else ""
        klass = "chip on" if on else "chip"
        current = ' aria-current="page"' if on else ""
        out.append(f'<a class="{klass}" href="{href}"{current}>{label}{count}</a>')
    return "".join(out)


def _disc_names(names) -> str:
    return _html(", ".join(str(n) for n in (names or []) if n))


def _disc_row(item: dict, origin: str, seq_label: str = "") -> str:
    """One catalog book: cover, title, who wrote and reads it, the facts, and what you can do."""
    asin = str(item.get("asin") or "")
    title = str(item.get("title") or "")
    badge = '<span class="ab-ko" title="A summary, review or study guide - not the book itself.">Knockoff</span>' if item.get("knockoff") else ""
    byline = []
    if item.get("authors"):
        byline.append("By " + _disc_names(item["authors"]))
    if item.get("narrators"):
        byline.append("Narrated by " + _disc_names(item["narrators"]))
    facts = []
    if _num(item.get("hours")) > 0:
        facts.append(f"{_num(item.get('hours')):.1f} hrs")
    if item.get("series"):
        seq = f" #{_html(str(item['sequence']))}" if item.get("sequence") else ""
        facts.append(_html(str(item["series"])) + seq)
    if item.get("year"):
        facts.append(_html(str(item["year"])))
    facts.append(f"ASIN {_html(asin)}")
    src = "/cover?asin=" + quote(asin, safe="") if asin else ""
    img = f'<img src="{_attr(src)}" alt="" loading="lazy" onerror="abCoverFail(this)">' if src else ""
    seq_html = f'<span class="ab-disc-seq">{seq_label}</span>' if seq_label else ""
    if item.get("in_library"):
        state_html = '<span class="ab-disc-pill ab-disc-have" title="A book in your library is this one (same ASIN, or same title and author).">In your library</span>'
    elif item.get("wishlisted") and watcher_mod.configured():
        state_html = '<span class="ab-disc-pill ab-disc-want">On your wishlist</span>'
    elif watcher_mod.configured():
        state_html = (
            f'<button class="btn primary" title="Put this book on your wishlist. Nothing is downloaded." '
            f"onclick=\"abWant(this,'{origin}')\">Get this</button>"
        )
    else:
        state_html = ""
    sub = f'<div class="ab-disc-a">{" &#183; ".join(byline)}</div>' if byline else ""
    return (
        f'<div class="ab-disc-row" data-asin="{_attr(asin)}">{seq_html}{img}'
        f'<div class="ab-disc-meta"><div class="ab-disc-t" title="{_attr(title)}">{badge}{_html(title)}</div>'
        f'{sub}<div class="ab-disc-m">{" &#183; ".join(facts)}</div></div>'
        f"{state_html}</div>"
    )


def _discover_search_body(result: dict) -> str:
    title, author = result.get("title") or "", result.get("author") or ""
    if watcher_mod.configured():
        intro = (
            '<p class="ab-disc-intro">Search the Audible catalog by title, with an author to narrow it. '
            "<b>Get this</b> puts a book on your wishlist &#8212; nothing is downloaded, and books "
            "already in your library are marked.</p>"
        )
    else:
        intro = (
            '<p class="ab-disc-intro">Search the Audible catalog by title, with an author to narrow it. '
            "Books already in your library are marked.</p>"
        )
    form = (
        '<form class="ab-disc-form" method="get" action="/discover" role="search">'
        f'<input class="ab-search" name="title" type="search" maxlength="200" autocomplete="off" '
        f'spellcheck="false" placeholder="Title or keywords" value="{_attr(title)}" aria-label="Title">'
        f'<input class="ab-search ab-disc-author" name="author" type="search" maxlength="200" '
        f'autocomplete="off" spellcheck="false" placeholder="Author (optional)" value="{_attr(author)}" '
        'aria-label="Author">'
        '<button class="primary" type="submit">Search</button></form>'
    )
    if not title and not author:
        return intro + form
    asked = " by ".join(f"&#8220;{_html(q)}&#8221;" for q in (title, author) if q)
    if not result.get("ok"):
        panel = f'<div class="ab-disc-empty ab-disc-err">{_html(result.get("error") or "")}</div>'
        return f'{intro}{form}<section class="ab-disc-panel" aria-label="Search results">{panel}</section>'
    results = result.get("results") or []
    if not results:
        body = f'<div class="ab-disc-empty">Nothing in the Audible catalog for {asked}.</div>'
    else:
        owned = sum(1 for r in results if r.get("in_library"))
        why = f'<span class="ab-disc-why">{owned} already in your library</span>' if owned else ""
        n = len(results)
        body = (
            f"<h3>{n} {_plural(n, 'result', 'results')} for {asked}{why}</h3>"
            + "".join(_disc_row(r, "search") for r in results)
        )
    return f'{intro}{form}<section class="ab-disc-panel" aria-label="Search results">{body}</section>'


RECS_SEED_WORDS = {
    "author": ("you own {n}", "you own 1"),
    "narrator": ("reads {n} of yours", "reads 1 of yours"),
    "genre": ("{n} of your books", "1 of your books"),
}


def _discover_recs_body(state) -> str:
    """For you: stored suggestions grouped by why they are suggested. Cache only."""
    recs = discover_mod.recommendations(state)
    fetched = recs.get("fetched_at")
    if watcher_mod.configured():
        intro = (
            '<p class="ab-disc-intro">Suggestions from what you already own: more by your top authors, '
            "books read by your top narrators, and your top genres. <b>Get this</b> puts one on your "
            "wishlist &#8212; nothing is ever added on its own, and books you own or already want "
            "are left out.</p>"
        )
    else:
        intro = (
            '<p class="ab-disc-intro">Suggestions from what you already own: more by your top authors, '
            "books read by your top narrators, and your top genres. Books you own are left out.</p>"
        )
    if fetched:
        n = int(recs.get("calls") or 0)
        status = (
            f"Updated <b>{_html(rel_age(fetched))}</b> &#183; {n} catalog "
            f"{_plural(n, 'request', 'requests')} (at most {discover_mod.MAX_CALLS} per refresh)"
        )
        if recs.get("last_error") or (recs.get("failed") and recs.get("failed") == n):
            status += ' &#183; <span class="warn">the last refresh could not reach the catalog</span>'
        elif recs.get("failed"):
            status += f' &#183; <span class="warn">{int(recs["failed"])} of them failed</span>'
        if recs.get("stale"):
            status += " &#183; over a week old"
    else:
        status = "No suggestions stored yet."
    stamp = str(int(round(float(fetched) * 1000))) if fetched else ""
    bar = (
        f'<div class="ab-disc-bar" id="ab-recs" data-fetched="{stamp}"><span>{status}</span>'
        '<span id="ab-recs-live" hidden><span class="ab-spin"></span> Asking the catalog&#8230;</span>'
        '<span class="ab-spacer"></span>'
        f'<button id="ab-recs-btn" title="Ask the catalog again (at most {discover_mod.MAX_CALLS} requests)." '
        'onclick="abRecsRefresh(true)">Refresh</button></div>'
    )
    if not fetched:
        empty = (
            "Building your suggestions from your library &#8212; this takes a few seconds."
            if recs.get("refreshing")
            else "Press <b>Refresh</b> to build suggestions from your library."
        )
        return f'{intro}{bar}<section class="ab-disc-panel"><div class="ab-disc-empty">{empty}</div></section>'
    signals = recs.get("signals") or {}
    if not any(signals.get(k) for k in ("authors", "narrators", "genres")):
        return (
            f'{intro}{bar}<section class="ab-disc-panel"><div class="ab-disc-empty">Your library has '
            "nothing to go on yet. Suggestions come from the authors, narrators and genres of the "
            "books Audiobind has matched.</div></section>"
        )
    panels = []
    for section in recs["sections"]:
        kind = section["kind"]
        groups = section.get("groups") or []
        head = f'<h3>{_html(section["title"])}</h3>'
        if not groups:
            if kind == "genre":
                note = (
                    "No genre data yet. Genres are read from the metadata.json Audiobind writes "
                    "beside each published book, so they show up once books are converted."
                )
            elif kind == "narrator":
                note = "No narrator data yet. Narrators are kept with each book's catalog match."
            else:
                note = "No author data yet."
            panels.append(f'<section class="ab-disc-panel">{head}<div class="ab-disc-empty">{note}</div></section>')
            continue
        parts = [head]
        many, one = RECS_SEED_WORDS[kind]
        for group in groups:
            owned = int(group.get("owned") or 0)
            words = one if owned == 1 else many.format(n=owned)
            parts.append(f'<h4>{_html(str(group.get("seed") or ""))}<span>{words}</span></h4>')
            items = group.get("items") or []
            if not group.get("ok"):
                parts.append('<div class="ab-disc-empty ab-disc-err">The catalog could not be reached for this one on the last refresh.</div>')
            elif not items:
                parts.append('<div class="ab-disc-empty">Nothing new &#8212; what the catalog lists is already yours or on your wishlist.</div>')
            else:
                parts.append("".join(_disc_row(i, "for_you") for i in items))
        panels.append(f'<section class="ab-disc-panel" aria-label="{_attr(section["title"])}">{"".join(parts)}</section>')
    return intro + bar + "".join(panels)


#: the wishlist state chips: (label, hover text)
WISHLIST_CHIPS = {
    "wanted": ("WANTED", "Waiting: the MAM watcher searches for it in a coming cycle."),
    "downloading": ("DOWNLOADING", "Found on MAM and handed to qBittorrent; it becomes In library once converted."),
    "in_library": ("IN LIBRARY", "A converted book in your library is this one."),
    "not_found": ("NOT FOUND", "MAM had no good match in several searches. Retry searches again."),
    "error": ("ERROR", "The download or the hand-off to qBittorrent failed - the note says why. "
                       "Retry searches again."),
}


def _until(timestamp, now: float | None = None) -> str:
    """"in 35m" / "in 2h" / "due now" (plain text); "" when unknown."""
    try:
        ts = float(timestamp or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    seconds = ts - (time.time() if now is None else float(now))
    if seconds <= 60:
        return "due now"
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    return f"in {int(seconds // 3600)}h {int(seconds % 3600 // 60)}m"


def _watcher_bar(status: dict) -> str:
    """One line above the wishlist: is anything going to look for these books?"""
    link = '<span class="ab-spacer"></span><a href="/settings#watcher">Watcher settings</a>'
    if not status["configured"]:
        text = "The MAM watcher is <b>off</b> (MAM_ID is not set): nothing here is searched for or downloaded."
    elif not status["enabled"]:
        text = f'<span class="warn">The MAM watcher is disabled: {_html(status["reason"])}.</span>'
    elif status.get("egress_guard") == "blocked":
        text = ('<span class="warn">The MAM watcher is <b>stopped</b>: this connection is not the one you allowed '
                f'({_html(status.get("egress_reason") or "")}). Nothing is searched for.</span>')
    elif status.get("egress_guard") == "unknown" and status.get("egress_checked_at"):
        text = ('<span class="warn">The MAM watcher is <b>stopped</b>: it cannot confirm this connection is the VPN, '
                "so it will not contact MAM.</span>")
    elif status["paused"]:
        text = "The MAM watcher is <b>paused</b>: nothing is searched for until you resume it."
    else:
        bits = [f"The MAM watcher checks up to <b>{int(status['max_per_cycle'])}</b> wanted "
                f"{_plural(int(status['max_per_cycle']), 'book', 'books')} every "
                f"<b>{int(status['interval_min'])} min</b>"]
        if status["busy"]:
            bits.append("a cycle is running now")
        elif status.get("next_cycle_at"):
            bits.append(f"next cycle {_html(_until(status['next_cycle_at']))}")
        last = (status.get("last_cycle") or {}).get("at")
        if last:
            bits.append(f"last cycle {_html(rel_age(last))}")
        if status["watcher_dry_run"]:
            bits.append('<span class="warn">dry run: decisions are noted, nothing is downloaded</span>')
        text = " &#183; ".join(bits)
    return f'<div class="ab-disc-bar" id="ab-watch-bar"><span>{text}</span>{link}</div>'


def _wishlist_row_html(row: dict) -> str:
    origins = {"search": "from Search", "for_you": "from For you", "series": "from Series", "api": "from the API"}
    asin = row["asin"]
    status = row.get("status") or "wanted"
    facts = []
    if row.get("hours"):
        facts.append(f"{_num(row['hours']):.1f} hrs")
    if row.get("series"):
        seq = f" #{_html(row['sequence'])}" if row.get("sequence") else ""
        facts.append(_html(row["series"]) + seq)
    facts.append(f"ASIN {_html(asin)}")
    facts.append(_html(f"added {rel_age(row['added_at'])} {origins.get(row.get('origin') or '', '')}".strip()))
    checked = rel_age(row.get("checked_at"))
    facts.append(_html(f"checked {checked}" if checked else "never checked"))
    src = "/cover?asin=" + quote(asin, safe="")
    by = f'<div class="ab-disc-a">By {_disc_names(row["authors"])}</div>' if row.get("authors") else ""
    note = ""
    if row.get("note"):
        bad = " ab-wl-bad" if status in ("error", "not_found") else ""
        note = f'<div class="ab-wl-note{bad}">{_html(row["note"])}</div>'
    label, hover = WISHLIST_CHIPS.get(status, (status.upper(), ""))
    retry = (
        '<button title="Put this book back to wanted: the watcher searches for it again in its next cycle." '
        'onclick="abRetry(this)">Retry</button>'
        if status in ("not_found", "error") else ""
    )
    return (
        f'<div class="ab-disc-row" data-asin="{_attr(asin)}" data-status="{_attr(status)}">'
        f'<img src="{_attr(src)}" alt="" loading="lazy" onerror="abCoverFail(this)">'
        f'<div class="ab-disc-meta"><div class="ab-disc-t" title="{_attr(row["title"])}">{_html(row["title"])}</div>'
        f'{by}<div class="ab-disc-m">{" &#183; ".join(facts)}</div>{note}</div>'
        f'<span class="ab-disc-pill ab-wl-{_attr(status)}" title="{_attr(hover)}">{_html(label)}</span>'
        f'<span class="ab-wl-acts">{retry}'
        '<button title="Take this book off your wishlist." onclick="abUnwant(this)">Remove</button></span></div>'
    )


def _discover_wishlist_body(state) -> str:
    rows = state.wishlist_all()
    status = watcher_mod.WATCHER.status(state)
    if status["enabled"]:
        how = ("The MAM watcher searches for <b>wanted</b> books and hands a good match to qBittorrent; "
               "a book turns <b>In library</b> once it is converted. ")
    else:
        how = "This list is the hand-off point: nothing here is searched for or downloaded while the watcher is off. "
    intro = (
        f'<p class="ab-disc-intro">The books you asked for with <b>Get this</b> or <b>Want</b>. {how}'
        "<b>Retry</b> searches again for a book that was not found or failed; "
        "<b>Remove</b> takes a book off.</p>"
    )
    bar = _watcher_bar(status)
    if not rows:
        return (intro + bar + '<section class="ab-disc-panel"><div class="ab-disc-empty">'
                "Your wishlist is empty.</div></section>")
    n = len(rows)
    counts = state.wishlist_status_counts()
    breakdown = " &#183; ".join(
        f"{counts[key]} {WISHLIST_CHIPS[key][0].lower()}" for key in WISHLIST_CHIPS if key != "wanted" and counts.get(key)
    )
    why = f'<span class="ab-disc-why">{breakdown}</span>' if breakdown else ""
    return intro + bar + (
        f'<section class="ab-disc-panel" aria-label="Wishlist"><h3>{n} {_plural(n, "book", "books")} wanted{why}</h3>'
        + "".join(_wishlist_row_html(r) for r in rows) + "</section>"
    )


def render_watcher_section(state) -> str:
    """Settings > Watcher: read-only status, Run a cycle now, Pause / Resume."""
    s = watcher_mod.WATCHER.status(state)
    warn = ""
    if not s["configured"]:
        return (
            '<section class="ab-acct ab-watch" id="watcher" aria-labelledby="ab-watch-head">'
            '<h2 class="ab-acct-head" id="ab-watch-head">MAM acquisition (optional)</h2>'
            '<div class="ab-acct-who">Audiobind can optionally look for books on your wishlist at '
            "MyAnonamouse (MAM) and hand them to qBittorrent. It is off, and you do not need it: "
            "Audiobind converts whatever lands in the ingest folder, from any downloader or a plain "
            "copy. To turn MAM acquisition on, set the environment variables <code>MAM_ID</code>, "
            "<code>QBIT_URL</code>, <code>QBIT_USER</code> and <code>QBIT_PASS</code> and restart "
            "the container.</div></section>"
        )
    if not s["enabled"]:
        state_html = '<span class="warn">Disabled</span>'
        warn = (
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>The watcher is disabled.</b> '
            f"{_html(s['reason'])}. Set QBIT_USER and QBIT_PASS (the qBittorrent WebUI login) "
            "and restart the container.</div>"
        )
    elif s["paused"]:
        state_html = '<span class="warn">Paused</span> &#8212; no cycle runs until you resume it'
    else:
        state_html = '<span class="ok">Enabled</span>' + (" &#8212; running a cycle now" if s["busy"] else "")
    if s["configured"] and s["watcher_dry_run"]:
        state_html += ' &#183; <span class="warn">dry run: decisions are noted and logged, nothing is downloaded</span>'
    guard = s.get("egress_guard") or "off"
    if guard == "blocked":
        warn += (
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>Stopped: MAM would not see the allowed address.</b> '
            f"{_html(s['egress_reason'])}. The watcher will not contact MAM from here. Either update "
            '"Only reach MAM from one IP" below to the address you really want, or start your VPN '
            "container (or fix MAM_PROXY); the next cycle will carry on.</div>")
    elif guard == "unknown" and s.get("egress_checked_at"):
        warn += (
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>Stopped: the guard could not check your connection.</b> '
            f"{_html(s['egress_reason'])}. Check that your VPN container is running and MAM_PROXY is right; "
            "the watcher will not contact MAM until it can see where it is connecting from.</div>")
    verdict_html = {"ok": '<span class="ok">allowed</span>', "blocked": '<span class="warn">blocked</span>',
                    "unknown": '<span class="warn">unverified</span>'}.get(guard, "")
    if s["egress_ip"]:
        sees = f"MAM sees <b>{_html(s['egress_ip'])}</b>"
        if s.get("egress_expected"):
            sees += f" &#183; expected <b>{_html(s['egress_expected'])}</b>"
        if verdict_html:
            sees += f" &#183; {verdict_html}"
        if s.get("egress_checked_at"):
            sees += f" &#183; Checked {_html(time.strftime('%H:%M', time.localtime(float(s['egress_checked_at']))))}"
    elif guard == "off":
        sees = "Not checked &#8212; the guard is off, so Audiobind does not look up what MAM sees"
    else:
        sees = "Not checked yet &#8212; it is looked up at the start of each cycle"
        if s.get("egress_expected"):
            sees += f" &#183; expected <b>{_html(s['egress_expected'])}</b>"
    guard_bits = []
    if s.get("egress_expected"):
        guard_bits.append(f"only reaches MAM from {_html(s['egress_expected'])}")
    if s.get("egress_home_ip"):
        guard_bits.append(f"refuses to run from your home IP ({_html(s['egress_home_ip'])})")
    if guard_bits:
        guard_html = " and ".join(guard_bits)
        if guard == "blocked":
            guard_html = f'<span class="warn">{guard_html} &#8212; blocking now</span>'
    else:
        guard_html = "not armed (set the allowed IP below, or MAM_HOME_WAN_IP, to arm it)"
    last = s["last_cycle"] or {}
    if last.get("at"):
        parts = [f"{int(last.get('searched') or 0)} searched", f"{int(last.get('sent') or 0)} sent to qBittorrent"]
        if last.get("would_send"):
            parts.append(f"{int(last['would_send'])} would be sent (dry run)")
        parts.append(f"{int(last.get('not_found') or 0)} not found")
        if last.get("errors"):
            parts.append(f"{int(last['errors'])} error(s)")
        last_html = f"{_html(rel_age(last['at']))} ({_html(str(last.get('trigger') or ''))}) &#8212; {_html(', '.join(parts))}"
    else:
        last_html = "never"
    if s["next_cycle_at"]:
        next_html = _html(_until(s["next_cycle_at"]))
    elif s["configured"] and s["enabled"] and not s["paused"]:
        next_html = "when the daemon is running"
    else:
        next_html = DASH
    error = s["last_error"] or {}
    error_html = (f'<span class="warn">{_html(str(error.get("message") or ""))}</span> &#183; {_html(rel_age(error.get("at")))}'
                  if error.get("message") else "none")
    rows = [
        ("Status", state_html),
        ("Schedule", f"every {int(s['interval_min'])} min, at most {int(s['max_per_cycle'])} "
                     f"{_plural(int(s['max_per_cycle']), 'book', 'books')} per cycle"),
        ("qBittorrent", f"{_html(urlsplit(s['qbit_url']).netloc or s['qbit_url'])} &#183; category <code>{_html(watcher_mod.qbit.CATEGORY)}</code> &#183; "
                        + ("login set" if s["qbit_credentials"] else '<span class="warn">no login set</span>')),
        ("Connection", sees),
        ("Guard", guard_html),
        ("Last cycle", last_html),
        ("Next cycle", next_html),
        ("Last error", error_html),
    ]
    if s["backoff_seconds"]:
        rows.append(("MAM backoff", f'<span class="warn">no MAM request for {int(s["backoff_seconds"])}s</span>'))
    grid = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    can_run = s["enabled"] and not s["paused"] and not s["busy"]
    run_btn = (f'<button class="ab-save" id="ab-watch-run" onclick="abWatcher(\'run\')"{"" if can_run else " disabled"} '
               'title="Run one cycle now in the background (same caps and pacing as a timed cycle).">'
               "Run a cycle now</button>")
    toggle = ""
    if s["configured"]:
        action, label = ("resume", "Resume watcher") if s["paused"] else ("pause", "Pause watcher")
        toggle = f'<button class="btn" id="ab-watch-toggle" onclick="abWatcher(\'{action}\')">{label}</button>'
    wdry = bool(s["watcher_dry_run"])
    dry_toggle = (
        f'<button class="btn dry{" on" if wdry else ""}" id="ab-watch-dry" aria-pressed="{"true" if wdry else "false"}" '
        f'title="Searches and reports what it would download, never downloads. Click to turn it {"off" if wdry else "on"}." '
        f'onclick="abWatcherDry({"false" if wdry else "true"})">Dry run <span class="ab-switch"></span></button>'
    )
    dry_line = (
        '<div class="ab-acct-who" id="ab-watch-dry-note">Watcher dry run is <b>' + ("on" if wdry else "off") +
        "</b>: searches MAM and reports what it would download, never downloads. It is separate from "
        "the library Dry run on the main page.</div>"
    )
    expected_field = _setting_text(
        "expected_egress", "Only reach MAM from one IP", setting_effective("expected_egress"),
        is_custom(SETTING_MAP["expected_egress"]), setting_env("expected_egress"),
        EXPECTED_EGRESS_HINT, "e.g. 213.152.187.195")
    vip_field = _setting_select(
        "vip", "VIP member", setting_effective("vip"), setting_choices("vip"),
        is_custom(SETTING_MAP["vip"]), setting_env("vip"), hint=VIP_HINT, titlecase=True)
    return (
        '<section class="ab-acct ab-watch" id="watcher" aria-labelledby="ab-watch-head">'
        '<h2 class="ab-acct-head" id="ab-watch-head">Watcher</h2>'
        '<div class="ab-acct-who">Searches MAM for the <b>wanted</b> books on your wishlist and hands a good '
        "match to qBittorrent; the ingest folder takes it from there. Configured with environment variables "
        "(<code>MAM_ID</code>, <code>WATCH_INTERVAL_MIN</code>, <code>WATCH_MAX_PER_CYCLE</code>, "
        "<code>QBIT_URL</code>, <code>QBIT_USER</code>, <code>QBIT_PASS</code>, <code>MAM_PROXY</code>, "
        "<code>MAM_USER_AGENT</code>, <code>MAM_HOME_WAN_IP</code>, <code>MAM_EXPECTED_EGRESS_IP</code>, <code>MAM_VIP</code>); the pause switch is kept "
        "across restarts.</div>"
        f'{warn}<dl class="ab-watch-grid">{grid}</dl>'
        + expected_field + vip_field +
        f'<div class="ab-acct-row">{run_btn}{toggle}{dry_toggle}<span class="ab-fb" id="ab-watch-fb"></span></div>'
        + dry_line +
        "</section>"
    )


WATCHER_SCRIPT = """
// ---- Settings > Watcher: Run a cycle now (a worker thread on the server), Pause / Resume
function abWatcher(action) {
  const fb = document.getElementById('ab-watch-fb');
  const run = document.getElementById('ab-watch-run');
  fb.className = 'ab-fb';
  fb.textContent = action === 'run' ? 'starting a cycle\\u2026' : 'saving\\u2026';
  if (action === 'run' && run) run.disabled = true;
  fetch('/api/watcher', {method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({action: action})})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    })
    .then(out => {
      if (!out.ok) {
        fb.className = 'ab-fb ab-bad';
        fb.textContent = out.res.error || 'that did not work';
        if (run) run.disabled = false;
        return;
      }
      if (action === 'run') { fb.textContent = 'cycle running\\u2026'; setTimeout(abWatchPoll, 1500); }
      else location.reload();
    })
    .catch(err => {
      if (String(err && err.message) === 'signed out') return;
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
      if (run) run.disabled = false;
    });
}
function abWatchPoll() {
  fetch('/api/watcher').then(r => {
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    return r.json();
  }).then(s => {
    if (s.busy) { setTimeout(abWatchPoll, 3000); return; }
    location.reload();
  }).catch(() => setTimeout(abWatchPoll, 6000));
}
"""


def render_discover(state, tab: str = "search", title: str = "", author: str = "", result: dict | None = None) -> str:
    """The Discover page. Search runs one catalog query only when a query was typed."""
    counts = state.counts()
    rc = rail_counts(state, counts)
    wanted = state.wishlist_count()
    if tab == "wishlist" and not watcher_mod.configured():
        tab = "search"  # no wishlist surface without MAM: land on Search
    if tab == "wishlist":
        body = _discover_wishlist_body(state)
    elif tab == "for_you":
        body = _discover_recs_body(state)
    else:
        tab = "search"
        if result is None:
            result = discover_mod.search(state, title, author)
        body = _discover_search_body(result)
    return DISCOVER_PAGE.format(
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        rail_total=sum(counts.values()),
        rail_review_count=rc["review"],
        rail_held_count=rc["held"],
        rail_failed_count=rc["failed"],
        series_item=_series_rail_item(_series_shelf(state, refresh=False)["missing_total"]),
        discover_item=_discover_rail_item(state, active=True),
        library=_html(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_html(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        disc_tabs=_discover_tabs(tab, wanted),
        disc_body=body,
        discover_script=DISCOVER_SCRIPT,
    )


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
            "match - always held for review; fix it with Edit ASIN in the book's ⋮ menu. "
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
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        lib_active="",
        set_active=" ab-active",
        series_item=_series_rail_item(_series_shelf(state, refresh=False)["missing_total"]),
        discover_item=_discover_rail_item(state),
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
        account_section=render_account_section(state),
        account_script=ACCOUNT_SCRIPT,
        watcher_section=render_watcher_section(state),
        watcher_script=WATCHER_SCRIPT,
    )


def _custom_mark(key: str, custom: bool, env_val) -> str:
    hidden = "" if custom else " hidden"
    return f'<span class="ab-custom" data-field="{key}"{hidden}>custom (was {_html(str(env_val))} from env)</span>'


def _setting_select(
    key: str, label: str, value: str, options: list[str], custom: bool, env_val, hint: str = "", titlecase: bool = False
) -> str:
    mark = _custom_mark(key, custom, env_val)
    selected = next((o for o in options if str(o).upper() == str(value).upper()), options[0])
    opt_html = "".join(
        f'<option value="{_attr(o)}"{" selected" if o == selected else ""}>{_html(o.title() if titlecase else o)}</option>' for o in options
    )
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        f"{hint_html}"
        f'<select class="ab-in" data-initial="{_attr(selected)}">{opt_html}</select>'
        "</div>"
    )


VIP_HINT = (
    "MyAnonamouse VIP membership (bought with bonus points). When this is No, torrents that only "
    "VIP members can download are skipped instead of attempted."
)

EXPECTED_EGRESS_HINT = (
    "Optional. Your VPN's exit address. If Audiobind ever sees a different address it stops before "
    "contacting MAM, because a MAM session is tied to one IP. Leave it empty to skip this check."
)


def _setting_text(key: str, label: str, value: str, custom: bool, env_val, hint: str = "", placeholder: str = "") -> str:
    """A free-text setting (validated on save by the server). Blank is a valid value."""
    mark = _custom_mark(key, custom, env_val)
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        f"{hint_html}"
        f'<input type="text" class="ab-in" value="{_attr(value or "")}" data-initial="{_attr(value or "")}" '
        f'placeholder="{_attr(placeholder)}" autocomplete="off" spellcheck="false">'
        '<div class="ab-acct-row"><button class="ab-save" onclick="abSaveSettings()">Save</button></div>'
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


def in_flight_units() -> set[str]:
    """Paths of books waiting in the manual queue plus the one encoding right now."""
    units = set(CONTROL.queue_snapshot()["units"])
    if CONTROL.current_unit:
        units.add(CONTROL.current_unit)
    return units


def _ingest_line() -> str:
    """Rail footer: the ingest folder is the universal way in, MAM configured or not."""
    root = ingest_mod.configured_dir()
    where = f' <span title="{_attr(root)}">{_html(root)}</span>' if root else ""
    state = "" if root and ingest_mod.ingest_root() is not None else " (set INGEST_DIR to use it)"
    return (
        f'<br><b>ingest</b> &larr;{where}{state}<br><small>Files dropped in the ingest folder are '
        "converted whether or not MAM is configured.</small>"
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
    # (the book encoding right now is no longer in the queue, so it is added here)
    queued = in_flight_units()
    queued_by_status: dict[str, int] = {}
    all_units = state.all(None)
    if queued:
        for u in all_units:
            if u.path in queued:
                queued_by_status[u.status] = queued_by_status.get(u.status, 0) + 1
        units = [u for u in units if u.path not in queued]
        total -= queued_by_status.get(wanted, 0) if wanted else sum(queued_by_status.values())
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
    # stored series listings only; stale ones are refreshed in the background
    series_shelf = _series_shelf(state)

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

    now = time.time()
    # one query each for the whole table, not one per row
    locks = state.all_locks()
    asin_pins = state.all_asin_overrides()

    def detail_panel(unit) -> str:
        ingest_row = is_ingest_row(unit)
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
            (
                # the container path of the torrent folder, which Audiobind only ever reads
                "Ingest source (read-only, still seeding): "
                f'<span class="dim"><code>{_html(ingest_mod.source_dir(unit.path))}</code></span>'
                if ingest_row
                else f'<span class="dim"><code>{_html(unit.path)}</code></span>'
            ),
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
            decision.append(f"Match confidence: <b>{_html(unit.confidence)}</b>")
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
                    ).format(a=_html(unit.asin or "the one you set"), c=unit.confidence or "?", t=tier)
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
        locked = locks.get(unit.path, set())

        def field(key, label_str, catalog_value, width_hint=""):
            placeholder = _attr(catalog_value or "catalog value")
            on = " ab-locked" if key in locked else ""
            return (
                f'<div class="ab-field{on}"><span class="ab-lab">{label_str}{lock_chip(key, key in locked)}</span>'
                f'<input class="ab-in" data-field="{key}" value="{_attr(custom[key])}" '
                f'placeholder="{placeholder}" spellcheck="false"{width_hint}></div>'
            )

        # f-strings only: user text must never sit inside a str.format() template,
        # or one brace in a folder name or override takes the whole page down
        editor = (
            f'<div class="ab-edit" data-path="{_attr(unit.path)}"><h4>Metadata</h4>'
            '<p class="ab-hint">Editing a field locks it automatically &mdash; a rescan, '
            "re-check or re-rip will never overwrite it again. One click unlocks. The lock "
            "lives with the book, so it survives restarts and rescans. What you set wins over "
            "the catalog when this book is converted - tags, metadata.json and the author/title "
            "layout. Leave a box empty to use the catalog value; separate several names with a "
            "semicolon.</p>"
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

        # other editions the catalog search also scored highly, each one click to use
        editions_html = ""
        try:
            alternates = json.loads(unit.alternates) if getattr(unit, "alternates", "") else []
        except ValueError:
            alternates = []
        if not isinstance(alternates, list):
            alternates = []
        others = [
            a for a in alternates if isinstance(a, dict) and a.get("asin") and a.get("asin") != unit.asin
        ][:4]
        # rows recorded since 0.15.0 carry the matcher's evidence: show the "why this match"
        # block in place of the plain match list and the "Other editions seen" list
        evidence = evidence_html(unit, data, alternates)
        if evidence:
            others = []
        if others:
            edition_rows = []
            for alt in others:
                edition_rows.append(
                    (
                        '<div class="ab-edition">'
                        '<span class="ab-edmain"><b>{ko}{ti}</b>'
                        '<span class="ab-edsub">{au} &middot; '
                        '<a class="ab-alink" href="https://www.audible.com/pd?asin={a}" '
                        'target="_blank" rel="noopener">{a}</a></span></span>'
                        '<button title="Set this Audible entry as the book\'s ASIN (locks it), then '
                        're-check against it." onclick="abUseEdition(this)" data-asin="{a}">'
                        "Use this edition</button></div>"
                    ).format(
                        ko=knockoff_badge(alt),
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
                "Using one sets it as this book's ASIN and locks it.</p></div>"
            )

        btn = '<button title="Re-check this book against the catalog and convert it if it passes the rules." onclick="abUnitAction(this,\'retry\')">Retry now</button>'
        force = '<button class="warn" title="Convert this book even though the automatic checks flagged it." onclick="abUnitAction(this,\'force\')">Convert anyway</button>'
        asin_btn = '<button title="Set this exact Audible entry for the book (locks it), then re-check against it." onclick="abUnitAction(this,\'set_asin\')">Use this ASIN</button>'

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
            + '<span class="ab-pinlab">Set the exact Audible entry for this book:</span>'
            + '<input data-asin value="{av}" placeholder="ASIN, e.g. B005EHN61O">'.format(
                av=_attr(unit.asin or "")
            )
            + asin_btn
            + lock_chip("asin", "asin" in locked)
            + '<span class="ab-pinhint">Setting it locks the match - every future '
            "scan checks this book against this exact entry instead of guessing. "
            "Passes your confidence setting &rarr; converts automatically. Still "
            "too unsure &rarr; this panel re-opens with the reason.</span>" + "</div>"
        )
        delete_title = (
            _attr(ingest_mod.DELETE_REFUSAL[:1].upper() + ingest_mod.DELETE_REFUSAL[1:] + ".")
            if ingest_row
            else "Permanently delete this book's source folder and its published files. Cannot be undone."
        )
        delete_source_attr = f' data-source="{SOURCE_INGEST}"' if ingest_row else ""
        delete_act = (
            '<div class="acts">'
            f'<button class="btn danger" title="{delete_title}" onclick="unitDelete(this)" '
            f'data-path="{_attr(unit.path)}" data-title="{_attr(unit.title or unit.path)}"{delete_source_attr} '
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
            + (evidence or block("The Audible match", decision))
            + editions_html
            + editor
            + series_hook_html(series_shelf["by_path"].get(unit.path))
            + delete_act
            + "</div></td></tr>"
        )

    def row(unit) -> str:
        tier = str(effective_val("auto_convert") or "high")
        haystack = " ".join(filter(None, [unit.title, unit.author, unit.path]))
        data = " ".join(f'data-sort-{key}="{_attr(str(sort_value(unit, key)))}"' for key in SORT_COLUMNS)
        hidden = "" if locks.get(unit.path) else " hidden"
        badge = f'<span class="ab-custom" title="Some of this book\'s metadata is locked by you"{hidden}>locked</span>'
        try:
            detail_data = json.loads(unit.detail or "{}")
        except ValueError:
            detail_data = {}
        if not isinstance(detail_data, dict):
            detail_data = {}

        reason_html = ""
        if unit.status in ("held", "review", "failed"):
            why, _todo = explain(unit.status, unit.confidence or "", unit.note, detail_data, tier=tier)
            rc = " failed" if unit.status == "failed" else ""
            reason_html = f'<span class="ab-breason{rc}">{_html(why)}</span>'

        match_html, match_title = match_cell(
            unit, detail_data, state.get_overrides(unit.path), asin_pins.get(unit.path, "")
        )
        match_attr = f' title="{_attr(match_title)}"' if match_title else ""
        age = rel_age(unit.updated_at, now)
        row_src_badge = INGEST_BADGE if is_ingest_row(unit) else ""
        # f-strings only: titles and paths come from folder names and the catalog
        return (
            f'<tr class="urow" onclick="toggleRow(this)" data-path="{_attr(unit.path)}" '
            f'data-search="{_attr(haystack)}" {data}>'
            f'<td class="ab-covcell">{cover_html(unit)}</td>'
            f'<td class="ab-bookcell"><span class="ab-btitle">{_html(unit.title or unit.path)}{badge}'
            f"{row_src_badge}</span>"
            f'<span class="ab-bauthor">{_html(unit.author or "") or DASH}</span>{reason_html}</td>'
            f"<td>{status_dot(unit.status, unit.updated_at, now)}</td>"
            f'<td class="ab-hidemd ab-match"{match_attr}>{match_html}</td>'
            f'<td class="ab-num">{fmt_hours(unit.hours)}</td>'
            f'<td class="ab-hidemd ab-scan">{_html(age) or DASH}</td>'
            f'<td class="ab-actcol">{kebab_menu(unit)}</td></tr>'
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

    def header(key: str, label: str, klass: str) -> str:
        on = key == sort_column
        arrow = ("&#9660;" if sort_dir == "desc" else "&#9650;") if on else ""
        aria = ("descending" if sort_dir == "desc" else "ascending") if on else "none"
        return (
            f'<th class="ab-sort{" ab-on" if on else ""} {klass}" data-column="{key}" aria-sort="{aria}">'
            f'<button type="button" class="ab-sortbtn">{label}<span class="ab-arrow">{arrow}</span></button></th>'
        )

    headers = '<th class="ab-covhead"></th>' + "".join(
        header(key, label, klass) for key, label, _width, klass in COLUMNS
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
        signout=signout_html(state),
        styles=STYLES,
        script=SCRIPT,
        version=__version__,
        library=CONFIG.host_library or str(CONFIG.library_root),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=CONFIG.host_output or str(CONFIG.output_root),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        dry_text=dry_header,
        ingest_line=_ingest_line(),
        health_stale=health_stale,
        health_text=health_text,
        health_scan=_fmt_age(heartbeat_age),
        rail_total=total,
        series_item=_series_rail_item(series_shelf["missing_total"]),
        discover_item=_discover_rail_item(state),
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
    queued = in_flight_units()
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
        # so the page notices a manual Retry / ASIN re-check and reloads - and when a
        # series listing arrives or a book is marked "I have this" (series shelf, badge)
        "rev": f"{state.rev()}|s{state.series_rev()}",
        "stats": library_stats(state),
        "last_run": state.last_run() or {},
        "heartbeat_age_seconds": state.heartbeat_age(),
        "library_root": str(CONFIG.library_root),
        "output_root": str(CONFIG.output_root),
        # "" when INGEST_DIR is unset; ingest_enabled is False also when it was missing at start
        "ingest_dir": ingest_mod.configured_dir(),
        "ingest_enabled": ingest_mod.ingest_root() is not None,
        "auto_convert": effective_val("auto_convert"),
        "dry_run": CONTROL.dry_run,
        "settings": {sk: setting_effective(sk) for sk in SETTING_KEYS},
        **CONTROL.describe(),
    }


#: caps the manual-action threads (each runs ffprobe over the book plus catalog calls)
MANUAL_SLOTS = threading.BoundedSemaphore(max(1, CONFIG.manual_workers))


def reset_manual_slots(n: int | None = None) -> None:
    """Re-size the manual-action cap (tests, or after CONFIG.manual_workers changes)."""
    global MANUAL_SLOTS
    MANUAL_SLOTS = threading.BoundedSemaphore(max(1, n if n is not None else CONFIG.manual_workers))


def run_manual(state, unit, force: bool = False, slot=None) -> None:
    """A Retry / Use this ASIN / Convert anyway request, run off the request thread.

    The book leaves the queue however it ends - converted, still in review, held, a dry
    run, already published or an exception - or it would vanish from every list until a
    restart (render() hides queued books and shows them in the queue strip instead).
    """
    try:
        process_unit(state=state, unit=unit, log=_log, force=force, manual=True)
    except Exception:  # noqa: BLE001 - a background thread has nobody to raise to
        logging.getLogger("audiobind").exception("manual action for %s failed", unit.state_key)
    finally:
        CONTROL.dequeue(unit.state_key)
        if slot is not None:
            slot.release()


        CONTROL.dequeue(unit.rel_path)
        if slot is not None:
            slot.release()


def _os_error_code(exc: OSError) -> str:
    """A client-safe label for an OSError: its errno name, never the message (which
    carries container paths). The full error goes to the log."""
    return errno.errorcode.get(exc.errno or 0, "error") if isinstance(exc, OSError) else "error"


def removal_unsafe(target: Path, root: Path, kind: str) -> str:
    """Re-check a planned delete target right before removing it ('' = go ahead).

    ``target`` was built from a resolved parent plus its own name when the delete was
    planned. If anything was swapped since - a parent folder replaced by a symlink, the
    root re-pointed, a folder turned into a link - the path no longer means what was
    checked, so the delete must stop.
    """
    try:
        parent_now = target.parent.resolve(strict=True)
    except OSError:
        return "its parent folder disappeared"
    if parent_now != target.parent:
        return "a parent folder was replaced by a link"
    try:
        target.relative_to(root)
    except ValueError:
        return "it is no longer inside the allowed folder"
    if target == root or target.parent == target:
        return "it is the root folder itself"
    if target.is_symlink() and kind != "link":
        return "the folder was replaced by a link"
    return ""


def plan_target(p) -> tuple[Path, str]:
    """(absolute path with a resolved parent, kind) - the leaf itself is never followed."""
    raw = Path(p)
    target = raw.parent.resolve() / raw.name
    if target.is_symlink():
        kind = "link"
    elif target.is_dir():
        kind = "dir"
    else:
        kind = "file"
    return target, kind


def output_sharers(state, rel: str, folder: Path) -> int:
    """How many OTHER rows point their output into ``folder`` (a re-rip that reached
    "already published" records the first book's output as its own)."""
    count = 0
    for other in state.all(None):
        if other.path == rel or not other.output:
            continue
        try:
            other_dir = Path(other.output).resolve().parent
        except (OSError, RuntimeError):
            continue
        if other_dir == folder or folder in other_dir.parents:
            count += 1
    return count


def host_name(value: str) -> str:
    """A Host header (or ALLOWED_HOSTS entry) as a bare lower-case name: no port, no brackets."""
    v = value.strip().lower()
    if v.startswith("["):  # [v6] or [v6]:port
        end = v.find("]")
        return v[1:end] if end > 0 else ""
    if v.count(":") == 1:  # name:port or v4:port; a bare v6 address has several colons
        v = v.split(":", 1)[0]
    return v.rstrip(".")


def allowed_host_names() -> set[str]:
    return {host_name(h) for h in CONFIG.allowed_hosts if host_name(h)}


_HOSTS_WARNED = False
_REFUSED_HOSTS: set[str] = set()


def warn_default_hosts() -> None:
    """One clear line at start-up when ALLOWED_HOSTS was never set."""
    global _HOSTS_WARNED
    if _HOSTS_WARNED or CONFIG.allowed_hosts_set:
        return
    _HOSTS_WARNED = True
    logging.getLogger("audiobind").warning(
        "ALLOWED_HOSTS is not set: the web UI only answers to %s, so it may be unreachable from "
        "other machines until you set ALLOWED_HOSTS to the LAN IP / hostname you browse to "
        "(for example ALLOWED_HOSTS=192.168.1.10,tower.local)",
        ",".join(CONFIG.allowed_hosts),
    )


#: added to every response (including BaseHTTPRequestHandler's own error pages)
SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
)
#: the largest JSON body any endpoint accepts
MAX_BODY = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    state = None
    server_version = f"audiobind/{__version__}"
    #: a client that stops sending (slowloris, Content-Length larger than the body) is cut off
    timeout = 15

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _read_json(self) -> dict | None:
        """The request body as a JSON object, or None after a 400/413 has been sent."""
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length not in (None, "") else 0
        except ValueError:
            self._send(400, b'{"error":"bad Content-Length"}', "application/json")
            return None
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._send(413, b'{"error":"request body too large"}', "application/json")
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return None
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"the body must be a JSON object"}', "application/json")
            return None
        return payload

    def _send(self, code: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _request_host(self) -> str:
        """The host this request was addressed to: X-Forwarded-Host behind a trusted proxy,
        else Host. Empty when there is none (HTTP/1.0 without a Host header)."""
        if CONFIG.trusted_proxy:
            forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
            if forwarded:
                return forwarded
        return (self.headers.get("Host") or "").strip()

    def _host_refused(self) -> bool:
        """DNS-rebinding guard for every request: refuse (403) a Host not in ALLOWED_HOSTS.

        A hostile page can point its own domain at this server's LAN address, which makes
        it "same-origin" to the browser - but the browser still sends that domain as Host.
        """
        requested = self._request_host()
        if requested and host_name(requested) in allowed_host_names():
            return False
        if requested:
            message = (
                f"Host '{requested[:100]}' is not allowed. Add it to ALLOWED_HOSTS in the "
                "container settings to use Audiobind at this address."
            )
        else:
            message = "Requests must name a Host (HTTP/1.1)."
        key = requested[:100].lower()
        if key not in _REFUSED_HOSTS and len(_REFUSED_HOSTS) < 64:  # once per host: no log flood
            _REFUSED_HOSTS.add(key)
            logging.getLogger("audiobind").warning(
                "refused a request for host %r (not in ALLOWED_HOSTS)", requested[:100]
            )
        self._send(403, message.encode("utf-8", "replace"), "text/plain; charset=utf-8")
        return True

    # ---- sign-in: the account (or the legacy AUTH_TOKEN) ---------------------------------

    #: reachable without signing in: the login page itself and the icon it shows
    AUTH_EXEMPT = ("/login", "/logout", "/favicon.svg", "/icon.svg")
    #: before the first account exists (and with no AUTH_TOKEN) nothing else is reachable:
    #: only these, plus the setup page and the health probe
    SETUP_EXEMPT = AUTH_EXEMPT + ("/setup", "/healthz")
    #: _auth_status code for "no account yet - go to /setup"
    SETUP_NEEDED = 428

    def _client_ip(self) -> str:
        """Source address for the failed-login limit (the proxy's last hop when trusted)."""
        if CONFIG.trusted_proxy:
            hops = [h.strip() for h in (self.headers.get("X-Forwarded-For") or "").split(",") if h.strip()]
            if hops:
                return hops[-1][:64]
        return self.client_address[0] if self.client_address else "?"

    def _setup_pending(self) -> bool:
        """First run: no account in the DB and no legacy AUTH_TOKEN to sign in with."""
        return not CONFIG.auth_token and not self.state.has_credentials()

    def _bearer_ok(self, given: str, creds) -> bool:
        """The account password (once an account exists), else the legacy AUTH_TOKEN."""
        if creds is None:
            return auth_mod.token_matches(given, CONFIG.auth_token)
        if auth_mod.BEARERS.hit(given, creds.version):
            return True
        if auth_mod.verify_password(given, creds):
            auth_mod.BEARERS.add(given, creds.version)
            return True
        return False

    def _auth_status(self, path: str) -> int:
        """0 when the request may proceed, else 401 (not signed in), 429 (locked out) or
        SETUP_NEEDED (no account exists yet)."""
        clean = path.rstrip("/")
        creds = self.state.get_credentials()
        if creds is None and not CONFIG.auth_token:
            return 0 if clean in self.SETUP_EXEMPT else self.SETUP_NEEDED
        if clean in self.AUTH_EXEMPT:
            return 0
        version = creds.version if creds else 0
        sid = auth_mod.cookie_value(self.headers.get("Cookie"))
        if sid and auth_mod.SESSIONS.valid(sid, version):
            return 0
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        if path.startswith("/api/") and scheme.lower() == "bearer" and given.strip():
            ip = self._client_ip()
            if auth_mod.LIMITER.blocked(ip):
                return 429
            if self._bearer_ok(given.strip(), creds):
                return 0
            auth_mod.LIMITER.record(ip)
            logging.getLogger("audiobind").warning("wrong API credentials from %s", ip)
        return 401

    def _auth_refused(self, path: str) -> bool:
        code = self._auth_status(path)
        if code == 0:
            return False
        if code == 429:
            wait = auth_mod.LIMITER.retry_after(self._client_ip())
            self._send(
                429,
                b'{"error":"too many failed sign-ins - try again later"}',
                "application/json",
                {"Retry-After": str(wait)},
            )
        elif code == self.SETUP_NEEDED:
            if self.command == "GET" and not path.startswith(("/api/", "/cover")):
                self._send(303, b"", "text/plain", {"Location": "/setup"})
            else:
                self._send(
                    403,
                    b'{"error":"no account yet - open /setup to create one","setup":"/setup"}',
                    "application/json",
                )
        elif self.command == "GET" and not path.startswith(("/api/", "/cover")):
            self._send(303, b"", "text/plain", {"Location": "/login"})
        else:
            self._send(
                401,
                b'{"error":"authentication required"}',
                "application/json",
                {"WWW-Authenticate": 'Bearer realm="audiobind"'},
            )
        return True

    def _form_body(self, names: tuple[str, ...]) -> tuple[dict[str, str], bool]:
        """({name: value}, wants_json) from a small form or JSON POST (login, setup).
        Fields that were not sent are absent from the dict, not empty."""
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > 4096:
            raise ValueError("bad length")
        raw = self.rfile.read(length) if length else b""
        if ctype == "application/json":
            payload = json.loads(raw or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("not an object")
            return {n: str(payload[n]) for n in names if payload.get(n) is not None}, True
        form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {n: form[n][0] for n in names if n in form}, False

    def _locked_out_message(self, ip: str) -> tuple[str, int]:
        wait = auth_mod.LIMITER.retry_after(ip)
        return f"Too many failed sign-ins. Try again in {max(1, (wait + 59) // 60)} minute(s).", wait

    def _handle_login(self) -> None:
        refusal = self._origin_refusal()
        if refusal is not None:
            code, message = refusal
            self._send(code, json.dumps({"error": message}).encode(), "application/json")
            return
        try:
            fields, wants_json = self._form_body(("username", "password", "token"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return
        if self._setup_pending():
            self._send(303, b"", "text/plain", {"Location": "/setup"})
            return
        creds = self.state.get_credentials()
        username = fields.get("username", "").strip()
        ip = self._client_ip()

        def reply(code: int, message: str, headers: dict | None = None) -> None:
            if wants_json:
                self._send(code, json.dumps({"error": message}).encode(), "application/json", headers)
            else:
                page = render_login(message, username, legacy=creds is None)
                self._send(code, page.encode(), "text/html; charset=utf-8", headers)

        if auth_mod.LIMITER.blocked(ip):
            message, wait = self._locked_out_message(ip)
            reply(429, message, {"Retry-After": str(wait)})
            return
        if creds is not None:
            # the account: both fields, one generic answer, the same work either way
            ok = auth_mod.check_login(creds, username, fields.get("password", ""))
        elif "password" in fields:
            # legacy AUTH_TOKEN, no account yet: any username, the token as the password
            ok = bool(username) and auth_mod.token_matches(fields["password"], CONFIG.auth_token)
        else:
            # a pre-0.16 token-only form or script ({"token": ...}) keeps working until an account exists
            ok = auth_mod.token_matches(fields.get("token", ""), CONFIG.auth_token)
        if not ok:
            auth_mod.LIMITER.record(ip)
            logging.getLogger("audiobind").warning("failed sign-in from %s", ip)
            reply(401, "That username or password is not right.")
            return
        auth_mod.LIMITER.reset(ip)
        version = creds.version if creds else 0
        cookie = {"Set-Cookie": auth_mod.session_cookie(auth_mod.SESSIONS.create(version))}
        logging.getLogger("audiobind").info("signed in from %s", ip)
        if wants_json:
            self._send(200, b'{"ok":true}', "application/json", cookie)
        else:
            self._send(303, b"", "text/plain", {**cookie, "Location": "/"})

    def _handle_setup(self) -> None:
        """First run: create THE account and sign this browser in. Refused once an account
        exists - and while AUTH_TOKEN guards the server, so nobody can claim an install
        that is still protected by its token."""
        refusal = self._origin_refusal()
        if refusal is not None:
            code, message = refusal
            self._send(code, json.dumps({"error": message}).encode(), "application/json")
            return
        try:
            fields, wants_json = self._form_body(("username", "password", "confirm"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad request"}', "application/json")
            return

        def refuse_existing() -> None:
            if wants_json:
                self._send(409, b'{"error":"setup is already done - sign in instead"}', "application/json")
            else:
                self._send(303, b"", "text/plain", {"Location": "/"})

        if not self._setup_pending():
            refuse_existing()
            return
        username = fields.get("username", "").strip()
        password = fields.get("password", "")
        problem = auth_mod.username_problem(username) or auth_mod.password_problem(
            password, fields.get("confirm", "")
        )
        if problem:
            if wants_json:
                self._send(400, json.dumps({"error": problem}).encode(), "application/json")
            else:
                self._send(400, render_setup(problem, username).encode(), "text/html; charset=utf-8")
            return
        version = self.state.create_credentials(username, auth_mod.hash_password(password))
        if not version:  # another setup won the race
            refuse_existing()
            return
        cookie = {"Set-Cookie": auth_mod.session_cookie(auth_mod.SESSIONS.create(version))}
        logging.getLogger("audiobind").info("account created from %s", self._client_ip())
        if wants_json:
            self._send(200, b'{"ok":true}', "application/json", cookie)
        else:
            self._send(303, b"", "text/plain", {**cookie, "Location": "/"})

    def _handle_account(self) -> None:
        """Settings > Account. Every change needs the current password (the access token
        while there is no account yet); a wrong one counts towards the sign-in lockout.
        A change bumps the credential version: this browser stays signed in, every other
        session ends."""
        payload = self._read_json()
        if payload is None:
            return
        action = str(payload.get("action", "")).lower()
        if action not in ("password", "username", "create"):
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        ip = self._client_ip()
        if auth_mod.LIMITER.blocked(ip):
            message, wait = self._locked_out_message(ip)
            self._send(429, json.dumps({"error": message}).encode(), "application/json", {"Retry-After": str(wait)})
            return
        creds = self.state.get_credentials()
        if (creds is None) != (action == "create"):
            message = "an account already exists" if creds else "there is no account yet - create one first"
            self._send(409, json.dumps({"error": message}).encode(), "application/json")
            return
        current = str(payload.get("current") or "")
        if creds is None:
            current_ok = auth_mod.token_matches(current, CONFIG.auth_token)
        else:
            current_ok = auth_mod.verify_password(current, creds)
        if not current_ok:
            auth_mod.LIMITER.record(ip)
            logging.getLogger("audiobind").warning("wrong current password on Settings from %s", ip)
            what = "access token" if creds is None else "current password"
            self._send(403, json.dumps({"error": f"The {what} is not right."}).encode(), "application/json")
            return
        username = str(payload.get("username") or "").strip()
        new_password = str(payload.get("new_password") or "")
        problem = ""
        if action in ("username", "create"):
            problem = auth_mod.username_problem(username)
        if not problem and action in ("password", "create"):
            problem = auth_mod.password_problem(new_password, str(payload.get("confirm") or ""))
        if problem:
            self._send(400, json.dumps({"error": problem}).encode(), "application/json")
            return
        old_version = creds.version if creds else 0
        if action == "password":
            version = self.state.set_password(auth_mod.hash_password(new_password))
        elif action == "username":
            version = self.state.set_username(username)
        else:
            version = self.state.set_credentials(username, auth_mod.hash_password(new_password))
        if not version:
            self._send(409, b'{"error":"the account changed meanwhile - reload the page"}', "application/json")
            return
        sid = auth_mod.cookie_value(self.headers.get("Cookie"))
        keep = sid if sid and auth_mod.SESSIONS.valid(sid, old_version) else ""
        auth_mod.SESSIONS.bump(version, keep=keep)
        auth_mod.BEARERS.clear()
        logging.getLogger("audiobind").info("account: %s changed from %s", action, ip)
        current_creds = self.state.get_credentials()
        self._send(
            200,
            json.dumps({"ok": True, "username": current_creds.username if current_creds else ""}).encode(),
            "application/json",
        )

    def _handle_logout(self) -> None:
        sid = auth_mod.cookie_value(self.headers.get("Cookie"))
        if sid:
            auth_mod.SESSIONS.revoke(sid)
        target = "/setup" if self._setup_pending() else "/login"
        self._send(303, b"", "text/plain", {"Set-Cookie": auth_mod.expired_cookie(), "Location": target})

    def do_GET(self):  # noqa: N802
        if self._host_refused():
            return
        path, _, query = self.path.partition("?")
        if self._auth_refused(path):
            return
        params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
        status = params.get("status", "")
        if path.rstrip("/") == "/login":
            if self._setup_pending():
                self._send(303, b"", "text/plain", {"Location": "/setup"})
            else:
                page = render_login(legacy=not self.state.has_credentials())
                self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/setup":
            # once there is an account - or a legacy AUTH_TOKEN - setup is over (or skipped)
            if self._setup_pending():
                self._send(200, render_setup().encode(), "text/html; charset=utf-8")
            else:
                self._send(303, b"", "text/plain", {"Location": "/"})
            return
        if path.rstrip("/") == "/logout":
            self._handle_logout()
            return
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
                    # "library" or "ingest" (additive, 0.16.0)
                    "source": u.source or key_source(u.path),
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
        if path.startswith("/api/series"):
            shelf = _series_shelf(self.state)
            payload = {
                "missing_total": shelf["missing_total"],
                "refreshing": series_mod.REFRESHER.busy,
                "series": [series_mod.public(v) for v in shelf["series"]],
            }
            self._send(200, json.dumps(payload).encode(), "application/json")
            return
        if path.rstrip("/") == "/api/wishlist":
            rows = [discover_mod.public_row(r) for r in self.state.wishlist_all()]
            self._send(200, json.dumps({"count": len(rows), "items": rows}).encode(), "application/json")
            return
        if path.rstrip("/") == "/api/watcher":
            self._send(200, json.dumps(watcher_mod.WATCHER.status(self.state)).encode(), "application/json")
            return
        if path.rstrip("/") == "/api/discover/search":
            result = discover_mod.search(self.state, params.get("title", ""), params.get("author", ""))
            payload = {**result, "count": len(result["results"])}
            self._send(200 if result["ok"] else 502, json.dumps(payload).encode(), "application/json")
            return
        if path.rstrip("/") in ("/discover", "/discover/search"):
            page = render_discover(self.state, "search", params.get("title", ""), params.get("author", ""))
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/discover/wishlist":
            self._send(200, render_discover(self.state, "wishlist").encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/discover/for-you":
            self._send(200, render_discover(self.state, "for_you").encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/api/discover/recommendations":
            # the stored result only: a GET never queries the catalog
            self._send(200, json.dumps(discover_mod.recommendations(self.state)).encode(), "application/json")
            return
        if path in ("/series", "/series/"):
            self._send(200, render_series(self.state).encode(), "text/html; charset=utf-8")
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
        self._send(200, body, kind, {"Cache-Control": "public, max-age=86400"})

    def _handle_unit(self) -> None:
        payload = self._read_json()
        if payload is None:
            return

        rel = str(payload.get("path", ""))
        action = str(payload.get("action", "")).lower()

        if action == "set_meta":
            if self.state.get(rel) is None:
                self._send(404, b'{"error":"no such unit"}', "application/json")
                return
            before = overrides_mod.custom_fields(self.state.get_overrides(rel))
            clean = overrides_mod.normalise(payload.get("fields") or {})
            self.state.set_overrides(rel, clean)
            # a human edit locks the field; a box emptied by hand hands it back to the catalog
            self.state.lock_fields(rel, overrides_mod.custom_fields(clean))
            for gone in sorted(before - overrides_mod.custom_fields(clean)):
                self.state.unlock_field(rel, gone)
            locked = sorted(self.state.locked_fields(rel))
            logging.getLogger("audiobind").info(
                "metadata overrides saved for %s: %s (locked: %s)",
                rel,
                sorted(clean) or "none",
                locked or "none",
            )
            self._send(
                200,
                json.dumps(
                    {
                        "ok": True,
                        "unit": rel,
                        "custom": sorted(overrides_mod.custom_fields(clean)),
                        "locked": locked,
                        "overrides": overrides_mod.describe(clean),
                    }
                ).encode(),
                "application/json",
            )
            return

        if action == "unlock":
            field = str(payload.get("field", ""))
            if field not in LOCKABLE_FIELDS:
                self._send(400, b'{"error":"that field cannot be locked"}', "application/json")
                return
            if self.state.get(rel) is None:
                self._send(404, b'{"error":"no such unit"}', "application/json")
                return
            if not self.state.unlock_field(rel, field):
                self._send(409, b'{"error":"that field is not locked"}', "application/json")
                return
            locked = sorted(self.state.locked_fields(rel))
            logging.getLogger("audiobind").info("unlocked %s for %s", field, rel)
            self._send(
                200,
                json.dumps({"ok": True, "unit": rel, "field": field, "locked": locked}).encode(),
                "application/json",
            )
            return

        if action == "delete":
            # an ingest row's files are a torrent that is still seeding: refused before anything
            # else, confirmed or not, so no path guard below ever sees an ingest key
            stored = self.state.get(rel)
            if ingest_mod.is_ingest(rel) or (stored is not None and stored.source == SOURCE_INGEST):
                logging.getLogger("audiobind").info("delete refused for ingest row %s", rel)
                self._send(
                    409,
                    json.dumps({"error": ingest_mod.DELETE_REFUSAL, "source": SOURCE_INGEST}).encode(),
                    "application/json",
                )
                return
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
            kinds: dict[Path, str] = {}
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
                    # remove through the resolved parent (TOCTOU: re-verified before removal)
                    target, kind = plan_target(p)
                    if removal_unsafe(target, lib_root, kind):
                        self._send(400, b'{"error":"source path escapes library root"}', "application/json")
                        return
                    targets.append(target)
                    kinds[target] = kind
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
            # never take a published book that another row also points at
            out_shared = output_sharers(self.state, rel, out_trash) if out_trash is not None else 0

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
                changed = ""
                for target in targets:
                    changed = removal_unsafe(target, Path(CONFIG.library_root).resolve(), kinds[target])
                    if not changed and Path(CONFIG.library_root).resolve() != lib_root:
                        changed = "the library folder was re-pointed"
                    if changed:
                        changed = f"{target.name}: {changed}"
                        break
                    try:
                        if target.is_symlink():
                            target.unlink()  # a link is removed, never followed
                        elif target.is_dir():
                            shutil.rmtree(str(target))
                        elif target.exists():
                            target.unlink()
                    except OSError as exc:
                        error = exc
                        break
                remaining = {f for f in before if os.path.lexists(f)}
                if changed:
                    log.error("HARD DELETE %s; stopped, the path changed on disk: %s", rel, changed)
                    self._send(
                        409,
                        json.dumps(
                            {
                                "error": "the book's folder changed on disk while it was being deleted "
                                f"({changed}). Stopped - nothing more was removed and the book was kept. "
                                "Scan again, then retry.",
                                "source_partial": remaining != before,
                            }
                        ).encode(),
                        "application/json",
                    )
                    return
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
                                f"of {len(before)} files removed) before a file system error "
                                f"({_os_error_code(error)}; details in the Audiobind log). "
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
                        json.dumps(
                            {
                                "error": f"source removal failed ({_os_error_code(error)}) - "
                                "details are in the Audiobind log"
                            }
                        ).encode(),
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
            output_kept_reason = ""
            if out_trash is not None and out_shared:
                output_removed = "kept"
                output_kept_reason = f"also used by {out_shared} other book(s)"
                log.info("HARD DELETE %s; output kept, %s: %s", rel, output_kept_reason, out_trash)
            elif out_trash is not None and out_trash.exists():
                changed = removal_unsafe(out_trash, out_root, "dir")
                if not changed and Path(CONFIG.output_root).resolve() != out_root:
                    changed = "the output folder was re-pointed"
                if changed:
                    log.error("HARD DELETE %s; output not removed, the path changed on disk: %s", rel, changed)
                    self._send(
                        409,
                        json.dumps(
                            {
                                "error": f"the published folder changed on disk ({changed}). Its removal "
                                "was stopped and the book was kept. Scan again, then retry.",
                                "source_removed": source_removed,
                            }
                        ).encode(),
                        "application/json",
                    )
                    return
                try:
                    shutil.rmtree(str(out_trash))
                    output_removed = "removed"
                    log.info("HARD DELETE %s; output removed: %s", rel, out_trash)
                except OSError as exc:
                    output_removed = f"failed ({_os_error_code(exc)}) - details are in the Audiobind log"
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
                        "output_kept_reason": output_kept_reason,
                    }
                ).encode(),
                "application/json",
            )
            return

        if action not in ("retry", "force", "recheck", "set_asin"):
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        # every manual action re-audits the book off the request thread: never stack one on a
        # book that is already encoding or waiting, and never run more than MANUAL_WORKERS
        if CONTROL.current_unit == rel:
            self._send(
                409,
                b'{"error":"this book is encoding right now - try again once it finishes"}',
                "application/json",
            )
            return
        if rel in CONTROL.queue_snapshot()["units"]:
            self._send(409, b'{"error":"this book is already waiting for an action"}', "application/json")
            return
        slot = MANUAL_SLOTS
        if not slot.acquire(blocking=False):
            self._send(
                429,
                b'{"error":"another action is already running - try again when it finishes"}',
                "application/json",
            )
            return
        started = ""
        try:
            started = self._start_manual(rel, action, payload, slot)
        finally:
            if not started:  # a started thread releases the slot when it ends
                slot.release()
        if started:
            self._send(
                200, json.dumps({"ok": True, "action": started, "unit": rel}).encode(), "application/json"
            )

    def _start_manual(self, rel: str, action: str, payload: dict, slot) -> str:
        """Validate and start one manual action; returns the action started, or '' after
        sending the refusal. The started thread owns ``slot`` from then on."""
        unit = ingest_mod.find_any_unit(rel)
        if unit is None:
            if ingest_mod.is_ingest(rel):
                self._send(
                    404,
                    b'{"error":"this torrent folder is no longer in the ingest folder - nothing to re-check"}',
                    "application/json",
                )
                return
            self._send(404, b'{"error":"no such unit"}', "application/json")
            return ""

        if action == "set_asin":
            asin = str(payload.get("asin", "")).strip().upper()
            if not valid_asin(asin):
                self._send(
                    400,
                    b'{"error":"that is not an ASIN - it is 8-14 letters and digits, like B005EHN61O"}',
                    "application/json",
                )
                return ""
        # atomic: two requests for one book can't both get past this
        if not CONTROL.try_enqueue(rel):
            self._send(409, b'{"error":"this book is already waiting for an action"}', "application/json")
            return ""
        try:
            if action == "set_asin":
                self.state.set_asin_override(rel, asin)
                # setting the ASIN by hand is an edit like any other: it locks
                self.state.lock_fields(rel, {"asin"})
                action = "retry"
            # "recheck" is the per-book Re-check all: the same manual re-audit Retry runs (a
            # hand-set ASIN is honoured and never fallen back from, auto-convert rules apply)
            logging.getLogger("audiobind").info("manual %s requested for %s", action, rel)
            threading.Thread(
                target=run_manual,
                args=(self.state, unit),
                kwargs={"force": action == "force", "slot": slot},
                daemon=True,
                name="manual-" + action,
            ).start()
        except BaseException:
            CONTROL.dequeue(rel)
            raise
        return action

    def _handle_settings(self) -> None:
        payload = self._read_json()
        if payload is None:
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
        for key, fold in (("auto_convert", str.upper), ("audio_bitrate", str.lower), ("region", str.lower), ("vip", str.upper)):
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

        # text: the egress pin is empty (check off) or a real IP address
        if "expected_egress" in settings:
            raw = settings["expected_egress"]
            try:
                if raw is not None and not isinstance(raw, str):
                    raise ValueError
                validated["expected_egress"] = valid_ip_or_empty(raw or "")
            except ValueError:
                errors.append(
                    f"expected_egress: {str(raw)[:64]!r} is not an IPv4 or IPv6 address "
                    "(leave it empty to turn the check off)"
                )

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

    def _handle_series(self) -> None:
        """I have this / Undo: the per-ASIN "owned without a rip" marker; and "refresh",
        which queues stale series listings for the background refresher (GETs never do)."""
        payload = self._read_json()
        if payload is None:
            return
        action = str(payload.get("action", "")).lower()
        if action == "refresh":
            _series_shelf(self.state, refresh=True)
            self._send(
                200,
                json.dumps({"ok": True, "refreshing": series_mod.REFRESHER.busy}).encode(),
                "application/json",
            )
            return
        asin = str(payload.get("asin", "")).strip().upper()
        if action not in ("mark_owned", "unmark_owned"):
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        if not valid_asin(asin):
            self._send(400, b'{"error":"that is not an ASIN"}', "application/json")
            return
        if action == "mark_owned":
            # only books from a series listing we fetched - not any string someone posts
            if self.state.series_image(asin) is None:
                self._send(404, b'{"error":"that book is not in any of your series"}', "application/json")
                return
            self.state.set_owned_mark(asin, True)
        else:
            self.state.set_owned_mark(asin, False)
        logging.getLogger("audiobind").info("series: %s %s", action, asin)
        self._send(
            200,
            json.dumps({"ok": True, "asin": asin, "owned": action == "mark_owned"}).encode(),
            "application/json",
        )

    def _handle_wishlist(self, remove: bool = False) -> None:
        """POST /api/wishlist adds one book (idempotent); POST /api/wishlist/remove takes one off.
        Every add is one click on a page (or one API call) - nothing adds rows on its own."""
        payload = self._read_json()
        if payload is None:
            return
        if remove:
            asin = str(payload.get("asin", "")).strip().upper()
            if not valid_asin(asin):
                self._send(400, b'{"error":"that is not an ASIN"}', "application/json")
                return
            if not self.state.wishlist_remove(asin):
                self._send(404, b'{"error":"that book is not on your wishlist"}', "application/json")
                return
            logging.getLogger("audiobind").info("wishlist: removed %s", asin)
            body = {"ok": True, "asin": asin, "removed": True, "count": self.state.wishlist_count()}
            self._send(200, json.dumps(body).encode(), "application/json")
            return
        if "series" in payload:
            # {"series": "<slug>", "missing": "all"}: the Series page's "Want all missing" click
            if str(payload.get("missing", "")).lower() != "all":
                self._send(400, b'{"error":"send \\"missing\\": \\"all\\" with a series"}', "application/json")
                return
            result, status, error = discover_mod.want_missing(self.state, str(payload.get("series") or ""))
            if result is None:
                self._send(status, json.dumps({"error": error}).encode(), "application/json")
                return
            body = {"ok": True, **result, "count": self.state.wishlist_count()}
            self._send(200, json.dumps(body).encode(), "application/json")
            return
        row, added, error = discover_mod.add(self.state, payload)
        if row is None:
            self._send(400, json.dumps({"error": error}).encode(), "application/json")
            return
        body = {"ok": True, "asin": row["asin"], "added": added, "item": discover_mod.public_row(row),
                "count": self.state.wishlist_count()}
        self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_wishlist_retry(self) -> None:
        """POST /api/wishlist/retry: a not_found / error row goes back to wanted."""
        payload = self._read_json()
        if payload is None:
            return
        asin = str(payload.get("asin", "")).strip().upper()
        if not valid_asin(asin):
            self._send(400, b'{"error":"that is not an ASIN"}', "application/json")
            return
        row = self.state.wishlist_get(asin)
        if row is None:
            self._send(404, b'{"error":"that book is not on your wishlist"}', "application/json")
            return
        if not self.state.wishlist_retry(asin):
            body = {"error": f"only a book that was not found or failed can be retried (this one is {row['status']})"}
            self._send(409, json.dumps(body).encode(), "application/json")
            return
        logging.getLogger("audiobind").info("wishlist: retry %s", asin)
        row = self.state.wishlist_get(asin) or row
        self._send(200, json.dumps({"ok": True, "asin": asin, "item": discover_mod.public_row(row)}).encode(),
                   "application/json")

    def _handle_watcher(self) -> None:
        """POST /api/watcher: {"action": "run" | "pause" | "resume"}."""
        payload = self._read_json()
        if payload is None:
            return
        action = str(payload.get("action", "")).lower()
        watcher = watcher_mod.WATCHER
        if action == "run":
            started, why = watcher.request_cycle(self.state)
            if not started:
                self._send(409, json.dumps({"error": why}).encode(), "application/json")
                return
        elif action in ("pause", "resume"):
            watcher.set_paused(self.state, action == "pause")
        else:
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        body = {"ok": True, "action": action, **watcher.status(self.state)}
        self._send(200, json.dumps(body).encode(), "application/json")

    def _post_refusal(self) -> tuple[int, str] | None:
        """Cross-site request guard for every POST: (status, error) to refuse, else None.

        * The body must be declared ``application/json``. A browser can only send that
          cross-origin after a CORS preflight, which this server never answers, so a page
          on another site cannot fire a no-cors ``text/plain`` POST at the API.
        * When the browser says where the request came from (Origin, else Referer), that
          host must be this server's own Host (or X-Forwarded-Host behind a proxy, when
          TRUSTED_PROXY is on).
          Requests with neither header - curl, scripts - stay allowed.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            return 415, "requests must be sent as application/json"
        return self._origin_refusal()

    def _origin_refusal(self) -> tuple[int, str] | None:
        """Origin/Referer, when sent, must name this server's own host."""
        source = self.headers.get("Origin")
        if source is None:
            source = self.headers.get("Referer")
        if source is None:
            return None
        claimed = urlsplit(source.strip()).netloc.lower() if source.strip() not in ("", "null") else ""
        forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",") if CONFIG.trusted_proxy else []
        allowed = {h.strip().lower() for h in (self.headers.get("Host") or "", *forwarded) if h.strip()}
        if not claimed or claimed not in allowed:
            return 403, "cross-site request refused"
        return None

    def do_POST(self):  # noqa: N802
        if self._host_refused():
            return
        path = self.path.partition("?")[0].rstrip("/")
        if path == "/login":
            self._handle_login()
            return
        if path == "/setup":
            self._handle_setup()
            return
        if path == "/logout":
            self._handle_logout()
            return
        if self._auth_refused(path):
            return
        refusal = self._post_refusal()
        if refusal is not None:
            code, message = refusal
            self._send(code, json.dumps({"error": message}).encode(), "application/json")
            return
        if path == "/api/account":
            self._handle_account()
            return
        if self.path.startswith("/api/unit"):
            self._handle_unit()
            return
        if self.path.startswith("/api/settings"):
            self._handle_settings()
            return
        if self.path.startswith("/api/series"):
            self._handle_series()
            return
        if path == "/api/wishlist":
            self._handle_wishlist()
            return
        if path == "/api/wishlist/remove":
            self._handle_wishlist(remove=True)
            return
        if path == "/api/wishlist/retry":
            self._handle_wishlist_retry()
            return
        if path == "/api/watcher":
            self._handle_watcher()
            return
        if path == "/api/discover/recommendations":
            payload = self._read_json()
            if payload is None:
                return
            if str(payload.get("action", "")).lower() != "refresh":
                self._send(400, b'{"error":"unknown action"}', "application/json")
                return
            queued = discover_mod.request_refresh(self.state, force=bool(payload.get("force")))
            # "refreshing" stays true for a refresh that already finished (memoised catalog
            # answers can be that fast): the page then polls once, sees the new result, reloads
            body = {"ok": True, "queued": queued, "refreshing": queued or discover_mod.RECS_REFRESHER.busy}
            self._send(200, json.dumps(body).encode(), "application/json")
            return
        if not self.path.startswith("/api/control"):
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        payload = self._read_json()
        if payload is None:
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
        elif action == "watcher_dry_run":
            # the watcher's own switch: never touches the pipeline's dry_run
            desired = bool(payload.get("value", not CONTROL.watcher_dry_run))
            CONTROL.set_watcher_dry_run(desired)
            self.state.set_meta("watcher_dry_run", desired)
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
    warn_default_hosts()
    server = ThreadingHTTPServer(("0.0.0.0", CONFIG.web_port), Handler)
    if CONFIG.web_port == 0:
        CONFIG.web_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
