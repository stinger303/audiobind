"""Status page and controls: watch the daemon, pause it, make it scan now."""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import os
import re
import shutil
import stat
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import __version__, matching
from . import abs_layout
from . import ingest as ingest_mod
from . import auth as auth_mod
from . import discover as discover_mod
from . import hardcover as hardcover_mod
from . import ipupdate as ipupdate_mod
from . import overrides as overrides_mod
from . import preset as preset_mod
from . import series as series_mod
from . import watcher as watcher_mod
from . import wizard as wizard_mod
from .advice import (
    ATTENTION_STATUSES,
    GROUP_REASONS,
    GROUP_TITLES,
    explain,
    group_order,
    reason_numbers,
    short_reason,
)
from .config import (
    CONFIG,
    DOWNLOAD_CLIENTS,
    GENERAL_SETTING_KEYS,
    IP_UPDATER_MODES,
    SECRET_SETTING_KEYS,
    SETTING_KEYS,
    SETTING_MAP,
    URL_SETTING_KEYS,
    WATCHER_TEXT_CHECKS,
    effective_val,
    is_custom,
    set_effective,
    setting_bounds,
    FIXED_BOUNDS,
    watch_interval_min,
    watch_max_incomplete,
    watch_max_per_cycle,
    watch_max_unsatisfied,
    wedge_per_cycle,
    wedge_per_day,
    wedge_reserve,
    setting_choices,
    setting_effective,
    setting_env,
    setting_env_defaults,
    valid_ip_or_empty,
)
from .control import CONTROL
from .covers import resolve as resolve_cover
from .covers import valid_asin
from . import pipeline as pipeline_mod
from .pipeline import CONVERT_LOCK, process_unit
from .state import LOCKABLE_FIELDS, SOURCE_INGEST, WISHLIST_REQUEUEABLE, key_source
from .util import loads_dict, redact_url

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
  --ab-text4:#7d828b;
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
.ab-rail-item:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:-2px;border-color:var(--ab-accent-bright)}
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
button.dry.on{border-color:rgba(242,163,60,.55);color:var(--ab-amber);background:var(--ab-amber-soft)}
button.dry .ab-switch{width:22px;height:12px;border-radius:6px;background:rgba(255,255,255,.14);position:relative;flex:none}
button.dry.on .ab-switch{background:rgba(242,163,60,.4)}
button.dry .ab-switch::after{content:"";position:absolute;top:2px;left:2px;width:8px;height:8px;border-radius:50%;background:var(--ab-text3);transition:left .15s}
button.dry.on .ab-switch::after{background:var(--ab-amber)}
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
.ab-search:focus{outline:2px solid var(--ab-accent-bright);outline-offset:0;border-color:var(--ab-accent-bright);background:rgba(255,255,255,.04)}
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
.ab-tbl tbody tr.urow{cursor:pointer}
.ab-tbl tbody tr.urow:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:-2px}
.ab-covrow{display:flex;align-items:center}
.ab-tbl .ab-rowcaret{display:inline-block;width:10px;margin-right:4px;color:var(--ab-text4);font-size:10px;
  transition:transform .15s;flex:none;}
.ab-tbl tr.urow[aria-expanded="true"] .ab-rowcaret{transform:rotate(90deg)}
.ab-tbl tbody tr.ab-sel{background:rgba(94,106,210,.08)}
.ab-covcell{padding:0 6px 0 10px!important}
.ab-covwrap{position:relative;width:30px;height:30px;border-radius:6px;
  background:var(--ab-surface);border:1px solid rgba(255,255,255,.12);overflow:hidden;}
.ab-covwrap::after{content:"\\266B";position:absolute;inset:0;display:flex;
  align-items:center;justify-content:center;color:var(--ab-text4);font-size:12px;}
.ab-thumb{position:relative;z-index:1;display:block;width:100%;height:100%;
  object-fit:cover;background:var(--ab-surface);}
.ab-thumb.ab-gone{display:none}
td.ab-bookcell{display:flex;flex-direction:column;gap:1px;min-width:0;max-width:min(720px,100%)}
.ab-bookcell .ab-btitle{font-weight:500;font-size:13px;color:var(--ab-text);letter-spacing:-.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.ab-bookcell .ab-bauthor{font-size:11.5px;color:var(--ab-text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}

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
.ab-st{display:inline-flex;align-items:center;gap:5px;max-width:100%;overflow:hidden;
  color:var(--ab-text2);font-size:12px;white-space:nowrap}
.ab-st-label{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-st-dot{width:8px;height:8px;border-radius:50%;flex:none;margin-right:7px;background:var(--ab-text4)}
.ab-st-ok .ab-st-dot{background:var(--ab-green)}
.ab-st-hold .ab-st-dot{background:var(--ab-amber)}
.ab-st-rev .ab-st-dot{background:var(--ab-amber)}
.ab-st-err .ab-st-dot{background:var(--ab-red)}
.ab-st-enc .ab-st-dot{background:var(--ab-accent-bright)}
.ab-st-moving .ab-st-dot{animation:ab-st-pulse 1.6s ease-in-out infinite}
@keyframes ab-st-pulse{0%,100%{opacity:1}50%{opacity:.45}}
.ab-st-age{flex:none;color:var(--ab-text4);font-size:10.5px;font-variant-numeric:tabular-nums}
.ab-match{min-width:0;font-size:12px;color:var(--ab-text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-match-cur{color:var(--ab-blue)}
.ab-conf-high{color:var(--ab-green)}
.ab-conf-medium{color:var(--ab-text2)}
.ab-conf-low{color:var(--ab-amber)}
.ab-conf-none{color:var(--ab-text4)}
/* 0.21.0 "Needs you": the match readout, the one-line WHY cell, the group headers */
/* the word is content, the meter decoration: when Match is narrow the meter loses ticks
   first, the confidence word (and the separator before it) never truncates; only a converted
   row's series prefix may ellipsize */
.ab-match-in{display:flex;align-items:center;min-width:0;max-width:100%;overflow:hidden}
.ab-mmeter{display:inline-flex;flex:0 1 auto;overflow:hidden;min-width:0;gap:2px;margin-right:7px}
.ab-mmeter b{flex:none;width:9px;height:4px;border-radius:2px;background:rgba(255,255,255,.1)}
.ab-match-lead{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ab-match-sep{flex:none;margin:0 5px}
.ab-match-word{flex:none;white-space:nowrap}
/* below 1360 px the Why column gives Match a little more room */
@media (max-width:1359px){.ab-tbl col.ab-col-why{width:min(300px,20vw)!important}}
.ab-mmeter b.ab-f{background:var(--ab-text2)}
.ab-why{display:flex;align-items:baseline;min-width:0;overflow:hidden;white-space:nowrap;font-size:12px}
.ab-why-l{flex:none;color:var(--ab-amber);font-weight:500}
.ab-why-sub{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  color:var(--ab-text3);font-family:var(--ab-mono);font-size:11px;margin-left:8px}
.ab-tbl tbody tr.ab-group:hover, .ab-tbl tbody tr.ab-more-row:hover{background:none}
.ab-tbl tbody tr.ab-group td{white-space:normal;padding:18px 8px 8px;border-bottom:none}
.ab-group-h{display:flex;align-items:center;gap:8px}
.ab-group-t{font-size:13.5px;font-weight:600;color:var(--ab-text)}
.ab-group-n{font-family:var(--ab-mono);font-size:11px;color:var(--ab-amber);
  background:var(--ab-amber-soft);border:1px solid rgba(242,163,60,.28);border-radius:4px;padding:0 5px}
.ab-group-p{margin-top:4px;font-size:12px;color:var(--ab-text3);line-height:1.5;max-width:900px}
.ab-tbl tbody tr.ab-more-row td{padding:6px 8px 6px 58px}
button.ab-more{height:auto;padding:4px 0;border:none;background:none;color:var(--ab-accent-bright);font-size:12.5px}
button.ab-more:hover{background:none;color:var(--ab-text)}
.ab-allclear{flex:none;padding:0 24px 8px;font-size:12px;color:var(--ab-text3)}
.chip.ab-alert .n{color:var(--ab-amber)}

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

tr.drow,tr.urow{scroll-margin-top:40px}
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
.ab-in:focus{outline:2px solid var(--ab-accent-bright);outline-offset:0;border-color:var(--ab-accent-bright);}
.ab-in::placeholder{color:var(--ab-text4);font-style:italic;}
.ab-editbar{display:flex;align-items:center;gap:10px;margin-top:12px;}
/* Settings tabs: the library's chip pills; the tab that is on is the indigo pill (the rail's active tint) */
.ab-tabs{margin:14px 0 4px}
.ab-tabs .chip.on{background:rgba(94,106,210,.14);border-color:rgba(94,106,210,.45);color:var(--ab-text)}
.ab-tabpanel[hidden], .ab-editbar[hidden]{display:none}
.ab-save{font-size:12.5px;padding:6px 12px;background:#1e4fa0;border-color:#2b5ea8;}
.ab-fb{font-size:12.5px;color:var(--ab-text3);}
.ab-fb.ab-good{color:#5fd38d;}
.ab-fb.ab-bad{color:#f2686b;}
.ab-fb.ab-warn{color:#e3b341;}
/* Settings > Watcher: the lines under a test (the folder check), the Check IP row, the preset box */
.ab-test-detail{font-size:12.5px;color:var(--ab-text3);margin:6px 0 10px;max-width:70ch}
.ab-test-detail p{margin:3px 0}
.ab-test-detail .ab-rule{color:var(--ab-text4)}
.ab-checkip-ip{display:flex;gap:8px;align-items:center;margin:6px 0}
.ab-checkip-ip input{max-width:22ch;font-family:var(--ab-mono, monospace)}
/* Settings > Watcher: where a value comes from - "from the container", or "set here" + a way back */
.ab-src{font-size:11px;font-weight:400;color:var(--ab-text4)}
.ab-src-here{color:var(--ab-text2)}
button.ab-src-reset{height:auto;padding:0;border:none;background:none;font-size:11px;font-weight:400;
  color:var(--ab-accent-bright)}
button.ab-src-reset:hover{background:none;color:var(--ab-text)}
.ab-src-clear{display:inline-flex;align-items:center;gap:5px}
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
.ab-ipupd-modes{margin:2px 0 6px;padding-left:18px}
.ab-range-wrap{display:flex;align-items:center;gap:10px}
.ab-range-val{font-family:var(--ab-mono);font-size:12px;color:var(--ab-text2);min-width:24px;text-align:right}
input[type=range]{-webkit-appearance:none;appearance:none;height:6px;
  background:rgba(255,255,255,.08);border-radius:3px;outline:none;flex:1;max-width:300px}
input[type=range]:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:4px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
  border-radius:50%;background:var(--ab-accent-bright);cursor:pointer;border:none}
input[type=range]::-moz-range-thumb{width:16px;height:16px;
  border-radius:50%;background:var(--ab-accent-bright);cursor:pointer;border:none}
select.ab-in{font:inherit;font-size:12.5px;padding:7px 10px;border-radius:8px;
  border:1px solid #2b3240;background:#0d1118;color:var(--ab-text);max-width:200px}
select.ab-in:focus{outline:2px solid var(--ab-accent-bright);outline-offset:0;border-color:var(--ab-accent-bright)}
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
.ab-abs-panel{margin-top:12px}
.ab-abs-line{font-size:12px;color:var(--ab-text2);padding:4px 0;border-top:1px solid var(--ab-border);overflow-wrap:anywhere}
.ab-abs-line code{font-size:11.5px}
.ab-abs-why{display:block;color:var(--ab-text4);font-size:11.5px}
.ab-abs-bad .ab-abs-why{color:#f2686b}
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
.ab-wizcard{flex:none;margin:12px 24px 0;background:var(--ab-surface);border:1px solid rgba(94,106,210,.45);
  border-radius:10px;padding:12px 16px;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;
  gap:10px 16px;font-size:12.5px;color:var(--ab-text3);line-height:1.5}
.ab-wizcard-text{flex:1 1 280px;min-width:0}
.ab-wizcard-text b{color:var(--ab-text);font-weight:600}
.ab-wizcard .ab-acct-row{flex-wrap:wrap;margin-top:0}
.ab-wizcard a.btn,.ab-wiz-open a.btn{text-decoration:none}
.ab-wiz-open{flex-wrap:wrap;margin:6px 0 10px}
.ab-wiz-open .ab-acct-who{margin-bottom:0;flex:1 1 220px}
.ab-setup-guide a{color:var(--ab-accent-bright);overflow-wrap:anywhere}
.ab-wiz-form{display:flex;flex-direction:column;gap:10px}
.ab-wiz-choices{border:0;margin:0;padding:0;display:flex;flex-direction:column;gap:8px;min-width:0}
.ab-wiz-choices legend{padding:0;margin-bottom:6px}
.ab-wiz-choice{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border:1px solid var(--ab-border);
  border-radius:8px;background:var(--ab-surface);cursor:pointer;font-size:13px;color:var(--ab-text);min-width:0}
.ab-wiz-choice:hover{border-color:rgba(94,106,210,.45)}
.ab-wiz-choice:focus-within{outline:2px solid var(--ab-accent-bright);outline-offset:1px}
.ab-wiz-choice input{margin:3px 0 0;flex:none;width:16px;height:16px;accent-color:var(--ab-accent-bright)}
.ab-wiz-choice b{font-weight:500;display:block;overflow-wrap:anywhere}
.ab-wiz-sub{display:block;font-size:11.5px;color:var(--ab-text3);margin-top:2px;line-height:1.4}
.ab-wiz-outcome{display:flex;flex-direction:column;gap:12px;min-width:0}
.ab-wiz-outcome:focus{outline:2px solid var(--ab-accent-bright);outline-offset:4px;border-radius:6px}
.ab-wiz-outcome:empty{display:none}
.ab-wiz-checks{list-style:none;margin:8px 0 4px;padding:0;display:flex;flex-direction:column;gap:5px}
.ab-wiz-checks li{display:block;position:relative;padding-left:22px;overflow-wrap:anywhere}
.ab-wiz-checks li.ab-good .ab-wiz-glyph{color:#5fd38d}
.ab-wiz-checks li.ab-bad .ab-wiz-glyph{color:#f2686b}
.ab-wiz-checks li.ab-warn .ab-wiz-glyph{color:#e3b341}
.ab-wiz-glyph{position:absolute;left:0;top:0;width:14px;text-align:center}
.ab-wiz-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.ab-wiz-fix{margin:6px 0 2px}
.ab-wiz-fix b{color:var(--ab-text)}
.ab-wiz code{overflow-wrap:anywhere}
.ab-wiz .ab-in{width:100%;max-width:420px;box-sizing:border-box}
.ab-wiz select.ab-in{width:auto;max-width:100%}
.ab-wiz .ab-setting[hidden]{display:none}
.ab-wiz .ab-fb,.ab-wiz .ab-test-detail{overflow-wrap:anywhere;min-width:0}
.ab-wiz .ab-test-detail:focus{outline:2px solid var(--ab-accent-bright);outline-offset:2px}
.ab-wiz .ab-src-clear{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ab-text3)}
.ab-wiz .ab-src-clear[hidden]{display:none}
.ab-wiz{max-width:720px;display:flex;flex-direction:column;gap:14px;min-width:0}
.ab-wiz-steps{display:flex;flex-wrap:wrap;gap:6px;list-style:none;margin:0;padding:0;font-size:12.5px}
.ab-wiz-steps li{display:flex;align-items:center;gap:6px;padding:4px 10px;border:1px solid var(--ab-border);
  border-radius:99px;color:var(--ab-text3);min-width:0}
.ab-wiz-steps li.ab-on{border-color:rgba(94,106,210,.45);background:rgba(94,106,210,.14);color:var(--ab-text)}
.ab-wiz-steps li.ab-later{color:var(--ab-text4)}
.ab-wiz-steps a{color:var(--ab-text2);text-decoration:none;display:flex;align-items:center;gap:6px;border-radius:4px}
.ab-wiz-steps a:hover{color:var(--ab-accent-bright);text-decoration:underline}
.ab-wiz-steps a:focus-visible{outline:2px solid var(--ab-accent-bright);outline-offset:2px}
.ab-wiz-n{font-family:var(--ab-mono);font-size:11.5px}
.ab-wiz-title{font-size:15px;font-weight:600;margin:4px 0 0;color:var(--ab-text);overflow-wrap:anywhere}
.ab-wiz .ab-hintbox{margin-bottom:0}
.ab-wiz .ab-acct-row{flex-wrap:wrap}
.ab-wiz a.btn{text-decoration:none}
.ab-wiz-ip{font-family:var(--ab-mono);overflow-wrap:anywhere;word-break:break-all}
.ab-wiz-when{margin:4px 0 0;font-size:12px;color:var(--ab-text3)}
.ab-wiz-addr{min-width:0;overflow-wrap:anywhere}
.ab-wiz-group{border:1px solid var(--ab-border);border-radius:8px;margin:0;padding:10px 12px 12px;min-width:0;
  display:flex;flex-direction:column;gap:10px}
.ab-wiz-group legend{padding:0 4px;font-size:12.5px;color:var(--ab-text2)}
.ab-wiz-next{margin:6px 0 4px;padding-left:20px;display:flex;flex-direction:column;gap:4px}
.ab-wiz textarea.ab-in{max-width:100%;resize:vertical}
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
.ab-disc-pill.ab-wl-downloaded{color:var(--ab-accent-bright);background:rgba(94,106,210,.14)}
.ab-disc-pill.ab-wl-in_library{color:var(--ab-green);background:var(--ab-green-soft)}
.ab-disc-pill.ab-wl-not_found{color:var(--ab-amber);background:var(--ab-amber-soft)}
.ab-disc-pill.ab-wl-error{color:var(--ab-red);background:var(--ab-red-soft)}
.ab-wl-note{font-size:11px;color:var(--ab-text3);margin-top:2px;line-height:1.4;overflow-wrap:anywhere}
.ab-wl-note.ab-wl-bad{color:var(--ab-amber)}
.ab-wl-acts{display:flex;gap:6px;flex:none}
.ab-wl-search{font-size:11.5px;color:var(--ab-text2);margin-top:2px;overflow-wrap:anywhere}
.ab-wl-edit{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
.ab-wl-edit input{flex:1 1 260px;min-width:160px}
.ab-watch-grid{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:12px 0;font-size:12.5px}
.ab-watch-grid dt{color:var(--ab-text3)}
.ab-watch-grid dd{margin:0;color:var(--ab-text2);overflow-wrap:anywhere}
.ab-watch-warn{border-color:rgba(242,163,60,.35);background:var(--ab-amber-soft);color:var(--ab-amber);margin:10px 0}
.ab-watch-warn b{color:var(--ab-amber)}
.ab-watch-actions{flex-wrap:wrap}

/* the hamburger and the drawer scrim exist on every page but only show at <=1080 px; on desktop
   the button is display:none and the scrim carries [hidden], so neither takes any room */
.ab-navbtn{display:none}
.ab-scrim{position:fixed;inset:0;z-index:65;background:rgba(0,0,0,.55)}
.ab-scrim[hidden]{display:none}

/* <=1080 px: the rail leaves the layout and becomes an off-canvas drawer opened by the hamburger */
@media(max-width:1080px){
  .ab-navbtn{display:inline-flex;flex:none;width:32px;height:32px;padding:0;justify-content:center}
  .ab-navbars,.ab-navbars::before,.ab-navbars::after{display:block;width:16px;height:2px;border-radius:1px;
    background:currentColor;position:relative}
  .ab-navbars::before,.ab-navbars::after{content:"";position:absolute;left:0}
  .ab-navbars::before{top:-5px}
  .ab-navbars::after{top:5px}
  .ab-rail{position:fixed;top:0;bottom:0;left:0;z-index:70;width:min(280px,85vw);overflow-y:auto;
    transform:translateX(-100%);visibility:hidden;box-shadow:none;
    transition:transform .2s ease,visibility 0s linear .2s}
  body.ab-nav-open .ab-rail{transform:none;visibility:visible;box-shadow:8px 0 32px rgba(0,0,0,.5);
    transition:transform .2s ease}
}
@media(max-width:820px){.ab-hidemd{display:none}.ab-tbl col.ab-col-tier{width:0!important}}
@media(max-width:820px){
  /* no row may grow past the screen: everything that sat on one line may now wrap */
  .ab-controls{flex-wrap:wrap}
  .ab-statsline{flex-wrap:wrap;row-gap:8px}
  .ab-toolbar{flex-wrap:wrap}
  .chip{white-space:nowrap;flex:none}
}
/* <=640 px: the phone layout */
@media(max-width:640px){
  /* iOS Safari zooms the whole page when a text box under 16px takes focus, and does not zoom back */
  input:not([type=checkbox]):not([type=radio]):not([type=range]), select, textarea{font-size:16px}
  /* top strip: hamburger + title on one line, health pill and version stacked under it */
  .ab-topbar{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:8px 10px;
    padding:12px 12px 10px}
  .ab-topbar>*:not(.ab-navbtn):not(h1){grid-column:1/-1;justify-self:start;max-width:100%}
  .ab-topbar h1{min-width:0;overflow-wrap:anywhere}
  .ab-topbar .ab-right:empty{display:none}
  .ab-status{white-space:nowrap}
  .ab-version{white-space:nowrap}
  .ab-controls{padding:10px 12px 4px;row-gap:8px}
  .ab-controls .ab-note{white-space:normal}
  .ab-queue{margin:10px 12px 0;grid-template-columns:minmax(0,1fr)}
  .ab-queue .ab-qtitle{overflow-wrap:anywhere}
  .ab-queue .ab-qmeta{flex-wrap:wrap;gap:4px 14px}
  /* stats: converted | written on row one, progress + a bar that fills the rest on row two */
  .ab-statsline{display:grid;grid-template-columns:auto minmax(0,1fr);row-gap:8px;padding:10px 12px 12px}
  .ab-stat{padding:0 12px;min-width:0}
  .ab-stat:nth-child(3){padding-left:0;border-left:none}
  .ab-libprog{margin-left:0;padding-left:12px;min-width:0}
  .ab-libprog .ab-libbar{flex:1;width:auto;min-width:0}
  .ab-libprog .ab-libnum{flex:none;white-space:nowrap}
  /* filters: one horizontally scrolling chip row, then the search box + Clear */
  .ab-toolbar{flex-direction:column;align-items:stretch;gap:10px;padding:12px 12px 8px}
  .ab-toolbar{min-width:0;max-width:100%;box-sizing:border-box}
  /* the row scrolls inside its own box: without min-width:0 the flex item grows to its content
     (502 px at 390) and the whole .ab-main scrolls sideways instead */
  .chips{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;
    padding:3px;flex:none;width:100%;min-width:0;max-width:100%;box-sizing:border-box}
  .chips::-webkit-scrollbar{display:none}
  .ab-searchwrap{margin-left:0;width:100%}
  .ab-search{flex:1;min-width:0;width:auto}
  .ab-clear{flex:none}
  .ab-allclear{padding:0 12px 8px}
  .ab-overflow{padding:8px 12px}
  /* table: no WHY column - the table becomes blocks and each row a grid; the WHY cell moves
     under the title (the same cell, not a copy) */
  .ab-tablewrap{padding:0 8px 16px}
  .ab-tbl,.ab-tbl tbody{display:block;width:100%}
  .ab-tbl colgroup,.ab-tbl col.ab-col-why{display:none}
  .ab-tbl thead{display:block;position:sticky;top:0;z-index:2;background:var(--ab-bg)}
  .ab-tbl thead tr{display:flex}
  .ab-tbl thead th{position:static;display:none}
  .ab-tbl thead th[data-column="title"]{display:block;flex:1;padding-left:66px}
  .ab-tbl tr[hidden]{display:none!important}
  .ab-tbl tbody tr.urow{display:grid;grid-template-columns:58px minmax(0,1fr) 44px;
    grid-template-rows:auto auto auto;align-items:center}
  .ab-tbl tbody tr.urow>td{display:block;min-width:0;border-bottom:none}
  .ab-tbl tbody tr.urow{border-bottom:1px solid var(--ab-border-subtle)}
  .ab-tbl tbody tr.urow>td.ab-covcell{grid-column:1;grid-row:1/span 2;padding:0 4px 0 10px!important}
  .ab-tbl tbody tr.urow>td.ab-bookcell{grid-column:2;grid-row:1;display:flex;max-width:none;padding-top:8px}
  .ab-tbl tbody tr.urow>td.ab-whycell{grid-column:2;grid-row:2;padding-bottom:8px}
  .ab-tbl tbody tr.urow>td.ab-whycell:empty{padding:0 0 8px}
  /* MATCH moves (it was auto-placed into the 58 px cover column and cut to "Mediu"): its own
     labelled line under the WHY cell, spanning book + actions */
  .ab-tbl tbody tr.urow>td.ab-match{grid-column:2/-1;grid-row:3;display:flex;align-items:baseline;
    gap:6px;padding:0 10px 8px 8px;margin-top:-4px;overflow:hidden}
  .ab-tbl tbody tr.urow>td.ab-match::before{content:"Match";flex:none;font-size:11px;
    text-transform:uppercase;letter-spacing:.04em;color:var(--ab-text3)}
  .ab-tbl tbody tr.urow>td.ab-match:empty{display:none}
  .ab-tbl tbody tr.urow>td.ab-match .ab-match-in{flex:0 1 auto;min-width:0}
  .ab-tbl tbody tr.urow>td.ab-actcol{grid-column:3;grid-row:1/span 2;width:auto}
  .ab-bookcell .ab-btitle{white-space:normal;overflow-wrap:anywhere;display:-webkit-box;
    -webkit-box-orient:vertical;-webkit-line-clamp:2}
  .ab-tbl .ab-why{flex-wrap:wrap;white-space:normal;column-gap:8px}
  .ab-tbl .ab-why .ab-why-sub{margin-left:0;white-space:normal;overflow-wrap:anywhere}
  .ab-tbl tbody tr.ab-group,.ab-tbl tbody tr.ab-more-row,.ab-tbl tbody tr.drow,
  .ab-tbl tbody tr.ab-empty{display:block}
  .ab-tbl tbody tr.ab-group>td,.ab-tbl tbody tr.ab-more-row>td,.ab-tbl tbody tr.drow>td,
  .ab-tbl tbody tr.ab-empty>td{display:block;width:100%}
  .ab-tbl tbody tr.ab-more-row td{padding-left:66px}
  /* expanded detail: a small cover with the reason beside it, actions full width below */
  .dd{display:block;padding:14px 12px 16px;overflow-wrap:anywhere}
  .dd>*+*{margin-top:18px}
  .ab-toprow{display:block}
  .ab-toprow::after{content:"";display:table;clear:both}
  .ab-topcov{float:left;margin:0 10px 6px 0}
  .ab-topcov .ab-covbigwrap{width:88px;height:88px}
  .ab-topcov .ab-covbigwrap::after{font-size:24px}
  .ab-topmain{display:block}
  .ab-actrow{clear:both;flex-direction:column;align-items:stretch}
  .ab-actrow button{width:100%;justify-content:center}
  .ab-pinrow{flex-direction:column;align-items:stretch}
  .ab-pinrow input{width:100%}
  .ab-pinrow button{width:100%;justify-content:center}
  .ab-pinrow button.ab-lock{width:auto;align-self:flex-start}
  .ab-meter span{flex:1 1 0;min-width:0}
  .ab-evwin,.ab-evrival{flex-wrap:wrap}
  .ab-evmeta{flex:1 1 160px}
  .acts .btn.danger{margin-left:0!important}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

# <=1080 px the rail is an off-canvas drawer; the hamburger in the top strip opens it. Every page
# carries this. Above 1080 px the rail is the plain sidebar and none of this does anything.
NAV_SCRIPT = """
// A session that ends while a page is open makes every API call answer 401 "authentication required".
// Most buttons would toast that bare phrase (a few already redirect); one wrapper sends them all to the
// sign-in page instead, so the next step is on screen rather than a status line to decode.
(function () {
  const native = window.fetch;
  if (!native) return;
  window.fetch = function () {
    return native.apply(this, arguments).then(function (r) {
      if (r.status === 401 && !window.abSignedOut) { window.abSignedOut = true; location.href = '/login'; }
      return r;
    });
  };
})();
const abNavMq = window.matchMedia('(max-width:1080px)');
function abNavParts() {
  return {rail: document.querySelector('.ab-rail'), btn: document.querySelector('.ab-navbtn'),
          scrim: document.querySelector('.ab-scrim')};
}
function abNav(open) {
  const p = abNavParts();
  if (!p.rail || !p.btn) return;
  const was = document.body.classList.contains('ab-nav-open');
  open = !!open && abNavMq.matches;
  document.body.classList.toggle('ab-nav-open', open);
  p.btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (abNavMq.matches) p.rail.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (p.scrim) p.scrim.hidden = !open;
  if (open) {
    const first = p.rail.querySelector('.ab-rail-item.ab-active') || p.rail.querySelector('a, button');
    if (first) setTimeout(() => first.focus(), 0);  // after visibility flips, so it is focusable
  } else if (was) {
    p.btn.focus();
  }
}
(function () {
  const p = abNavParts();
  if (!p.rail) return;
  p.rail.id = p.rail.id || 'ab-rail';
  function sync() {
    document.body.classList.remove('ab-nav-open');
    if (p.btn) p.btn.setAttribute('aria-expanded', 'false');
    if (p.scrim) p.scrim.hidden = true;
    if (abNavMq.matches) p.rail.setAttribute('aria-hidden', 'true');
    else p.rail.removeAttribute('aria-hidden');
  }
  sync();
  if (abNavMq.addEventListener) abNavMq.addEventListener('change', sync); else abNavMq.addListener(sync);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.body.classList.contains('ab-nav-open')) { e.preventDefault(); abNav(false); }
  });
  // An open confirm dialog keeps Tab inside itself: focus must not wander onto the page behind it,
  // where a stray Enter would press a button the dialog is not asking about.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    const modal = document.getElementById('ab-modal');
    if (!modal || modal.hidden) return;
    const stops = [...modal.querySelectorAll('button:not([disabled])')];
    if (!stops.length) return;
    const at = stops.indexOf(document.activeElement);
    const next = e.shiftKey ? (at <= 0 ? stops.length - 1 : at - 1) : (at === stops.length - 1 ? 0 : at + 1);
    e.preventDefault();
    stops[next].focus();
  });
  p.rail.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('a[href]') && document.body.classList.contains('ab-nav-open')) abNav(false);
  });
})();
"""

SCRIPT = """
// The Set up downloading card: "No, just my library" is stored on the server, so the card never returns.
function abWizardDismiss() {
  const fb = document.getElementById('ab-wizcard-fb');
  if (fb) { fb.className = 'ab-fb'; fb.textContent = 'saving...'; }
  fetch('/api/setup/downloading', {method:'POST', headers:{'Content-Type':'application/json'},
                                   body: JSON.stringify({action: 'dismiss'})})
    .then(r => r.json().then(body => ({ok: r.ok && body.ok, body: body})))
    .then(res => {
      if (!res.ok) { if (fb) { fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'that did not work'; } return; }
      const card = document.getElementById('ab-wizcard');
      if (card) card.remove();
      const next = document.getElementById('ab-search');
      if (next) next.focus();
    })
    .catch(() => { if (fb) { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; } });
}
// The button a top-bar click came from. The inline onclick handlers call these synchronously, so
// the event in flight is still the click (Safari does not focus a clicked button, so
// document.activeElement is not enough).
function abClickedButton() {
  const ev = window.event;
  const t = ev && (ev.currentTarget || ev.target);
  return (t && t.closest) ? t.closest('button') : null;
}
// Every top-bar control posts the same way: the clicked button says "working..." while the daemon
// answers, and a refusal or an unreachable daemon is said out loud and the button comes back -
// never a reload into a page that looks as if nothing was clicked.
function abControl(payload, delay, btn) {
  const html = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.textContent = 'working\\u2026'; }
  const undo = () => { if (btn) { btn.disabled = false; btn.innerHTML = html; } };
  fetch('/api/control', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify(payload)})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    })
    .then(out => {
      if (!out.ok) { undo(); abToast(out.res.error || 'that did not work'); return; }
      setTimeout(() => location.reload(), delay);
    })
    .catch(err => {
      if (String(err && err.message) === 'signed out') return;
      undo();
      abToast('Could not reach the daemon - nothing was changed.');
    });
}
function act(action) {
  if (action === 'recheck' && !confirm(
      'Re-match every book against Audible?\\n\\n' +
      'This re-checks every book in the library, including ones already decided, and can take a while. ' +
      'Books that are already converted are left alone; a book that now passes your auto-convert rules ' +
      'starts converting.')) return;
  abControl({action: action}, 250, abClickedButton());
}
function stop_now() {
  if (!confirm('Stop the current conversion?\\n\\nThe partial file is discarded and the book is retried later. ' +
               'Conversions stay paused until you press Resume.')) return;
  abControl({action: 'stop'}, 500, abClickedButton());
}
function dry(on) {
  abControl({action: 'dry_run', value: on}, 250, abClickedButton());
}
function abWatcherDry(on) {
  abControl({action: 'watcher_dry_run', value: on}, 0, abClickedButton());
}
// Retry now / Convert anyway / Use this ASIN, from a detail panel or a table row:
// each of those carries data-path, so the nearest one names the book
function abUnitAction(btn, action) {
  const el = btn.closest('[data-path]');
  if (!el) return;
  const body = {action: action, path: el.dataset.path};
  let note = 'queued - re-checking…';
  if (action === 'set_asin') {
    const input = el.querySelector('input[data-asin]');
    body.asin = (input && input.value || '').trim();
    if (!body.asin) { abToast('Enter the Audible ASIN first - 8 to 14 letters and digits, like B005EHN61O.'); if (input) input.focus(); return; }
    note = 'setting ASIN ' + body.asin + ' - re-checking…';
  } else if (action === 'force') {
    if (!confirm('Convert \\u201c' + abBookTitle(el.dataset.path) + '\\u201d anyway?\\n\\n' +
                 'This skips the checks that flagged the book (confidence, runtime, damaged source) and ' +
                 'starts encoding it. If it was already converted it is encoded again. ' +
                 'Encoding can take a long time.')) return;
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
// the title shown on the book's row, for confirmations that must name what they touch
function abBookTitle(path) {
  const row = abFindRow(path);
  const t = row && row.querySelector('.ab-btitle');
  const first = t && t.firstChild;
  return ((first && first.textContent) || path || 'this book').trim();
}
function unitDelete(btn) {
  const path = btn.dataset.path;
  const title = btn.dataset.title || path;
  abOpenDeleteModal(path, title, btn);
}
let abDeleteTrigger = null;
function abOpenDeleteModal(path, title, trigger) {
  abDeleteTrigger = trigger;
  const confirmBtn = document.getElementById('ab-modal-confirm');
  if (trigger && trigger.dataset.source === 'ingest') {
    // a seeding torrent: only Audiobind's converted copy goes, the torrent's files are never touched
    document.getElementById('ab-modal-title').textContent = 'Remove from Audiobind?';
    document.getElementById('ab-modal-warning').textContent =
      'This removes the converted copy from /output and forgets the book in Audiobind. ' +
      'The torrent and its files stay in qBittorrent and keep seeding. This cannot be undone.';
    confirmBtn.textContent = 'Remove from library';
  } else {
    document.getElementById('ab-modal-title').textContent = 'Delete this book?';
    document.getElementById('ab-modal-warning').textContent =
      'This permanently deletes ' + title + "'s source folder and its published files. " +
      "This cannot be undone."
    confirmBtn.textContent = 'Delete permanently';
  }
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
  const ingest = abDeleteTrigger.dataset.source === 'ingest';
  const btn = document.getElementById('ab-modal-confirm');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = ingest ? 'removing\u2026' : 'deleting\u2026';
  abToast(ingest ? 'removing from Audiobind\u2026' : 'deleting permanently\u2026');
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
      // C2: a loose single-file book is a file, never a folder; files Audiobind did not write that
      // were left beside its removed conversion are said separately from the conversion itself
      const what = result.source_kind === 'file' ? 'file' : 'folder';
      const others = result.output_other_files_left
        ? ' Other files in that output folder were left alone: ' + result.output_other_files_left + '.' : '';
      if (result.dismissed) {
        abToast('Removed from Audiobind. The torrent is still seeding in qBittorrent.' +
                (result.output === 'kept' ? ' Output kept: ' + result.output_kept_reason + '.' : '') +
                others);
      } else if (result.source_kept) {
        abToast('Deleted from Audiobind and your output library. The source ' + what + ' stays (' +
                (result.source_kept_reason || 'your library is read-only') +
                ') - it will reappear on the next scan unless you remove it from the share.' +
                (result.output === 'kept' ? ' Output kept: ' + result.output_kept_reason + '.' : '') +
                others);
      } else if (result.source_removed) {
        if (result.output === 'kept') {
          abToast('Deleted the source ' + what + '. The converted copy stays in your output library (' +
                  result.output_kept_reason + ').');
        } else if (result.output === 'removed') {
          abToast('Deleted the source ' + what + ' and the converted copy in your output library.' + others);
        } else {
          abToast('Deleted the source ' + what +
                  '. There was no converted copy in your output library to remove.');
        }
      } else if (result.output === 'kept') {
        abToast('Deleted. Output kept: ' + result.output_kept_reason + '.');
      } else if (others) {
        abToast('Deleted the converted copy from your output library.' + others);
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
function abFocusAsin(row) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return false;
  panel.hidden = false;
  row.classList.add('ab-sel');
  abRowSync(row);
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
    // hidden by the search box or past its group's first rows: show it
    if (row.dataset.group) {
      const head = document.querySelector('tr.ab-group[data-group="' + row.dataset.group + '"]');
      if (head) head.dataset.open = '1';
    }
    const box = abSearchBox();
    if (box) box.value = '';
    try { sessionStorage.setItem(SEARCH_KEY, ''); } catch (e) {}
    abApplyView();
  }
  if (row && abFocusAsin(row)) return;
  // the table may not have this one (another status filter, past the row limit):
  // reload on a view that does, panel open
  try { sessionStorage.setItem(SEARCH_KEY, ''); } catch (e) {}
  location.href = '/?status=' + encodeURIComponent(el.dataset.status || '') +
    '&q=' + encodeURIComponent(path) + '&open=' + encodeURIComponent(path);
}
function abCoverFail(img) {
  img.classList.add('ab-gone');
}
// Enter in one of these boxes presses the button that belongs to it (Use this ASIN, Save metadata);
// they are not in a <form>, so without this Enter did nothing at all
function abEnterClicks(ev, input) {
  if (ev.key !== 'Enter' || ev.isComposing) return;
  const scope = input.closest('.ab-pinrow, .ab-edit');
  const btn = scope && scope.querySelector('button[data-enter]');
  if (!btn || btn.disabled) return;
  ev.preventDefault();
  btn.click();
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
  chip.textContent = on ? 'Locked by you' : 'Unlocked';
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
    abRowSync(row);
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
function abRowSync(row) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return;
  row.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
}
function toggleRow(row, ev) {
  const panel = row.nextElementSibling;
  if (!panel || !panel.classList.contains('drow')) return;
  // a click on the kebab, a button or a link does its own job and must not also toggle the row
  const hit = ev && ev.target && ev.target.closest ? ev.target.closest('a,button,input,select,textarea') : null;
  if (hit && row.contains(hit)) return;
  panel.hidden = !panel.hidden;
  row.classList.toggle('ab-sel', !panel.hidden);
  abRowSync(row);
}
function abRowKey(ev, row) {
  if (ev.target !== row || (ev.key !== 'Enter' && ev.key !== ' ')) return;
  ev.preventDefault();
  toggleRow(row, ev);
}
document.addEventListener('DOMContentLoaded', () => {
  // a manual Retry now / Use this ASIN that still needs a decision re-opens its panel
  document.querySelectorAll('tr.drow[data-reopen]').forEach(panel => {
    panel.hidden = false;
    if (panel.previousElementSibling) {
      panel.previousElementSibling.classList.add('ab-sel');
      abRowSync(panel.previousElementSibling);
    }
  });
});
const SEARCH_KEY = 'audiobind_query';
const AB_NUMERIC = {why: true, tier: true, runtime: true, scan: true, title: false};
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
  const heads = [...body.querySelectorAll('tr.ab-group')];
  if (heads.length) {
    // "Needs you": sort inside each group, keep the groups in their order, page each one
    heads.forEach(head => {
      const group = head.dataset.group;
      const more = body.querySelector('tr.ab-more-row[data-group="' + group + '"]');
      body.appendChild(head);
      let hits = 0, paged = 0;
      pairs.forEach(([row, panel]) => {
        if (row.dataset.group !== group) return;
        if (!row.hidden) {
          hits += 1;
          if (!terms.length && !head.dataset.open && hits > AB_GROUP_PAGE) {
            row.hidden = true;
            paged += 1;
          }
        }
        body.appendChild(row);
        if (panel) body.appendChild(panel);
      });
      if (more) {
        body.appendChild(more);
        more.hidden = paged === 0;
        const btn = more.querySelector('button.ab-more');
        if (btn) btn.textContent = 'Show ' + paged + ' more ' + (btn.dataset.label || '');
      }
      head.hidden = hits === 0;
    });
  } else {
    pairs.forEach(([row, panel]) => {
      body.appendChild(row);
      if (panel) body.appendChild(panel);
    });
  }
  const open = openSet();
  pairs.forEach(([row, panel]) => {
    if (!panel) return;
    const wanted = open.has(panel.dataset.path) && !row.hidden;
    panel.hidden = !wanted;
    row.classList.toggle('ab-sel', wanted);
    abRowSync(row);
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
const AB_GROUP_PAGE = 12;
function abShowMore(btn) {
  // plain unhide: every row is already on the page, nothing is fetched
  const head = document.querySelector('tr.ab-group[data-group="' + btn.dataset.group + '"]');
  if (head) head.dataset.open = '1';
  abApplyView();
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
function abScanUI(scan, label) {
  const live = document.getElementById('ab-scanlive');
  const btn = document.getElementById('ab-scanbtn');
  const re = document.getElementById('ab-recheckbtn');
  if (!live) return;
  live.hidden = false;
  const text = document.getElementById('ab-scanlive-text');
  // control.scan_label names the book while the scan encodes it inline (one encode can run
  // ~15 minutes); "Scanning n/m" is only the fallback when nothing is encoding
  if (text) text.textContent = label || ('Scanning ' + (scan.done || 0) + '/' + (scan.total || 0) + '\u2026');
  if (btn) { btn.disabled = true; btn.textContent = 'Scanning\u2026'; }
  if (re) re.disabled = true;
}
function abScanIdle() {
  const live = document.getElementById('ab-scanlive');
  const btn = document.getElementById('ab-scanbtn');
  const re = document.getElementById('ab-recheckbtn');
  if (live) live.hidden = true;
  if (btn && btn.disabled) { btn.disabled = false; btn.textContent = 'Scan for changes'; }
  if (re) re.disabled = false;
}
function abAuthed(r) {
  if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
  return r.json();
}
function abDur(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  const m = Math.floor((s % 3600) / 60);
  return Math.floor(s / 3600) + 'h ' + (m < 10 ? '0' : '') + m + 'm';
}
// the encode strip's speed / ETA / elapsed, rebuilt from /api/state with text nodes only
function abQMeta(prog) {
  const box = document.getElementById('ab-qprog');
  if (!box) return;
  const parts = [];
  const add = (bold, rest) => {
    const f = document.createDocumentFragment();
    if (bold) { const b = document.createElement('b'); b.textContent = bold; f.appendChild(b); }
    f.appendChild(document.createTextNode(rest));
    parts.push(f);
  };
  if (!prog || !Object.keys(prog).length) {
    add('', 'analysing - the bar appears once encoding starts');
  } else {
    if (prog.speed) add(prog.speed + 'x', ' realtime');
    if (prog.eta) add(abDur(prog.eta), ' left');
    if (prog.elapsed) add('', abDur(prog.elapsed) + ' elapsed');
  }
  box.textContent = '';
  parts.forEach((f, i) => {
    if (i) box.appendChild(document.createTextNode(' \u00b7 '));
    box.appendChild(f);
  });
}
function tick() {
  fetch('/api/state').then(abAuthed).then(d => {
    abScanFinishedToast(d);
    const bar = document.getElementById('ab-qfill');
    if (d.scan && d.scan.scanning) { abScanUI(d.scan, d.scan_label); return; }
    abScanIdle();
    if (!bar) return;
    if (!d.running) { if (!abBusy()) location.reload(); return; }
    const p = (d.progress && d.progress.percent) || 0;
    bar.style.width = p.toFixed(1) + '%';
    const label = document.getElementById('ab-qpct');
    if (label) label.textContent = p.toFixed(1) + '%';
    abQMeta(d.progress);
    const qt = document.getElementById('ab-qtitle');
    const name = d.current_title || d.current_unit;
    if (qt && name && qt.textContent !== name) qt.textContent = name;
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
<title>Audiobind &middot; {view_heading}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand"><a href="/?status=all" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item{lib_active}" href="/?status=all"{lib_current}>
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item{rail_attention_alert}{att_active}" href="/?status=attention"{att_current}>
      <span class="ab-glyph">&#9888;</span> Needs your review
      <span class="ab-count">{rail_attention_count}</span></a>
    {series_item}
    {discover_item}
    <a class="ab-rail-item" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{ingest_line}{signout}
  </div>
</aside>

<div class="ab-scrim" hidden onclick="abNav(false)"></div>
<main class="ab-main">
  <header class="ab-topbar">
    <button class="ab-navbtn btn" type="button" aria-label="Open navigation" aria-controls="ab-rail"
            aria-expanded="false" onclick="abNav(true)"><span class="ab-navbars" aria-hidden="true"></span></button>
    <h1><a href="/?status=all" title="Back to the library" style="color:inherit;text-decoration:none">{view_heading}</a></h1>
    <span class="ab-status{health_stale}"><span class="ab-dot"></span>{health_text}<small> &middot; last scan {health_scan}</small></span>
    <span class="ab-version">v{version}</span>
    {dry_text}
  </header>

{queue_strip}
{wizard_card}

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
             aria-label="Filter books" placeholder="Filter books&hellip;" value="{query}">
      <button class="ab-clear btn" onclick="abClearSearch()">Clear</button>
    </div>
  </section>

{allclear}
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
  <div class="ab-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="ab-modal-title" aria-describedby="ab-modal-warning">
    <h3 id="ab-modal-title"></h3>
    <p id="ab-modal-warning"></p>
    <div class="ab-modal-actions">
      <button class="btn danger" id="ab-modal-confirm" onclick="abModalConfirm()">Delete permanently</button>
      <button class="btn" id="ab-modal-cancel" onclick="abModalCancel()">Cancel</button>
    </div>
  </div>
</div>

<script>
{nav_script}
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
  <div class="ab-rail-brand"><a href="/?status=all" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item{lib_active}" href="/?status=all"{lib_current}>
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item{rail_attention_alert}{att_active}" href="/?status=attention"{att_current}>
      <span class="ab-glyph">&#9888;</span> Needs your review
      <span class="ab-count">{rail_attention_count}</span></a>
    {series_item}
    {discover_item}
    <a class="ab-rail-item{set_active}" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{ingest_line}{signout}
  </div>
</aside>
<div class="ab-scrim" hidden onclick="abNav(false)"></div>
<main class="ab-main">
  <header class="ab-topbar">
    <button class="ab-navbtn btn" type="button" aria-label="Open navigation" aria-controls="ab-rail"
            aria-expanded="false" onclick="abNav(true)"><span class="ab-navbars" aria-hidden="true"></span></button>
    <h1>Settings</h1>
    <span class="ab-version">v{version}</span>
    <div class="ab-right"></div>
  </header>
  <section class="ab-settings" style="padding:24px;max-width:640px;">
    <div class="ab-hintbox">
      <b>Apply later.</b> Changes take effect on the next scan or conversion. Existing m4b files are never touched.</div>
    {settings_tabs}
    <form id="ab-settings-form" class="ab-settings-form" onsubmit="abSaveSettings(); return false;" novalidate>
    {library_panel}
    {watcher_panel}
    {advanced_panel}
    <div class="ab-editbar" style="margin-top:8px;"{savebar_hidden}>
      <button class="ab-save" type="submit">Save settings</button>
      <button class="btn" type="button" onclick="abResetSettings()">Reset to defaults</button>
      <span class="ab-fb" id="ab-settings-fb" role="status"></span>
    </div>
    </form>
    {account_panel}
  </section>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>

<div id="ab-modal" class="ab-modal" hidden>
  <div class="ab-modal-backdrop" onclick="abModalCancel()"></div>
  <div class="ab-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="ab-modal-title" aria-describedby="ab-modal-warning">
    <h3 id="ab-modal-title"></h3>
    <p id="ab-modal-warning"></p>
    <div class="ab-modal-actions">
      <button class="btn danger" id="ab-modal-confirm" onclick="abModalConfirm()">Move and rename</button>
      <button class="btn" id="ab-modal-cancel" onclick="abModalCancel()">Cancel</button>
    </div>
  </div>
</div>
<script>
{nav_script}
{settings_script}
{account_script}
{watcher_script}
{library_script}
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
.ab-login .ab-remember{{display:flex;align-items:center;gap:8px;margin-bottom:2px;cursor:pointer}}
.ab-login .ab-remember-hint{{color:var(--ab-text3);font-size:11px;margin:0 0 14px 22px}}
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
    <label class="ab-remember"><input type="checkbox" name="remember" value="1">
      Keep me signed in for 30 days</label>
    <div class="ab-remember-hint">For this browser only. Leave it off on a shared computer.</div>
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
    <label for="ab-setup-code">Setup code (printed in the container log when Audiobind first started)</label>
    <input class="ab-in" id="ab-setup-code" name="setup_code" type="text" autocomplete="off"
           autocapitalize="characters" spellcheck="false" maxlength="64" autofocus required>
    <label for="ab-setup-username">Username</label>
    <input class="ab-in" id="ab-setup-username" name="username" type="text" value="{setup_username}"
           autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="64"
           required>
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


def render_login(error: str = "", username: str = "", legacy: bool = False, remembered: str = "") -> str:
    """``username`` (e.g. after a failed attempt) wins over ``remembered`` (the ab_user cookie)."""
    username = username or remembered
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
    return (
        '<br><form method="post" action="/logout" style="display:inline;margin:0">'
        '<button type="submit" style="background:none;border:0;padding:0;font:inherit;cursor:pointer;'
        'text-decoration:underline;color:var(--ab-text3)">sign out</button></form>'
    )


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
      <span class="ab-fb" data-acct-fb role="status"></span></div>
  </form>
  <form class="ab-acct-form" data-acct="username" onsubmit="return abAccountSubmit(this)">
    <span class="ab-acct-title">Change username</span>
    <label class="ab-acct-lbl">New username
      <input class="ab-in" name="username" type="text" autocomplete="username" autocapitalize="none"
             spellcheck="false" maxlength="64" required></label>
    <label class="ab-acct-lbl">Current password
      <input class="ab-in" name="current" type="password" autocomplete="current-password" required></label>
    <div class="ab-acct-row"><button class="ab-save" type="submit">Change username</button>
      <span class="ab-fb" data-acct-fb role="status"></span></div>
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
      <span class="ab-fb" data-acct-fb role="status"></span></div>
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
  const submit = form.querySelector('button[type=submit]');
  if (submit) submit.disabled = true;  // a second click must not send the password change twice
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
    })
    .finally(() => { if (submit) submit.disabled = false; });
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

function abUnitShow(el) {
  const wrap = el.closest('.ab-range-wrap');
  const unit = wrap && wrap.querySelector('[data-unit-one]');
  if (unit) unit.textContent = Number(el.value) === 1 && el.value.trim() !== '' ? unit.dataset.unitOne : unit.dataset.unitMany;
}

document.addEventListener('input', ev => {
  if (ev.target && ev.target.matches && ev.target.matches('input[type=number]')) abUnitShow(ev.target);
});

// each [data-setting] is a wrapper div; the value lives on the select/input inside it
function abSettingControl(wrap) {
  return wrap.querySelector('select, input, textarea');
}

function abSettingValue(el) {
  if (el.tagName === 'SELECT') return el.value;
  if (el.type === 'range' || el.type === 'number') return parseInt(el.value, 10);
  return el.value;
}

// Save: only what you changed (an untouched field keeps following the container). A secret's box
// is always empty - typing sends a new value, blank keeps the stored one, Clear removes it.
// fbId: where to report (default: the top bar). quiet: no "nothing changed", no reload (the
// connection tests save first). Resolves true when the values are saved (or nothing changed).
function abSaveSettings(fbId, quiet) {
  const fb = document.getElementById(fbId || 'ab-settings-fb');
  const settings = {};
  const clear = [];
  document.querySelectorAll('[data-setting]').forEach(wrap => {
    const key = wrap.dataset.setting;
    const box = wrap.querySelector('input[data-clear]');
    if (box && box.checked) { clear.push(key); return; }
    const el = abSettingControl(wrap);
    if (!el) return;
    if (String(el.value) === String(el.dataset.initial)) return;
    settings[key] = abSettingValue(el);
  });
  if (Object.keys(settings).length === 0 && clear.length === 0) {
    if (!quiet) {
      fb.className = 'ab-fb';
      fb.textContent = 'nothing changed';
      setTimeout(() => { fb.textContent = ''; }, 4000);
    }
    return Promise.resolve(true);
  }
  fb.className = 'ab-fb';
  fb.textContent = 'saving...';
  return fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({settings: settings, clear: clear})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      if (!res.ok) {
        fb.className = 'ab-fb ab-bad';
        fb.textContent = res.body.error || 'could not save';
        return false;
      }
      fb.className = 'ab-fb ab-good';
      fb.textContent = 'saved';
      let reload = clear.length > 0;
      document.querySelectorAll('[data-setting]').forEach(wrap => {
        const key = wrap.dataset.setting;
        const el = abSettingControl(wrap);
        if (!el || !Object.prototype.hasOwnProperty.call(settings, key)) return;
        if (wrap.hasAttribute('data-secret')) {
          el.value = '';  // the page never keeps a secret, not even the one just typed
          el.placeholder = 'set - leave blank to keep it';
        }
        el.dataset.initial = el.value;
        if (wrap.hasAttribute('data-reload')) reload = true;
      });
      if (reload && !quiet) {
        // the Watcher status, the "set here" marks and any clamped value are rendered by the server
        setTimeout(() => { location.reload(); }, 700);
      }
      document.querySelectorAll('[data-setting] .ab-custom').forEach(mark => {
        const key = mark.dataset.field;
        mark.hidden = res.body.custom.indexOf(key) === -1;
      });
      setTimeout(() => { fb.textContent = ''; fb.className = 'ab-fb'; }, 6000);
      return true;
    })
    .catch(() => {
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
      return false;
    });
}

function abResetSettings() {
  if (!confirm('Reset the conversion settings to the container defaults?\\n\\n' +
               'Every value saved on this page is forgotten and the values from the container environment ' +
               'apply again. The watcher connection settings and secrets are kept.')) return;
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
          if (el.type === 'number') abUnitShow(el);
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

// Each setting's label is a <span> beside its control, so a screen reader met an unnamed box. Name the
// control from the label's own text (not the "set here" / Reset badges inside it) and point it at
// the hint, which holds the unit, the limits and what the setting does.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.ab-setting').forEach((wrap, i) => {
    const label = wrap.querySelector('.ab-setting-label');
    const control = wrap.querySelector('select, textarea, input:not([type=checkbox]):not([type=hidden])');
    const first = label && label.firstChild;
    const name = first && first.nodeType === 3 ? first.textContent.trim() : '';
    if (!control || !name || control.hasAttribute('aria-label') || control.hasAttribute('aria-labelledby')) return;
    control.setAttribute('aria-label', name);
    const hint = wrap.querySelector('.ab-setting-hint');
    if (hint) {
      hint.id = hint.id || 'ab-sh-' + i;
      control.setAttribute('aria-describedby', hint.id);
    }
  });
});
// The tabs, the rail and a refresh are all plain page loads, so a typed-but-unsaved value used to vanish
// without a word. Every control remembers what the server last had (data-initial; Save and Reset both
// update it before any reload), so "differs from that" is exactly "not saved yet".
function abSettingsDirty() {
  return [...document.querySelectorAll('[data-setting]')].some(wrap => {
    const el = abSettingControl(wrap);
    return el && String(el.value) !== String(el.dataset.initial);
  });
}
window.addEventListener('beforeunload', function (e) {
  if (!abSettingsDirty()) return;
  e.preventDefault();
  e.returnValue = '';  // the browser shows its own "leave site? changes you made may not be saved"
});
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
  <div class="ab-rail-brand"><a href="/?status=all" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item{lib_active}" href="/?status=all"{lib_current}>
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item{rail_attention_alert}{att_active}" href="/?status=attention"{att_current}>
      <span class="ab-glyph">&#9888;</span> Needs your review
      <span class="ab-count">{rail_attention_count}</span></a>
    {series_item}
    {discover_item}
    <a class="ab-rail-item" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &rarr; <span title="{output_attr}">{output}</span>{signout}
  </div>
</aside>
<div class="ab-scrim" hidden onclick="abNav(false)"></div>
<main class="ab-main">
  <header class="ab-topbar">
    <button class="ab-navbtn btn" type="button" aria-label="Open navigation" aria-controls="ab-rail"
            aria-expanded="false" onclick="abNav(true)"><span class="ab-navbars" aria-hidden="true"></span></button>
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
{nav_script}
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
// Want / Want all missing: onto the wishlist (the Wishlist tab); nothing is downloaded
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
    return _html(text).replace('"', "&quot;").replace("'", "&#x27;")


def public_settings(values: dict) -> dict:
    """A settings dict safe to send to a browser: a secret (the tracker session, the qBittorrent
    password) becomes True/False - is one set - and never its value. SEC-05c: an address with an
    ``@`` in it is masked (redact_url), so no login in a URL reaches a page or a JSON answer."""
    return {key: (bool(value) if key in SECRET_SETTING_KEYS
                  else redact_url(value) if key in URL_SETTING_KEYS and "@" in str(value or "")
                  else value)
            for key, value in values.items()}


def _breakable_path(p) -> str:
    """The path HTML-escaped, with a zero-width break after every separator.

    The rail footer is narrow: a long path wraps at a "/" instead of mid-word
    ("audiob /ooks"). Only for visible text - a title attribute keeps the plain path.
    """
    return _html(str(p or "")).replace("/", "/&#8203;").replace("\\", "\\&#8203;")


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
    "review": ("Review", "rev", False),
    "failed": ("Failed", "err", False),
    "ready": ("Ready", "enc", False),
    "interrupted": ("Interrupted", "rev", False),
}
#: states that wait on the operator read "waiting 3h" instead of "3h ago"
WAITING_STATUSES = {"review"}


def status_dot(status: str, updated_at=0.0, now: float | None = None, label: bool = True, dot: bool = True) -> str:
    """One labeled dot: color + word + age. Replaces the old per-row status pill.

    The word sits in its own span so a narrow column ellipsizes the word, never the age.
    ``label=False`` is the Waiting column (0.21.0): the dot and the bare age ("6d"); the
    word stays in the title. ``dot=False`` is the Needs you view (0.21.0-dev4): the group
    header names the status already, so only the age shows (the title keeps status + age).
    """
    name = (status or "").lower()
    word, family, moving = STATUS_DOTS.get(name, (name.capitalize() or "Unknown", "idle", False))
    age = rel_age(updated_at, now, waiting=name in WAITING_STATUSES)
    classes = f"ab-st ab-st-{family}" + (" ab-st-moving" if moving else "")
    title = word + (f" · {age}" if age else "")
    if not label:
        bare = short_age(updated_at, now)
        age_html = f'<span class="ab-st-age">{_html(bare)}</span>' if bare else DASH
        return (
            f'<span class="{classes}" title="{_attr(title)}">'
            + ('<span class="ab-st-dot" aria-hidden="true"></span>' if dot else "")
            + f"{age_html}</span>"
        )
    age_html = f'<span class="ab-st-age">{_html(age)}</span>' if age else ""
    return (
        f'<span class="{classes}" title="{_attr(title)}">'
        f'<span class="ab-st-dot" aria-hidden="true"></span>'
        f'<span class="ab-st-label">{_html(word)}</span>{age_html}</span>'
    )


def status_age(status: str, updated_at=0.0, now: float | None = None) -> str:
    """"Held · 7d": the status word and how long it has been in it. Plain text.

    0.21.0-dev4: the table has no Waiting column; this is the row's tooltip and the detail
    panel's line, so the age never leaves the product.
    """
    name = (status or "").lower()
    word = STATUS_DOTS.get(name, (name.capitalize() or "Unknown",))[0]
    age = short_age(updated_at, now)
    return f"{word} · {age}" if age else word


def short_age(timestamp, now: float | None = None) -> str:
    """The bare age for the Waiting column, always relative, never a date.

    "<1m", "39m", "5h", "6d", "12d", then weeks ("5w") up to 8 weeks, months ("5mo") up to a
    year, then years ("2y"). Empty when the time is unknown.
    """
    try:
        ts = float(timestamp or 0)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    now = time.time() if now is None else float(now)
    seconds = max(0.0, now - ts)
    days = seconds / 86400
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if days < 14:
        return f"{int(days)}d"
    if days < 56:
        return f"{int(days // 7)}w"
    if days < 365:
        return f"{max(2, int(days // 30))}mo"
    return f"{int(days // 365)}y"


#: the long form ("High Confidence") is the cell's title and the panel's word; the cell itself
#: shows the short word after the meter (0.21.0, the mockup's wording)
CONFIDENCE_WORDS = {
    "HIGH": ("High Confidence", "ab-conf-high"),
    "MEDIUM": ("Medium Confidence", "ab-conf-medium"),
    "LOW": ("Low Confidence", "ab-conf-low"),
}


def series_label(detail: dict, overrides: dict) -> str:
    """"Jack Reacher #14" - the series the file is tagged with (Q-B12: overrides.series_of, the
    recorded Audnex value with the operator's series on top). Plain text."""
    name, seq, _source = overrides_mod.series_of((detail or {}).get("match"), overrides or {})
    if not name:
        return ""
    return f"{name} #{seq}" if seq else name


def series_source_note(detail: dict, overrides: dict) -> str:
    """Q-B12: one plain sentence when the series shown is not the one the file will carry from
    Audnex - the Audible fallback. "" otherwise (Audnex, the operator's own, or no series)."""
    match = (detail or {}).get("match") or {}
    name, _seq, source = overrides_mod.series_of(match, overrides or {})
    if not name or source != overrides_mod.SERIES_CATALOG or "series_source" not in match:
        return ""
    if match.get("audnex_checked"):
        return (f"The series ({name}) comes from the Audible search. Audnex lists no series for this "
                "book, so the converted file won't carry one unless you set one above.")
    return f"The series ({name}) comes from the Audible search; Audnex hasn't been checked for this book yet."


def match_cell(unit, detail: dict, overrides: dict, asin_override: str = "") -> tuple[str, str]:
    """(HTML, plain-text title) for the Match column.

    A hand-set ASIN reads "Current ASIN · <word>"; an automatic match reads
    "<series #n> · <word>" on a converted row and just "<word>" on a row that needs you, the word being High / Medium / Low / Collection / Not on Audible.
    The title keeps the long form ("Jack Reacher #14 · High Confidence"). No match is the dash.
    """
    conf = (unit.confidence or "").upper()
    match = (detail or {}).get("match") or {}
    conf_html = conf_text = ""
    if conf in CONFIDENCE_WORDS:
        conf_text, cls = CONFIDENCE_WORDS[conf]
        conf_html = f'<span class="ab-match-word {cls}">{conf_text.split()[0]}</span>'
    elif conf == "COLLECTION":
        conf_text = "Looks like a collection"
        conf_html = '<span class="ab-match-word ab-conf-none">Collection</span>'
    elif asin_override and match.get("asin_pin_failed"):
        conf_text = "Not in Audible's catalog"
        conf_html = '<span class="ab-match-word ab-conf-low">Not on Audible</span>'

    if asin_override:
        lead_html = '<span class="ab-match-lead ab-match-cur">Current ASIN</span>'
        lead_text = f"Current ASIN {asin_override} (you set it)"
    else:
        lead_text = series_label(detail, overrides) if conf_html else ""
        lead_html = f'<span class="ab-match-lead">{_html(lead_text)}</span>' if lead_text else ""

    parts_html = [p for p in (lead_html, conf_html) if p]
    if not parts_html:
        return DASH, ""
    title = " · ".join(p for p in (lead_text, conf_text) if p)
    # a row that needs you shows the bare word (the series position stays in the title);
    # a hand-set ASIN keeps its "Current ASIN · <word>" either way
    if not asin_override and (unit.status or "").lower() in ATTENTION_STATUSES and conf_html:
        parts_html = [conf_html]
    # 0.21.0: a readout, not a button - segments filled to the level, the word beside it
    level = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}.get(conf, 0)
    meter = (
        '<span class="ab-mmeter" aria-hidden="true">'
        + "".join('<b class="ab-f"></b>' if i < level else "<b></b>" for i in range(3))
        + "</span>"
        if level
        else ""
    )
    return (
        '<span class="ab-match-in">'
        + meter
        + '<span class="ab-match-sep">&middot;</span>'.join(parts_html)
        + "</span>",
        title,
    )


LOCK_ON_TEXT = "Locked by you"
LOCK_OFF_TEXT = "Unlocked"


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


#: on the table row: an ingest book never blurs with a library one
INGEST_BADGE = (
    '<span class="ab-ingest-badge" title="From the ingest folder: a downloaded torrent that is '
    'still seeding. Audiobind only reads it.">&#8675; ingest</span>'
)


def kebab_menu(unit) -> str:
    """The visible row menu that replaced the hover-only buttons."""
    path = _attr(unit.path)
    title = _attr(unit.title or unit.path)
    # the delete item reads this: an ingest row's Delete opens the remove-from-Audiobind wording
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
#: rows shown per "Needs you" group before its "Show N more" button
GROUP_PAGE = 12

COLUMNS = (
    # Book has no width: it is the content, so it absorbs whatever room is left and a long
    # title uses it (td.ab-bookcell caps the text at min(720px,100%)). Why and Match are fixed:
    # Why narrows below 1360 px (the 20vw media rule), Match is a meter and one word, so 150 px
    # holds it - the meter shrinks before the word ever clips. Below 820 px Match's cells drop
    # (.ab-hidemd) and its col goes to 0 so the room returns to Book.
    # 0.21.0-dev4: Length and Waiting are gone from the table in every view. Why carries the
    # runtimes where they matter; the age lives in the row's tooltip and the detail panel.
    ("title", "Book", "", ""),
    ("why", "Why", "min(300px,22vw)", ""),
    ("tier", "Match", "150px", "ab-hidemd"),
)
COVER_WIDTH = "50px"
#: the cover column and the row's action column flank COLUMNS; every colspan uses this
TOTAL_COLUMNS = len(COLUMNS) + 1 + 1
#: the sortable header columns
SORT_COLUMNS = tuple(key for key, *_ in COLUMNS)
#: every key a row carries sort data for and a stored sort may name: the header columns plus
#: length and last activity, which no longer have a header ("scan" - newest first - stays the
#: default order)
SORT_KEYS = SORT_COLUMNS + ("runtime", "scan")
NUMERIC_COLUMNS = {"why", "tier", "runtime", "scan"}
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
    if column in ("why", "status"):
        return status_rank(unit.status)
    if column == "tier":
        return tier_rank(unit.confidence)
    if column == "runtime":
        return float(unit.hours or 0)
    if column == "scan":
        return float(unit.updated_at or 0)
    return (unit.title or unit.path or "").lower()


def sort_units(units: list, column: str, direction: str) -> list:
    if column not in SORT_KEYS:
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
    *,
    same_author: bool = True,
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
        if not same_author:
            return "Same score as the pick, but a different author — check which one you have"
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
        f'ASIN <a class="ab-alink" href="https://www.audible.com/pd/{asin}" '
        f'target="_blank" rel="noopener">{asin}</a>'
    )
    winner_corroborated = match.get("corroborated") is True
    if winner_corroborated:
        facts.append("Audnex agrees")
    if pinned:
        score_html = '<div class="ab-evv">&mdash;</div><div class="ab-evl">you picked this edition</div>'
    else:
        mid = "" if score >= 0.85 else " ab-mid"
        label = "match score" if author_known else "title-only score"
        score_html = f'<div class="ab-evv{mid}">{score:.2f}</div><div class="ab-evl">{label}</div>'
        grade = str(match.get("confidence") or "").upper()
        if grade:
            facts.append(f"graded {_html(grade)}")
        if "margin" in match:
            facts.append(f"margin {_num(match.get('margin')):.2f} over the next different book")
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
            alt_authors = alt.get("authors") or []
            reason = rival_reason(
                alt,
                local_hours,
                tolerance,
                winner_score=score,
                author_known=author_known,
                winner_corroborated=winner_corroborated,
                # unknown author on either side: don't accuse
                same_author=not authors
                or not alt_authors
                or matching.norm(authors[0]) == matching.norm(str(alt_authors[0])),
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
        + hardcover_line(data)
        + rivals_html
        + "</div>"
    )


#: how a Hardcover comparison point is named in the panel line
_HC_FIELD = {"series": "series", "position": "series position", "year": "year", "first published": "first published"}


def hardcover_line(data: dict) -> str:
    """The detail panel's one Hardcover line: what it lists and whether that agrees with the catalogue.

    "" while Hardcover evidence is off (toggle off or no token) - even for a row recorded while it was
    on - and for a row recorded without it. Reads the stored detail only; never a request."""
    hc = (data or {}).get("hardcover")
    if not isinstance(hc, dict) or not hardcover_mod.enabled():
        return ""
    status = hc.get("status")
    if status == "not_found":
        text = "Hardcover has no record of this book."
    elif status == "error":
        text = (f"Hardcover could not be asked: {_html(str(hc.get('message') or 'lookup failed'))}"
                " &#8212; it is asked again on the next scan.")
    elif status == "found":
        rec = hc.get("record") if isinstance(hc.get("record"), dict) else {}
        title = _html(str(rec.get("title") or ""))
        url = str(rec.get("url") or "")
        if url.startswith("https://hardcover.app/books/"):
            title = f'<a class="ab-alink" href="{_attr(url)}" target="_blank" rel="noopener">{title}</a>'
        facts = []
        series, pos = str(rec.get("series") or ""), str(rec.get("position") or "")
        if series:
            facts.append(f"book {_html(pos)} of {_html(series)}" if pos else f"in {_html(series)}")
        if rec.get("via") == "asin" and rec.get("edition_year"):
            facts.append(f"{_html(str(rec['edition_year']))} edition")
        elif rec.get("first_year"):
            facts.append(f"first published {_html(str(rec['first_year']))}")
        verdict = hc.get("verdict")
        points = [p for p in (hc.get("points") or []) if isinstance(p, dict)]
        if verdict == "disagrees":
            why = []
            for p in points:
                if p.get("agrees") is not False:
                    continue
                if p.get("field") == "book":
                    why.append(f"its record is a different book ({_html(str(p.get('hardcover') or ''))})")
                else:
                    name = _HC_FIELD.get(str(p.get("field")), _html(str(p.get("field"))))
                    why.append(f"{name} {_html(str(p.get('hardcover') or ''))}, catalogue says "
                               f"{_html(str(p.get('catalogue') or ''))}")
            judged = "<b>disagrees with the catalogue</b>: " + "; ".join(why)
        elif verdict == "agrees":
            judged = "agrees with the catalogue"
        else:
            judged = "lists nothing the catalogue can be compared with"
        listed = f" &#8212; {', '.join(facts)}" if facts else ""
        text = f"Hardcover: {title}{listed} &#183; {judged}."
    else:
        return ""
    return f'<p class="ab-hint ab-hc" data-hardcover="{_attr(str(status))}">{text}</p>'


def read_sort(state) -> tuple:
    column = state.get_meta("sort_column", "scan")
    direction = state.get_meta("sort_dir", "desc")
    if column not in SORT_KEYS:
        column = "scan"
    if direction not in ("asc", "desc"):
        direction = "desc"
    return column, direction


# --- Series shelf (approved mockups 3 and 3b, 0.15.0) -------------------------------------
# Everything here reads the stored series listings; nothing on a render path touches the
# network (series.aggregate queues stale listings for its background refresher instead).
# f-strings only: series and book titles come from the catalog and from folder names.

AUDIBLE_PD = "https://www.audible.com/pd/"
_EMPTY_SHELF = {"series": [], "by_path": {}, "missing_total": 0}


def _series_shelf(state, refresh: bool = False) -> dict:
    """The aggregation, or an empty shelf - a series problem must never blank a page."""
    try:
        return series_mod.aggregate(state, refresh=refresh)
    except Exception:  # noqa: BLE001
        logging.getLogger("audiobind").exception("series shelf could not be built")
        return dict(_EMPTY_SHELF)


def _alert_class(count) -> str:
    """The rail's amber mark, only for an item (Needs you, Series) that has something in it."""
    try:
        return " ab-alert" if int(count) > 0 else ""
    except (TypeError, ValueError):
        return ""


def _rail_view_marks(view: str | None) -> dict:
    """Active state for the rail's Library and Needs-your-review items, from the view render() computed.
    Library is the `all` view, Needs your review the `attention` view; any other page passes None."""
    def marks(on: bool) -> tuple[str, str]:
        return (" ab-active", ' aria-current="page"') if on else ("", "")
    lib_active, lib_current = marks(view == "all")
    att_active, att_current = marks(view == "attention")
    return {"lib_active": lib_active, "lib_current": lib_current,
            "att_active": att_active, "att_current": att_current}


def _series_rail_item(missing: int, active: bool = False) -> str:
    """The rail's Series tab, under Library; the count is books missing across series (an opportunity, not an alarm)."""
    classes = "ab-rail-item" + (" ab-active" if active else "")
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
            '<p class="ab-series-intro">Every series your books belong to that Audible lists '
            "with two or more books, checked against the Audible series order. " + want_help +
            "<b>I have this</b> marks a book you own without a rip, so it stops counting as "
            "missing. A series total only ever comes from the Audible catalog &#8212; when it "
            "has no listing, the total says unknown.</p>"
        )
        try:
            wanted = frozenset(state.wishlist_asins())
        except Exception:  # noqa: BLE001 - the wishlist must never blank the Series page
            logging.getLogger("audiobind").debug("series page: wishlist ASINs unreadable", exc_info=True)
            wanted = frozenset()
        cards = "".join(_series_card(v) for v in views)
        panels = "".join(_series_panel(v, wanted) for v in views)
        body = f'{intro}<section class="ab-series-shelf" aria-label="Your series">{cards}</section>{panels}'
    else:
        body = (
            '<div class="ab-series-empty">No series on your shelf yet. Once one of your books belongs '
            "to a series Audible lists with two or more books, it shows up here &mdash; with what you have and "
            "what you're missing.</div>"
        )
    return SERIES_PAGE.format(
        **_rail_view_marks(None),
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        rail_total=sum(counts.values()),
        rail_attention_count=rc["attention"],
        rail_attention_alert=_alert_class(rc["attention"]),
        series_item=_series_rail_item(shelf["missing_total"], active=True),
        discover_item=_discover_rail_item(state),
        library=_breakable_path(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_breakable_path(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        body=body,
        nav_script=NAV_SCRIPT,
        series_script=SERIES_SCRIPT,
    )


# ---------------------------------------------------------------------------- Discover
# Search the catalog, suggestions from what you own, the wishlist. A "Get this" / "Want"
# click is the only thing that ever adds a wishlist row. f-strings only for anything that
# carries a catalog title; numeric character references in new markup.

DISCOVER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Audiobind &#183; {page_title}</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="apple-touch-icon" href="/favicon.svg">
<meta name="theme-color" content="#5e6ad2">
<style>{styles}</style></head><body>
<aside class="ab-rail">
  <div class="ab-rail-brand"><a href="/?status=all" title="Back to the library">Audiobind</a></div>
  <div class="ab-rail-label">Workspace</div>
  <nav class="ab-rail-section" aria-label="Main">
    <a class="ab-rail-item{lib_active}" href="/?status=all"{lib_current}>
      <span class="ab-glyph">&#8962;</span> Library
      <span class="ab-count">{rail_total}</span></a>
    <a class="ab-rail-item{rail_attention_alert}{att_active}" href="/?status=attention"{att_current}>
      <span class="ab-glyph">&#9888;</span> Needs your review
      <span class="ab-count">{rail_attention_count}</span></a>
    {series_item}
    {discover_item}
    <a class="ab-rail-item" href="/settings">
      <span class="ab-glyph">&#9881;</span> Settings
      </a>
  </nav>
  <div class="ab-rail-spacer"></div>
  <div class="ab-rail-foot">
    <b>library</b> <span title="{library_attr}">{library}</span><br>
    <b>output</b> &#8594; <span title="{output_attr}">{output}</span>{signout}
  </div>
</aside>
<div class="ab-scrim" hidden onclick="abNav(false)"></div>
<main class="ab-main">
  <header class="ab-topbar">
    <button class="ab-navbtn btn" type="button" aria-label="Open navigation" aria-controls="ab-rail"
            aria-expanded="false" onclick="abNav(true)"><span class="ab-navbars" aria-hidden="true"></span></button>
    <h1>{page_title}</h1>
    <span class="ab-version">v{version}</span>
    <div class="ab-right"></div>
  </header>
  <div class="ab-disc-wrap">
{disc_body}
  </div>
</main>
<div id="ab-toast" role="status" aria-live="polite"></div>
<script>
{nav_script}
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
  // the spoken name carries the book's title; it must not outlive the visible words it describes
  const spoken = btn.getAttribute('aria-label');
  const speak = () => { if (spoken) btn.setAttribute('aria-label', spoken); };
  btn.removeAttribute('aria-label');
  btn.disabled = true;
  btn.textContent = 'adding\\u2026';
  abPost('/api/wishlist', {asin: row.dataset.asin, origin: origin})
    .then(out => {
      if (!out.ok) {
        abToast(out.res.error || 'that did not work');
        btn.disabled = false;
        btn.textContent = label;
        speak();
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
      speak();
    });
}
// A book the watcher has already handed to qBittorrent: what removing or re-queuing it does to the torrent
function abWishlistInFlight(row) {
  return row.dataset.status === 'downloading' || row.dataset.status === 'downloaded';
}
function abWishlistTitle(row) {
  const t = row.querySelector('.ab-disc-t');
  return ((t && t.textContent) || 'this book').trim();
}
function abUnwant(btn) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  if (abWishlistInFlight(row) && !confirm('Remove \\u201c' + abWishlistTitle(row) + '\\u201d from your wishlist?\\n\\n' +
      'The watcher already sent it to qBittorrent. Removing it here does not stop or delete the torrent, ' +
      'and Audiobind stops tracking it - it will not be marked In library when it lands.')) return;
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
// Re-queue: a book already handed to qBittorrent (or in the library) goes back to wanted. Confirms first:
// the existing torrent is not removed, so the watcher can send a second copy.
function abRequeue(btn) {
  const row = btn.closest('[data-asin]');
  if (!row) return;
  if (!confirm('Search again for \\u201c' + abWishlistTitle(row) + '\\u201d?\\n\\n' +
      'It is already ' + row.dataset.status.replace('_', ' ') + '. Re-queuing puts it back to wanted and the watcher ' +
      'searches the tracker again, which can send a second copy to qBittorrent. ' +
      'The existing torrent is not removed from qBittorrent.')) return;
  btn.disabled = true;
  abPost('/api/wishlist/requeue', {asin: row.dataset.asin})
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
// Edit title: the watcher searches this title for the row (blank = the catalog title again)
function abEditTitle(btn) {
  const row = btn.closest('[data-asin]');
  if (!row || row.querySelector('.ab-wl-edit')) return;
  const form = document.createElement('div');
  form.className = 'ab-wl-edit';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'ab-in';
  input.value = row.dataset.search || '';
  input.placeholder = 'Title to search the tracker for (blank = catalog title)';
  input.setAttribute('aria-label', 'Title to search the tracker for');
  const save = document.createElement('button');
  save.className = 'btn primary';
  save.textContent = 'Save';
  const cancel = document.createElement('button');
  cancel.className = 'btn';
  cancel.textContent = 'Cancel';
  form.append(input, save, cancel);
  row.querySelector('.ab-disc-meta').appendChild(form);
  input.focus();
  input.select();
  cancel.onclick = () => form.remove();
  input.onkeydown = e => {
    if (e.key === 'Enter') save.click();
    else if (e.key === 'Escape') cancel.click();
  };
  save.onclick = () => {
    save.disabled = true;
    abPost('/api/wishlist/title', {asin: row.dataset.asin, title: input.value})
      .then(out => {
        if (!out.ok) { abToast(out.res.error || 'that did not work'); save.disabled = false; return; }
        const item = out.res.item || {};
        const meta = row.querySelector('.ab-disc-meta');
        let mark = row.querySelector('.ab-wl-search');
        const custom = item.search_title || '';
        if (custom) {
          if (!mark) {
            mark = document.createElement('div');
            mark.className = 'ab-wl-search';
            meta.querySelector('.ab-disc-t').after(mark);
          }
          mark.textContent = 'searching for \u201c' + custom + '\u201d';
        } else if (mark) {
          mark.remove();
        }
        row.dataset.search = custom;
        form.remove();
        // only a row the server put back to wanted changes; one past searching keeps its status
        if (item.status !== 'wanted') {
          abToast('Saved - it is already ' + String(item.status || row.dataset.status).replace('_', ' ') +
                  ', so it is not searched again. Use Re-queue to search for it again.');
          return;
        }
        const pill = row.querySelector('.ab-disc-pill');
        if (pill) { pill.textContent = 'WANTED'; pill.className = 'ab-disc-pill ab-wl-wanted'; pill.title = ''; }
        const note = row.querySelector('.ab-wl-note');
        if (note) note.remove();
        const retry = row.querySelector('.ab-wl-retry');
        if (retry) retry.remove();
        row.dataset.status = 'wanted';
        abToast(custom ? 'Saved - the watcher searches for that title in its next cycle'
                       : 'Back to the catalog title - the watcher searches for it in its next cycle');
      })
      .catch(() => { abToast('could not reach the daemon'); save.disabled = false; });
  };
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

def _discover_rail_item(state, active: str | bool = "") -> str:
    """The rail's Discover books tab, under Series, and (with the watcher configured) the Wishlist tab
    right below it. The count badge on Wishlist is the books still wanted/downloading."""
    if active is True:
        active = "discover"

    def link(key: str, href: str, glyph: str, label: str, badge: str = "") -> str:
        on = active == key
        classes = "ab-rail-item" + (" ab-active" if on else "")
        current = ' aria-current="page"' if on else ""
        return (
            f'<a class="{classes}" href="{href}"{current}>\n'
            f'      <span class="ab-glyph">{glyph}</span> {label}{badge}</a>'
        )

    out = link("discover", "/discover", "&#8981;", "Discover books")
    if watcher_mod.configured():  # a watcher-only surface: nothing consumes the wishlist without the watcher
        try:
            wanted = state.wishlist_count()
        except Exception:  # noqa: BLE001 - the rail must never blank a page
            logging.getLogger("audiobind").debug("rail: wishlist count unreadable", exc_info=True)
            wanted = 0
        hidden = "" if wanted else " hidden"
        badge = (
            f'\n      <span class="ab-count" id="ab-rail-wanted" title="books on your wishlist"{hidden}>{int(wanted)}</span>'
        )
        out += "\n    " + link("wishlist", "/wishlist", "&#9734;", "Wishlist", badge)
    return out


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
            f'<button class="btn" title="Put this book on your wishlist. Nothing is downloaded." '
            f'aria-label="Get this: {_attr(title)}" '
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
    "wanted": ("WANTED", "Waiting: the watcher searches for it in a coming cycle."),
    "downloading": ("DOWNLOADING", "Found on the tracker and handed to qBittorrent; it becomes In library once converted."),
    "downloaded": ("DOWNLOADED", "The torrent finished in qBittorrent; it becomes In library once the converted book lands."),
    "in_library": ("IN LIBRARY", "A converted book in your library is this one."),
    "not_found": ("NOT FOUND", "The tracker had no good match in several searches. Retry searches again."),
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


def _wedge_sentence(w: dict) -> str:
    """The wedge budget in one sentence (ledger only; shown only when a freeleech parameter is set)."""
    if not w.get("can_spend"):
        return "No freeleech parameter set, so no credits are ever spent."
    spent = int(w.get("spent_day") or 0)
    text = f"Freeleech credits: {spent} spent in the last 24 hours"
    if w.get("left") is not None:
        text += f", {int(w['left'])} left of today's allowance"
    if w.get("reserve"):
        text += f", {int(w['reserve'])} kept in reserve"
    return text + "."


def _watcher_bar(status: dict) -> str:
    """One line above the wishlist: is anything going to look for these books?"""
    link = '<span class="ab-spacer"></span><a href="/settings#watcher">Watcher settings</a>'
    if not status["configured"]:
        text = (f"The watcher is <b>off</b> ({_html(status.get('reason') or 'not configured')}): "
                "nothing here is searched for or downloaded.")
    elif not status["enabled"]:
        text = f'<span class="warn">The watcher is disabled: {_html(status["reason"])}.</span>'
    elif status.get("egress_guard") == "blocked":
        text = ('<span class="warn">The watcher is <b>stopped</b>: this connection is not the one you allowed '
                f'({_html(status.get("egress_reason") or "")}). Nothing is searched for.</span>')
    elif status.get("egress_guard") == "unknown" and status.get("egress_checked_at"):
        text = ('<span class="warn">The watcher is <b>stopped</b>: it cannot confirm this connection is the VPN, '
                "so it will not contact the tracker.</span>")
    elif status["paused"]:
        text = "The watcher is <b>paused</b>: nothing is searched for until you resume it."
    else:
        bits = [f"The watcher checks up to <b>{int(status['max_per_cycle'])}</b> wanted "
                f"{_plural(int(status['max_per_cycle']), 'book', 'books')} every "
                f"<b>{int(status['interval_min'])} min</b>"]
        if status["busy"]:
            bits.append("a cycle is running now")
        elif status.get("next_cycle_at"):
            bits.append(f"next cycle {_html(_until(status['next_cycle_at']))}")
        last = (status.get("last_cycle") or {}).get("at")
        if last:
            bits.append(f"last cycle {_html(rel_age(last))}")
        bits.append(_html(_wedge_sentence(status.get("wedges") or {})))
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
        f'class="ab-wl-retry" aria-label="Retry: {_attr(row["title"])}" onclick="abRetry(this)">Retry</button>'
        if status in ("not_found", "error") else ""
    )
    requeue = (
        '<button title="Search for this book again. It is already past searching, so the torrent in qBittorrent '
        'is not removed and the watcher can send a second copy." '
        f'class="ab-wl-requeue" aria-label="Re-queue: {_attr(row["title"])}" onclick="abRequeue(this)">Re-queue</button>'
        if status in WISHLIST_REQUEUEABLE else ""
    )
    custom = row.get("search_title") or ""
    searching =f'<div class="ab-wl-search">searching for &#8220;{_html(custom)}&#8221;</div>' if custom else ""
    edit = (
        '<button title="Change the title the watcher searches the tracker for (the title shown here stays)." '
        f'aria-label="Edit title: {_attr(row["title"])}" onclick="abEditTitle(this)">Edit title</button>'
        if status != "in_library" else ""
    )
    return (
        f'<div class="ab-disc-row" data-asin="{_attr(asin)}" data-status="{_attr(status)}" '
        f'data-search="{_attr(custom or row["title"])}">'
        f'<img src="{_attr(src)}" alt="" loading="lazy" onerror="abCoverFail(this)">'
        f'<div class="ab-disc-meta"><div class="ab-disc-t" title="{_attr(row["title"])}">{_html(row["title"])}</div>'
        f'{searching}{by}<div class="ab-disc-m">{" &#183; ".join(facts)}</div>{note}</div>'
        f'<span class="ab-disc-pill ab-wl-{_attr(status)}" title="{_attr(hover)}">{_html(label)}</span>'
        f'<span class="ab-wl-acts">{retry}{requeue}{edit}'
        f'<button title="Take this book off your wishlist." aria-label="Remove from wishlist: {_attr(row["title"])}" '
        'onclick="abUnwant(this)">Remove</button></span></div>'
    )


def _discover_wishlist_body(state) -> str:
    rows = state.wishlist_all()
    status = watcher_mod.WATCHER.status(state)
    if status["enabled"]:
        how = ("The watcher searches for <b>wanted</b> books and hands a good match to qBittorrent; "
               "a book turns <b>In library</b> once it is converted. ")
    else:
        how = "This list is the hand-off point: nothing here is searched for or downloaded while the watcher is off. "
    intro = (
        f'<p class="ab-disc-intro">The books you asked for with <b>Get this</b> or <b>Want</b>. {how}'
        "<b>Retry</b> searches again for a book that was not found or failed; "
        "<b>Re-queue</b> searches again for one already sent to qBittorrent; "
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


#: the session hint when Tracker URL is on myanonamouse.net: where that site gives you the value. Only
#: "Preferences > Security" and the toggle's name are used - both are already in docs/ip-updater.md
MAM_SESSION_HINT = (
    "On MyAnonamouse: Preferences \u2192 Security \u2192 create a session for the address the tracker will see "
    "(your VPN's address if the tracker is reached through one), tick 'Allow session to set dynamic seedbox IP', "
    "and paste the long value MAM gives you. It is never shown again once saved.")

#: under the qBittorrent category field (Settings and the template say the same)
ONE_FOLDER_THREE_NAMES = (
    "One folder, three names. Example: host folder /mnt/user/downloads/audiobooks \u2192 Audiobind's Ingest path "
    "(Audiobind calls it /ingest) \u2192 in qBittorrent, the category's save path is whatever qBittorrent's own "
    "Docker page maps that host folder to (often /downloads/audiobooks or /data/audiobooks). "
    "Never type /ingest into qBittorrent.")

#: what the proxy and the IP checks do not cover (ONB-16), and where the two container-only settings
#: live on Unraid (ONB-17): said the same on the Watcher tab, the Advanced tab, the template and the docs
VPN_SCOPE_NOTE = ("The proxy and the IP checks cover Audiobind's own visits to the tracker (searches and .torrent "
                  "files). The download itself is qBittorrent's; route qBittorrent through your VPN separately.")
UNRAID_CONTAINER_ONLY_PATH = ("On Unraid: Docker tab \u2192 Audiobind \u2192 Edit \u2192 Show more settings \u2192 "
                              "'Tracker proxy' / 'Home IP (guard)' \u2192 Apply (restarts the container).")

#: the one rule for the qBittorrent address (template, Settings and docs say the same)
QBIT_URL_HINT = ("The address you open qBittorrent's WebUI with, e.g. http://192.168.1.10:8080. Only if Audiobind "
                 "shares a VPN container's network with qBittorrent: http://127.0.0.1:<its port>.")

#: hints for the watcher's connection settings (Settings > Watcher > Where it looks)
WATCH_FIELD_HINTS = {
    "tracker_url": "The site's address, e.g. https://tracker.example - no path; the paths are below.",
    "tracker_session": ("The value of the site's session cookie, copied from your browser while you are signed in. "
                        "It is never shown again once saved."),
    "tracker_cookie_name": "The name of that session cookie, e.g. session_id.",
    "tracker_search_path": "Where the site answers searches, e.g. /search.json.",
    "tracker_search_body": ('The search request as a JSON object. {{title}} and {{author}} are filled in for each '
                            'book, e.g. {"q": "{{title}} {{author}}"}.'),
    "tracker_search_method": "How the search is sent. Most sites take POST.",
    "tracker_search_encoding": "How a POST search is encoded: json, or form for a classic web form.",
    "tracker_search_category": ("Optional. The tracker's own value for the search request, filled in for {{category}} "
                                "- not one of Audiobind's tiers; every tracker defines its own (often a number). A "
                                "preset file can supply the list as a dropdown. Empty = {{category}} is left out of "
                                "the request."),
    "tracker_download_path": "Where the site hands out .torrent files, e.g. /download.php.",
    "tracker_download_param": "The download link's torrent id parameter. Usually tid.",
    "tracker_freeleech_param": ("Optional. The parameter that spends a freeleech credit on a download. Empty = "
                                "Audiobind never spends one."),
    "tracker_user_agent": "Optional. Empty = audiobind/<version>; none = send no User-Agent at all.",
    "qbit_url": QBIT_URL_HINT,
    "qbit_user": "The qBittorrent WebUI login.",
    "qbit_pass": "The qBittorrent WebUI password. It is never shown again once saved.",
    "qbit_category": "The category new torrents get. " + ONE_FOLDER_THREE_NAMES,
    "watch_max_incomplete": "Add nothing while this many torrents in that category are still downloading. 0 = off.",
    "download_client": ("The program the watcher hands torrents to. qBittorrent unless you choose another; only the "
                        "chosen client's settings below are used."),
    "deluge_url": "Your Deluge web UI, e.g. http://deluge:8112. Its Label plugin must be enabled.",
    "deluge_pass": "The Deluge web UI password. It is never shown again once saved.",
    "transmission_url": ("Your Transmission RPC, e.g. http://transmission:9091/transmission/rpc (a bare "
                         "http://host:9091 gets that path added). Transmission 3.0 or newer."),
    "transmission_user": "Optional. Only when Transmission asks for a login.",
    "transmission_pass": "Optional. It is never shown again once saved.",
    "rtorrent_url": ("scgi://rtorrent:5000 for rTorrent's SCGI port, or http://rutorrent/RPC2 for an XML-RPC "
                     "mount behind a web server. SCGI has no password: keep that port off any network you do not trust."),
    "rtorrent_user": "Optional. Only for an http(s) /RPC2 mount that asks for a login.",
    "rtorrent_pass": "Optional. It is never shown again once saved.",
    "hardcover_token": ("From hardcover.app > Settings > Hardcover API (with or without the leading "
                        "\"Bearer\"). It is never shown again once saved. Set in the container as HARDCOVER_TOKEN."),
}


def _source_mark(key: str) -> str:
    """Where a watcher setting's value comes from: this page (with a way back) or the container."""
    if is_custom(SETTING_MAP[key]):
        return (
            '<span class="ab-src ab-src-here">set here</span>'
            f'<button type="button" class="ab-src-reset" onclick="abResetSetting(\'{key}\')" '
            'title="Forget the value saved here and use the container\'s again">Reset to container value</button>'
        )
    return '<span class="ab-src">from the container</span>'


def _watch_wrap(key: str, label: str, hint: str, control: str, secret: bool = False) -> str:
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}" data-reload{" data-secret" if secret else ""}>'
        f'<span class="ab-setting-label">{_html(label)}{_source_mark(key)}</span>'
        f"{hint_html}{control}</div>"
    )


def _watch_text(key: str, label: str, placeholder: str = "", area: bool = False, hint: str | None = None) -> str:
    value = _attr(str(setting_effective(key) or ""))
    # these are URLs, paths, parameter names and logins: a phone keyboard must not capitalise the
    # first letter, and the address boxes get the URL keyboard. (type stays text: scgi:// and
    # relative paths are valid here and the form never validates natively.)
    kb = ' autocapitalize="none"' + (' inputmode="url"' if key.endswith("_url") else "")
    if area:
        control = (f'<textarea class="ab-in" rows="3" data-initial="{value}" placeholder="{_attr(placeholder)}" '
                   f'autocomplete="off" spellcheck="false" autocapitalize="none">{value}</textarea>')
    else:
        control = (f'<input type="text" class="ab-in" value="{value}" data-initial="{value}" '
                   f'placeholder="{_attr(placeholder)}" autocomplete="off" spellcheck="false"{kb}>')
    if hint is None:
        hint = EXPECTED_EGRESS_HINT if key == "expected_egress" else WATCH_FIELD_HINTS.get(key, "")
    return _watch_wrap(key, label, hint, control)


def _watch_secret(key: str, label: str) -> str:
    """A secret: an empty password box, a placeholder that says whether one is set, and Clear.
    The stored value is never written into the page - not as a value, an attribute or a title."""
    is_set = bool(setting_effective(key))
    placeholder = "set - leave blank to keep it" if is_set else "not set"
    control = (f'<input type="password" class="ab-in" value="" data-initial="" placeholder="{placeholder}" '
               'autocomplete="new-password" spellcheck="false">')
    if is_set:
        control += '<label class="ab-src ab-src-clear"><input type="checkbox" data-clear> Clear</label>'
    hint = WATCH_FIELD_HINTS.get(key, "")
    if key == "tracker_session" and ipupdate_mod.is_mam(str(setting_effective("tracker_url") or "")):
        hint = MAM_SESSION_HINT
    return _watch_wrap(key, label, hint, control, secret=True)


def _watch_select(key: str, label: str, hint: str, titlecase: bool = False) -> str:
    options = setting_choices(key)
    value = str(setting_effective(key))
    selected = next((o for o in options if str(o).upper() == value.upper()), options[0])
    opt_html = "".join(
        f'<option value="{_attr(o)}"{" selected" if o == selected else ""}>{_html(o.title() if titlecase else o)}</option>'
        for o in options
    )
    return _watch_wrap(key, label, hint, f'<select class="ab-in" data-initial="{_attr(selected)}">{opt_html}</select>')


CATEGORY_NONE = "None (leave {{category}} out)"
CATEGORY_DROPDOWN_HINT = ("The tracker's own category for the search request, from your preset file. The value saved "
                          "is the number or text in brackets. None = {{category}} is left out of the request.")


def _watch_category(label: str = "Search category") -> str:
    """The search category: a dropdown when the preset file supplies the tracker's own list, else a text field.
    The option value is the bare key; a saved value that is not in the list stays, as its own option."""
    preset, _why = preset_mod.load()
    categories = preset.categories if preset is not None else {}
    if not categories:
        return _watch_text("tracker_search_category", label)
    value = str(setting_effective("tracker_search_category") or "").strip()
    options = [("", CATEGORY_NONE)] + [(k, f"{n} ({k})") for k, n in categories.items()]
    if value and value not in categories:
        options.append((value, f"{value} (not in the preset list)"))
    opt_html = "".join(
        f'<option value="{_attr(k)}"{" selected" if k == value else ""}>{_html(text)}</option>' for k, text in options
    )
    control = f'<select class="ab-in" data-initial="{_attr(value)}">{opt_html}</select>'
    return _watch_wrap("tracker_search_category", label, CATEGORY_DROPDOWN_HINT, control)


def _watch_number(key: str, label: str, value: int, suffix: str, hint: str = "") -> str:
    lo, hi = setting_bounds(key)
    control = (
        '<div class="ab-range-wrap">'
        f'<input type="number" class="ab-in" min="{lo}" max="{hi}" value="{value}" step="1" data-initial="{value}">'
        f"{_unit_span(suffix, value)}</div>"
    )
    return _watch_wrap(key, label, hint, control)


def _preset_box() -> str:
    """Settings > Watcher > Where it looks: "Fill the tracker fields from preset file" - only when the file is
    there. A good file: its own label, the fields it will fill, which of them replace something, one button.
    A bad file: a plain error and no button. The file is read here only to draw this box."""
    preset, why = preset_mod.load()
    path = _html(str(preset_mod.path_of()))
    if preset is None:
        if not why:
            return ""
        return ('<div class="ab-hintbox ab-watch-warn" id="ab-preset-error" role="alert"><b>Tracker preset file.</b> '
                f"{_html(why)} Nothing was changed. Fix <code>{path}</code>, or remove it.</div>")
    plan = preset_mod.plan(preset, setting_effective)

    def names(keys) -> str:
        return ", ".join(_html(preset_mod.label_of(k)) for k in keys)

    parts = [f'<div class="ab-acct-who" id="ab-preset"><b>Tracker preset: {_html(preset.label)}</b> '
             f"(from <code>{path}</code>). "]
    if not preset.values:
        # a category list and nothing to fill: name it, say what it does, offer no button that would fill nothing
        return "".join(parts) + ("It lists the tracker's search categories, so Search category below is a dropdown. "
                                 "It has no fields to fill, and nothing is saved from it.</div>")
    if plan["fill"]:
        parts.append(f"It will fill: {names(plan['fill'])}. ")
    if plan["replace"]:
        parts.append(f"It will replace what is set now in: <b>{names(plan['replace'])}</b> (you are asked first). ")
    if plan["same"]:
        parts.append(f"Already the same: {names(plan['same'])}. ")
    parts.append("Your session cookie value is never in a preset: you still paste that yourself. "
                 "Nothing is saved until you press the button.</div>")
    if not plan["fill"] and not plan["replace"]:
        return "".join(parts)
    return (
        "".join(parts)
        + '<div class="ab-acct-row"><button class="btn" type="button" id="ab-preset-fill" onclick="abPreset(false)">'
        "Fill the tracker fields from preset file</button>"
        '<span class="ab-fb" id="ab-preset-fb" role="status"></span></div>'
        '<div class="ab-hintbox" id="ab-preset-confirm" role="alert" hidden><span id="ab-preset-confirm-text"></span> '
        '<button class="btn" type="button" id="ab-preset-yes" onclick="abPreset(true)">Replace them</button> '
        '<button class="btn" type="button" id="ab-preset-no" onclick="abPresetCancel()">Cancel</button></div>'
    )


def _watch_connection_fields() -> str:
    """Settings > Watcher > Where it looks: the tracker and qBittorrent connection."""
    return (
        '<div class="ab-acct-title">Where it looks</div>'
        '<div class="ab-acct-who">The tracker and the qBittorrent it hands torrents to. A value saved here wins over '
        "the container's; Reset to container value goes back to it. Two settings stay container-only: "
        "<code>TRACKER_PROXY</code> and <code>TRACKER_HOME_IP</code>. "
        f"{_html(UNRAID_CONTAINER_ONLY_PATH)} {_html(VPN_SCOPE_NOTE)}</div>"
        + _preset_box()
        + _watch_text("tracker_url", "Tracker URL", "https://tracker.example")
        + _watch_secret("tracker_session", "Session cookie value")
        + _watch_text("tracker_cookie_name", "Session cookie name", "session_id")
        + _watch_text("tracker_search_path", "Search path", "/search.json")
        + _watch_text("tracker_search_body", "Search request body", '{"q": "{{title}} {{author}}"}', area=True)
        + _watch_select("tracker_search_method", "Search method", WATCH_FIELD_HINTS["tracker_search_method"])
        + _watch_select("tracker_search_encoding", "Search encoding", WATCH_FIELD_HINTS["tracker_search_encoding"])
        + _watch_category("Search category")
        + _watch_text("tracker_download_path", "Download path", "/download.php")
        + _watch_text("tracker_download_param", "Download parameter", "tid")
        + _watch_text("tracker_freeleech_param", "Freeleech parameter")
        + _watch_text("tracker_user_agent", "User-Agent", "audiobind/<version>")
        + _watch_select("vip", "VIP / Premium member", VIP_HINT, titlecase=True)
        + _download_client_fields()
    )


def _download_client_fields() -> str:
    """Settings > Watcher > Download client: which client, its connection, one Test connection. Every
    client's fields are in the form (so each setting is there exactly once); the ones of the clients
    not chosen are hidden, and the select shows them as it changes."""
    kind = watcher_mod.client_kind()
    options = "".join(
        f'<option value="{_attr(key)}"{" selected" if key == kind else ""}>{_html(label)}</option>'
        for key, label in DOWNLOAD_CLIENTS.items()
    )
    select = _watch_wrap(
        "download_client", "Download client", WATCH_FIELD_HINTS["download_client"],
        f'<select class="ab-in" data-initial="{_attr(kind)}" onchange="abDownloadClient(this.value)">{options}</select>')

    def group(name: str, body: str) -> str:
        return f'<div class="ab-dc" data-client="{name}"{"" if name == kind else " hidden"}>{body}</div>'

    return (
        '<div class="ab-acct-title">Download client</div>'
        + select
        + group("qbittorrent",
                _watch_text("qbit_url", "qBittorrent URL", "http://192.168.1.10:8080")
                + _watch_text("qbit_user", "qBittorrent user")
                + _watch_secret("qbit_pass", "qBittorrent password"))
        + group("deluge",
                _watch_text("deluge_url", "Deluge URL", "http://deluge:8112")
                + _watch_secret("deluge_pass", "Deluge password"))
        + group("transmission",
                _watch_text("transmission_url", "Transmission URL", "http://transmission:9091/transmission/rpc")
                + _watch_text("transmission_user", "Transmission user")
                + _watch_secret("transmission_pass", "Transmission password"))
        + group("rtorrent",
                _watch_text("rtorrent_url", "rTorrent URL", "scgi://rtorrent:5000")
                + _watch_text("rtorrent_user", "rTorrent user")
                + _watch_secret("rtorrent_pass", "rTorrent password"))
        + (_watch_text("qbit_category", "qBittorrent category", "audiobooks") if kind == "qbittorrent" else
           _watch_text("qbit_category", f"{DOWNLOAD_CLIENTS[kind]} label", "audiobooks", hint=(
               "The label new torrents get (rTorrent: custom1, which ruTorrent shows as the label). Have the "
               "client move or save that label's downloads into Audiobind's ingest folder. Set in the container "
               "as QBIT_CATEGORY, whichever client is chosen.")))
        + '<div class="ab-acct-row">'
        '<button class="btn" type="button" id="ab-dc-test" onclick="abWatchTest(\'qbit\', \'ab-dc-test-fb\')">'
        "Test connection</button>"
        '<span class="ab-fb" id="ab-dc-test-fb" role="status"></span></div>'
        '<div class="ab-test-detail" id="ab-dc-test-fb-detail" hidden></div>'
    )


def _watch_schedule_fields() -> str:
    """Settings > Watcher > How often it looks: pacing and the account-protection budgets."""
    fields = (
        '<div class="ab-acct-title">How often it looks</div>'
        + _watch_number("watch_interval", "Check the wishlist every", watch_interval_min(), "minutes")
        + _watch_number("watch_per_cycle", "Books per cycle", watch_max_per_cycle(), "books")
        + _watch_number("watch_max_incomplete", "Downloads in progress allowed", watch_max_incomplete(), "torrents",
                        WATCH_FIELD_HINTS["watch_max_incomplete"])
    )
    for key, label, value, suffix in (
        ("wedge_per_cycle", "Freeleech credits per cycle", wedge_per_cycle(), "credits"),
        ("wedge_per_day", "Freeleech credits per day", wedge_per_day(), "credits"),
        ("wedge_reserve", "Freeleech credits kept in reserve", wedge_reserve(), "credits"),
        ("watch_max_unsatisfied", "Unsatisfied snatches allowed", watch_max_unsatisfied(), "torrents"),
    ):
        fields += _watch_number(key, label, value, suffix, WEDGE_HINTS[key])
    return fields


def _watch_test_and_save() -> str:
    """Settings > Watcher > Test the connection (the page's one Save settings sits under the form)."""
    return (
        '<div class="ab-acct-title">Test the connection</div>'
        '<div class="ab-acct-who">Saves what you typed, then makes one request with it. Nothing about the test '
        "is kept. Test tracker runs one real search for the title in the box; the qBittorrent test only reads "
        "(its version, its categories and where the category saves) and adds nothing.</div>"
        + _watch_wrap_plain(
            "Search the tracker for",
            f'<input type="text" class="ab-in" id="ab-watch-test-query" maxlength="{watcher_mod.MAX_TEST_QUERY}" '
            f'value="{_attr(watcher_mod.DEFAULT_TEST_QUERY)}" autocomplete="off" spellcheck="false" '
            'aria-label="Title to search the tracker for">')
        + '<div class="ab-acct-row">'
        '<button class="btn" type="button" id="ab-watch-test-qbit" onclick="abWatchTest(\'qbit\')">'
        f"Test {_html(watcher_mod.client_label())}</button>"
        '<button class="btn" type="button" id="ab-watch-test-tracker" onclick="abWatchTest(\'tracker\')">Test tracker</button>'
        '<span class="ab-fb" id="ab-watch-test-fb" role="status"></span></div>'
        '<div class="ab-test-detail" id="ab-watch-test-fb-detail" hidden></div>'
    )


def _watch_wrap_plain(label: str, control: str) -> str:
    """A labelled control that is not a saved setting (no data-setting: the Save button never reads it)."""
    return f'<div class="ab-setting"><span class="ab-setting-label">{_html(label)}</span>{control}</div>'


def _egress_pin_field() -> str:
    """Settings > Watcher: the allowed-IP pin, beside the Guard row that refers to it."""
    return _watch_text("expected_egress", "Only reach the tracker from one IP", "e.g. 198.51.100.7") + _check_ip_block()


CHECK_IP_NOTE = (
    "Which address would the tracker see from here, right now? Works with the guard off or armed. It asks an "
    "address-lookup service (through the tracker proxy, if you set one) and never contacts the tracker. A tracker "
    "that ties your session to an address needs the session created for exactly this address.")


def _check_ip_block() -> str:
    """Settings > Watcher: the Check IP button, the address in a box that can be copied, and the guard's verdict."""
    return (
        '<div class="ab-setting ab-checkip">'
        f'<div class="ab-setting-hint">{_html(CHECK_IP_NOTE)}</div>'
        '<div class="ab-acct-row"><button class="btn" type="button" id="ab-checkip" onclick="abCheckIp()">'
        'Check IP</button><span class="ab-fb" id="ab-checkip-fb" role="status"></span></div>'
        '<div class="ab-checkip-ip" id="ab-checkip-row" hidden>'
        '<input type="text" class="ab-in" id="ab-checkip-ip" readonly aria-label="The address the tracker would see" '
        'onclick="this.select()">'
        '<button class="btn" type="button" id="ab-checkip-copy" onclick="abCopyIp()">Copy</button></div></div>'
    )


def render_watcher_section(state) -> str:
    """Settings > Watcher: status, the connection settings, pacing, connection tests, Run / Pause."""
    s = watcher_mod.WATCHER.status(state)
    guide_link = (
        '<div class="ab-acct-who ab-setup-guide">'
        f'<a href="{wizard_mod.PATH}">View the setup guide</a></div>'
    )
    warn = ""
    if not s["configured"]:
        # QBIT_URL has no default: while it is empty it is one of the things still to set
        still = list(s["missing"])
        if watcher_mod.client_kind() == "qbittorrent" and not watcher_mod.qbit_url():
            still.append("QBIT_URL")
        return (
            '<section class="ab-acct ab-watch" id="watcher" aria-labelledby="ab-watch-head">'
            '<h2 class="ab-acct-head" id="ab-watch-head">Acquisition source (optional)</h2>'
            '<div class="ab-acct-who">Audiobind can optionally look for books on your wishlist at a '
            "private tracker you configure and hand them to qBittorrent. It is off, and you do not need it: "
            "Audiobind converts whatever lands in the ingest folder, from any downloader or a plain "
            "copy. Still to set: " + ", ".join(f"<code>{_html(m)}</code>" for m in still) +
            " (the site's URL, its search and download paths, the search request body, and its session cookie's name and value), "
            "plus the qBittorrent URL, user and password (<code>QBIT_URL</code>, <code>QBIT_USER</code>, "
            "<code>QBIT_PASS</code>). Fill them in below and save - no restart needed - or set them as container "
            "variables. A Wishlist tab appears in the sidebar once these settings are complete.</div>"
            + _wizard_open_button()
            + guide_link
            + _watch_connection_fields()
            + _watch_test_and_save()
            + _egress_pin_field()
            + _ip_updater_section(s)
            + "</section>"
        )
    if not s["enabled"]:
        state_html = '<span class="warn">Disabled</span>'
        warn = (
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>The watcher is disabled.</b> '
            f"{_html(s['reason'])}. "
            + (("Fill in the qBittorrent URL below (or QBIT_URL in the container) and save."
                if not s["qbit_url"] else
                "Set the qBittorrent user and password below (or QBIT_USER and QBIT_PASS in the container) and save.")
               if s.get("download_client", "qbittorrent") == "qbittorrent" else
               "Fill in the download client's settings below (or in the container) and save.")
            + "</div>"
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
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>Stopped: the tracker would not see the allowed address.</b> '
            f"{_html(s['egress_reason'])}. The watcher will not contact the tracker from here. Either update "
            '"Only reach the tracker from one IP" to the address you really want, or start your VPN '
            "container (or fix TRACKER_PROXY); the next cycle will carry on.</div>")
    elif guard == "unknown" and s.get("egress_checked_at"):
        warn += (
            '<div class="ab-hintbox ab-watch-warn" role="alert"><b>Stopped: the guard could not check your connection.</b> '
            f"{_html(s['egress_reason'])}. Check that your VPN container is running and TRACKER_PROXY is right; "
            "the watcher will not contact the tracker until it can see where it is connecting from.</div>")
    verdict_html = {"ok": '<span class="ok">allowed</span>', "blocked": '<span class="warn">blocked</span>',
                    "unknown": '<span class="warn">unverified</span>'}.get(guard, "")
    if s["egress_ip"]:
        sees = f"The tracker sees <b>{_html(s['egress_ip'])}</b>"
        if s.get("egress_expected"):
            sees += f" &#183; expected <b>{_html(s['egress_expected'])}</b>"
        if verdict_html:
            sees += f" &#183; {verdict_html}"
        if s.get("egress_checked_at"):
            sees += f" &#183; Checked {_html(time.strftime('%H:%M', time.localtime(float(s['egress_checked_at']))))}"
    elif guard == "off":
        sees = "Not checked &#8212; the guard is off, so Audiobind does not look up what the tracker sees"
    else:
        sees = "Not checked yet &#8212; it is looked up at the start of each cycle"
        if s.get("egress_expected"):
            sees += f" &#183; expected <b>{_html(s['egress_expected'])}</b>"
    guard_bits = []
    if s.get("egress_expected"):
        guard_bits.append(f"only reaches the tracker from {_html(s['egress_expected'])}")
    if s.get("egress_home_ip"):
        guard_bits.append(f"refuses to run from your home IP ({_html(s['egress_home_ip'])})")
    if guard_bits:
        guard_html = " and ".join(guard_bits)
        if guard == "blocked":
            guard_html = f'<span class="warn">{guard_html} &#8212; blocking now</span>'
    else:
        guard_html = "not armed (set the allowed IP, or TRACKER_HOME_IP, to arm it)"
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
        ("Download limit", (f"adds nothing while {int(s['max_incomplete'])} torrent(s) are still downloading"
                            if s["max_incomplete"] else "off")),
        ("Tracker", f"{_html(s['tracker_host'])}" if s.get("tracker_host") else DASH),
        (("qBittorrent", (_html(urlsplit(s['qbit_url']).netloc or s['qbit_url']) if s['qbit_url']
                          else '<span class="warn">no address set</span>') + f" &#183; category <code>{_html(s['qbit_category'])}</code> &#183; "
                         + ("login set" if s["qbit_credentials"] else '<span class="warn">no login set</span>'))
         if s.get("download_client", "qbittorrent") == "qbittorrent" else
         (_html(s["client_label"]), (f"{_html(urlsplit(s['client_url']).netloc or s['client_url'])}" if s["client_url"]
                                     else '<span class="warn">no address set</span>')
                                    + f" &#183; label <code>{_html(s['qbit_category'])}</code>")),
        ("Connection", sees),
        ("Guard", guard_html),
        ("Last cycle", last_html),
        ("Next cycle", next_html),
        ("Last error", error_html),
    ]
    if s["backoff_seconds"]:
        rows.append(("Tracker backoff", f'<span class="warn">no tracker request for {int(s["backoff_seconds"])}s</span>'))
    grid = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows)
    ready = ""
    if s["enabled"] and not s["paused"]:
        nxt = (f" Next check {_html(time.strftime('%H:%M', time.localtime(float(s['next_cycle_at']))))}."
               if s["next_cycle_at"] else "")
        ready = ('<div class="ab-acct-who" id="ab-watch-ready"><b>Ready.</b> Add a book: Discover books '
                 f"&#8594; Want.{nxt}</div>")
    can_run = s["enabled"] and not s["paused"] and not s["busy"]
    run_btn = (f'<button class="btn" type="button" id="ab-watch-run" onclick="abWatcher(\'run\')"{"" if can_run else " disabled"} '
               'title="Run one cycle now in the background (same caps and pacing as a timed cycle).">'
               "Run a cycle now</button>")
    toggle = ""
    if s["configured"]:
        action, label = ("resume", "Resume watcher") if s["paused"] else ("pause", "Pause watcher")
        toggle = f'<button class="btn" type="button" id="ab-watch-toggle" onclick="abWatcher(\'{action}\')">{label}</button>'
    wdry = bool(s["watcher_dry_run"])
    dry_toggle = (
        f'<button class="btn dry{" on" if wdry else ""}" id="ab-watch-dry" type="button" aria-pressed="{"true" if wdry else "false"}" '
        f'title="Searches and reports what it would download, never downloads. Click to turn it {"off" if wdry else "on"}." '
        f'onclick="abWatcherDry({"false" if wdry else "true"})">Dry run <span class="ab-switch"></span></button>'
    )
    dry_line = (
        '<div class="ab-acct-who" id="ab-watch-dry-note">Watcher dry run is <b>' + ("on" if wdry else "off") +
        "</b>: searches the tracker and reports what it would download, never downloads. It is separate from "
        "the library Dry run on the main page.</div>"
    )
    return (
        '<section class="ab-acct ab-watch" id="watcher" aria-labelledby="ab-watch-head">'
        '<h2 class="ab-acct-head" id="ab-watch-head">Watcher</h2>'
        '<div class="ab-acct-who">Searches the tracker for the <b>wanted</b> books on your wishlist and hands a good '
        "match to qBittorrent; the ingest folder takes it from there. Everything below can be set here or as a "
        "container variable; a value saved here wins and applies from the next cycle, no restart. The pause switch "
        "is kept across restarts.</div>"
        f'{guide_link}{warn}{ready}<dl class="ab-watch-grid">{grid}</dl>'
        + _egress_pin_field()
        + _watch_connection_fields()
        + _watch_schedule_fields()
        + _watch_test_and_save()
        + _ip_updater_section(s)
        + f'<div class="ab-acct-row ab-watch-actions">{run_btn}{toggle}{dry_toggle}'
        + '<span class="ab-fb" id="ab-watch-fb" role="status"></span></div>'
        + dry_line
        + "</section>"
    )


#: Settings > Watcher > Allowed-IP updater: one plain sentence per mode, shown under the select
IP_UPDATER_MODE_HINTS = {
    "off": ("Off (default): nothing automatic. If your VPN's address changes and the IP guard is armed, the "
            "watcher stops and tells you."),
    "notify": "Shows and logs when the IP has drifted; never writes to the tracker.",
    "auto": ("MAM only: when your VPN's address changes, tells MAM the new one. Needs 'Allow session to "
             "set dynamic seedbox IP' ticked on MAM's Preferences \u2192 Security page, and TRACKER_HOME_IP set. "
             "('Seedbox IP' is MAM's name for the address you connect from.)"),
}


def _ip_updater_section(s: dict) -> str:
    """Settings > Watcher > Allowed-IP updater, always rendered: the mode select, a sentence per mode,
    then the live drift / status rows - or, while the mode is off, one line saying so (the status
    carries no updater data then, so nothing was looked up to draw this)."""
    kind = watcher_mod.ip_updater_mode()
    options = "".join(f'<option value="{m}"{" selected" if m == kind else ""}>{m}</option>'
                      for m in IP_UPDATER_MODES)
    modes = "".join(f"<li><b>{m}</b> &#8212; {_html(IP_UPDATER_MODE_HINTS[m])}</li>" for m in IP_UPDATER_MODES)
    select = _watch_wrap(
        "ip_updater", "Allowed-IP updater", "",
        f'<select class="ab-in" data-initial="{_attr(kind)}">{options}</select>'
        f'<ul class="ab-setting-hint ab-ipupd-modes">{modes}</ul>')
    rows = _ip_updater_rows(s)
    if rows:
        body = '<dl class="ab-watch-grid ab-ipupd-grid">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>"
    elif kind != "off":
        body = '<div class="ab-acct-who">Its drift and status show here once the watcher is set up.</div>'
    else:
        body = ('<div class="ab-acct-who" id="ab-ipupd-off">The allowed-IP updater is <b>off</b>. Switched on, it '
                "would watch for the IP the tracker sees drifting away from the allowed IP, then tell you "
                "(<code>notify</code>) or update the tracker's dynamic seedbox IP for you (<code>auto</code>).</div>")
    return '<div class="ab-acct-title">Allowed-IP updater</div>' + select + body


def _ip_updater_rows(s: dict) -> list[tuple[str, str]]:
    """Settings > Watcher: the allowed-IP updater's rows. None at all while IP_UPDATER=off."""
    upd = s.get("ip_updater")
    if not upd:
        return []
    mode = str(upd.get("mode") or "off")
    mode_html = f"<code>IP_UPDATER={_html(mode)}</code>"
    can_write = bool(upd.get("can_write"))
    if mode == "auto" and not can_write:
        mode_html += (" &#8212; acting as <code>notify</code>: no MyAnonamouse session is configured, "
                      "so Audiobind never changes the allowed IP on the tracker")
    elif mode == "auto":
        mode_html += " &#8212; a drift is set on MyAnonamouse automatically, then verified"
    elif can_write:
        mode_html += " &#8212; read-only: Audiobind only changes MyAnonamouse when you press the button"
    decision = str(upd.get("decision") or "")
    label = {"update": '<span class="warn">drifted &#8212; update needed</span>', "noop": '<span class="ok">in step</span>',
             "refuse": '<span class="warn">no change</span>'}.get(decision, DASH)
    verdict_html = f"{label} &#183; {_html(str(upd.get('reason') or ''))}"
    if upd.get("last_updated_at"):
        last = (f"{_html(rel_age(upd['last_updated_at']))} &#183; {_html(upd.get('last_old') or DASH)} &#8594; "
                f"{_html(upd.get('last_new') or DASH)}")
    else:
        last = "never"
    if can_write:
        title = ("Ask MyAnonamouse which IP it sees, set it as the session's dynamic seedbox IP, check that MAM now "
                 "sees it, then make it the address this installation only reaches the tracker from.")
    else:
        title = ("After you have changed the allowed IP on the tracker's website: check the egress IP again and make it "
                 "the address this installation only reaches the tracker from. Nothing is sent to the tracker.")
    button = (
        '<button class="btn" type="button" id="ab-watch-useip" onclick="abWatcher(\'use_current_ip\')" '
        f'title="{_html(title)}">Use the current IP</button>'
    )
    rows = [
        ("Allowed-IP updater", mode_html),
        ("Allowed IP", f"{_html(s.get('egress_expected') or 'not pinned')} &#183; {verdict_html}"),
        ("Last updated", f"{last} {button}"),
    ]
    result = upd.get("last_result") or {}
    if result:
        when = _html(rel_age(result["at"])) if result.get("at") else DASH
        if result.get("ok"):
            text = f'<span class="ok">set on MyAnonamouse</span> &#183; {_html(result.get("ip") or DASH)} (verified)'
        else:
            text = (f'<span class="warn">failed ({_html(result.get("kind") or "error")})</span> &#183; '
                    f'{_html(result.get("message") or "")}')
        rows.append(("Last MAM result", f"{when} &#183; {text}"))
    return rows


ABS_LAYOUT_WARNING = (
    "This moves and renames folders inside /output. Audiobookshelf may treat a renamed folder as "
    "a new book and lose its listening progress."
)


def render_library_section() -> str:
    """Settings > Library: "Match Audiobookshelf layout" - preview, then apply behind a confirm."""
    return (
        '<section class="ab-acct" id="library" aria-labelledby="ab-lib-head">'
        '<h2 class="ab-acct-head" id="ab-lib-head">Library</h2>'
        '<div class="ab-acct-title">Match Audiobookshelf layout</div>'
        '<div class="ab-acct-who">New conversions are filed the way Audiobookshelf expects: '
        "<code>Author/Series/1 - Title/</code> or <code>Author/Title/</code>, with several authors "
        "joined by <code>&amp;</code>. Books converted before that keep their old folders until you "
        "move them here. Preview first: nothing changes until you press Apply. Books whose new folder "
        "already exists, or whose folder holds another book's audio, are skipped. Every move is recorded "
        "in an <code>abs-layout-&lt;time&gt;.json</code> journal beside the database.</div>"
        f'<div class="ab-hintbox ab-watch-warn" role="alert"><b>Heads up.</b> {_html(ABS_LAYOUT_WARNING)}</div>'
        '<div class="ab-acct-row">'
        '<button class="btn" type="button" id="ab-abs-preview" onclick="abAbsPreview()">Preview</button>'
        '<button class="btn" type="button" id="ab-abs-apply" onclick="abAbsAsk()">Apply</button>'
        '<span class="ab-fb" id="ab-abs-fb" role="status"></span></div>'
        '<div id="ab-abs-plan" hidden></div>'
        + _hardcover_section()
        + "</section>"
    )


HARDCOVER_EVIDENCE_HINT = (
    "Off: Hardcover is never asked. On (with a token): after each book is matched and graded, Hardcover is "
    "asked whether it knows the book, and the book's detail panel gets one line saying what it lists for "
    "series and year and whether that agrees with the Audible catalogue. It never changes a grade or what "
    "converts. Answers are cached (two weeks; two days for a book it does not have). Set in the container as HARDCOVER_EVIDENCE."
)


def _hardcover_section() -> str:
    """Settings > Library > Hardcover evidence: the toggle, the token, Test connection."""
    return (
        '<div class="ab-acct-title">Hardcover evidence</div>'
        '<div class="ab-acct-who">A second opinion from Hardcover (hardcover.app), '
        'shown beside the Audible match. Evidence only: it is '
        "never the reason a book converts or is held.</div>"
        + _watch_select("hardcover_evidence", "Hardcover evidence", HARDCOVER_EVIDENCE_HINT, titlecase=True)
        + _watch_secret("hardcover_token", "Hardcover API token")
        + '<div class="ab-acct-row">'
        '<button class="btn" type="button" id="ab-hc-test" '
        "onclick=\"abWatchTest('hardcover', 'ab-hc-test-fb', '/api/hardcover/test')\">Test connection</button>"
        '<span class="ab-fb" id="ab-hc-test-fb" role="status"></span></div>'
    )


LIBRARY_SCRIPT = """
// ---- Settings > Library: Match Audiobookshelf layout (preview, then apply behind the confirm modal)
function abAbsRel(path, root) {
  path = String(path || '');
  if (root && path.indexOf(root.replace(/\\/+$/, '') + '/') === 0) return path.slice(root.replace(/\\/+$/, '').length + 1);
  return path;
}
function abAbsPost(body) {
  return fetch('/api/library/abs-layout', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify(body)})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(res => ({ok: r.ok, res: res}));
    });
}
function abAbsLine(cls, from, to, note) {
  const row = document.createElement('div');
  row.className = 'ab-abs-line ' + cls;
  const a = document.createElement('code'); a.textContent = from; row.appendChild(a);
  if (to) {
    row.appendChild(document.createTextNode(' \\u2192 '));
    const b = document.createElement('code'); b.textContent = to; row.appendChild(b);
  }
  if (note) {
    const n = document.createElement('span'); n.className = 'ab-abs-why'; n.textContent = note; row.appendChild(n);
  }
  return row;
}
function abAbsRender(res) {
  const box = document.getElementById('ab-abs-plan');
  const root = res.output_root || '';
  box.textContent = '';
  const moves = res.moves || res.moved || [];
  const failed = res.failed || [];
  const skipped = res.skipped || [];
  const panel = document.createElement('section');
  panel.className = 'ab-disc-panel ab-abs-panel';
  const head = document.createElement('h3');
  const verb = res.dry_run ? 'would be renamed' : 'renamed';
  head.textContent = moves.length + ' book(s) ' + verb;
  const why = document.createElement('span');
  why.className = 'ab-disc-why';
  why.textContent = ' \\u00b7 ' + (res.unchanged || 0) + ' already in place \\u00b7 ' + skipped.length + ' skipped' +
                    (failed.length ? ' \\u00b7 ' + failed.length + ' failed' : '');
  head.appendChild(why);
  panel.appendChild(head);
  if (!moves.length && !failed.length && !skipped.length) {
    const empty = document.createElement('div');
    empty.className = 'ab-disc-empty';
    empty.textContent = 'Every converted book already matches the Audiobookshelf layout.';
    panel.appendChild(empty);
  }
  moves.forEach(m => panel.appendChild(abAbsLine('', abAbsRel(m.from, root), abAbsRel(m.to, root), '')));
  if (failed.length) {
    const h = document.createElement('h4'); h.textContent = 'Failed'; panel.appendChild(h);
    failed.forEach(f => panel.appendChild(abAbsLine('ab-abs-bad', abAbsRel(f.from, root), abAbsRel(f.to, root), f.error)));
  }
  if (skipped.length) {
    const h = document.createElement('h4'); h.textContent = 'Skipped'; panel.appendChild(h);
    skipped.forEach(s => panel.appendChild(abAbsLine('ab-abs-skip', abAbsRel(s.from, root), '', s.reason)));
  }
  if (res.journal) {
    const j = document.createElement('div');
    j.className = 'ab-acct-who';
    j.textContent = 'Undo journal: ' + res.journal;
    panel.appendChild(j);
  }
  box.appendChild(panel);
  box.hidden = false;
}
function abAbsPreview() {
  const fb = document.getElementById('ab-abs-fb');
  const btn = document.getElementById('ab-abs-preview');
  fb.className = 'ab-fb';
  fb.textContent = 'working out the plan\\u2026';
  btn.disabled = true;
  abAbsPost({dry_run: true}).then(out => {
    btn.disabled = false;
    if (!out.ok) { fb.className = 'ab-fb ab-bad'; fb.textContent = out.res.error || 'that did not work'; return; }
    fb.textContent = 'preview only - nothing was changed';
    abAbsRender(out.res);
  }).catch(err => {
    btn.disabled = false;
    if (String(err && err.message) === 'signed out') return;
    fb.className = 'ab-fb ab-bad';
    fb.textContent = 'could not reach the daemon';
  });
}
let abAbsTrigger = null;
function abAbsAsk() {
  abAbsTrigger = document.getElementById('ab-abs-apply');
  document.getElementById('ab-modal-title').textContent = 'Match the Audiobookshelf layout?';
  document.getElementById('ab-modal-warning').textContent = """ + json.dumps(ABS_LAYOUT_WARNING) + """ +
    ' Books whose new folder already exists are skipped, never overwritten. Run Preview first to see every move.';
  document.getElementById('ab-modal-confirm').textContent = 'Move and rename';
  document.getElementById('ab-modal').hidden = false;
  document.getElementById('ab-modal-cancel').focus();
}
function abModalCancel() {
  document.getElementById('ab-modal').hidden = true;
  if (abAbsTrigger) abAbsTrigger.focus();
}
function abModalConfirm() {
  const btn = document.getElementById('ab-modal-confirm');
  const fb = document.getElementById('ab-abs-fb');
  const apply = document.getElementById('ab-abs-apply');
  btn.disabled = true;
  apply.disabled = true;
  btn.textContent = 'moving\\u2026';
  fb.className = 'ab-fb';
  fb.textContent = 'moving books\\u2026';
  const done = () => { btn.disabled = false; apply.disabled = false; document.getElementById('ab-modal').hidden = true; };
  abAbsPost({dry_run: false, confirm: true}).then(out => {
    done();
    if (!out.ok && !out.res.dry_run && out.res.renamed === undefined) {
      fb.className = 'ab-fb ab-bad';
      fb.textContent = out.res.error || 'that did not work';
      abToastSettings(out.res.error || 'The layout change did not run.');
      return;
    }
    const n = out.res.renamed || 0, bad = out.res.failed_count || 0;
    if (bad) {
      fb.className = 'ab-fb ab-bad';
      fb.textContent = n + ' renamed, ' + bad + ' failed';
      abToastSettings('Renamed ' + n + ' book(s); ' + bad + ' could not be moved - see the list below.');
    } else {
      fb.className = 'ab-fb ab-good';
      fb.textContent = n + ' renamed';
      abToastSettings('Renamed ' + n + ' book(s) to the Audiobookshelf layout.');
    }
    abAbsRender(out.res);
  }).catch(err => {
    done();
    if (String(err && err.message) === 'signed out') return;
    fb.className = 'ab-fb ab-bad';
    fb.textContent = 'could not reach the daemon';
    abToastSettings('Could not reach the daemon - nothing is known to have moved. Preview again to check.');
  });
}
window.addEventListener('keydown', function(e) {
  const modal = document.getElementById('ab-modal');
  if (e.key === 'Escape' && modal && !modal.hidden) abModalCancel();
});
"""


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
// ---- Settings > Watcher: "Reset to container value" for one setting
function abResetSetting(key) {
  const fb = document.getElementById('ab-settings-fb');
  fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({reset_keys: [key]})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      if (res.ok) { location.reload(); return; }
      if (fb) { fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'could not reset'; }
    })
    .catch(() => { if (fb) { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach the daemon'; } });
}
// ---- Settings > Watcher: Test qBittorrent / Test tracker - save what is typed, then one request
// ---- Settings > Watcher > Download client: show only the chosen client's fields
function abDownloadClient(kind) {
  document.querySelectorAll('.ab-dc[data-client]').forEach(g => { g.hidden = g.dataset.client !== kind; });
}
function abShowDetail(fbId, body) {
  const box = document.getElementById(fbId + '-detail');
  if (!box) return;
  box.textContent = '';
  const lines = (body.details || []).slice();
  if (!lines.length) { box.hidden = true; return; }
  lines.forEach(t => { const p = document.createElement('p'); p.textContent = t; box.appendChild(p); });
  if (body.rule) {
    const p = document.createElement('p'); p.className = 'ab-rule'; p.textContent = body.rule; box.appendChild(p);
  }
  box.hidden = false;
}
function abWatchTest(target, fbId, url) {
  fbId = fbId || 'ab-watch-test-fb';
  url = url || ('/api/watcher/test-' + target);
  const fb = document.getElementById(fbId);
  const detail = document.getElementById(fbId + '-detail');
  if (detail) { detail.hidden = true; detail.textContent = ''; }
  let payload = {};
  if (target === 'tracker') {
    const q = document.getElementById('ab-watch-test-query');
    if (q) payload = {query: q.value};
  }
  fb.className = 'ab-fb';
  fb.textContent = 'saving, then testing...';
  abSaveSettings(fbId, true).then(saved => {
    if (!saved) return;
    fb.className = 'ab-fb';
    fb.textContent = 'testing...';
    return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)})
      .then(r => {
        if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
        return r.json().then(body => ({ok: r.ok && body.ok, body: body}));
      })
      .then(res => {
        // a connected qBittorrent whose folders are not proven right is amber, a proven problem is red
        const chain = res.body.chain || '';
        fb.className = 'ab-fb ' + (!res.ok ? 'ab-bad' : chain === 'problem' ? 'ab-bad' : chain === 'unknown' ? 'ab-warn' : 'ab-good');
        fb.textContent = res.body.message || res.body.error || 'no answer';
        abShowDetail(fbId, res.body);
      });
  }).catch(err => {
    if (String(err && err.message) === 'signed out') return;
    fb.className = 'ab-fb ab-bad';
    fb.textContent = 'could not reach the daemon';
  });
}
// ---- Settings > Watcher: fill the tracker fields from the preset file (the server checks the whole file first)
function abPresetCancel() {
  document.getElementById('ab-preset-confirm').hidden = true;
  document.getElementById('ab-preset-fb').textContent = '';
}
function abPreset(overwrite) {
  const fb = document.getElementById('ab-preset-fb');
  const confirmBox = document.getElementById('ab-preset-confirm');
  fb.className = 'ab-fb';
  fb.textContent = 'filling...';
  fetch('/api/watcher/preset', {method:'POST', headers:{'Content-Type':'application/json'},
                                body: JSON.stringify({overwrite: overwrite === true})})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json().then(body => ({status: r.status, ok: r.ok, body: body}));
    })
    .then(res => {
      if (res.status === 409 && res.body.needs_confirm) {
        fb.textContent = '';
        document.getElementById('ab-preset-confirm-text').textContent =
          'This replaces what is set now in: ' + (res.body.replace || []).join(', ') + '. Nothing is saved until you say so.';
        confirmBox.hidden = false;
        return;
      }
      confirmBox.hidden = true;
      if (!res.ok) { fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'that did not work'; return; }
      const n = (res.body.filled || []).length + (res.body.replaced || []).length;
      fb.className = 'ab-fb ab-good';
      fb.textContent = 'filled ' + n + ' field' + (n === 1 ? '' : 's') + ' from "' + res.body.label + '" - now paste your session cookie value';
      setTimeout(() => { location.reload(); }, 1200);
    })
    .catch(err => {
      if (String(err && err.message) === 'signed out') return;
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
    });
}
// ---- Settings > Watcher: Check IP - which address would the tracker see (never contacts the tracker)
function abCheckIp() {
  const fb = document.getElementById('ab-checkip-fb');
  const row = document.getElementById('ab-checkip-row');
  const box = document.getElementById('ab-checkip-ip');
  fb.className = 'ab-fb';
  fb.textContent = 'checking...';
  row.hidden = true;
  fetch('/api/watcher/check-ip', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'})
    .then(r => {
      if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
      return r.json();
    })
    .then(body => {
      fb.className = 'ab-fb ' + (body.ok && body.guard !== 'blocked' ? 'ab-good' : 'ab-bad');
      fb.textContent = body.message || body.error || 'no answer';
      if (body.ip) { box.value = body.ip; row.hidden = false; box.focus(); box.select(); }
    })
    .catch(err => {
      if (String(err && err.message) === 'signed out') return;
      fb.className = 'ab-fb ab-bad';
      fb.textContent = 'could not reach the daemon';
    });
}
function abCopyIp() {
  const box = document.getElementById('ab-checkip-ip');
  const fb = document.getElementById('ab-checkip-fb');
  box.focus();
  box.select();
  const done = () => { fb.className = 'ab-fb ab-good'; fb.textContent = 'copied ' + box.value; };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(box.value).then(done, () => { if (document.execCommand('copy')) done(); });
  } else if (document.execCommand('copy')) {
    done();
  }
}
"""


def render_discover(state, tab: str = "search", title: str = "", author: str = "", result: dict | None = None) -> str:
    """Discover books: the search form with the recommendations under it (one screen). A typed
    query runs one catalog search and its results replace the recommendations. tab="wishlist"
    renders the separate Wishlist page (the library page's rail, no watcher: falls back to search)."""
    if tab == "wishlist" and not watcher_mod.configured():
        tab = "search"  # no wishlist surface without the watcher: land on Discover books
    if tab == "wishlist":
        body = _discover_wishlist_body(state)
        page_title, active = "Wishlist", "wishlist"
    else:
        page_title, active = "Discover books", "discover"
        if result is None and (title or author):
            result = discover_mod.search(state, title, author)
        if result is not None and (result.get("title") or result.get("author") or title or author):
            body = _discover_search_body(result)
        else:
            body = _discover_search_body({"title": "", "author": ""}) + _discover_recs_body(state)
    return _discover_shell(state, page_title, body, DISCOVER_SCRIPT, active)


def _discover_shell(state, page_title: str, body: str, script: str, active: str = "") -> str:
    """The rail, the top bar and one content column (the Discover books page's frame; the Set up downloading
    wizard uses it too). ``body`` and ``script`` go in as they are."""
    counts = state.counts()
    rc = rail_counts(state, counts)
    return DISCOVER_PAGE.format(
        **_rail_view_marks(None),
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        page_title=page_title,
        rail_total=sum(counts.values()),
        rail_attention_count=rc["attention"],
        rail_attention_alert=_alert_class(rc["attention"]),
        series_item=_series_rail_item(_series_shelf(state, refresh=False)["missing_total"]),
        discover_item=_discover_rail_item(state, active=active),
        library=_breakable_path(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_breakable_path(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        disc_body=body,
        nav_script=NAV_SCRIPT,
        discover_script=script,
    )


# ---------------------------------------------------------------------------- Set up downloading
def _wizard_open_button() -> str:
    """Settings > Watcher, while the watcher is not set up: the way into the guided setup, above the fields."""
    return (
        '<div class="ab-acct-row ab-wiz-open">'
        f'<a class="btn ab-save" id="ab-wiz-open" href="{wizard_mod.PATH}">Set up downloading</a>'
        '<span class="ab-acct-who">A short guide, one question at a time. It fills in the same fields as below.</span>'
        "</div>"
    )


def _wizard_card(state) -> str:
    """The Library page's one-time, dismissible card. Not shown once it was answered, nor while a tracker is
    already set up. "No, just my library" stores the answer: the card never returns."""
    if not wizard_mod.card_visible(state, watcher_mod.configured()):
        return ""
    lead = "Your library is being scanned." if CONTROL.scanning else "Your library is set up."
    return (
        '<section class="ab-wizcard" id="ab-wizcard" aria-labelledby="ab-wizcard-head">'
        '<div class="ab-wizcard-text"><b id="ab-wizcard-head">Fetch books too?</b> '
        f"{lead} Do you also want Audiobind to fetch books for you?</div>"
        '<div class="ab-acct-row">'
        f'<a class="btn ab-save" id="ab-wizcard-yes" href="{wizard_mod.PATH}">Set up downloading</a>'
        '<button class="btn" type="button" id="ab-wizcard-no" onclick="abWizardDismiss()">No, just my library</button>'
        '<span class="ab-fb" id="ab-wizcard-fb" role="status"></span></div></section>'
    )


def _wizard_indicator(step: int, mode: str) -> str:
    """1 How books arrive · 2 Download client · 3 The address the tracker sees · 4 Your tracker · Done.
    The one shown is marked. Once the person chose "a private tracker", every step and Done is a link; before
    that only step 1 exists for them (``resolve_step`` sends anything else back to it), so the rest are
    listed and never links."""
    reachable = mode == wizard_mod.MODE_TRACKER
    items = []
    for number, name in list(wizard_mod.STEPS) + [(wizard_mod.DONE, "Done")]:
        glyph = "&#10003;" if number == wizard_mod.DONE else str(number)
        label = f'<span class="ab-wiz-n" aria-hidden="true">{glyph}</span> {_html(name)}'
        target = "done" if number == wizard_mod.DONE else str(number)
        if number == step:
            items.append(f'<li class="ab-on" aria-current="step">{label}</li>')
        elif number == 1 or (reachable and (number in wizard_mod.BUILT or number == wizard_mod.DONE)):
            klass = "ab-done" if number < step else "ab-ahead"
            items.append(f'<li class="{klass}"><a href="{wizard_mod.PATH}?step={target}">{label}</a></li>')
        else:
            items.append(f'<li class="ab-later" aria-disabled="true">{label}</li>')
    return f'<nav aria-label="Set up downloading: the steps"><ol class="ab-wiz-steps">{"".join(items)}</ol></nav>'


#: Step 1's three answers: (value stored, what the person reads, one line under it)
WIZARD_ARRIVALS = (
    (wizard_mod.MODE_LIBRARY, "Only my library: nothing to fetch",
     "Audiobind converts the books that are already in your library. Nothing is downloaded."),
    (wizard_mod.MODE_FILES, "My own downloader, or I copy files into a folder",
     "Audiobind watches one folder and converts what shows up in it, whatever put it there."),
    (wizard_mod.MODE_TRACKER, "A private tracker, through a download client",
     "Audiobind looks for the books on your wishlist at your tracker and hands them to your download client."),
)

#: where the person is told to point the ingest folder (the Unraid click path, word for word)
WIZARD_INGEST_MISSING = (
    "In Unraid: Docker \u2192 Audiobind \u2192 Edit \u2192 Show more settings \u2192 <b>Ingest path</b> \u2192 pick "
    "the folder your downloader finishes audiobooks into \u2192 Apply.")


def _wizard_check(ok: bool | None, text: str) -> str:
    """One line of a status list: a glyph AND words, so the state never rests on colour alone."""
    glyph, klass, word = {True: ("&#10003;", "ab-good", "OK"), False: ("&#10007;", "ab-bad", "Problem"),
                          None: ("&#9679;", "ab-warn", "Note")}[ok]
    return (f'<li class="{klass}"><span class="ab-wiz-glyph" aria-hidden="true">{glyph}</span>'
            f'<span class="ab-wiz-sr">{word}: </span>{text}</li>')


def _wizard_ingest_status() -> str:
    """Step 1, for "my own downloader" and "a private tracker": is the ingest folder there? Reads what ingest
    computes (mounted, readable, no overlap with the library or output folders, how many folders it holds)."""
    st = ingest_mod.status()
    lines = []
    if not st["configured"]:
        lines.append(_wizard_check(False, "Audiobind has no ingest folder yet, so it has nowhere to look for books."))
        if st["half"]:
            lines.append(_wizard_check(None, _html(st["half"])))
        note = (f'<p class="ab-wiz-fix">{WIZARD_INGEST_MISSING}</p>'
                "<p>Applying restarts the container. Open this page again afterwards: it comes back to this step.</p>")
    else:
        where = f"<code>{_html(st['dir'])}</code>"
        lines.append(_wizard_check(st["mounted"], f"The folder {where} is mounted." if st["mounted"] else
                                   f"There is no folder {where} inside the container: the mount is missing."))
        if st["mounted"]:
            lines.append(_wizard_check(st["readable"], "Audiobind can read it." if st["readable"] else
                                       "Audiobind cannot read it. Check the folder's permissions on the server."))
            if st["overlap"]:
                lines.append(_wizard_check(False, "It overlaps your library or output folder, so ingest is switched "
                                           "off: published books would be picked up again. Pick a different folder. "
                                           f"<small>({_html(st['overlap'])})</small>"))
            else:
                lines.append(_wizard_check(True, "It does not overlap your library or output folder."))
            if st["folders"] is not None:
                n = st["folders"]
                lines.append(_wizard_check(True, f"{n} {'folder' if n == 1 else 'folders'} seen"
                                           + ("." if n else " yet. That is fine: books show up here when your "
                                              "downloader finishes them.")))
        if st["ok"] and not st["running"]:
            lines.append(_wizard_check(None, "Audiobind checked this folder when it started and switched ingest off "
                                       "for this run. Restart the container to turn it on."))
        note = "" if st["ok"] else (f'<p class="ab-wiz-fix">{WIZARD_INGEST_MISSING}</p>'
                                    "<p>Applying restarts the container. Open this page again afterwards: it comes "
                                    "back to this step.</p>")
    return ('<div class="ab-hintbox ab-wiz-ingest" id="ab-wiz-ingest"><b>The ingest folder</b>'
            f'<ul class="ab-wiz-checks">{"".join(lines)}</ul>{note}</div>')


def _wizard_step1(state, mode: str) -> str:
    """Step 1: how do books arrive? Three answers. "Only my library" closes the flow; "my own downloader" shows the
    ingest folder and finishes; only "a private tracker" goes on to step 2."""
    choices = "".join(
        f'<label class="ab-wiz-choice"><input type="radio" name="ab-arrive" value="{value}"'
        f'{" checked" if value == mode else ""}>'
        f'<span><b>{_html(label)}</b><span class="ab-wiz-sub">{_html(sub)}</span></span></label>'
        for value, label, sub in WIZARD_ARRIVALS
    )
    if mode == wizard_mod.MODE_LIBRARY:
        outcome = ('<div class="ab-hintbox" role="status"><b>Nothing to fetch.</b> Audiobind will convert what is '
                   "already in your library.</div>"
                   '<div class="ab-acct-row"><a class="btn ab-save" id="ab-wiz-done" href="/?status=all">'
                   "Back to the Library</a></div>")
    elif mode == wizard_mod.MODE_FILES:
        outcome = (_wizard_ingest_status()
                   + '<div class="ab-hintbox" role="status"><b>That is all.</b> Audiobind converts every book that '
                   "appears in the ingest folder, whatever put it there. You will not see the download steps.</div>"
                   '<div class="ab-acct-row"><a class="btn ab-save" id="ab-wiz-done" href="/?status=all">'
                   "Back to the Library</a></div>")
    elif mode == wizard_mod.MODE_TRACKER:
        outcome = (_wizard_ingest_status()
                   + '<div class="ab-acct-row"><a class="btn ab-save" id="ab-wiz-next" '
                   f'href="{wizard_mod.PATH}?step=2">Next: the download client</a></div>')
    else:
        outcome = ""
    return (
        '<form class="ab-wiz-form" onsubmit="abWizardMode(); return false;" novalidate>'
        '<fieldset class="ab-wiz-choices"><legend class="ab-acct-who">Audiobind always converts the books in '
        "your library. How do new books arrive, if any?</legend>" + choices + "</fieldset>"
        '<div class="ab-acct-row"><button class="btn ab-save" type="submit" id="ab-wiz-continue">Continue</button>'
        '<span class="ab-fb" id="ab-wiz-fb" role="status"></span></div></form>'
        f'<div id="ab-wiz-outcome" tabindex="-1" class="ab-wiz-outcome">{outcome}</div>'
    )


def _wizard_field(field_id: str, label: str, control: str, hint: str = "", wrap_attrs: str = "") -> str:
    """One labelled box of step 2. The hint is tied to the control (aria-describedby) so it is read with it."""
    hint_html = f'<div class="ab-setting-hint" id="{field_id}-hint">{hint}</div>' if hint else ""
    return (f'<div class="ab-setting" id="{field_id}-wrap"{wrap_attrs}>'
            f'<label class="ab-setting-label" for="{field_id}">{_html(label)}</label>{hint_html}{control}</div>')


def _wizard_client_info(host: str) -> dict:
    """What step 2's boxes hold for each download client: the saved address, user and category, whether a password
    is saved (never the password), and the keys they are saved under - the Watcher tab's own. The qBittorrent
    address, when none is saved, is the Host the browser used with qBittorrent's usual port."""
    prefill = wizard_mod.default_client_url(host, lambda name: host_accepted(name))
    placeholders = {"qbittorrent": "http://your-server:8080", "deluge": "http://your-server:8112",
                    "transmission": "http://your-server:9091/transmission/rpc", "rtorrent": "scgi://your-server:5000"}
    info = {}
    for kind, label in DOWNLOAD_CLIENTS.items():
        keys = wizard_mod.CLIENT_KEYS[kind]
        url = str(setting_effective(keys["url"]) or "")
        prefilled = kind == "qbittorrent" and not url and bool(prefill)
        info[kind] = {
            "label": label, "keys": keys, "url": prefill if prefilled else url, "prefilled": prefilled,
            "placeholder": placeholders[kind], "has_user": bool(keys["user"]),
            "user": str(setting_effective(keys["user"]) or "") if keys["user"] else "",
            "pass_set": bool(setting_effective(keys["pass"])),
        }
    return info


def _wizard_step2(state, host: str) -> str:
    """Step 2: which download client, and how to sign in to it. Saved under the Watcher tab's own keys; Test
    saves, then runs the tab's client check and answers in plain words."""
    kind = watcher_mod.client_kind()
    info = _wizard_client_info(host)
    now = info[kind]
    options = "".join(
        f'<option value="{_attr(key)}"{" selected" if key == kind else ""}>{_html(label)}</option>'
        for key, label in DOWNLOAD_CLIENTS.items())
    category = str(setting_effective("qbit_category") or wizard_mod.DEFAULT_CATEGORY)
    url_hint = ("The address you open the client's web page (its WebUI) with."
                + (" This is the address you are using to open Audiobind, with qBittorrent's usual port 8080. Change "
                   "it if yours is different." if now["prefilled"] else ""))
    pass_placeholder = "set - leave blank to keep it" if now["pass_set"] else "not set"
    fields = (
        _wizard_field("ab-wiz-client", "Download client",
                      f'<select class="ab-in" id="ab-wiz-client" aria-describedby="ab-wiz-client-hint" '
                      f'onchange="abWizardClient(this.value)">{options}</select>',
                      "The program Audiobind hands torrents to. qBittorrent is the usual one.")
        + _wizard_field("ab-wiz-url", "Address (URL)",
                        f'<input type="text" class="ab-in" id="ab-wiz-url" value="{_attr(now["url"])}" '
                        f'placeholder="{_attr(now["placeholder"])}" autocomplete="off" spellcheck="false" '
                        'autocapitalize="none" inputmode="url" aria-describedby="ab-wiz-url-hint">', url_hint)
        + _wizard_field("ab-wiz-user", "User name",
                        f'<input type="text" class="ab-in" id="ab-wiz-user" value="{_attr(now["user"])}" '
                        'autocomplete="off" spellcheck="false" autocapitalize="none" '
                        'aria-describedby="ab-wiz-user-hint">',
                        "The one you sign in to the client's web page with.",
                        wrap_attrs="" if now["has_user"] else " hidden")
        + _wizard_field("ab-wiz-pass", "Password",
                        f'<input type="password" class="ab-in" id="ab-wiz-pass" value="" '
                        f'placeholder="{pass_placeholder}" autocomplete="new-password" spellcheck="false" '
                        'aria-describedby="ab-wiz-pass-hint">'
                        f'<label class="ab-src ab-src-clear" id="ab-wiz-clear-wrap"{"" if now["pass_set"] else " hidden"}>'
                        '<input type="checkbox" id="ab-wiz-clear"> Clear the saved password</label>',
                        "It is saved on the server and never shown here again.")
        + _wizard_field("ab-wiz-category", "Category",
                        f'<input type="text" class="ab-in" id="ab-wiz-category" value="{_attr(category)}" '
                        'autocomplete="off" spellcheck="false" autocapitalize="none" '
                        'aria-describedby="ab-wiz-category-hint">',
                        "The label Audiobind puts on the torrents it adds, so its books stay apart from the rest. "
                        "Test shows whether the client has it and where it saves it.")
    )
    return (
        '<p class="ab-acct-who">Audiobind logs in to your download client to hand it torrents. Fill in the boxes '
        "and press Test: it signs in, looks at the categories and adds nothing.</p>"
        f'<form class="ab-wiz-form" id="ab-wiz-client-form" data-clients="{_attr(json.dumps(info))}" '
        'onsubmit="abWizardClientTest(); return false;" novalidate>'
        + fields
        + '<div class="ab-acct-row"><button class="btn ab-save" type="submit" id="ab-wiz-test">Test</button>'
        '<span class="ab-fb" id="ab-wiz-test-fb" role="status"></span></div>'
        '<div class="ab-test-detail" id="ab-wiz-test-fb-detail" tabindex="-1" hidden></div>'
        '<div class="ab-acct-row">'
        f'<a class="btn" id="ab-wiz-back" href="{wizard_mod.PATH}?step=1">Back</a>'
        '<button class="btn ab-save" type="button" id="ab-wiz-continue" onclick="abWizardClientContinue()">'
        "Continue</button></div></form>"
    )


def _wizard_address_box(view: dict) -> str:
    """Step 3's finding: the address the tracker sees, when it was checked, and whether it is the one
    the session was made for. ``view`` is ``wizard.address_view``. Drawn by the page and by Check
    again's answer. ``data-auto`` asks the page to check once by itself when nothing was ever checked."""
    ip_html = f'<code class="ab-wiz-ip">{_html(view["ip"])}</code>'
    expected_html = f'<code class="ab-wiz-ip">{_html(view["expected"])}</code>'
    lines = []
    if view["at"] is None:
        lines.append(_wizard_check(None, "Not checked yet. Audiobind is checking now; this can take a few "
                                         "seconds."))
    elif view["failed"]:
        fix = ("Check that your tracker proxy is running." if view["proxy"] else
               "Check this container's internet connection.")
        lines.append(_wizard_check(False, (
            "Audiobind could not find out its address: neither address lookup answered. "
            f"{fix} You can still go on; this does not stop the setup.")))
    else:
        how = "through your tracker proxy" if view["proxy"] else "straight from this container, with no proxy"
        lines.append(_wizard_check(True, f"Audiobind's requests to your tracker leave from {ip_html}, "
                                         f"{how}."))
    verdict = view["verdict"]
    if verdict == "match":
        lines.append(_wizard_check(True, "That is the address you said your tracker session was created for, "
                                         "so the two match."))
    elif verdict == "mismatch":
        lines.append(_wizard_check(False, (
            f"Your tracker session was created for {expected_html}, but your requests leave from {ip_html}. "
            f"The session will be refused; create a new one from this address ({ip_html}) and paste it in "
            "the next step. Until the two match, Audiobind does not contact your tracker.")))
    elif verdict == "home":
        lines.append(_wizard_check(False, (
            "That is your home connection, so Audiobind will not contact your tracker from it. Send "
            "Audiobind through your VPN (the tracker proxy) and check again.")))
    elif verdict == "unset":
        lines.append(_wizard_check(None, (
            "You have not told Audiobind which address your tracker session was created for, so it cannot "
            f"compare the two. Create your session from {ip_html}. To make Audiobind stop rather than use "
            "any other address, enter it under Settings &rarr; Watcher &rarr; &ldquo;Only reach the tracker "
            "from one IP&rdquo;.")))
    elif view["expected"]:  # no address now, but the person did say which one the session is for
        lines.append(_wizard_check(None, (
            f"Your tracker session is for {expected_html}. Until Audiobind can see its own address, it does "
            "not contact your tracker.")))
    when = (f'<p class="ab-wiz-when" id="ab-wiz-addr-when">Last checked: {_html(view["checked"])}.</p>'
            if view["checked"] else "")
    auto = ' data-auto="1"' if view["at"] is None else ""
    return (f'<div class="ab-hintbox ab-wiz-addr" id="ab-wiz-addr"{auto}>'
            "<b>The address your tracker sees</b>"
            f'<ul class="ab-wiz-checks">{"".join(lines)}</ul>{when}</div>')


#: Check again's closing words, by verdict
WIZARD_ADDRESS_VERDICTS = {"match": " It matches your session's address.",
                           "mismatch": " It does not match your session's address.",
                           "home": " That is your home connection."}


def _wizard_address_message(view: dict) -> str:
    """Check again's one line, beside the button."""
    if view["failed"]:
        return "Checked just now: the address could not be found."
    closing = WIZARD_ADDRESS_VERDICTS.get(view["verdict"], "")
    return f"Checked just now: your requests leave from {view['ip']}.{closing}"


def _wizard_step3(state) -> str:
    """Step 3: the address the tracker sees. Shows the last egress check on record (a page view sends
    nothing); Check again runs the watcher's own probe once. Continue is never held back by it."""
    return (
        '<p class="ab-acct-who">Your tracker ties each session to the internet address it was created '
        "from. If Audiobind's requests arrive from a different address, the session is refused. This step "
        "shows the address your tracker sees and whether it is the one your session was made for.</p>"
        + _wizard_address_box(wizard_mod.address_view(state))
        + '<div class="ab-acct-row"><button class="btn" type="button" id="ab-wiz-recheck" '
        'onclick="abWizardAddress()">Check again</button>'
        '<span class="ab-fb" id="ab-wiz-recheck-fb" role="status"></span></div>'
        '<p class="ab-setting-hint">Check again asks an address lookup service which address your requests '
        "come from, the same way the watcher checks before it searches. It does not contact your tracker.</p>"
        '<div class="ab-acct-row">'
        f'<a class="btn" id="ab-wiz-back" href="{wizard_mod.PATH}?step=2">Back</a>'
        f'<a class="btn ab-save" id="ab-wiz-continue" href="{wizard_mod.PATH}?step=4">Continue</a></div>'
    )


#: step 4's search settings: (settings key, element id suffix, placeholder) - shown under "Search settings"
WIZARD_SEARCH_FIELDS = (
    ("tracker_search_path", "tpath", "/search.json"),
    ("tracker_search_body", "tbody", '{"q": "{{title}} {{author}}"}'),
    ("tracker_search_method", "tmethod", ""),
    ("tracker_search_encoding", "tenc", ""),
    ("tracker_search_category", "tcat", ""),
    ("tracker_download_path", "tdl", "/download.php"),
)

#: what a session cookie is, said once, for the person who has none yet. Says where it comes from and
#: never asks for a password.
WIZARD_SESSION_WHAT = (
    "A session cookie is a long code your tracker hands out so a program like Audiobind can search as you "
    "without ever knowing your password. You make one on your tracker's own website, on its preferences page "
    "(often under Security): create a session for the address from step 3, copy its value and paste it here. "
    "Audiobind never asks for your tracker password.")


def _wizard_tracker_text(key: str, suffix: str, placeholder: str, area: bool = False) -> str:
    """One text box of step 4, holding the saved value, with ``data-key`` (the Watcher tab's own settings key)
    and ``data-initial`` so only what changed is saved."""
    value = _attr(str(setting_effective(key) or ""))
    common = (f'class="ab-in" id="ab-wiz-{suffix}" data-key="{key}" data-initial="{value}" '
              f'placeholder="{_attr(placeholder)}" autocomplete="off" spellcheck="false" '
              'autocapitalize="none" '
              f'aria-describedby="ab-wiz-{suffix}-hint"')
    if area:
        control = f"<textarea rows=\"3\" {common}>{value}</textarea>"
    else:
        url = ' inputmode="url"' if key.endswith("_url") else ""
        control = f'<input type="text" {common}{url} value="{value}">'
    hint = _html(WATCH_FIELD_HINTS.get(key, ""))
    return _wizard_field(f"ab-wiz-{suffix}", wizard_mod.TRACKER_LABELS[key], control, hint)


def _wizard_tracker_select(key: str, suffix: str, options: list, hint: str) -> str:
    """One choice of step 4: ``options`` is [(value, text)]; the saved value is selected."""
    value = str(setting_effective(key) or "")
    if not any(v == value for v, _t in options):
        found = next((v for v, _t in options if v.upper() == value.upper()), None)
        value = found if found is not None else options[0][0]
    opts = "".join(f'<option value="{_attr(v)}"{" selected" if v == value else ""}>{_html(t)}</option>'
                   for v, t in options)
    control = (f'<select class="ab-in" id="ab-wiz-{suffix}" data-key="{key}" data-initial="{_attr(value)}" '
               f'aria-describedby="ab-wiz-{suffix}-hint">{opts}</select>')
    return _wizard_field(f"ab-wiz-{suffix}", wizard_mod.TRACKER_LABELS[key], control, _html(hint))


def _wizard_search_fields() -> str:
    """Step 4's search settings, with the Watcher tab's own hints and choices. The search category is a
    dropdown when the preset file lists the tracker's categories, as on the Watcher tab."""
    parts = []
    preset, _why = preset_mod.load()
    categories = preset.categories if preset is not None else {}
    for key, suffix, placeholder in WIZARD_SEARCH_FIELDS:
        if key in ("tracker_search_method", "tracker_search_encoding"):
            options = [(c, c) for c in setting_choices(key)]
            parts.append(_wizard_tracker_select(key, suffix, options, WATCH_FIELD_HINTS[key]))
        elif key == "tracker_search_category" and categories:
            options = [("", CATEGORY_NONE)] + [(k, f"{n} ({k})") for k, n in categories.items()]
            saved = str(setting_effective(key) or "").strip()
            if saved and saved not in categories:
                options.append((saved, f"{saved} (not in the preset list)"))
            parts.append(_wizard_tracker_select(key, suffix, options, CATEGORY_DROPDOWN_HINT))
        else:
            parts.append(_wizard_tracker_text(key, suffix, placeholder, area=key == "tracker_search_body"))
    return "".join(parts)


def _wizard_step4(state) -> str:
    """Step 4: your tracker. The address, the session cookie, then the search settings, saved under the
    Watcher tab's own keys through its own save (so its checks apply). Test saves, then sends ONE search
    and says what came back."""
    session_set = bool(setting_effective("tracker_session"))
    if session_set:
        session_hint = ("A session cookie is saved. It is never shown here again; paste a new one to "
                        "replace it.")
        what = ""
    else:
        session_hint = "Paste the value here. It is saved on the server and never shown again."
        what = (f'<div class="ab-hintbox" id="ab-wiz-session-what"><b>What is a session cookie?</b> '
                f"{_html(WIZARD_SESSION_WHAT)}</div>")
    session = _wizard_field(
        "ab-wiz-tsession", wizard_mod.TRACKER_LABELS["tracker_session"],
        '<input type="password" class="ab-in" id="ab-wiz-tsession" data-key="tracker_session" data-secret '
        f'value="" placeholder="{"set - leave blank to keep it" if session_set else "not set"}" '
        'autocomplete="new-password" spellcheck="false" aria-describedby="ab-wiz-tsession-hint">'
        + ('<label class="ab-src ab-src-clear"><input type="checkbox" id="ab-wiz-tclear"> Clear the saved '
           "session cookie</label>" if session_set else ""),
        _html(session_hint))
    query = _wizard_field(
        "ab-wiz-tquery", "Title to test with",
        '<input type="text" class="ab-in" id="ab-wiz-tquery" value="" '
        f'placeholder="{_attr(watcher_mod.DEFAULT_TEST_QUERY)}" autocomplete="off" '
        'aria-describedby="ab-wiz-tquery-hint">',
        f"Test searches your tracker once for this title. Empty = {_html(watcher_mod.DEFAULT_TEST_QUERY)}.")
    return (
        '<p class="ab-acct-who">Audiobind searches your tracker for the books on your wishlist. Fill in the '
        "boxes from top to bottom and press Test: it saves them, then sends one search and tells you what "
        "came back.</p>"
        '<form class="ab-wiz-form" id="ab-wiz-tracker-form" onsubmit="abWizardTrackerTest(); return false;" '
        "novalidate>"
        + _wizard_tracker_text("tracker_url", "turl", "https://tracker.example")
        + what + session
        + _wizard_field(
            "ab-wiz-tcookie", wizard_mod.TRACKER_LABELS["tracker_cookie_name"],
            '<input type="text" class="ab-in" id="ab-wiz-tcookie" data-key="tracker_cookie_name" '
            f'data-initial="{_attr(str(setting_effective("tracker_cookie_name") or ""))}" '
            f'value="{_attr(str(setting_effective("tracker_cookie_name") or ""))}" placeholder="session_id" '
            'autocomplete="off" spellcheck="false" autocapitalize="none" '
            'aria-describedby="ab-wiz-tcookie-hint">',
            "The name your tracker gives that cookie, shown beside it or on your tracker's API page, "
            "e.g. session_id.")
        + '<fieldset class="ab-wiz-group"><legend>Search settings</legend>'
        '<p class="ab-setting-hint">How Audiobind asks your tracker for a book. Your tracker\'s API page '
        "lists "
        "these; the two choices already hold the usual answers.</p>"
        + _wizard_search_fields() + "</fieldset>"
        + query
        + '<div class="ab-acct-row"><button class="btn ab-save" type="submit" id="ab-wiz-test">Test</button>'
        '<span class="ab-fb" id="ab-wiz-test-fb" role="status"></span></div>'
        '<p class="ab-setting-hint">The rest of the tracker settings (download parameter, freeleech, '
        "User-Agent, VIP) keep their usual values; Settings &rarr; Watcher has them.</p>"
        '<div class="ab-acct-row">'
        f'<a class="btn" id="ab-wiz-back" href="{wizard_mod.PATH}?step=3">Back</a>'
        '<button class="btn ab-save" type="button" id="ab-wiz-continue" onclick="abWizardTrackerContinue()">'
        "Continue</button></div></form>"
    )


def _wizard_done(state) -> str:
    """The end state after step 4: what is now set up, each line with a glyph and words, and what Audiobind
    does next. Anything still missing is named with the step that fixes it, and the heading says so."""
    missing = wizard_mod.tracker_missing()
    if missing:  # the watcher's own reason names the tracker first; the address is what can be said here
        client_ready = bool(watcher_mod.client_url().strip())
    else:
        client_ready = not watcher_mod.disabled_reason()
    lines = []
    if missing:
        lines.append(_wizard_check(False, f"Your tracker: still missing {_html(', '.join(missing))}. "
                                          f'<a href="{wizard_mod.PATH}?step=4">Step 4</a> has the boxes.'))
    else:
        lines.append(_wizard_check(True, "Your tracker: the address, session cookie and search settings are "
                                         "saved."))
    label, category = watcher_mod.client_label(), watcher_mod.qbit_category()
    if client_ready:
        lines.append(_wizard_check(True, f"Download client: {_html(label)} at "
                                         f"<code>{_html(watcher_mod.client_url())}</code>, category "
                                         f"<code>{_html(category)}</code>."))
    else:
        lines.append(_wizard_check(False, f"Download client: {_html(label)}'s address or sign-in is not "
                                          f'finished. <a href="{wizard_mod.PATH}?step=2">Step 2</a> has it.'))
    st = ingest_mod.status()
    if st["ok"]:
        lines.append(_wizard_check(True, "Finished downloads are picked up from "
                                         f"<code>{_html(st['dir'])}</code>."))
    else:
        lines.append(_wizard_check(False, "There is no working ingest folder yet, so finished downloads "
                                          "cannot reach the converter. "
                                          f'<a href="{wizard_mod.PATH}?step=1">Step 1</a> '
                                          "shows how to set it."))
    view = wizard_mod.address_view(state)
    step3 = f'<a href="{wizard_mod.PATH}?step=3">Step 3</a>'
    if not view["ip"]:
        lines.append(_wizard_check(None, f"The address your tracker sees has not been found yet. {step3} "
                                         "checks it."))
    elif view["verdict"] in ("mismatch", "home"):
        lines.append(_wizard_check(False, f"Your tracker sees <code class=\"ab-wiz-ip\">{_html(view['ip'])}"
                                          "</code>, and Audiobind will not use it: "
                                          + ("it is your home connection. " if view["verdict"] == "home" else
                                             "your session was created for another address. ")
                                          + f"{step3} says what to do."))
    else:
        closing = (" It matches your session's address." if view["verdict"] == "match" else "")
        lines.append(_wizard_check(True, f"Your tracker sees <code class=\"ab-wiz-ip\">{_html(view['ip'])}"
                                         f"</code> (checked {_html(view['checked'])}).{closing}"))
    if watcher_mod.WATCHER.paused(state):
        lines.append(_wizard_check(None, "The watcher is paused, so nothing is searched until you resume it "
                                         "on Settings &rarr; Watcher."))
    if CONTROL.watcher_dry_run:
        lines.append(_wizard_check(None, "Watcher dry run is on: each round searches and says what it would "
                                         "add, but adds nothing."))
    ready = not missing and client_ready and st["ok"] and view["verdict"] not in ("mismatch", "home")
    if ready:
        if not wizard_mod.read(state)["dismissed"]:
            state.set_meta(wizard_mod.META_DISMISSED, True)  # finished: the Library card has nothing to ask
        interval, per_round = watcher_mod.watch_interval_min(), watcher_mod.watch_max_per_cycle()
        title = "Done: downloading is set up"
        nxt = (
            '<div class="ab-hintbox" id="ab-wiz-next-steps"><b>What happens now</b><ol class="ab-wiz-next">'
            f"<li>Every {interval} minutes, Audiobind searches your tracker for the books on your "
            "wishlist: at "
            f"most {per_round} a round, one request at a time, a few seconds apart.</li>"
            f"<li>When it finds one, it hands the torrent to {_html(label)} with the category "
            f"&ldquo;{_html(category)}&rdquo;.</li>"
            "<li>When the download finishes, the converter picks the book up from the ingest folder, tags it "
            "and adds it to your library.</li></ol>"
            "<p>Nothing is fetched until a book is on your wishlist.</p></div>")
    else:
        title = "Almost done"
        nxt = ('<div class="ab-hintbox" id="ab-wiz-next-steps" role="status"><b>Not fetching yet.</b> '
               "Audiobind fetches nothing until every line marked as a problem is fixed. Everything you "
               "set up "
               "so far is saved.</div>")
    return (
        f'<h2 class="ab-wiz-title" id="ab-wiz-title">{title}</h2>'
        f'<div class="ab-hintbox" id="ab-wiz-summary"><b>What is set up</b>'
        f'<ul class="ab-wiz-checks">{"".join(lines)}</ul></div>'
        + nxt
        + '<div class="ab-acct-row">'
        f'<a class="btn" id="ab-wiz-back" href="{wizard_mod.PATH}?step=4">Back</a>'
        '<a class="btn ab-save" id="ab-wiz-wishlist" href="/discover/wishlist">Open your wishlist</a>'
        '<a class="btn" id="ab-wiz-library" href="/?status=all">Back to the Library</a>'
        '<a class="btn" id="ab-wiz-settings" href="/settings?tab=watcher">Settings &rarr; Watcher</a></div>'
    )


def _wizard_body(state, step: int, mode: str, host: str) -> str:
    """The content of one step."""
    if step == 1:
        return _wizard_step1(state, mode)
    if step == 2:
        return _wizard_step2(state, host)
    if step == 3:
        return _wizard_step3(state)
    return _wizard_step4(state)


def render_wizard(state, step_param=None, host: str = "") -> str:
    """GET /setup/downloading: the "Set up downloading" guide. ``step_param`` is the ?step= text (None = resume
    where the person was; "done" = the end state); ``host`` is the Host the browser used, for the client
    address pre-fill. Showing a step stores it, so a container restart in the middle resumes there."""
    set_effective(state)
    progress = wizard_mod.read(state)
    step = wizard_mod.resolve_step(step_param, progress["mode"], progress["step"])
    wizard_mod.remember_step(state, step)
    if step == wizard_mod.DONE:
        inner = _wizard_done(state)
    else:
        name = dict(wizard_mod.STEPS)[step]
        inner = (f'<h2 class="ab-wiz-title" id="ab-wiz-title">Step {step} of {len(wizard_mod.STEPS)}: '
                 f"{_html(name)}</h2>" + _wizard_body(state, step, progress["mode"], host))
    body = '<div class="ab-wiz">' + _wizard_indicator(step, progress["mode"]) + inner + "</div>"
    return _discover_shell(state, "Set up downloading", body, WIZARD_SCRIPT)


WIZARD_SCRIPT = """
// POST one answer to the guide; resolves {ok, status, body}. A 401 is handled by the page-wide fetch wrapper.
function abWizPost(payload) {
  return fetch('/api/setup/downloading', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify(payload)})
    .then(r => r.json().then(body => ({ok: r.ok && body.ok !== false, status: r.status, body: body})));
}
// Step 1: store the answer, then show the page again: it draws what that answer needs.
function abWizardMode() {
  const fb = document.getElementById('ab-wiz-fb');
  const picked = document.querySelector('input[name="ab-arrive"]:checked');
  fb.className = 'ab-fb';
  if (!picked) { fb.className = 'ab-fb ab-bad'; fb.textContent = 'Choose one of the three first.'; return; }
  fb.textContent = 'saving...';
  abWizPost({action: 'mode', mode: picked.value}).then(res => {
    if (!res.ok) { fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'that did not work'; return; }
    location.href = '/setup/downloading?step=1#ab-wiz-outcome';
  }).catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; });
}
// Step 2. The boxes hold one client at a time; each client's values and keys come from the form's data-clients.
function abWizClients() { return JSON.parse(document.getElementById('ab-wiz-client-form').dataset.clients); }
function abWizardClient(kind) {
  const c = abWizClients()[kind];
  document.getElementById('ab-wiz-url').value = c.url;
  document.getElementById('ab-wiz-url').placeholder = c.placeholder;
  document.getElementById('ab-wiz-user').value = c.user;
  document.getElementById('ab-wiz-user-wrap').hidden = !c.has_user;
  document.getElementById('ab-wiz-pass').value = '';
  document.getElementById('ab-wiz-pass').placeholder = c.pass_set ? 'set - leave blank to keep it' : 'not set';
  document.getElementById('ab-wiz-clear-wrap').hidden = !c.pass_set;
  document.getElementById('ab-wiz-clear').checked = false;
}
// Save the boxes under the Watcher tab's own settings keys, through the tab's own save. A blank password keeps
// the saved one. Resolves true when saved; says why in `fb` when not.
function abWizSave(fb) {
  const kind = document.getElementById('ab-wiz-client').value;
  const keys = abWizClients()[kind].keys;
  const settings = {download_client: kind, qbit_category: document.getElementById('ab-wiz-category').value.trim()};
  settings[keys.url] = document.getElementById('ab-wiz-url').value.trim();
  if (keys.user) settings[keys.user] = document.getElementById('ab-wiz-user').value.trim();
  const pass = document.getElementById('ab-wiz-pass').value;
  if (pass) settings[keys.pass] = pass;
  const clear = document.getElementById('ab-wiz-clear').checked && !pass ? [keys.pass] : [];
  if (!settings[keys.url]) {
    fb.className = 'ab-fb ab-bad'; fb.textContent = 'Type the address first.'; return Promise.resolve(false);
  }
  return fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify({settings: settings, clear: clear})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      if (!res.ok) { fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'that was not saved'; return false; }
      return true;
    });
}
function abWizDetail(body) {
  const box = document.getElementById('ab-wiz-test-fb-detail');
  box.textContent = '';
  (body.details || []).forEach(t => { const p = document.createElement('p'); p.textContent = t; box.appendChild(p); });
  if (body.rule) { const p = document.createElement('p'); p.className = 'ab-rule'; p.textContent = body.rule; box.appendChild(p); }
  box.hidden = !box.childNodes.length;
}
function abWizardClientTest() {
  const fb = document.getElementById('ab-wiz-test-fb');
  const btn = document.getElementById('ab-wiz-test');
  document.getElementById('ab-wiz-test-fb-detail').hidden = true;
  fb.className = 'ab-fb'; fb.textContent = 'saving, then signing in...';
  btn.disabled = true;
  abWizSave(fb).then(saved => {
    if (!saved) return;
    fb.textContent = 'signing in... this can take a few seconds';
    return abWizPost({action: 'test'}).then(res => {
      const b = res.body;
      fb.className = 'ab-fb ' + (!b.ok || b.chain === 'problem' ? 'ab-bad' : b.chain === 'unknown' ? 'ab-warn' : 'ab-good');
      fb.textContent = b.message || b.error || 'no answer';
      if (b.saved_url) document.getElementById('ab-wiz-url').value = b.saved_url;
      abWizDetail(b);
    });
  }).catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; })
    .finally(() => { btn.disabled = false; });
}
function abWizardClientContinue() {
  const fb = document.getElementById('ab-wiz-test-fb');
  fb.className = 'ab-fb'; fb.textContent = 'saving...';
  abWizSave(fb).then(saved => { if (saved) location.href = '/setup/downloading?step=3'; })
    .catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; });
}
// Step 3: Check again asks Audiobind to look up its address once; the answer redraws the finding.
function abWizardAddress() {
  const fb = document.getElementById('ab-wiz-recheck-fb');
  const btn = document.getElementById('ab-wiz-recheck');
  if (!fb || !btn) return;
  fb.className = 'ab-fb'; fb.textContent = 'checking... this can take a few seconds';
  btn.disabled = true;
  abWizPost({action: 'address'}).then(res => {
    const b = res.body;
    const box = document.getElementById('ab-wiz-addr');
    if (b.html && box) box.outerHTML = b.html;
    const bad = !b.ok || b.verdict === 'mismatch' || b.verdict === 'home';
    fb.className = 'ab-fb ' + (bad ? 'ab-bad' : 'ab-good');
    fb.textContent = b.message || b.error || 'no answer';
  }).catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; })
    .finally(() => { btn.disabled = false; });
}
(function () {
  const box = document.getElementById('ab-wiz-addr');
  if (box && box.dataset.auto === '1') abWizardAddress();
})();
// Step 4. Save what changed under the Watcher tab's own keys (data-key), through the tab's own save, so its
// checks apply. A blank session box keeps the saved cookie. Resolves true when saved; says why in `fb`.
function abWizTrackerSave(fb) {
  const settings = {};
  document.querySelectorAll('#ab-wiz-tracker-form [data-key]').forEach(el => {
    if (el.hasAttribute('data-secret')) { if (el.value) settings[el.dataset.key] = el.value; return; }
    const value = el.value.trim();
    if (value !== el.dataset.initial) settings[el.dataset.key] = value;
  });
  const clearBox = document.getElementById('ab-wiz-tclear');
  const clear = clearBox && clearBox.checked && !settings.tracker_session ? ['tracker_session'] : [];
  if (!Object.keys(settings).length && !clear.length) return Promise.resolve(true);
  return fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
                                 body: JSON.stringify({settings: settings, clear: clear})})
    .then(r => r.json().then(body => ({ok: r.ok, body: body})))
    .then(res => {
      if (!res.ok) {
        fb.className = 'ab-fb ab-bad'; fb.textContent = res.body.error || 'that was not saved'; return false;
      }
      document.querySelectorAll('#ab-wiz-tracker-form [data-key]').forEach(el => {
        if (el.hasAttribute('data-secret')) el.value = ''; else el.dataset.initial = el.value.trim();
      });
      return true;
    });
}
function abWizardTrackerTest() {
  const fb = document.getElementById('ab-wiz-test-fb');
  const btn = document.getElementById('ab-wiz-test');
  fb.className = 'ab-fb'; fb.textContent = 'saving, then searching once...';
  btn.disabled = true;
  abWizTrackerSave(fb).then(saved => {
    if (!saved) return;
    fb.textContent = 'searching your tracker once... this can take a few seconds';
    const query = document.getElementById('ab-wiz-tquery').value.trim();
    return abWizPost({action: 'tracker_test', query: query}).then(res => {
      fb.className = 'ab-fb ' + (res.body.ok ? 'ab-good' : 'ab-bad');
      fb.textContent = res.body.message || res.body.error || 'no answer';
    });
  }).catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; })
    .finally(() => { btn.disabled = false; });
}
function abWizardTrackerContinue() {
  const fb = document.getElementById('ab-wiz-test-fb');
  fb.className = 'ab-fb'; fb.textContent = 'saving...';
  abWizTrackerSave(fb).then(saved => { if (saved) location.href = '/setup/downloading?step=done'; })
    .catch(() => { fb.className = 'ab-fb ab-bad'; fb.textContent = 'could not reach Audiobind'; });
}
// After an answer the page reloads at the result: put keyboard and screen-reader focus on it.
(function () {
  if (location.hash !== '#ab-wiz-outcome') return;
  const box = document.getElementById('ab-wiz-outcome');
  if (box && box.textContent.trim()) box.focus();
})();
function abToast(text) {
  const el = document.getElementById('ab-toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('on');
  clearTimeout(abToast.t);
  abToast.t = setTimeout(() => el.classList.remove('on'), 4000);
}
"""


def render_settings(state, tab: str = "") -> str:
    """Render the settings page with current effective values, on the tab named by ?tab=."""
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
            "output_format",
            "Output format",
            setting_effective("output_format"),
            setting_choices("output_format"),
            custom("output_format"),
            setting_env("output_format"),
            hint=OUTPUT_FORMAT_HINT,
        )
    )
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
    # Advanced: the catalog region and what only the container can set
    advanced = (
        _setting_select(
            "region",
            "Catalog region",
            setting_effective("region"),
            setting_choices("region"),
            custom("region"),
            setting_env("region"),
            hint="Applies to future catalog lookups.",
        )
        + _container_only_section()
    )

    tab = settings_tab(tab)
    account = render_account_section(state) + SIGNOUT_PANEL_HTML

    def panel(name: str, body: str) -> str:
        hidden = "" if name == tab else " hidden"
        return f'<div class="ab-tabpanel" id="ab-tab-{name}" data-tab="{name}"{hidden}>{body}</div>'

    rc = rail_counts(state, counts)
    return SETTINGS_PAGE.format(
        signout=signout_html(state),
        styles=STYLES,
        version=__version__,
        **_rail_view_marks(None),
        set_active=" ab-active",
        series_item=_series_rail_item(_series_shelf(state, refresh=False)["missing_total"]),
        discover_item=_discover_rail_item(state),
        rail_total=total,
        rail_attention_count=rc["attention"],
        rail_attention_alert=_alert_class(rc["attention"]),
        library=_breakable_path(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_breakable_path(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        ingest_line=_ingest_line(),
        settings_tabs=settings_tab_bar(tab),
        library_panel=panel("library", "\n".join(controls_parts) + render_library_section()),
        watcher_panel=panel("watcher", render_watcher_section(state)),
        advanced_panel=panel("advanced", advanced),
        account_panel=panel("account", account),
        savebar_hidden=" hidden" if tab == "account" else "",
        nav_script=NAV_SCRIPT,
        settings_script=SETTINGS_SCRIPT,
        account_script=ACCOUNT_SCRIPT,
        watcher_script=WATCHER_SCRIPT,
        library_script=LIBRARY_SCRIPT,
    )


#: the Settings page's tabs, in order: (?tab= value, label). The first is the default.
SETTINGS_TABS = (("library", "Library"), ("watcher", "Watcher"), ("account", "Account"), ("advanced", "Advanced"))


def settings_tab(value: str) -> str:
    """The tab a ?tab= value names; anything unknown (or nothing) is the first, Library."""
    value = str(value or "").strip().lower()
    return value if value in dict(SETTINGS_TABS) else SETTINGS_TABS[0][0]


def settings_tab_bar(active: str) -> str:
    """The tab row: the library's chip pills, plain links (a refresh or bookmark keeps the tab)."""
    links = []
    for name, label in SETTINGS_TABS:
        on = name == active
        current = ' aria-current="page"' if on else ""
        links.append(f'<a class="chip{" on" if on else ""}" href="/settings?tab={name}"{current}>{label}</a>')
    return f'<nav class="chips ab-tabs" aria-label="Settings sections">{"".join(links)}</nav>'


#: the Account tab's sign-out (the rail keeps its own link too)
SIGNOUT_PANEL_HTML = (
    '<form class="ab-acct-form" method="post" action="/logout">'
    '<span class="ab-acct-title">Sign out</span>'
    '<div class="ab-acct-row"><button class="btn" type="submit">Sign out of this browser</button></div>'
    "</form>"
)


def _container_only_section() -> str:
    """Settings > Advanced: what can only be set in the container's environment, read-only. The proxy
    can carry a login, so only whether it is set is shown."""
    proxy = "set" if CONFIG.tracker_proxy else "not set"
    home = _html(CONFIG.tracker_home_ip) if CONFIG.tracker_home_ip else "not set"
    return (
        '<section class="ab-acct" id="container-only" aria-labelledby="ab-env-head">'
        '<h2 class="ab-acct-head" id="ab-env-head">Set in the container only</h2>'
        '<div class="ab-acct-who">Read from the container\'s environment; change them there and restart the '
        f"container. {_html(UNRAID_CONTAINER_ONLY_PATH)} {_html(VPN_SCOPE_NOTE)}</div>"
        '<dl class="ab-watch-grid">'
        f"<dt><code>TRACKER_PROXY</code></dt><dd>{proxy} &#8212; how tracker requests leave the container "
        "(it can hold a login, so its value is never shown)</dd>"
        f"<dt><code>TRACKER_HOME_IP</code></dt><dd>{home} &#8212; the watcher refuses to reach the tracker "
        "from this address</dd>"
        "</dl></section>"
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


OUTPUT_FORMAT_HINT = (
    "What each book is published as, always one file per book. M4B: one .m4b with chapters, "
    "tags and cover (the default). MP3: one .mp3 with chapters, tags and cover embedded. "
    "M4B + MP3: both files in the same folder, the m4b first. MP3 chapter marks are read by "
    "ffprobe-based players (Audiobookshelf, Plex) but not by Apple's Music/iTunes. Applies to "
    "future conversions only."
)

#: the page label of each numeric setting, for save errors ("Scan interval: use a whole number ...")
SETTING_LABELS = {
    "runtime_delta": "Wrong-edition tolerance",
    "scan_interval": "Scan interval",
    "nice": "CPU priority (nice)",
    "watch_interval": "Check the wishlist every",
    "watch_per_cycle": "Books per cycle",
    "wedge_reserve": "Freeleech credits kept in reserve",
    "wedge_per_cycle": "Freeleech credits per cycle",
    "wedge_per_day": "Freeleech credits per day",
    "watch_max_unsatisfied": "Unsatisfied snatches allowed",
    "watch_max_incomplete": "Downloads in progress allowed",
}

WEDGE_HINTS = {
    "wedge_per_cycle": "Most freeleech credits one cycle may spend. 0 = no limit.",
    "wedge_per_day": ("Most freeleech credits spent in any 24 hours. 0 = no daily limit, which also "
                      "switches the reserve off."),
    "wedge_reserve": ("Keep this many of the daily allowance unspent, in case you want them. 0 = no reserve. "
                      "It is measured against the daily limit, so it only applies when a daily limit is set; "
                      "a reserve at or above the daily limit means nothing is ever spent. "
                      "Counted from what Audiobind itself spent, never read from the site."),
    "watch_max_unsatisfied": ("Add nothing while this many torrents Audiobind added are still downloading or "
                              "below a 1.0 ratio, so unsatisfied snatches stay under the account's limit. "
                              "0 = no limit."),
}

VIP_HINT = (
    "Whether your account holds the tracker's top membership tier. Some trackers call it VIP, others Premium. "
    "When this is No, torrents restricted to that tier are skipped instead of attempted. "
    "(Environment variable TRACKER_VIP.)"
)

EXPECTED_EGRESS_HINT = (
    "Optional. Your VPN's exit address. If Audiobind ever sees a different address it stops before "
    "contacting the tracker, because a tracker session is often tied to one IP. Leave it empty to skip this check."
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


def _unit_span(word: str, value: int) -> str:
    """The unit label for a number field: singular at exactly 1, and both forms kept for the live swap."""
    one = word[:-1] if word.endswith("s") else word
    shown = one if value == 1 else word
    return f'<span data-unit-one="{_attr(one)}" data-unit-many="{_attr(word)}">{_html(shown)}</span>'


def _setting_number(
    key: str, label: str, value: int, vmin: int, vmax: int, suffix: str, custom: bool, env_val, hint: str = ""
) -> str:
    mark = _custom_mark(key, custom, env_val)
    hint_html = f'<div class="ab-setting-hint">{_html(hint)}</div>' if hint else ""
    return (
        f'<div class="ab-setting" data-setting="{key}">'
        f'<span class="ab-setting-label">{_html(label)}{mark}</span>'
        f"{hint_html}"
        '<div class="ab-range-wrap">'
        f'<input type="number" class="ab-in" min="{vmin}" max="{vmax}" value="{value}" step="1" '
        f'data-initial="{value}">'
        f"{_unit_span(suffix, value)}"
        "</div></div>"
    )


def in_flight_units() -> set[str]:
    """Paths of books waiting in the manual queue plus the one encoding right now."""
    units = set(CONTROL.queue_snapshot()["units"])
    if CONTROL.current_unit:
        units.add(CONTROL.current_unit)
    return units


def _ingest_line() -> str:
    """Rail footer: the ingest folder is the universal way in, watcher configured or not."""
    root = ingest_mod.configured_dir()
    where = f' <span title="{_attr(root)}">{_html(root)}</span>' if root else ""
    state = "" if root and ingest_mod.ingest_root() is not None else " (off &#8212; set Ingest path in the container settings)"
    half = ingest_mod.half_configured()  # only one of the mount and Ingest dir is there: say which, and where to fix it
    if half:
        state = f' <span class="warn">({_html(half)})</span>'
    return (
        f'<br><b>ingest</b> &larr;{where}{state}<br><small>Files dropped in the ingest folder are '
        "converted whether or not the watcher is configured.</small>"
    )


def _group_sentence(key: str) -> str:
    """The sentence under a group header; the runtime one names your real tolerance."""
    sentence = GROUP_REASONS.get(key, ("", ""))[1]
    pct = round(_tolerance() * 100)
    # the shipped sentence carries the default; whatever the setting is, name the real number
    return re.sub(r"\d+ % tolerance", f"{pct} % tolerance", sentence)


def grouped_rows(units: list, row, detail_panel, total_columns: int = TOTAL_COLUMNS, detail_of=None) -> str:
    """The "Needs you" table body: one header per group, most severe group first.

    Rows keep the order they arrive in (already sorted). A group is a status; its one
    sentence is the one for the reason most of its rows share. Rows past GROUP_PAGE are
    hidden behind a "Show N more" button.
    """
    detail_of = detail_of or _detail_dict
    out = []
    for status in group_order():
        members = [u for u in units if u.status == status]
        if not members:
            continue
        keys = [short_reason(u.status, u.confidence or "", u.note, detail_of(u))[0] for u in members]
        tally: dict[str, int] = {}
        for k in keys:
            tally[k] = tally.get(k, 0) + 1
        lead_key = max(tally, key=lambda k: (tally[k], -keys.index(k)))
        title = GROUP_TITLES[status]
        out.append(
            f'<tr class="ab-group" data-group="{status}"><td colspan="{total_columns}">'
            f'<div class="ab-group-h"><span class="ab-group-t">{title}</span>'
            f'<span class="ab-group-n">{len(members)}</span></div>'
            f'<p class="ab-group-p">{_html(_group_sentence(lead_key))}</p></td></tr>'
        )
        for i, unit in enumerate(members):
            out.append(row(unit, group=status, paged=i >= GROUP_PAGE) + detail_panel(unit))
        more = len(members) - GROUP_PAGE
        if more > 0:
            out.append(
                f'<tr class="ab-more-row" data-group="{status}"><td colspan="{total_columns}">'
                f'<button type="button" class="ab-more" data-group="{status}" '
                f'data-label="{title.lower()}" onclick="abShowMore(this)">'
                f"Show {more} more {title.lower()}</button></td></tr>"
            )
    return "".join(out)


def _detail_dict(unit) -> dict:
    return loads_dict(unit.detail)


#: chip text per status: the same words the group headers use
STATUS_CHIP_LABELS = {
    "converted": "Converted",
    "ready": "Ready",
    "review": "Review",
    "held": "Held",
    "failed": "Failed",
    "interrupted": "Stopped",
}

#: Settings > Watcher connection tests (POST only): path -> what is tested
WATCHER_TEST_ROUTES = {"/api/watcher/test-qbit": "qbit", "/api/watcher/test-tracker": "tracker",
                       "/api/watcher/check-ip": "ip"}
#: Settings > Watcher > Where it looks: fill the tracker fields from the preset file (POST only)
WATCHER_PRESET_ROUTE = "/api/watcher/preset"
#: Settings > Library > Hardcover evidence: Test connection (POST only)
HARDCOVER_TEST_ROUTE = "/api/hardcover/test"

#: routes do_POST serves and do_GET does not: a GET on one is 405 + "Allow: POST", never the page
#: shell (/api/series, /api/wishlist, /api/watcher, /api/discover/recommendations take both)
POST_ONLY_ROUTES = {
    "/logout": "sign out with POST",
    "/api/account": "account changes are a POST",
    "/api/unit": "unit actions are a POST",
    "/api/settings": "settings are saved with a POST",
    "/api/wishlist/remove": "this wishlist action is a POST",
    "/api/wishlist/retry": "this wishlist action is a POST",
    "/api/wishlist/requeue": "this wishlist action is a POST",
    "/api/wishlist/title": "this wishlist action is a POST",
    "/api/library/abs-layout": "the layout tool is a POST",
    "/api/control": "control actions are a POST",
    wizard_mod.API: "the setup guide's answers are a POST",
    **{route: "a connection test is a POST" for route in WATCHER_TEST_ROUTES},
    HARDCOVER_TEST_ROUTE: "a connection test is a POST",
    WATCHER_PRESET_ROUTE: "filling from the preset file is a POST",
}

#: ?status= values that open the "Needs you" view
ATTENTION_ALIASES = ("attention", "needs-you")


def render(state, status: str = "", query: str = "") -> str:
    counts = state.counts()
    stats = library_stats(state)
    status = (status or "").strip().lower()
    all_units = state.all(None)
    total = sum(counts.values())

    # books with a manual conversion request are waiting on the one-at-a-time
    # encoder: they leave every list (the queue strip shows them instead) and
    # the visible counts stay honest
    # (the book encoding right now is no longer in the queue, so it is added here)
    queued = in_flight_units()
    queued_by_status: dict[str, int] = {}
    if queued:
        for u in all_units:
            if u.path in queued:
                queued_by_status[u.status] = queued_by_status.get(u.status, 0) + 1
    total -= sum(queued_by_status.values())
    visible_counts = {name: counts.get(name, 0) - queued_by_status.get(name, 0) for name in counts}
    attention_count = sum(max(0, visible_counts.get(name, 0)) for name in ATTENTION_STATUSES)

    # 0.21.0: "Needs you" is the default view whenever something needs you; with nothing
    # to do the default is All plus a one-line all-clear. ?status=all is the old All view.
    # A server-side search (?q=) with no status looks through the whole library.
    if status in ATTENTION_ALIASES or (not status and not query and attention_count > 0):
        view = "attention"
    elif status in counts:
        view = status
    else:
        view = "all"
    if view == "attention":
        units = [u for u in all_units if u.status in ATTENTION_STATUSES]
    elif view == "all":
        units = list(all_units)
    else:
        units = state.all(view)
    if queued:
        units = [u for u in units if u.path not in queued]

    attention = view == "attention"
    # the heading names the view, so clicking Needs you visibly lands somewhere
    if attention:
        view_heading = f"Needs your review &middot; {attention_count}"
    elif view == "all":
        view_heading = "Library"
    else:
        view_heading = _html(STATUS_CHIP_LABELS.get(view, view.capitalize()))

    sort_column, sort_dir = read_sort(state)
    units = sort_units(units, sort_column, sort_dir)
    if query:
        units = [u for u in units if search_matches(u, query)]

    # chips: Needs you first, the problem statuses, the rest, All last
    def chip(href: str, label: str, n: int, on: bool, alert: bool = False) -> str:
        cls = "chip" + (" on" if on else "") + (" ab-alert" if alert else "")
        return f'<a class="{cls}" href="{href}">{label} <span class="n">{n}</span></a>'

    chips = []
    if attention_count:
        chips.append(chip("/?status=attention", "Needs your review", attention_count, view == "attention", alert=True))
    ordered = [n for n in STATUS_ORDER if n in ATTENTION_STATUSES] + [
        n for n in STATUS_ORDER if n not in ATTENTION_STATUSES
    ]
    for name in ordered:
        visible = visible_counts.get(name, 0)
        if visible > 0:
            chips.append(chip(f"/?status={name}", STATUS_CHIP_LABELS.get(name, name.capitalize()), visible, view == name))
    chips.append(chip("/?status=all", "All", total, view == "all"))
    chips_html = "".join(chips)
    rc = rail_counts(state, counts)
    # stored series listings only; stale ones are refreshed in the background
    series_shelf = _series_shelf(state)

    # nothing needs you: one line above the table, no box, no icon
    allclear = ""
    if view == "all" and not status and attention_count == 0:
        allclear = (
            f'<p class="ab-allclear">All clear - last scan {_html(_fmt_age(state.heartbeat_age()))}, '
            "nothing needs you.</p>"
        )

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
    overrides_by_path = state.all_overrides()
    parsed_details: dict[str, dict] = {}

    def detail_of(unit) -> dict:
        """The row's detail blob as a dict, parsed once per render (the row, its panel, the
        group tally and the re-open pass each used to parse it) and never a non-dict: one hand-
        edited row holding ``[]`` or ``null`` crashed the whole page in the panel's ``.get``."""
        data = parsed_details.get(unit.path)
        if data is None:
            data = parsed_details[unit.path] = _detail_dict(unit)
        return data

    def detail_panel(unit) -> str:
        ingest_row = is_ingest_row(unit)
        data = detail_of(unit)
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
                f'ASIN <a class="ab-alink" href="https://www.audible.com/pd/{_attr(unit.asin)}" '
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
                f'<div class="ab-meterhint">{unit.confidence.capitalize()} confidence &middot; How sure we are this is the '
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

        custom = overrides_mod.describe(overrides_by_path.get(unit.path, {}))
        locked = locks.get(unit.path, set())
        # Q-B12: the boxes' placeholders are the recorded series the file is tagged with when
        # nothing is typed, and a fallback to the Audible search value says so
        base_series, base_seq, _src = overrides_mod.series_of(match_data, {})
        series_note = series_source_note(data, overrides_by_path.get(unit.path, {}))
        series_note_html = (
            f'<p class="ab-hint ab-series-note">{_html(series_note)}</p>' if series_note else ""
        )

        def field(key, label_str, catalog_value, width_hint=""):
            placeholder = _attr(catalog_value or "catalog value")
            on = " ab-locked" if key in locked else ""
            return (
                f'<div class="ab-field{on}"><span class="ab-lab">{label_str}{lock_chip(key, key in locked)}</span>'
                f'<input class="ab-in" data-field="{key}" value="{_attr(custom[key])}" '
                f'placeholder="{placeholder}" spellcheck="false" autocomplete="off" '
                f'aria-label="{_attr(label_str)}" onkeydown="abEnterClicks(event,this)"{width_hint}></div>'
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
            + field("series", "series", base_series)
            + field("series_index", "series index", base_seq)
            + "</div>"
            + series_note_html
            + '<div class="ab-editbar">'
            '<button class="ab-save" data-enter onclick="abSaveMeta(this)">Save metadata</button>'
            '<span class="ab-fb" role="status"></span></div></div>'
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
                        '<a class="ab-alink" href="https://www.audible.com/pd/{a}" '
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
        asin_btn = '<button data-enter title="Set this exact Audible entry for the book (locks it), then re-check against it." onclick="abUnitAction(this,\'set_asin\')">Use this ASIN</button>'

        explain_html = (
            '<div class="explain">'
            + f"<div><h4>{why_header}</h4><p>{_html(why)}</p>{meter}"
            + f'<div class="ab-meterhint ab-panel-age">{_html(status_age(unit.status, unit.updated_at, now))}</div></div>'
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
            + '<input data-asin value="{av}" placeholder="ASIN, e.g. B005EHN61O" aria-label="Audible ASIN" '
            'autocomplete="off" autocapitalize="characters" spellcheck="false" maxlength="14" '
            'onkeydown="abEnterClicks(event,this)">'.format(av=_attr(unit.asin or ""))
            + asin_btn
            + lock_chip("asin", "asin" in locked)
            + '<span class="ab-pinhint">Setting it locks the match - every future '
            "scan checks this book against this exact entry instead of guessing. "
            "Passes your confidence setting &rarr; converts automatically. Still "
            "too unsure &rarr; this panel re-opens with the reason.</span>" + "</div>"
        )
        delete_title = (
            "Remove the converted copy from the output folder and forget this book in Audiobind. "
            "The torrent and its files stay in qBittorrent and keep seeding."
            if ingest_row
            else "Permanently delete this book's source folder and its published files. Cannot be undone."
        )
        delete_source_attr = f' data-source="{SOURCE_INGEST}"' if ingest_row else ""
        delete_label = "Remove from library" if ingest_row else "Delete"
        delete_act = (
            '<div class="acts">'
            f'<button class="btn danger" title="{delete_title}" onclick="unitDelete(this)" '
            f'data-path="{_attr(unit.path)}" data-title="{_attr(unit.title or unit.path)}"{delete_source_attr} '
            f'style="margin-left:auto">{delete_label}</button></div>'
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

    def row(unit, group: str = "", paged: bool = False) -> str:
        tier = str(effective_val("auto_convert") or "high")
        haystack = " ".join(filter(None, [unit.title, unit.author, unit.path]))
        data = " ".join(f'data-sort-{key}="{_attr(str(sort_value(unit, key)))}"' for key in SORT_KEYS)
        hidden = "" if locks.get(unit.path) else " hidden"
        badge = f'<span class="ab-custom" title="Some of this book\'s metadata is locked by you"{hidden}>locked</span>'
        detail_data = detail_of(unit)

        # one short reason per row, the numbers beside it; the full sentence on hover
        why_html = ""
        _key, why_label = short_reason(unit.status, unit.confidence or "", unit.note, detail_data)
        if why_label:
            why, _todo = explain(unit.status, unit.confidence or "", unit.note, detail_data, tier=tier)
            numbers = reason_numbers(unit.status, unit.confidence or "", unit.note, detail_data)
            sub = f'<span class="ab-why-sub">{_html(numbers)}</span>' if numbers else ""
            why_html = (
                f'<div class="ab-why" title="{_attr(why)}"><span class="ab-why-l">{_html(why_label)}</span>{sub}</div>'
            )

        match_html, match_title = match_cell(
            unit, detail_data, overrides_by_path.get(unit.path, {}), asin_pins.get(unit.path, "")
        )
        match_attr = f' title="{_attr(match_title)}"' if match_title else ""
        row_src_badge = INGEST_BADGE if is_ingest_row(unit) else ""
        group_attr = f' data-group="{_attr(group)}"' if group else ""
        # f-strings only: titles and paths come from folder names and the catalog
        return (
            f'<tr class="urow" onclick="toggleRow(this, event)" data-path="{_attr(unit.path)}" '
            'tabindex="0" role="button" aria-expanded="false" onkeydown="abRowKey(event,this)" '
            f'title="{_attr(status_age(unit.status, unit.updated_at, now))}" '
            f'data-search="{_attr(haystack)}" {data}{group_attr}{" hidden" if paged else ""}>'
            f'<td class="ab-covcell"><div class="ab-covrow"><span class="ab-rowcaret" aria-hidden="true">&#9656;</span>'
            f'{cover_html(unit)}</div></td>'
            f'<td class="ab-bookcell"><span class="ab-btitle">{_html(unit.title or unit.path)}{badge}'
            f"{row_src_badge}</span>"
            f'<span class="ab-bauthor">{_html(unit.author or "") or DASH}</span></td>'
            f'<td class="ab-whycell">{why_html}</td>'
            f'<td class="ab-hidemd ab-match"{match_attr}>{match_html}</td>'
            f'<td class="ab-actcol">{kebab_menu(unit)}</td></tr>'
        )

    if view == "attention":
        rows = grouped_rows(units, row, detail_panel, detail_of=detail_of)
    else:
        rows = "".join(row(u) + detail_panel(u) for u in units)

    # a re-opened panel shows its banner once; the flag is consumed so the next
    # page load starts collapsed again
    for u in units:
        flagged = bool(detail_of(u).get("reopen"))
        if flagged and u.status in ("review", "held", "failed"):
            state.clear_reopen(u.path)
    if not units:
        if not all_units and not status and not query:
            empty = (f"No books found in {_html(str(CONFIG.library_root))} yet. If a scan has finished and this is "
                     "still empty, check the Library path in the container settings.")
        else:
            empty = "no books match this filter"
        rows = f'<tr class="ab-empty"><td colspan="{TOTAL_COLUMNS}">{empty}</td></tr>'

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
        (f'<col class="ab-col-{key}" style="width:{width}">' if key in ("why", "tier") else f'<col style="width:{width}">')
        if width
        else "<col>"
        for key, _l, width, _c in COLUMNS
    )

    # controls
    paused = CONTROL.paused
    scanning = CONTROL.scanning
    prog = CONTROL.scan_progress()
    scan_button = (
        '<button class="btn" title="Re-check every book in the library now and '
        'convert the ones that pass the automatic rules." '
        'onclick="act(\'scan\')" id="ab-scanbtn"{di}>{lbl}</button>'
    ).format(
        di=" disabled" if (paused or scanning) else "",
        lbl=("Scanning {} / {}&hellip;".format(prog["done"], prog["total"]) if scanning else "Scan for changes"),
    )
    recheck_button = (
        '<button class="btn" id="ab-recheckbtn" title="Re-check every book against '
        "Audible, including ones already decided - refreshes the match, the Other "
        'editions list and the chapters. Books still follow the auto-convert rules." '
        "onclick=\"act('recheck')\"{di2}>Re-match all&hellip;</button>"
    ).format(di2=" disabled" if (paused or scanning) else "")
    pause_button = (
        '<button class="btn" title="Finish the book that is encoding, then stop converting '
        'until you press Resume." onclick="act(\'resume\')">Resume</button>'
        if paused
        else (
            '<button class="btn warn" title="Finish the book that is encoding, then stop '
            'converting until you press Resume." onclick="act(\'pause\')">Pause after this '
            "book</button>"
            if CONTROL.running
            else ""  # nothing is encoding: the queue strip carries Pause/Stop while something is
        )
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
        else ""
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
        queue_meta = " &middot; <b>{}</b> waiting in queue".format(qsnap["count"]) if qsnap["count"] else ""

        title_html = _html(CONTROL.current_title or CONTROL.current_unit)
        queue_strip = (
            '<section class="ab-queue" aria-label="Currently encoding">'
            '<div class="ab-label"><span class="ab-pulse"></span> Now encoding</div>'
            '<div class="ab-qtitle" id="ab-qtitle">{ti}</div>'
            '<div class="ab-qact">'
            '<button class="btn" onclick="act(\'pause\')">Pause</button>'
            '<button class="btn danger" onclick="stop_now()">Stop</button>'
            "</div>"
            '<div class="ab-qmeta"><span id="ab-qprog">{mp}</span>{mq}</div>'
            '<div class="ab-qbarwrap">'
            '<div class="ab-qbar"><i id="ab-qfill" style="width:{pc:.1f}%"></i></div>'
            '<span class="ab-qpct" id="ab-qpct">{pc:.1f}%</span>'
            "</div></section>"
        ).format(ti=title_html, mp=" &middot; ".join(meta_parts), mq=queue_meta, pc=percent)
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
        **_rail_view_marks(view),
        signout=signout_html(state),
        styles=STYLES,
        nav_script=NAV_SCRIPT,
        script=SCRIPT,
        version=__version__,
        library=_breakable_path(CONFIG.host_library or str(CONFIG.library_root)),
        library_attr=_attr(CONFIG.host_library or str(CONFIG.library_root)),
        output=_breakable_path(CONFIG.host_output or str(CONFIG.output_root)),
        output_attr=_attr(CONFIG.host_output or str(CONFIG.output_root)),
        dry_text=dry_header,
        ingest_line=_ingest_line(),
        health_stale=health_stale,
        health_text=health_text,
        health_scan=_fmt_age(heartbeat_age),
        rail_total=total,
        series_item=_series_rail_item(series_shelf["missing_total"]),
        discover_item=_discover_rail_item(state),
        rail_attention_count=rc["attention"],
        rail_attention_alert=_alert_class(rc["attention"]),
        scan_button=scan_button,
        recheck_button=recheck_button,
        pause_button=pause_button,
        stop_button=stop_button,
        dry_button=dry_button,
        dry_note=dry_note,
        queue_strip=queue_strip,
        wizard_card=_wizard_card(state) if (status in ("", "all") and not query) else "",
        allclear=allclear,
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
        view_heading=view_heading,
        query=_attr(query),
        sort_column_js=json.dumps(sort_column),
        sort_dir_js=json.dumps(sort_dir),
    )


def library_stats(state) -> dict:
    total, converted, hours, out_mb = state.library_totals()
    progress = round(100.0 * converted / total, 1) if total else 0.0
    return {
        "hours": round(hours, 1),
        "gb": round(out_mb / 1000.0, 2),
        "converted": converted,
        "total": total,
        "progress": progress,
    }


def rail_counts(state, counts: dict) -> dict:
    """DB counts minus books currently waiting in the manual-conversion queue."""
    adjusted = {k: counts.get(k, 0) for k in ATTENTION_STATUSES}
    queued = in_flight_units()
    if queued:
        for status in state.statuses_of(queued).values():  # the queue is a handful of paths: look them up
            if status in adjusted:
                adjusted[status] -= 1
    adjusted["attention"] = sum(adjusted[k] for k in ATTENTION_STATUSES)
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
        "settings": public_settings({sk: setting_effective(sk) for sk in SETTING_KEYS}),
        **CONTROL.describe(),
    }


#: caps the manual-action threads (each runs ffprobe over the book plus catalog calls)
MANUAL_SLOTS = threading.BoundedSemaphore(max(1, CONFIG.manual_workers))


def reset_manual_slots(n: int | None = None) -> None:
    """Re-size the manual-action cap (tests, or after CONFIG.manual_workers changes)."""
    global MANUAL_SLOTS
    MANUAL_SLOTS = threading.BoundedSemaphore(max(1, n if n is not None else CONFIG.manual_workers))


def run_manual(state, unit, force: bool = False, slot=None, reencode: bool = False) -> None:
    """A Retry / Use this ASIN / Convert anyway request, run off the request thread.

    The book leaves the queue however it ends - converted, still in review, held, a dry
    run, already published or an exception - or it would vanish from every list until a
    restart (render() hides queued books and shows them in the queue strip instead).
    """
    try:
        # reencode: only Convert anyway may encode a book that is already converted again
        process_unit(state=state, unit=unit, log=_log, force=force, manual=True, reencode=reencode)
    except Exception:  # noqa: BLE001 - a background thread has nobody to raise to
        logging.getLogger("audiobind").exception("manual action for %s failed", unit.state_key)
    finally:
        CONTROL.dequeue(unit.state_key)
        if slot is not None:
            slot.release()


#: a delete that finds the encoder lock taken: a conversion or a layout move is running
DELETE_BUSY = (b'{"error":"a book is being converted or the library layout is being changed - '
               b'nothing was deleted. Try again when it finishes."}')


OUTPUT_CHANGED = (b'{"error":"The published folder changed on disk. '
                  b'Please re-check the book before deleting it."}')

SOURCE_CHANGED = (b'{"error":"The source changed on disk or this row has no audited source ownership. '
                  b'Please re-check the book before deleting it."}')


def source_ownership_for(row) -> dict:
    try:
        ownership = json.loads(row.detail).get("source", {}).get("ownership", {})
    except (ValueError, TypeError, AttributeError):
        return {}
    return ownership if isinstance(ownership, dict) else {}


def conversion_claim(rel: str) -> str:
    """WEBAPI-01: why a conversion has a claim on ``rel`` right now ('' = none).

    begin_unit sets current_unit before it takes the book off the queue, so a manual action is
    always one or the other from the moment it is queued until its encode ends."""
    if CONTROL.current_unit == rel:
        return "cannot delete a book that is currently encoding"
    if rel in CONTROL.queue_snapshot()["units"]:
        return "this book is waiting for a manual action - delete it once that finishes"
    return ""


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


#: WEBAPI-02c: the file in the state directory where Delete records where each target went
#: (source path -> trash name) before the first rename, so a crash mid-delete can be undone
DELETE_JOURNAL = "delete-intent.json"
_JOURNAL_LOCK = threading.Lock()


def delete_journal(state) -> Path | None:
    """The delete journal beside ``state``'s database (None for a state without one)."""
    path = getattr(state, "path", None)
    return Path(path).parent / DELETE_JOURNAL if path else None


def _journal_entries(journal: Path) -> list[dict]:
    """The journal's entries ([] when there is none). ValueError when it cannot be understood."""
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("path"), str) and isinstance(e.get("trash"), str) for e in entries
    ):
        raise ValueError(f"{journal} is not a delete journal")
    return entries


def _journal_write(journal: Path, entries: list[dict]) -> None:
    """Replace the journal atomically and durably (an empty one is removed)."""
    if not entries:
        try:
            journal.unlink()
        except FileNotFoundError:
            pass
        return
    tmp = journal.with_name(f".{journal.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"entries": entries}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, journal)
    try:
        fd = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def settle_journal(journal: Path | None, moved: dict[Path, Path]) -> None:
    """Drop the journal entries of ``moved`` whose trash is gone (removed, or put back). An entry
    whose trash is still there stays for the startup recovery. Never raises (logged)."""
    if journal is None or not moved:
        return
    done = {(str(path), str(trash)) for path, trash in moved.items() if not os.path.lexists(trash)}
    try:
        with _JOURNAL_LOCK:
            entries = _journal_entries(journal)
            kept = [e for e in entries if (e["path"], e["trash"]) not in done]
            if len(kept) != len(entries):
                _journal_write(journal, kept)
    except (OSError, ValueError) as exc:
        logging.getLogger("audiobind").error("HARD DELETE: could not update %s: %s", journal, exc)


def recover_deletes(journal: Path) -> list[Path]:
    """WEBAPI-02c, at startup: put back every path a Delete moved aside and never finished (the
    process died between the rename and the removal), then clear the journal. Each path put back
    is logged; one that cannot go back (something new is at its name) stays listed and is logged
    again on the next start. Running it twice changes nothing. Returns the paths put back."""
    log = logging.getLogger("audiobind")
    with _JOURNAL_LOCK:
        try:
            entries = _journal_entries(journal)
        except (OSError, ValueError) as exc:
            log.error("HARD DELETE recovery: %s could not be read (%s) - check it by hand", journal, exc)
            return []
        restored, kept = [], []
        for entry in entries:
            path, trash = Path(entry["path"]), Path(entry["trash"])
            if not os.path.lexists(trash):
                log.info("HARD DELETE recovery: %s was already removed completely - nothing to put back", path)
            elif put_back({path: trash}):
                kept.append(entry)  # put_back logged why
            else:
                restored.append(path)
                log.warning("HARD DELETE recovery: an interrupted delete was undone - put %s back from %s",
                            path, trash)
        try:
            _journal_write(journal, kept)
        except OSError as exc:
            log.error("HARD DELETE recovery: could not update %s: %s", journal, exc)
    return restored


def move_aside(paths: list[Path], journal: Path | None = None) -> tuple[dict[Path, Path], OSError | None]:
    """WEBAPI-02b: rename each path to a hidden trash name in its own folder (one atomic rename
    each - a link is moved, never followed). On the first failure the ones already moved are put
    back, so either all of them moved ({path: trash}, None) or none did ({}, the error).

    WEBAPI-02c: with a ``journal``, every path -> trash pair is written there (durably) before the
    first rename; a journal that cannot be written stops the delete before anything moved."""
    planned = {path: path.with_name(f".{path.name}.audiobind-delete-{os.getpid()}-{os.urandom(4).hex()}")
               for path in paths}
    if journal is not None and planned:
        try:
            with _JOURNAL_LOCK:
                entries = _journal_entries(journal)
                entries += [{"path": str(path), "trash": str(trash)} for path, trash in planned.items()]
                _journal_write(journal, entries)
        except (OSError, ValueError) as exc:
            logging.getLogger("audiobind").error("HARD DELETE: could not record the intent in %s: %s", journal, exc)
            return {}, OSError(errno.EIO, f"the delete journal {journal} could not be written: {exc}")
    moved: dict[Path, Path] = {}
    for path, trash in planned.items():
        try:
            os.rename(path, trash)
        except OSError as exc:
            put_back(moved)
            settle_journal(journal, planned)
            return {}, exc
        moved[path] = trash
    return moved, None


def put_back(moved: dict[Path, Path], journal: Path | None = None) -> list[Path]:
    """Rename what is left of each trashed path back to its own name - never over something that
    appeared there since. Returns the trash paths that could not go back (logged). With a
    ``journal``, the entries of the ones that went back (or were already gone) are dropped."""
    left = []
    for path, trash in moved.items():
        if not os.path.lexists(trash):
            continue  # removed completely
        if not os.path.lexists(path):
            try:
                os.rename(trash, path)
                continue
            except OSError:
                pass
        left.append(trash)
        logging.getLogger("audiobind").error("HARD DELETE: could not put %s back at %s", trash, path)
    settle_journal(journal, moved)
    return left


def staged_entries(moved: dict[Path, Path], root: Path, entries: dict) -> dict:
    """Translate the authorized names to the hidden staging paths."""
    staged = {}
    for target, trash in moved.items():
        for name, identity in entries.items():
            path = root / name
            if path.is_relative_to(target):
                staged[(trash / path.relative_to(target)).relative_to(root).as_posix()] = identity
    return staged


def staged_matches(moved: dict[Path, Path], root: Path, entries: dict) -> bool:
    try:
        return pipeline_mod.source_entries(root, list(moved.values())) == entries
    except (OSError, ValueError, RuntimeError):
        return False


def remove_trashed(trash: Path, root: Path, entries: dict) -> None:
    """Purge only staged entries from the authorized inventory, never a recursive sweep."""
    children = {}
    for name, identity in entries.items():
        path = root / name
        if path.is_relative_to(trash):
            children.setdefault(path.parent, []).append((path, identity))

    def remove(parent_fd, path, identity):
        info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if identity is None:
            child_fd = os.open(path.name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                for child, expected in children.get(path, ()):
                    remove(child_fd, child, expected)
            finally:
                os.close(child_fd)
            os.rmdir(path.name, dir_fd=parent_fd)
        else:
            if not stat.S_ISREG(info.st_mode) or [info.st_size, info.st_mtime_ns] != identity:
                raise OSError(errno.ESTALE, "staged file identity changed", str(path))
            os.unlink(path.name, dir_fd=parent_fd)

    parent_fd = os.open(trash.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        remove(parent_fd, trash, entries[trash.relative_to(root).as_posix()])
    finally:
        os.close(parent_fd)


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


#: Delete's output_kept_reason for a row whose output Audiobind did not write
OUTPUT_NOT_OURS = "that folder was not created by Audiobind"


def audiobind_wrote_output(unit_state) -> bool:
    """True only for a row whose ``output`` is a book Audiobind itself encoded and published
    (pipeline.wrote_output; which files in its folder are Audiobind's is pipeline.wrote_files)."""
    return pipeline_mod.wrote_output(unit_state)


def output_sharers(state, rel: str, folder: Path) -> int:
    """How many OTHER rows point their output into ``folder`` (a re-rip that reached
    "already published" records the first book's output as its own)."""
    count = 0
    for other_path, other_output in state.outputs():
        if other_path == rel:
            continue
        try:
            other_dir = Path(other_output).resolve().parent
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


_PRIVATE_V4 = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",  # CGNAT / Tailscale
    )
)
_PRIVATE_V6 = tuple(ipaddress.ip_network(n) for n in ("::1/128", "fc00::/7", "fe80::/10"))


def is_private_literal(name: str) -> bool:
    """True when `name` (already port-stripped) is a literal IP in a local/private range.

    A literal IP cannot be DNS-rebound, so it is safe to answer when ALLOWED_HOSTS is unset.
    Hostnames, public IPs and garbage are all False; this never raises.
    """
    try:
        addr = ipaddress.ip_address((name or "").strip())
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        nets = _PRIVATE_V4 if addr.version == 4 else _PRIVATE_V6
        return any(addr in n for n in nets)
    except (ValueError, TypeError, AttributeError):
        return False


def allowed_host_names() -> set[str]:
    return {host_name(h) for h in CONFIG.allowed_hosts if host_name(h)}


def host_accepted(name: str) -> bool:
    """The Host check every request passes: ``name`` (port-stripped) is listed in ALLOWED_HOSTS, or - with the
    list unset - is a literal private IP, which cannot be DNS-rebound."""
    return name in allowed_host_names() or (not CONFIG.allowed_hosts_set and is_private_literal(name))


_HOSTS_WARNED = False
_REFUSED_HOSTS: set[str] = set()


def warn_default_hosts() -> None:
    """One clear line at start-up when ALLOWED_HOSTS was never set."""
    global _HOSTS_WARNED
    if _HOSTS_WARNED or CONFIG.allowed_hosts_set:
        return
    _HOSTS_WARNED = True
    logging.getLogger("audiobind").warning(
        "ALLOWED_HOSTS is not set: the web UI answers to this machine and to private LAN "
        "addresses (like 192.168.1.10). Set ALLOWED_HOSTS to restrict it to exactly the names "
        "you list (for example ALLOWED_HOSTS=192.168.1.10,tower.local)"
    )


SECURITY_HEADERS_CSP = "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
#: added to every response (including BaseHTTPRequestHandler's own error pages)
SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    # No script-src on purpose: the page is built on one inline <script> and inline onclick=
    # handlers, so a script-src without 'unsafe-inline' breaks every button and one with it
    # buys nothing. Do not "fix" this by adding one.
    ("Content-Security-Policy", SECURITY_HEADERS_CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
)
#: the largest JSON body any endpoint accepts
MAX_BODY = 64 * 1024


def peer_is_trusted(peer: str, allowed: list[str]) -> bool:
    """Is ``peer`` (an address string) inside one of the IPs / CIDRs in ``allowed``?
    Bad entries and an unparsable peer are never a match."""
    try:
        address = ipaddress.ip_address(str(peer).split("%", 1)[0])
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    for entry in allowed:
        try:
            if address in ipaddress.ip_network(str(entry).strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent connections. Over the cap the peer gets a
    503 and the socket is closed at once - nothing queues, so a flood of idle or slow
    connections cannot starve the UI of threads."""

    daemon_threads = True

    def __init__(self, address, handler, max_connections: int = 64):
        self.max_connections = max(1, int(max_connections))
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._open = 0
        self._open_lock = threading.Lock()
        self.peak_open = 0
        super().__init__(address, handler)

    def open_connections(self) -> int:
        with self._open_lock:
            return self._open

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        with self._open_lock:
            self._open += 1
            self.peak_open = max(self.peak_open, self._open)
        try:
            super().process_request(request, client_address)
        except BaseException:  # the thread never started: its finally will not run
            self._release()
            raise

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a peer that left, or one cut at the header deadline: not worth a traceback
        super().handle_error(request, client_address)

    def _release(self) -> None:
        with self._open_lock:
            self._open -= 1
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()


class Handler(BaseHTTPRequestHandler):
    state = None
    server_version = f"audiobind/{__version__}"
    #: a client that stops sending (slowloris, Content-Length larger than the body) is cut off
    timeout = 15
    #: total seconds allowed to deliver the request line and headers (``timeout`` is per recv,
    #: so a peer sending one byte every few seconds would otherwise hold a thread for ever)
    HEADER_DEADLINE = 10.0
    _header_timer = None

    def _header_deadline_hit(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle_one_request(self) -> None:
        timer = threading.Timer(self.HEADER_DEADLINE, self._header_deadline_hit)
        timer.daemon = True
        self._header_timer = timer
        timer.start()
        try:
            super().handle_one_request()
        finally:
            timer.cancel()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:  # the headers are in (or the request was refused): the deadline has done its job
            if self._header_timer is not None:
                self._header_timer.cancel()

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
            for one in value if isinstance(value, list) else [value]:  # Set-Cookie may repeat
                self.send_header(name, one)
        self.end_headers()
        self.wfile.write(body)

    def _proxy_trusted(self) -> bool:
        """True when X-Forwarded-* may be believed: TRUSTED_PROXY is on AND this connection's
        peer address is one of TRUSTED_PROXY_IPS. Anyone else's forwarded headers are ignored."""
        return CONFIG.trusted_proxy and peer_is_trusted(
            self.client_address[0] if self.client_address else "", CONFIG.trusted_proxy_ips)

    def _request_host(self) -> str:
        """The host this request was addressed to: X-Forwarded-Host behind a trusted proxy,
        else Host. Empty when there is none (HTTP/1.0 without a Host header)."""
        if self._proxy_trusted():
            forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
            if forwarded:
                return forwarded
        return (self.headers.get("Host") or "").strip()

    def _host_refused(self) -> bool:
        """DNS-rebinding guard for every request: refuse (403) a Host not in ALLOWED_HOSTS.

        A hostile page can point its own domain at this server's LAN address, which makes
        it "same-origin" to the browser - but the browser still sends that domain as Host.
        With ALLOWED_HOSTS unset a literal private/local IP is accepted too (it cannot be
        rebound); once ALLOWED_HOSTS is set, exactly the listed names pass and nothing implicit.
        """
        requested = self._request_host()
        if requested:
            name = host_name(requested)
            if host_accepted(name):
                return False
        if requested and CONFIG.allowed_hosts_set:
            # a list is set: exactly its names answer, the IP included only if it is listed
            message = (
                f"Audiobind doesn't answer to '{name[:100]}'. Allowed hosts (ALLOWED_HOSTS) in the container settings lists "
                f"exactly the names it answers to: add '{name[:100]}' to that list and keep the names you "
                "already use."
            )
        elif requested:
            message = (
                f"Audiobind doesn't answer to '{name[:100]}' yet. It does answer at its IP address (the one you "
                f"use for the Unraid web UI). To use '{name[:100]}' as well, set Allowed hosts (ALLOWED_HOSTS) in the "
                f"container settings to every name you use, e.g. 192.168.1.10,{name[:100]}. A list replaces the default, "
                "so include the IP."
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

    #: the one other path reachable without signing in, once an account exists too: an uptime
    #: monitor must not need a session. It exposes nothing but "ok" / "stale".
    HEALTH_PATH = "/healthz"
    #: reachable without signing in: the login page itself and the icon it shows
    AUTH_EXEMPT = ("/login", "/logout", "/favicon.svg", "/icon.svg")
    #: before the first account exists (and with no AUTH_TOKEN) nothing else is reachable:
    #: only these, plus the setup page and the health probe
    SETUP_EXEMPT = AUTH_EXEMPT + ("/setup", HEALTH_PATH)
    #: _auth_status code for "no account yet - go to /setup"
    SETUP_NEEDED = 428

    def _session_version(self, creds) -> int:
        """What a session is stamped with: the account's version, or - before an account exists -
        a number bound to the legacy AUTH_TOKEN's hash."""
        return creds.version if creds else auth_mod.legacy_version(CONFIG.auth_token)

    def _https(self) -> bool:
        """The browser reached us over https: only knowable through a trusted proxy that says so."""
        if not self._proxy_trusted():
            return False
        return (self.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower() == "https"

    def _client_ip(self) -> str:
        """Source address for the failed-login limit (the proxy's last hop when trusted)."""
        if self._proxy_trusted():
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
            return auth_mod.token_matches(auth_mod.login_secret(given), CONFIG.auth_token)
        if auth_mod.BEARERS.hit(given, creds.version):
            return True
        if auth_mod.verify_login_password(given, creds):  # SEC-06b: as sent, then trimmed
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
        if clean in self.AUTH_EXEMPT or clean == self.HEALTH_PATH:
            return 0  # the public liveness probe: it only ever says "ok" or "stale"
        version = self._session_version(creds)
        sid = auth_mod.cookie_value(self.headers.get("Cookie"))
        if sid and auth_mod.SESSIONS.valid(sid, version):
            return 0
        header = self.headers.get("Authorization") or ""
        scheme, _, given = header.partition(" ")
        # SEC-06b: passed on as sent; _bearer_ok tries it as is, then trimmed, like the sign-in form
        if path.startswith("/api/") and scheme.lower() == "bearer" and auth_mod.login_secret(given):
            ip = self._client_ip()
            if creds is not None and auth_mod.BEARERS.hit(given, creds.version):
                return 0  # a password already proven: no PBKDF2, nothing to count
            if not auth_mod.LIMITER.attempt(ip):  # counted before verifying, cleared on success
                return 429
            if self._bearer_ok(given, creds):
                auth_mod.LIMITER.reset(ip)
                return 0
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
            fields, wants_json = self._form_body(("username", "password", "token", "remember"))
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

        if not auth_mod.LIMITER.attempt(ip):  # counted before verifying, cleared on success
            message, wait = self._locked_out_message(ip)
            reply(429, message, {"Retry-After": str(wait)})
            return
        password = fields.get("password", "")  # SEC-06b: check_login tries it as sent, then trimmed
        if creds is not None:
            # the account: both fields, one generic answer, the same work either way
            ok = auth_mod.check_login(creds, username, password)
        elif "password" in fields:
            # legacy AUTH_TOKEN, no account yet: any username, the token as the password
            ok = bool(username) and auth_mod.token_matches(auth_mod.login_secret(password), CONFIG.auth_token)
        else:
            # a pre-0.16 token-only form or script ({"token": ...}) keeps working until an account exists
            ok = auth_mod.token_matches(fields.get("token", ""), CONFIG.auth_token)
        if not ok:
            logging.getLogger("audiobind").warning("failed sign-in from %s", ip)
            reply(401, "That username or password is not right.")
            return
        auth_mod.LIMITER.reset(ip)
        version = self._session_version(creds)
        remember = fields.get("remember", "").strip().lower() in ("1", "on", "true", "yes")
        ttl = auth_mod.REMEMBER_TTL if remember else auth_mod.SESSION_TTL
        sid = auth_mod.SESSIONS.create(version, ttl=ttl)
        cookies = [auth_mod.session_cookie(sid, ttl, self._https())]
        if username:
            cookies.append(auth_mod.username_cookie(username, self._https()))
        cookie = {"Set-Cookie": cookies}
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
            fields, wants_json = self._form_body(("username", "password", "confirm", "setup_code"))
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
        ip = self._client_ip()
        if not auth_mod.setup_code_matches(self.state, fields.get("setup_code", "")):
            # SEC-01: no code, no account - checked before anything else is looked at.
            # SEC-01c: never metered - 40 random bits need no limiter, and a peer sharing the
            # owner's limiter key must not be able to lock out the right code; the log is the trail
            logging.getLogger("audiobind").warning("setup refused (wrong setup code) from %s", ip)
            problem = "That setup code is not right. It is printed in the container log when Audiobind first started."
            if wants_json:
                self._send(403, json.dumps({"error": problem}).encode(), "application/json")
            else:
                page = render_setup(problem, fields.get("username", "").strip())
                self._send(403, page.encode(), "text/html; charset=utf-8")
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
        auth_mod.consume_setup_code(self.state)
        cookie = {"Set-Cookie": auth_mod.session_cookie(auth_mod.SESSIONS.create(version), secure=self._https())}
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
        creds = self.state.get_credentials()
        if (creds is None) != (action == "create"):
            message = "an account already exists" if creds else "there is no account yet - create one first"
            self._send(409, json.dumps({"error": message}).encode(), "application/json")
            return
        if not auth_mod.LIMITER.attempt(ip):  # counted before verifying, cleared on success
            message, wait = self._locked_out_message(ip)
            self._send(429, json.dumps({"error": message}).encode(), "application/json", {"Retry-After": str(wait)})
            return
        current = str(payload.get("current") or "")
        if creds is None:
            current_ok = auth_mod.token_matches(current, CONFIG.auth_token)
        else:
            current_ok = auth_mod.verify_login_password(current, creds)  # SEC-06b: as sign-in does
        if current_ok:
            auth_mod.LIMITER.reset(ip)
        if not current_ok:
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
        old_version = self._session_version(creds)
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
        """POST only, through the same origin check as every other POST (a cross-site GET or
        form cannot sign anyone out)."""
        refusal = self._origin_refusal()
        if refusal is not None:
            code, message = refusal
            self._send(code, json.dumps({"error": message}).encode(), "application/json")
            return
        sid = auth_mod.cookie_value(self.headers.get("Cookie"))
        if sid:
            auth_mod.SESSIONS.revoke(sid)
        target = "/setup" if self._setup_pending() else "/login"
        self._send(303, b"", "text/plain", {"Set-Cookie": auth_mod.expired_cookie(self._https()), "Location": target})

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
                page = render_login(
                    legacy=not self.state.has_credentials(),
                    remembered=auth_mod.remembered_username(self.headers.get("Cookie")),
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/setup":
            # once there is an account - or a legacy AUTH_TOKEN - setup is over (or skipped)
            if self._setup_pending():
                self._send(200, render_setup().encode(), "text/html; charset=utf-8")
            else:
                self._send(303, b"", "text/plain", {"Location": "/"})
            return
        if self._refuse_get_on_post_only(path):
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
            units = self.state.all(status or None, blobs=False)  # the listing carries no detail/alternates
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
        if path.rstrip("/") in ("/wishlist", "/discover/wishlist"):
            # without the watcher there is no wishlist surface: fall back to the library page
            if not watcher_mod.configured():
                self._send(200, render(self.state, "", "").encode(), "text/html; charset=utf-8")
                return
            self._send(200, render_discover(self.state, "wishlist").encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/discover/for-you":
            self._send(200, render_discover(self.state, "search").encode(), "text/html; charset=utf-8")
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
        if path.rstrip("/") == wizard_mod.PATH:
            page = render_wizard(self.state, params.get("step"), self._request_host())
            self._send(200, page.encode(), "text/html; charset=utf-8")
            return
        if path == "/settings" or path.startswith("/settings"):
            body = render_settings(self.state, params.get("tab", ""))
            self._send(200, body.encode(), "text/html; charset=utf-8")
            return
        if path.rstrip("/") == "/api" or path.startswith("/api/"):
            # an API client asked for something that does not exist: JSON, never the page shell
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        body = render(self.state, status, params.get("q", ""))
        self._send(200, body.encode(), "text/html; charset=utf-8")

    def _refuse_get_on_post_only(self, path: str) -> bool:
        """A GET on a POST_ONLY_ROUTES path answers 405 + Allow: POST; True when it did."""
        message = POST_ONLY_ROUTES.get(path.rstrip("/"))
        if message is None:
            return False
        self._send(405, json.dumps({"error": message}).encode(), "application/json", {"Allow": "POST"})
        return True

    def _send_cover(self, asin: str, rel_path: str) -> None:
        try:
            found = resolve_cover(self.state, asin=asin, rel_path=rel_path)
        except Exception:  # noqa: BLE001 - a cover that cannot be resolved is a 404, not a 500
            logging.getLogger("audiobind").debug("cover for %s could not be resolved", asin or rel_path, exc_info=True)
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
            # an ingest row's files are a torrent that is still seeding under a read-only mount:
            # only Audiobind's converted copy goes, the source block below never runs for it, and
            # the key is dismissed so the next scan does not convert the torrent all over again
            stored = self.state.get(rel)
            ingest_row = ingest_mod.is_ingest(rel) or (stored is not None and stored.source == SOURCE_INGEST)
            if not payload.get("confirm"):
                self._send(400, b'{"error":"confirmation required"}', "application/json")
                return

            # Refuse while encoding
            if CONTROL.running and CONTROL.current_unit == rel:
                self._send(
                    409, b'{"error":"cannot delete a book that is currently encoding"}', "application/json"
                )
                return

            # a queued Retry / Convert anyway would re-create the row (and re-convert) right after
            if rel in CONTROL.queue_snapshot()["units"]:
                self._send(
                    409,
                    b'{"error":"this book is waiting for a manual action - delete it once that finishes"}',
                    "application/json",
                )
                return

            # Validate unit exists in state
            unit_state = self.state.get(rel)
            if unit_state is None:
                self._send(404, b'{"error":"no such unit"}', "application/json")
                return

            lib_root = Path(CONFIG.library_root).resolve()
            src_path: Path | None = None
            targets: list[Path] = []
            kinds: dict[Path, str] = {}
            if not ingest_row:
                # Path traversal guard: resolve source inside LIBRARY_ROOT
                src_path = (lib_root / rel.split("#", 1)[0]).resolve()
                try:
                    src_path.relative_to(lib_root)
                except ValueError:
                    self._send(400, b'{"error":"source path escapes library root"}', "application/json")
                    return

                ownership = source_ownership_for(unit_state)
                if ownership.get("blocked"):
                    self._send(409, json.dumps({
                        "error": ownership["blocked"] + ". Re-check the book before deleting.",
                    }).encode(), "application/json")
                    return
                if not ownership and src_path.exists():
                    self._send(409, SOURCE_CHANGED, "application/json")
                    return
                for name in ownership.get("targets", []):
                    raw = lib_root / name
                    target, kind = plan_target(raw)
                    if target != raw or removal_unsafe(target, lib_root, kind):
                        self._send(409, SOURCE_CHANGED, "application/json")
                        return
                    targets.append(target)
                    kinds[target] = kind

            # Path traversal guard: resolve output inside OUTPUT_ROOT
            out_root = Path(CONFIG.output_root).resolve()

            def plan_output(row, again=False):
                """(not_ours, out_trash, out_shared, wrote, output name) for ``row``; None once a
                refusal was sent. ``again``: the re-read under the lock - a path that passed the
                first time changed."""
                out_trash = None
                out_name = ""
                # only a folder Audiobind wrote is ever removed: a "ready" (already published) row
                # points at a book that was there before, and must not take it with it
                not_ours = bool(row.output) and not audiobind_wrote_output(row)
                if row.output and not not_ours:
                    out_path = Path(row.output).resolve()
                    try:
                        out_path.relative_to(out_root)
                    except ValueError:
                        if again:
                            self._send(409, b'{"error":"the published folder changed on disk (it is no longer '
                                       b'inside the output folder). Its removal was stopped and the book was '
                                       b'kept. Scan again, then retry.","source_removed":false}',
                                       "application/json")
                        else:
                            self._send(400, b'{"error":"output path escapes output root"}', "application/json")
                        return None
                    out_parent = out_path.parent
                    out_name = out_path.name
                    if out_parent != out_root:
                        out_trash = out_parent
                # never take a published book that another row also points at
                out_shared = output_sharers(self.state, rel, out_trash) if out_trash is not None else 0
                # CORE-04a: the files in that folder Audiobind wrote (CORE-04c - None: unknown
                # provenance, which proves only the row's own output; see pipeline.attribute)
                return not_ours, out_trash, out_shared, pipeline_mod.wrote_files(row), out_name

            if plan_output(unit_state) is None:
                return

            # HARD DELETE: the published output is removed permanently and the book is
            # forgotten. The source folder is removed too when the library mount allows
            # writes; on a read-only library (the documented, recommended setup) the
            # source stays on disk and the book reappears on the next scan until the
            # operator removes the folder from the share. The path guards above are the
            # only thing between this call and the filesystem, so they must stay strict.
            log = logging.getLogger("audiobind")
            journal = delete_journal(self.state)  # WEBAPI-02c: where each move aside is recorded first

            # WEBAPI-01: the checks at the top ran before the walk above, and a Convert anyway can
            # have queued this book since. Decided again under the encoder lock (no encode can
            # begin while it is held), never waited for: a conversion holding it gets a busy
            # answer, and nothing has been changed yet. WEBAPI-02: a layout move takes the lock too,
            # so it cannot move the published folder between the read below and the decision.
            # WEBAPI-02b: only the decision and the renames that move the targets aside (fast, atomic,
            # same folder) happen under it; the removal itself and the answer come after it is released
            # C2: what the source is, so neither the reasons below nor the page call a loose file a folder
            source_kind = "folder"
            if targets and not any(kinds[t] == "dir" or (kinds[t] == "link" and t.is_dir()) for t in targets):
                source_kind = "file"
            elif not targets and src_path is not None and src_path.is_file():
                source_kind = "file"
            if not CONVERT_LOCK.acquire(blocking=False):
                self._send(409, DELETE_BUSY, "application/json")
                return
            source_trash: dict[Path, Path] = {}
            output_trash: dict[Path, Path] = {}
            source_staged = {}
            output_staged = {}
            source_removed = False
            source_kept_reason = ""
            output_removed = "absent"
            output_kept_reason = ""
            output_removed_files = None
            output_left = {}
            # C2: Audiobind's own files went, others in that folder were left (a short reason)
            output_other_files_left = ""
            try:
                claim = conversion_claim(rel)
                if claim:
                    log.info("HARD DELETE %s; stopped before removing anything: %s", rel, claim)
                    self._send(409, json.dumps({"error": claim, "source_partial": False}).encode(),
                               "application/json")
                    return
                # WEBAPI-02: the row as it is now - a layout move may have re-pointed it during
                # the walk - and it cannot change again while the lock is held
                unit_state = self.state.get(rel)
                if unit_state is None:
                    self._send(404, b'{"error":"no such unit"}', "application/json")
                    return
                planned = plan_output(unit_state, again=True)
                if planned is None:
                    return
                not_ours, out_trash, out_shared, out_wrote, out_name = planned

                if not ingest_row and (
                    ownership != source_ownership_for(unit_state)
                    or (not ownership and src_path.exists())
                ):
                    self._send(409, SOURCE_CHANGED, "application/json")
                    return

                if targets:
                    changed = ""
                    for target in targets:
                        changed = removal_unsafe(target, Path(CONFIG.library_root).resolve(), kinds[target])
                        if not changed and Path(CONFIG.library_root).resolve() != lib_root:
                            changed = "the library folder was re-pointed"
                        if changed:
                            changed = f"{target.name}: {changed}"
                            break
                    if changed:
                        log.error("HARD DELETE %s; stopped, the path changed on disk: %s", rel, changed)
                        self._send(
                            409,
                            json.dumps(
                                {
                                    "error": "the book's folder changed on disk while it was being deleted "
                                    f"({changed}). Stopped - nothing was removed and the book was kept. "
                                    "Scan again, then retry.",
                                    "source_partial": False,
                                }
                            ).encode(),
                            "application/json",
                        )
                        return
                    try:
                        current = pipeline_mod.source_entries(lib_root, targets)
                    except (OSError, ValueError, RuntimeError):
                        current = None
                    if current != ownership.get("entries"):
                        self._send(409, SOURCE_CHANGED, "application/json")
                        return
                    source_trash, error = move_aside(targets, journal)
                    if error is None:
                        source_staged = staged_entries(source_trash, lib_root, current)
                        if not staged_matches(source_trash, lib_root, source_staged):
                            put_back(source_trash, journal)
                            self._send(409, SOURCE_CHANGED, "application/json")
                            return
                    elif error.errno == errno.EROFS or "Read-only" in str(error):
                        source_kept_reason = "your source library is mounted read-only"
                        log.info("HARD DELETE %s; source kept: %s", rel, error)
                    elif error.errno in (errno.EACCES, errno.EPERM):
                        source_kept_reason = (
                            f"Audiobind doesn't have permission to delete the source {source_kind}")
                        log.info("HARD DELETE %s; source kept: %s", rel, error)
                    else:
                        log.error("HARD DELETE %s; source could not be moved aside: %s", rel, error)
                        self._send(500, json.dumps({
                            "error": f"source removal failed ({_os_error_code(error)}) - nothing was removed; "
                            "details are in the Audiobind log",
                            "source_removed": False,
                        }).encode(), "application/json")
                        return
                elif ingest_row:
                    source_kept_reason = "the files are a torrent that is still seeding in qBittorrent"
                    log.info("HARD DELETE %s; ingest source left alone (still seeding)", rel)
                elif src_path is not None and src_path.exists() and "#" not in rel:
                    # a row whose folder no longer holds a book we recognise: never rmtree a
                    # folder we cannot prove belongs to this one book
                    source_kept_reason = "the folder no longer holds this book's audio, so it was left alone"
                    log.info("HARD DELETE %s; source left alone - no matching book on disk at %s", rel, src_path)
                else:
                    log.info("HARD DELETE %s; source folder not found at %s", rel, src_path)

                if not_ours:
                    output_removed = "kept"
                    output_kept_reason = OUTPUT_NOT_OURS
                    log.info("HARD DELETE %s; output kept, %s: %s", rel, output_kept_reason, unit_state.output)
                elif out_trash is not None and out_shared:
                    output_removed = "kept"
                    output_kept_reason = f"also used by {out_shared} other book(s)"
                    log.info("HARD DELETE %s; output kept, %s: %s", rel, output_kept_reason, out_trash)
                elif out_trash is not None and out_trash.exists():
                    changed = removal_unsafe(out_trash, out_root, "dir")
                    if not changed and Path(CONFIG.output_root).resolve() != out_root:
                        changed = "the output folder was re-pointed"
                    if changed:
                        put_back(source_trash, journal)  # nothing is removed yet: the source comes back whole
                        log.error("HARD DELETE %s; output not removed, the path changed on disk: %s", rel, changed)
                        self._send(
                            409,
                            json.dumps(
                                {
                                    "error": f"the published folder changed on disk ({changed}). Its removal "
                                    "was stopped and the book was kept. Scan again, then retry.",
                                    "source_removed": False,
                                }
                            ).encode(),
                            "application/json",
                        )
                        return
                    present = pipeline_mod.folder_files(out_trash)
                    own, left = pipeline_mod.attribute(out_trash, present, out_wrote, out_name)
                    if not left:
                        moves = [out_trash]
                        output_removed = "removed"
                    else:
                        # CORE-04a: the folder also holds files Audiobind never wrote (the owner's own
                        # M4B beside an MP3 Audiobind added) or cannot prove it wrote (CORE-04c):
                        # only Audiobind's files go, and each one left is named with the reason
                        moves = [out_trash / name for name in own]
                        names = sorted(left)
                        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
                        others = (f"{len(left)} file(s) Audiobind did not write or could not prove "
                                  f"it wrote ({shown})")
                        if own:
                            # C2: our conversion WAS removed - "kept" would tell the owner it is still there
                            output_removed = "removed"
                            output_other_files_left = others
                        else:
                            output_removed = "kept"
                            output_kept_reason = f"the folder holds only {others}"
                        output_removed_files = own
                        output_left = left
                    try:
                        output_entries = pipeline_mod.source_entries(out_root, moves)
                    except (OSError, ValueError, RuntimeError):
                        output_entries = None
                    expected_files = {(out_trash / name).relative_to(out_root).as_posix() for name in own}
                    if output_entries is None or {
                        name for name, identity in output_entries.items() if identity is not None
                    } != expected_files or (out_wrote is not None and any(
                        output_entries[(out_trash / name).relative_to(out_root).as_posix()] != out_wrote[name]
                        for name in own
                    )):
                        put_back(source_trash, journal)
                        self._send(409, OUTPUT_CHANGED, "application/json")
                        return
                    output_trash, error = move_aside(moves, journal)
                    if error is not None:
                        put_back(source_trash, journal)
                        log.error("HARD DELETE %s; could not move the output aside %s: %s", rel, out_trash, error)
                        self._send(500, json.dumps({
                            "ok": False,
                            "error": f"the converted copy could not be removed ({_os_error_code(error)}; "
                            "details are in the Audiobind log) - nothing was removed and the book was kept. "
                            "Fix the permissions and delete again.",
                            "output": "failed",
                            "source_removed": False,
                        }).encode(), "application/json")
                        return
                    output_staged = staged_entries(output_trash, out_root, output_entries)
                    if not staged_matches(output_trash, out_root, output_staged):
                        put_back(output_trash, journal)
                        put_back(source_trash, journal)
                        self._send(409, OUTPUT_CHANGED, "application/json")
                        return
                else:
                    log.info("HARD DELETE %s; output -> none", rel)
            finally:
                CONVERT_LOCK.release()

            # WEBAPI-02b: the slow part, with the encoder free. Each target already sits under a trash
            # name beside its own; a removal that fails puts what is left back under its name
            if source_trash:
                trashed_before = {str(lib_root / name) for name, identity in source_staged.items()
                                  if identity is not None}
                error = None
                for trash in source_trash.values():
                    try:
                        remove_trashed(trash, lib_root, source_staged)
                    except OSError as exc:
                        error = exc
                        break
                if error is None:
                    source_removed = True
                    settle_journal(journal, source_trash)
                    log.info("HARD DELETE %s; source removed: %s", rel, ", ".join(map(str, source_trash)))
                else:
                    remaining = {f for f in trashed_before if os.path.lexists(f)}
                    put_back(source_trash, journal)
                    failure = None
                    if remaining != trashed_before:
                        # some files are already gone: never call that "kept", and keep the row
                        # (and the published output) so the operator can see and finish the job
                        log.error("HARD DELETE %s; source PARTIALLY removed: %s", rel, error)
                        failure = (500, {
                            "error": f"the source was only partly deleted ({len(trashed_before) - len(remaining)} "
                            f"of {len(trashed_before)} files removed) before a file system error "
                            f"({_os_error_code(error)}; details in the Audiobind log). "
                            "Nothing else was touched - fix the permissions and delete again.",
                            "source_partial": True,
                        })
                    elif error.errno == errno.EROFS or "Read-only" in str(error):
                        source_kept_reason = "your source library is mounted read-only"
                        log.info("HARD DELETE %s; source kept: %s", rel, error)
                    elif error.errno in (errno.EACCES, errno.EPERM):
                        source_kept_reason = (
                            f"Audiobind doesn't have permission to delete the source {source_kind}")
                        log.info("HARD DELETE %s; source kept: %s", rel, error)
                    else:
                        log.error("HARD DELETE %s; source removal failed: %s", rel, error)
                        failure = (500, {
                            "error": f"source removal failed ({_os_error_code(error)}) - "
                            "details are in the Audiobind log"
                        })
                    if failure is not None:
                        put_back(output_trash, journal)
                        self._send(failure[0], json.dumps(failure[1]).encode(), "application/json")
                        return

            if output_trash:
                try:
                    for trash in output_trash.values():
                        remove_trashed(trash, out_root, output_staged)
                    settle_journal(journal, output_trash)
                    if output_removed_files is None:
                        log.info("HARD DELETE %s; output removed: %s", rel, out_trash)
                    else:
                        log.info("HARD DELETE %s; output %s: removed only %s; left %s", rel,
                                 output_other_files_left or output_kept_reason,
                                 ", ".join(output_removed_files) or "nothing",
                                 "; ".join(f"{name} ({why})" for name, why in output_left.items()))
                except OSError as exc:
                    # the book is NOT forgotten: the row stays so the operator can see the
                    # converted copy is still there, fix the cause and delete again
                    put_back(output_trash, journal)
                    log.error("HARD DELETE %s; could not remove output %s: %s", rel, out_trash, exc)
                    self._send(
                        500,
                        json.dumps(
                            {
                                "ok": False,
                                "error": f"the converted copy could not be removed ({_os_error_code(exc)}; "
                                "details are in the Audiobind log). The book was kept - fix the "
                                "permissions and delete again."
                                + (f" The source {source_kind} was already removed."
                                   if source_removed else ""),
                                "output": "failed",
                                "source_removed": source_removed,
                            }
                        ).encode(),
                        "application/json",
                    )
                    return

            # Remove from state
            self.state.delete(rel)
            result = {
                "ok": True,
                "deleted": rel,
                "source_removed": source_removed,
                "source_kind": source_kind,
                "source_kept": bool(source_kept_reason),
                "source_kept_reason": source_kept_reason,
                "output": output_removed,
                "output_kept_reason": output_kept_reason,
            }
            if output_other_files_left:
                result["output_other_files_left"] = output_other_files_left
            if output_removed_files is not None:
                result["output_removed_files"] = output_removed_files
                result["output_left"] = output_left  # CORE-04c: {name: why it was not removed}
            if ingest_row:
                # the torrent folder is still in INGEST_DIR: without this the next scan converts it again
                self.state.dismiss(rel)
                result["dismissed"] = True
                log.info("HARD DELETE %s; removed from Audiobind, the torrent keeps seeding", rel)
            self._send(200, json.dumps(result).encode(), "application/json")
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
                return ""
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
            if self.state.undismiss(rel):
                # an explicit action on a removed ingest book is the operator bringing it back
                logging.getLogger("audiobind").info("%s: removal undone by a manual %s", rel, action)
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
                kwargs={"force": action == "force", "reencode": action == "force", "slot": slot},
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

        # Reset to defaults: clear the general settings and hand back their environment values. The
        # watcher's connection settings are left alone (a typed-in session or password is never wiped
        # by resetting the conversion settings) and none of their values is sent back.
        if payload.get("reset"):
            for key in GENERAL_SETTING_KEYS:
                self.state.set_meta(f"setting_{key}", None)
            set_effective(self.state)
            env = setting_env_defaults()
            env_defaults = {key: env[key] for key in GENERAL_SETTING_KEYS if key in env}
            self._send(
                200,
                json.dumps({"ok": True, "env_defaults": public_settings(env_defaults)}).encode(),
                "application/json",
            )
            return

        # Reset to container value: drop the override of the named settings, one or several
        reset_keys = payload.get("reset_keys")
        if reset_keys is not None:
            if not isinstance(reset_keys, list) or not reset_keys or any(k not in SETTING_KEYS for k in reset_keys):
                self._send(400, b'{"error":"unknown setting"}', "application/json")
                return
            for key in reset_keys:
                self.state.set_meta(f"setting_{key}", None)
            set_effective(self.state)
            watcher_mod.WATCHER.settings_changed(self.state)
            custom = [key for key in SETTING_KEYS if is_custom(SETTING_MAP[key])]
            self._send(200, json.dumps({"ok": True, "custom": custom}).encode(), "application/json")
            return

        settings = payload.get("settings") or {}
        if not isinstance(settings, dict):
            self._send(400, b'{"error":"settings must be an object"}', "application/json")
            return
        # Clear: a secret's "Clear" box - store an empty value, so no session / password is used
        clear = payload.get("clear") or []
        if not isinstance(clear, list) or any(k not in SECRET_SETTING_KEYS for k in clear):
            self._send(400, b'{"error":"only the session, the download client passwords and the Hardcover token '
                       b'can be cleared"}',
                       "application/json")
            return

        errors = []
        validated = {}

        # selects: one of the offered choices (which include an unusual env value)
        for key, fold in (("auto_convert", str.upper), ("output_format", str.upper), ("audio_bitrate", str.lower),
                          ("region", str.lower), ("vip", str.upper), ("tracker_search_method", str.upper),
                          ("tracker_search_encoding", str.lower), ("download_client", str.lower),
                          ("ip_updater", str.lower), ("hardcover_evidence", str.lower)):
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
        for key in ("runtime_delta", "scan_interval", "nice", "watch_interval", "watch_per_cycle",
                    "wedge_reserve", "wedge_per_cycle", "wedge_per_day", "watch_max_unsatisfied",
                    "watch_max_incomplete"):
            if key not in settings:
                continue
            lo, hi = setting_bounds(key)
            try:
                v = int(settings[key])
                if key in FIXED_BOUNDS:
                    v = max(lo, min(hi, v))  # politeness limits: pulled back, never stored raw
                elif v < lo or v > hi:
                    raise ValueError
                validated[key] = v
            except (ValueError, TypeError):
                errors.append(f"{SETTING_LABELS.get(key, key)}: use a whole number from {lo} to {hi}")

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

        # the watcher's text settings: each through the same check the container value gets at start.
        # A blank secret keeps what is stored (the page never holds it to send back); messages never
        # repeat what was typed.
        for key, (label, check) in WATCHER_TEXT_CHECKS.items():
            if key not in settings:
                continue
            raw = "" if settings[key] is None else settings[key]
            if not isinstance(raw, str):
                errors.append(f"{label}: must be text")
                continue
            if key in SECRET_SETTING_KEYS and raw == "":
                continue
            try:
                validated[key] = check(raw)
            except ValueError as exc:
                errors.append(str(exc))
        for key in clear:
            validated[key] = ""  # Clear wins over anything typed in the same save

        if errors:
            self._send(400, json.dumps({"error": "; ".join(errors)}).encode(), "application/json")
            return

        for key, value in validated.items():
            self.state.set_meta(f"setting_{key}", value)

        set_effective(self.state)
        # in the daemon, a save that completes the watcher's configuration starts it (no restart)
        watcher_mod.WATCHER.settings_changed(self.state)
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

    def _handle_wishlist_requeue(self) -> None:
        """POST /api/wishlist/requeue: a downloading / downloaded / in_library row goes back to wanted
        (the explicit "search again"). The torrent already in qBittorrent is left alone."""
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
        if not self.state.wishlist_requeue(asin):
            body = {"error": "only a book that is downloading, downloaded or in your library can be re-queued "
                             f"(this one is {row['status']}; Retry covers a book that was not found or failed)"}
            self._send(409, json.dumps(body).encode(), "application/json")
            return
        logging.getLogger("audiobind").info("wishlist: re-queue %s (was %s)", asin, row["status"])
        row = self.state.wishlist_get(asin) or row
        self._send(200, json.dumps({"ok": True, "asin": asin, "item": discover_mod.public_row(row)}).encode(),
                   "application/json")

    def _handle_wishlist_title(self) -> None:
        """POST /api/wishlist/title: {"asin", "title"} sets the title the watcher searches for;
        blank clears it. A wanted / not_found / error row goes back to wanted; a row already past
        searching (downloading / downloaded / in_library) only has the title saved."""
        payload = self._read_json()
        if payload is None:
            return
        asin = str(payload.get("asin", "")).strip().upper()
        if not valid_asin(asin):
            self._send(400, b'{"error":"that is not an ASIN"}', "application/json")
            return
        title = payload.get("title", "")
        if title is None:
            title = ""
        if not isinstance(title, str) or len(title) > 300:
            self._send(400, b'{"error":"the title must be text of at most 300 characters"}', "application/json")
            return
        row = self.state.wishlist_set_search_title(asin, title)
        if row is None:
            self._send(404, b'{"error":"that book is not on your wishlist"}', "application/json")
            return
        logging.getLogger("audiobind").info("wishlist: search title for %s %s", asin,
                                            "set" if row["search_title"] else "cleared")
        self._send(200, json.dumps({"ok": True, "asin": asin, "item": discover_mod.public_row(row)}).encode(),
                   "application/json")

    def _handle_watcher_test(self, target: str) -> None:
        """POST /api/watcher/test-qbit | test-tracker | check-ip: one test with the saved values.
        200 {"ok": true, "message": "..."} or 502 {"ok": false, "message": why}. The result is only
        answered, never stored. test-qbit adds "chain", "details" and "rule" (the folder check);
        test-tracker takes an optional {"query": "<title>"}; check-ip adds "ip" and "guard" and never
        contacts the tracker. No answer carries a setting's value: the messages are built from results."""
        payload = self._read_json()
        if payload is None:
            return
        if target == "qbit":
            answer = watcher_mod.probe_qbit_report()
        elif target == "ip":
            answer = watcher_mod.check_ip()
        else:
            query = payload.get("query", "")
            if query is not None and not isinstance(query, str):
                self._send(400, b'{"ok":false,"message":"the search title must be text"}', "application/json")
                return
            ok, message = watcher_mod.probe_tracker(self.state, query or "")
            answer = {"ok": ok, "message": message}
        self._send(200 if answer["ok"] else 502, json.dumps(answer).encode(), "application/json")

    def _handle_watcher_preset(self) -> None:
        """POST /api/watcher/preset {"overwrite": bool}: fill the tracker fields from the preset file.
        The file is checked as a whole first (a bad one is a 400 and nothing is saved). A field that is
        empty (or still at its built-in value) is filled; a field holding something else is replaced
        only when the body says ``"overwrite": true`` - otherwise 409 with the fields' names, nothing saved.
        Everything is stored in one transaction, under the keys the tab's own Save uses."""
        payload = self._read_json()
        if payload is None:
            return
        preset, why = preset_mod.load()
        if preset is None:
            self._send(400, json.dumps({"ok": False, "error": why or "There is no tracker preset file."}).encode(),
                       "application/json")
            return
        if not preset.values:
            self._send(400, json.dumps({
                "ok": False, "error": "This preset file only lists search categories; it has no fields to fill. "
                                      "Nothing was changed."}).encode(), "application/json")
            return
        set_effective(self.state)
        plan = preset_mod.plan(preset, setting_effective)
        overwrite = payload.get("overwrite") is True
        if plan["replace"] and not overwrite:
            self._send(409, json.dumps({
                "ok": False, "needs_confirm": True, "replace": [preset_mod.label_of(k) for k in plan["replace"]],
                "error": "Some of these fields already hold something else."}).encode(), "application/json")
            return
        keys = plan["fill"] + (plan["replace"] if overwrite else [])
        if keys:
            self.state.set_meta_many({f"setting_{key}": preset.values[key] for key in keys})
            set_effective(self.state)
            watcher_mod.WATCHER.settings_changed(self.state)
        self._send(200, json.dumps({
            "ok": True, "label": preset.label,
            "filled": [preset_mod.label_of(k) for k in plan["fill"]],
            "replaced": [preset_mod.label_of(k) for k in plan["replace"]] if overwrite else [],
            "unchanged": [preset_mod.label_of(k) for k in plan["same"]]}).encode(), "application/json")

    def _handle_hardcover_test(self) -> None:
        """POST /api/hardcover/test: one request with the saved token (asked for, so it runs even
        while HARDCOVER_EVIDENCE is off). Answered, never stored; the token is never in the answer."""
        if self._read_json() is None:
            return
        set_effective(self.state)
        token = hardcover_mod.token()
        if not token:
            ok, message = False, "No Hardcover token is set - paste one above and test again"
        else:
            ok, message = hardcover_mod.test_connection(token)
        self._send(200 if ok else 502, json.dumps({"ok": ok, "message": message}).encode(), "application/json")

    def _handle_watcher(self) -> None:
        """POST /api/watcher: {"action": "run" | "pause" | "resume" | "use_current_ip"}. The last only
        while IP_UPDATER is not off; with it off the action is unknown, exactly as before."""
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
        elif action == "use_current_ip" and watcher_mod.ip_updater_mode() != "off":
            ok, message = watcher_mod.use_current_ip(self.state)
            if not ok:
                self._send(409, json.dumps({"error": message}).encode(), "application/json")
                return
        else:
            self._send(400, b'{"error":"unknown action"}', "application/json")
            return
        body = {"ok": True, "action": action, **watcher.status(self.state)}
        self._send(200, json.dumps(body).encode(), "application/json")

    def _handle_wizard(self) -> None:
        """POST /api/setup/downloading {"action": ...}: the Set up downloading guide's answers.
        "dismiss": the Library page's card is answered for good ("No, just my library").
        "mode": step 1's answer (library | files | tracker). Only "tracker" leads on to step 2; the other two
        also answer the card, so it does not ask a person who has already said what they want.
        "test": step 2's Test on the saved values (200 ok, 502 not), in plain words; see wizard.test_client.
        "address": step 3's Check again - one egress probe, stored as the watcher stores it (200 found,
        502 not), with the redrawn finding; see wizard.check_address.
        "tracker_test": step 4's Test - ONE search on the saved settings, {"query": optional title}
        (200 answered, 502 not), in plain words; see wizard.test_tracker."""
        payload = self._read_json()
        if payload is None:
            return
        action = str(payload.get("action", "")).lower()
        if action == "dismiss":
            self.state.set_meta(wizard_mod.META_DISMISSED, True)
            self._send(200, b'{"ok":true}', "application/json")
            return
        if action == "mode":
            mode = payload.get("mode")
            if mode not in wizard_mod.MODES:
                self._send(400, b'{"error":"choose one of: only my library, my own downloader, a private tracker"}',
                           "application/json")
                return
            answers = {wizard_mod.META_MODE: mode, wizard_mod.META_STEP: 1}
            if mode != wizard_mod.MODE_TRACKER:
                answers[wizard_mod.META_DISMISSED] = True  # answered: the Library page's card has nothing more to ask
            self.state.set_meta_many(answers)
            self._send(200, json.dumps({"ok": True, "mode": mode}).encode(), "application/json")
            return
        if action == "test":
            set_effective(self.state)
            folder = ingest_mod.configured_dir()
            extra = [f"Audiobind reads its ingest folder: {folder}." if folder else
                     "Audiobind has no ingest folder yet (step 1 shows how to set it).", ONE_FOLDER_THREE_NAMES]
            answer = wizard_mod.test_client(self.state, extra)
            self._send(200 if answer["ok"] else 502, json.dumps(answer).encode(), "application/json")
            return
        if action == "address":
            view = wizard_mod.check_address(self.state)
            answer = {"ok": not view["failed"], "message": _wizard_address_message(view),
                      "ip": view["ip"], "verdict": view["verdict"], "checked": view["checked"],
                      "html": _wizard_address_box(view)}
            self._send(200 if answer["ok"] else 502, json.dumps(answer).encode(), "application/json")
            return
        if action == "tracker_test":
            query = payload.get("query", "")
            if query is not None and not isinstance(query, str):
                self._send(400, b'{"ok":false,"message":"the title to test with must be text"}',
                           "application/json")
                return
            answer = wizard_mod.test_tracker(self.state, query or "")
            self._send(200 if answer["ok"] else 502, json.dumps(answer).encode(), "application/json")
            return
        self._send(400, b'{"error":"unknown action"}', "application/json")

    def _handle_abs_layout(self) -> None:
        """POST /api/library/abs-layout: {"dry_run": bool, "confirm": bool}.

        Settings > Library "Match Audiobookshelf layout". A dry run (the default) only reads and
        returns the plan; the real run needs confirm: true and moves folders inside OUTPUT_ROOT.
        """
        payload = self._read_json()
        if payload is None:
            return
        if payload.get("dry_run", True) is not False:
            result = abs_layout.plan(self.state)
            body = {
                "ok": True,
                "dry_run": True,
                "output_root": str(CONFIG.output_root),
                "moves": [m.public() for m in result["moves"]],
                "skipped": result["skipped"],
                "unchanged": result["unchanged"],
            }
            self._send(200, json.dumps(body).encode(), "application/json")
            return
        if payload.get("confirm") is not True:
            self._send(400, b'{"error":"confirmation required"}', "application/json")
            return
        # never while a book is being encoded: the encoder may be writing into these folders
        if not CONVERT_LOCK.acquire(blocking=False):
            self._send(409, b'{"error":"a book is being converted - try again when it finishes"}',
                       "application/json")
            return
        try:
            result = abs_layout.apply(self.state)
        except OSError as exc:
            logging.getLogger("audiobind").error("Audiobookshelf layout: run stopped: %s", exc)
            self._send(500, json.dumps({"error": f"could not start: {exc.strerror or exc}"}).encode(),
                       "application/json")
            return
        finally:
            CONVERT_LOCK.release()
        body = {
            "ok": not result["failed"],
            "dry_run": False,
            "output_root": str(CONFIG.output_root),
            "renamed": len(result["moved"]),
            "failed_count": len(result["failed"]),
            "skipped_count": len(result["skipped"]),
            **result,
        }
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
        forwarded = (self.headers.get("X-Forwarded-Host") or "").split(",") if self._proxy_trusted() else []
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
        # B10: the path segment, never a bare prefix - /api/units is another route's name
        if path == "/api/unit" or path.startswith("/api/unit/"):
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
        if path == "/api/wishlist/requeue":
            self._handle_wishlist_requeue()
            return
        if path == "/api/wishlist/title":
            self._handle_wishlist_title()
            return
        if path in WATCHER_TEST_ROUTES:
            self._handle_watcher_test(WATCHER_TEST_ROUTES[path])
            return
        if path == HARDCOVER_TEST_ROUTE:
            self._handle_hardcover_test()
            return
        if path == WATCHER_PRESET_ROUTE:
            self._handle_watcher_preset()
            return
        if path == "/api/watcher":
            self._handle_watcher()
            return
        if path == wizard_mod.API:
            self._handle_wizard()
            return
        if path == "/api/library/abs-layout":
            self._handle_abs_layout()
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
            if column not in SORT_KEYS or direction not in ("asc", "desc"):
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
    auth_mod.SESSIONS.attach(state)
    warn_default_hosts()
    if not CONFIG.auth_token and not state.has_credentials():
        auth_mod.announce_setup_code(state)  # SEC-01: the code /setup asks for
    server = BoundedServer(("0.0.0.0", CONFIG.web_port), Handler, CONFIG.max_connections)
    if CONFIG.web_port == 0:
        CONFIG.web_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    return server
