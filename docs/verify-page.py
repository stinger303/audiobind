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
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="audiobind-verify-")
os.environ.setdefault("STATE_DIR", TMP)
# shown in the ingest row's detail panel; the web server never scans it (only the daemon does)
os.environ.setdefault("INGEST_DIR", "/ingest")
os.environ["WEB_PORT"] = os.environ.get("VERIFY_PORT", "8099")
PORT = os.environ["WEB_PORT"]
# a fresh install with no legacy token: the page must walk through first-run setup
os.environ.pop("AUTH_TOKEN", None)

import json                                                  # noqa: E402
import time                                                  # noqa: E402
import urllib.parse                                          # noqa: E402

from audiobind import catalog, series, web                   # noqa: E402
from audiobind.state import State, UnitState                 # noqa: E402

state = State(pathlib.Path(TMP))
BOOKS = [
    ("Connelly/The Burning Room", "converted", "HIGH", "The Burning Room", "Michael Connelly", 12.3),
    ("Child/61 Hours", "review", "LOW", "61 Hours", "Lee Child", 14.1),
    ('Odd/A "Quoted" Book', "held", "MEDIUM", 'A "Quoted" Book', "Someone & Co", 3.0),
    ("Long/" + "x" * 60, "failed", "NONE", "A Very Long Title " * 4, "Nobody", 44.5),
    # an ingest row: a torrent folder from INGEST_DIR, with a brace in its name on purpose
    ("ingest:Andy Weir - Project Hail Mary [MAM] {B08G9PRS1K}", "review", "LOW", "Project Hail Mary",
     "Andy Weir", 16.2),
]
INGEST_KEY = BOOKS[-1][0]
for path, status, conf, title, author, hours in BOOKS:
    state.save(UnitState(path=path, status=status, confidence=conf, title=title, author=author,
                         hours=hours, files=3, src_mb=420, out_mb=310, chapters=31,
                         output="/output/x.m4b"))
# a series with a stored Audible listing, so /series renders cards and a missing list without
# touching the network (no ASIN on these rows: the library page must stay free of https links)
SERIES_BOOKS = [(n, f"Reacher Book {n}") for n in (1, 2, 3)]
for n, title in SERIES_BOOKS:
    detail = {"match": {"asin": "", "title": title, "authors": ["Lee Child"],
                        "series": "Jack Reacher", "sequence": str(n)}}
    state.save(UnitState(path=f"Lee Child/Jack Reacher/{n}", status="converted", confidence="HIGH",
                         title=title, author="Lee Child", hours=12.0, files=1, detail=json.dumps(detail)))
LISTING = [{"asin": f"B0000000J{n}", "title": ("Personal" if n == 4 else f"Reacher Book {n}"),
            "sequence": str(n), "hours": 12.9, "image": f"https://m.media-amazon.com/images/I/J{n}.jpg"}
           for n in range(1, 6)]
LISTING.append({"asin": "B0NOVELLA1", "title": "High Heat", "sequence": "", "hours": 2.0, "image": ""})
state.series_cache_put(series.group_key("Jack Reacher", "Lee Child"), True,
                       {"series_asin": "B0SERIES01", "title": "Jack Reacher", "entries": LISTING,
                        "fetched_at": time.time()},
                       [(e["asin"], e["image"]) for e in LISTING])
state.heartbeat()

server = web.serve(state)
problems: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(("  ok   " if condition else "  FAIL ") + name + ("" if condition else f"  <- {detail}"))
    if not condition:
        problems.append(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def fetch(path: str, data: bytes | None = None, headers: dict | None = None):
    """(status, headers, body text) without following redirects."""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers=headers or {})
    try:
        with NO_REDIRECT.open(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read().decode()
    except urllib.error.HTTPError as exc:
        out = exc.code, exc.headers, exc.read().decode()
        exc.close()
        return out


def node_check(name: str, page: str, filename: str) -> None:
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    check(f"{name}: exactly one inline script", len(scripts) == 1, f"found {len(scripts)}")
    if scripts and shutil.which("node"):
        js = pathlib.Path(TMP) / filename
        js.write_text(scripts[0], encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        check(f"{name}: inline JS parses (node --check)", proc.returncode == 0, proc.stderr.strip()[:400])


# ---- first run (0.16.0-dev2): no account yet, so everything but /setup goes to /setup
print("first-run setup:")
status, headers, _ = fetch("/")
check("fresh DB: / redirects to /setup", status == 303 and headers.get("Location") == "/setup",
      f"{status} {headers.get('Location')}")
status, headers, _ = fetch("/settings")
check("fresh DB: /settings redirects to /setup", status == 303 and headers.get("Location") == "/setup")
status, _, body = fetch("/api/units")
check("fresh DB: the API is refused", status == 403 and "Connelly" not in body)
status, _, setup_page = fetch("/setup")
check("setup page served", status == 200)
check("setup: username, password and confirm fields",
      all(f'name="{n}"' in setup_page for n in ("username", "password", "confirm")))
check("setup: no unexpanded {placeholders}",
      not re.search(r"\{(styles|setup_error|setup_username|setup_min)\}", setup_page))
check("setup: own component class, numeric refs only",
      'class="ab-setup"' in setup_page and "&#183;" in setup_page and "&middot;" not in setup_page)
check("setup: no scripts, no CDN", "<script" not in setup_page and "http://" not in setup_page
      and "https://" not in setup_page)
status, headers, _ = fetch(
    "/setup",
    urllib.parse.urlencode({"username": "verify", "password": "verify-pass-1", "confirm": "verify-pass-1"}).encode(),
    {"Content-Type": "application/x-www-form-urlencoded"},
)
COOKIE = (headers.get("Set-Cookie") or "").split(";", 1)[0]
check("setup creates the account and signs in", status == 303 and COOKIE.startswith("audiobind_session="),
      f"{status} {COOKIE[:20]}")
status, headers, _ = fetch("/setup", headers={"Cookie": COOKIE})
check("after setup /setup redirects to /", status == 303 and headers.get("Location") == "/")
status, _, login_page = fetch("/login")
check("login page has username + password", 'name="username"' in login_page and 'name="password"' in login_page)
check("login: no unexpanded {placeholders}",
      not re.search(r"\{(styles|login_error|login_username|login_hint)\}", login_page))


def get(path: str) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Cookie": COOKIE})
    return urllib.request.urlopen(req, timeout=10).read().decode()


html = get("/")
print(f"rendered page: {len(html)} bytes")
check("no unexpanded {placeholders}",
      not re.search(r"\{(version|rows|headers|styles|script|colgroup|query|series_item)\}", html))
check("search box present", 'id="ab-search"' in html)
check("sortable headers present", html.count("ab-sort") >= 5)
check("fixed column widths", "<colgroup>" in html)
check("status chips still present", 'class="chip' in html)
check("favicon link intact", "/favicon.svg" in html)
check("dark theme intact", "#08090a" in html)
check("quoted title escaped", "&quot;Quoted&quot;" in html)
check("every book rendered",
      all(t in html for t in ("The Burning Room", "61 Hours", "Quoted", "Very Long Title", "Project Hail Mary")))
# ---- ingest rows (0.16.0): one badge per ingest row in the table and one on its shelf card,
# the container source path in its panel, and Delete marked so it asks the server (which refuses)
badges = html.count('class="ab-ingest-badge"')
check("ingest badge on the row and its shelf card (and nowhere else)", badges == 2, f"found {badges}")
check("ingest badge uses a numeric ref", "&#8675; ingest" in html)
check("ingest detail shows the container source dir",
      'Ingest source (read-only, still seeding): <span class="dim"><code>'
      "/ingest/Andy Weir - Project Hail Mary [MAM] {B08G9PRS1K}</code>" in html)
markers = html.count('data-source="ingest"')
check("ingest Delete controls marked (kebab + panel) and no library row marked", markers == 2, f"found {markers}")
check("detail panels rendered", html.count('class="dd"') == len(BOOKS) + len(SERIES_BOOKS))
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
api_units = {u["path"]: u for u in json.loads(get("/api/units"))["units"]}
check("/api/units carries source per unit",
      api_units[INGEST_KEY]["source"] == "ingest" and api_units["Child/61 Hours"]["source"] == "library")
api_state = json.loads(get("/api/state"))
check("/api/state reports the ingest dir", api_state.get("ingest_dir") == "/ingest" and "counts" in api_state)
status_code, refusal = None, {}
try:
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/unit", headers={"Content-Type": "application/json", "Cookie": COOKIE},
        data=json.dumps({"action": "delete", "path": INGEST_KEY, "confirm": True}).encode()), timeout=10)
except urllib.error.HTTPError as exc:
    status_code, refusal = exc.code, json.loads(exc.read() or b"{}")
    exc.close()
check("Delete on an ingest row is refused",
      status_code == 409 and "remove the torrent in qBittorrent" in refusal.get("error", "")
      and state.get(INGEST_KEY) is not None)
filtered = get("/?q=connelly")
table_body = re.search(r'<tbody id="ab-body">(.*?)</tbody>', filtered, re.S)
check("?q= narrows server-side",
      "Burning Room" in (table_body.group(1) if table_body else "") and
      (not table_body or "61 Hours" not in table_body.group(1)))

# ---- the Series page: same served-bytes checks, plus the approved copy and the deep link
print("series page:")
check("rail has a Series tab under Library with the missing badge",
      re.search(r'</span> Library.*?href="/series">\s*<span class="ab-glyph">&#9878;</span> Series\s*'
                r'<span class="ab-count"[^>]*>2</span>.*?</span> Settings', html, re.S) is not None)
check("library detail panel carries the series hook",
      "Jack Reacher &mdash; you're 3 of 5" in html and 'href="/series#jack-reacher"' in html)
page = get("/series")
check("series: no unexpanded {placeholders}",
      not re.search(r"\{(version|styles|series_script|series_item|body|rail_total)\}", page))
check("series: Series tab is the active one", 'ab-active ab-alert" href="/series" aria-current="page"' in page)
check("series: card shows owned of total", 'of 5 owned' in page and "2 MISSING" in page)
check("series: missing list heading", "Missing from Jack Reacher &mdash; 2 books" in page)
check("series: row copy", "Not in your library" in page and "I have this" in page
      and "Copy Audible link" in page and "next gap: #4 Personal" in page)
check("series: unnumbered entries hidden behind a toggle", '<details class="ab-series-extra">' in page)
check("series: deep-link target present", 'id="ab-series-jack-reacher"' in page)
check("series: covers go through the local proxy", '/cover?asin=B0000000J4' in page)
external = set(re.findall(r"https?://[^\"' <>]+", page))
check("series: the only external URLs are Audible book links",
      all(u.startswith("https://www.audible.com/pd?asin=") for u in external), str(sorted(external))[:300])
check("series: no external scripts or stylesheets", "<script src" not in page and 'rel="stylesheet"' not in page)
sscript = re.findall(r"<script>(.*?)</script>", page, re.S)
check("series: exactly one inline script", len(sscript) == 1, f"found {len(sscript)}")
if sscript and shutil.which("node"):
    js = pathlib.Path(TMP) / "series.js"
    js.write_text(sscript[0], encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    check("series: inline JS parses (node --check)", proc.returncode == 0, proc.stderr.strip()[:400])
api = json.loads(get("/api/series"))
check("/api/series responds", api.get("missing_total") == 2 and api["series"][0]["slug"] == "jack-reacher")

# ---- Discover (0.17.0-dev1): the catalog is faked in-process - this script never searches Audible
print("discover page:")
FAKE_RESULTS = [
    catalog.Product(asin="B00LIBRARY", title="The Burning Room", authors=["Michael Connelly"],
                    narrators=["Titus Welliver"], hours=10.4, year="2014",
                    image="https://m.media-amazon.com/images/I/burning.jpg"),
    catalog.Product(asin="B00NEWBOOK", title="The Late Show", authors=["Michael Connelly"],
                    narrators=["Katherine Moennig"], series="Renée Ballard", sequence="1", hours=10.2,
                    year="2017", image="https://m.media-amazon.com/images/I/late.jpg"),
]
catalog.query = lambda **kw: list(FAKE_RESULTS)
check("rail has a Discover tab between Series and Settings",
      re.search(r'</span> Series.*?href="/discover">\s*<span class="ab-glyph">&#8981;</span> Discover.*?</span> Settings',
                html, re.S) is not None)
disc = get("/discover")
check("discover: no unexpanded {placeholders}",
      not re.search(r"\{(version|styles|discover_script|discover_item|series_item|disc_tabs|disc_body)\}", disc))
check("discover: Discover tab is the active one", 'ab-active" href="/discover" aria-current="page"' in disc)
check("discover: title and author fields, no results before a search",
      'name="title"' in disc and 'name="author"' in disc and 'class="ab-disc-row"' not in disc)
check("discover: no CDN references", "http://" not in disc and "https://" not in disc)
node_check("discover", disc, "discover.js")
found = get("/discover?title=connelly")
check("discover: results rendered", "2 results for &#8220;connelly&#8221;" in found)
owned_row = re.search(r'data-asin="B00LIBRARY">.*?(?=<div class="ab-disc-row"|</section>)', found, re.S)
check("discover: a book already in the library is marked, with no Get this",
      owned_row is not None and "In your library" in owned_row.group(0) and "Get this" not in owned_row.group(0))
check("discover: other books get a Get this button", "abWant(this,'search')\">Get this</button>" in found)
check("discover: covers go through the local proxy, no external URLs",
      "/cover?asin=B00NEWBOOK" in found and "https://" not in found)
status, _, added = fetch("/api/wishlist", json.dumps({"asin": "B00NEWBOOK", "origin": "search"}).encode(),
                         {"Content-Type": "application/json", "Cookie": COOKIE})
check("Get this adds to the wishlist", status == 200 and json.loads(added).get("added") is True)
wish = get("/discover/wishlist")
check("wishlist tab lists it with Remove", "The Late Show" in wish and "abUnwant(this)" in wish)
check("rail shows the wishlist count", 'id="ab-rail-wanted" title="books on your wishlist">1</span>' in wish)
node_check("wishlist", wish, "wishlist.js")
check("/api/wishlist responds", json.loads(get("/api/wishlist")).get("count") == 1)

# ---- Settings > Account (0.16.0-dev2)
print("settings page:")
settings_page = get("/settings")
check("settings: no unexpanded {placeholders}",
      not re.search(r"\{(styles|controls|settings_script|account_section|account_script|acct_user|acct_min)\}",
                    settings_page))
check("settings: Account section names the signed-in user",
      'class="ab-acct"' in settings_page and '<b id="ab-acct-user">verify</b>' in settings_page)
check("settings: change-password and change-username forms",
      'data-acct="password"' in settings_page and 'data-acct="username"' in settings_page
      and settings_page.count('name="current"') == 2)
check("settings: reset-password hint", "docker exec -it audiobind audiobind reset-password" in settings_page)
check("settings: sign-out link", 'href="/logout"' in settings_page)
check("settings: no CDN references", "http://" not in settings_page and "https://" not in settings_page)
node_check("settings", settings_page, "settings.js")
creds = state.get_credentials()
served = html + settings_page + get("/api/state") + get("/api/units")
check("no password, hash or salt in any served page",
      "verify-pass-1" not in served and creds.password_hash not in served and creds.salt not in served)

server.shutdown()
state.close()
shutil.rmtree(TMP, ignore_errors=True)
print("\nFAILURES:", problems if problems else "none")
sys.exit(1 if problems else 0)
