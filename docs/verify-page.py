#!/usr/bin/env python3
"""Boot the real web server against a throwaway DB, GET the page, check the rendered HTML/JS.

This is the "verify what the browser actually receives" step: the unit tests check the render
functions, this checks the served bytes - no unexpanded ``{placeholders}``, no CDN references,
and an inline script that ``node --check`` accepts.

Run: python3 docs/verify-page.py
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="audiobind-verify-")
os.environ.setdefault("STATE_DIR", TMP)
os.environ["WEB_PORT"] = os.environ.get("VERIFY_PORT", "8099")
PORT = os.environ["WEB_PORT"]

from audiobind import web                                    # noqa: E402
from audiobind.state import State, UnitState                 # noqa: E402

state = State(pathlib.Path(TMP))
BOOKS = [
    ("Connelly/The Burning Room", "converted", "HIGH", "The Burning Room", "Michael Connelly", 12.3),
    ("Child/61 Hours", "review", "LOW", "61 Hours", "Lee Child", 14.1),
    ('Odd/A "Quoted" Book', "held", "MEDIUM", 'A "Quoted" Book', "Someone & Co", 3.0),
    ("Long/" + "x" * 60, "failed", "NONE", "A Very Long Title " * 4, "Nobody", 44.5),
]
for path, status, conf, title, author, hours in BOOKS:
    state.save(UnitState(path=path, status=status, confidence=conf, title=title, author=author,
                         hours=hours, files=3, src_mb=420, out_mb=310, chapters=31,
                         output="/output/x.m4b"))
state.heartbeat()

server = web.serve(state)


def get(path: str) -> str:
    return urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10).read().decode()


html = get("/")
problems: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + name + ("" if condition else f"  <- {detail}"))
    if not condition:
        problems.append(name)


print(f"rendered page: {len(html)} bytes")
check("no unexpanded {placeholders}",
      not re.search(r"\{(version|rows|headers|styles|script|colgroup|query)\}", html))
check("search box present", 'id="ab-search"' in html)
check("sortable headers present", html.count("ab-sort") >= 5)
check("fixed column widths", "<colgroup>" in html and "table-layout:fixed" in html)
check("status chips still present", 'class="chip' in html)
check("favicon link intact", "/favicon.svg" in html)
check("dark theme intact", "#0f1115" in html)
check("quoted title escaped", "&quot;Quoted&quot;" in html)
check("every book rendered",
      all(t in html for t in ("The Burning Room", "61 Hours", "Quoted", "Very Long Title")))
check("detail panels rendered", html.count('class="dd"') == len(BOOKS))
check("no CDN references", "http://" not in html and "https://" not in html)

script = re.findall(r"<script>(.*?)</script>", html, re.S)
check("exactly one inline script", len(script) == 1, f"found {len(script)}")
if script and shutil.which("node"):
    js = pathlib.Path(TMP) / "page.js"
    js.write_text(script[0], encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    check("inline JS parses (node --check)", proc.returncode == 0, proc.stderr.strip()[:400])
elif script:
    print("  skip  node not installed - JS not parsed")

check("/api/state responds", '"counts"' in get("/api/state"))
units = get("/api/units?status=review")
check("/api/units filter works", '"61 Hours"' in units and "Burning" not in units)
filtered = get("/?q=connelly")
check("?q= narrows server-side", "Burning Room" in filtered and "61 Hours</td>" not in filtered)

server.shutdown()
state.close()
shutil.rmtree(TMP, ignore_errors=True)
print("\nFAILURES:", problems if problems else "none")
sys.exit(1 if problems else 0)
