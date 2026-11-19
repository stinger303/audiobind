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

from audiobind import catalog, config, discover, series, web  # noqa: E402
from audiobind.config import CONFIG                          # noqa: E402
from audiobind.state import State, UnitState                 # noqa: E402

# Discover / Series / wishlist checks below are the watcher surfaces, which only render once the
# tracker settings are complete. The watcher section turns it off again to prove an install
# without a tracker shows none of them.
TRACKER_SETTINGS = {"tracker_url": "https://tracker.example", "tracker_search_path": "/search.json",
                    "tracker_search_body": "{\"q\": \"{{title}} {{author}}\"}",
                    "tracker_download_path": "/download.php", "tracker_cookie_name": "session_cookie"}
for _name, _value in TRACKER_SETTINGS.items():
    setattr(CONFIG, _name, _value)
CONFIG.tracker_session = "VERIFY-SESSION"

state = State(pathlib.Path(TMP))
BOOKS = [
    ("Connelly/The Burning Room", "converted", "HIGH", "The Burning Room", "Michael Connelly", 12.3),
    ("Child/61 Hours", "review", "LOW", "61 Hours", "Lee Child", 14.1),
    ('Odd/A "Quoted" Book', "held", "MEDIUM", 'A "Quoted" Book', "Someone & Co", 3.0),
    ("Long/" + "x" * 60, "failed", "NONE", "A Very Long Title " * 4, "Nobody", 44.5),
    # an ingest row: a torrent folder from INGEST_DIR, with a brace in its name on purpose
    ("ingest:Andy Weir - Project Hail Mary [TRACKER] {B08G9PRS1K}", "review", "LOW", "Project Hail Mary",
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
# ---- 0.21.0 "Needs you": the default view is the triage list, grouped; the shelf is gone
check("no Needs-attention shelf (markup, script or styles)",
      "ab-shelf" not in html and "abShelfInit" not in html and "SHELF_KEY" not in html)
check("Needs your review chip is the default, marked on",
      '<a class="chip on ab-alert" href="/?status=attention">Needs your review <span class="n">4</span></a>' in html)
check("Needs your review rows are grouped by reason",
      all(f'<span class="ab-group-t">{t}</span>' in html for t in ("Failed", "Held", "Review"))
      and 'class="ab-why"' in html and "ab-breason" not in html)
check("default view leaves converted books out", "The Burning Room" not in html.split('<tbody id="ab-body">')[1])
age_rule = re.search(r"\.ab-st-age\{([^}]*)\}", html)
label_rule = re.search(r"\.ab-st-label\{([^}]*)\}", html)
check(".ab-st-age is not the ellipsized element; the label is",
      age_rule is not None and "ellipsis" not in age_rule.group(1) and "flex:none" in age_rule.group(1)
      and label_rule is not None and "text-overflow:ellipsis" in label_rule.group(1))
foot = html.split('<div class="ab-rail-foot">')[1].split("</div>")[0]
check("rail-foot path breaks at slashes (&#8203;), titles stay plain",
      "/&#8203;" in foot and all("&#8203;" not in t for t in re.findall(r'title="([^"]*)"', foot)))
all_html = get("/?status=all")
# 0.21.0-dev4 column budget: Book is the one column without a width, so it gets what the
# others leave; Match is a fixed 150 px (a meter and one word). A change that hands the slack
# back to Match fails here. Below 1360 px the stylesheet narrows Why; 9 px is the scrollbar.
colgroup = html.split("<colgroup>")[1].split("</colgroup>")[0]
cols = re.findall(r'<col(?: class="([^"]*)")? style="width:([^"]+)">', colgroup)
narrow_why = re.search(r"col\.ab-col-why\{width:([^!;}]+)", html)
check("Match is a fixed 150 px column", ("ab-col-tier", "150px") in cols)
check("Book has no width (bare <col>), so it absorbs the room", '<col style="width:50px"><col><col class="ab-col-why"' in colgroup)


def _px(width: str, viewport: int) -> float:
    found = re.fullmatch(r"min\((\d+)px,(\d+)vw\)", width)
    return min(float(found.group(1)), viewport * int(found.group(2)) / 100) if found else float(width[:-2])


book_at = {}
for vp in (1280, 1440):
    widths = [narrow_why.group(1) if (cls == "ab-col-why" and vp < 1360 and narrow_why) else w for cls, w in cols]
    book_at[vp] = vp - 212 - 1 - 48 - 9 - sum(_px(w, vp) for w in widths)
check("column budget at 1440: Book gets >= 380 px", book_at[1440] >= 380, f"{book_at[1440]:.0f} px")
check("column budget at 1280: Book keeps a usable width (>= 300 px)", book_at[1280] >= 300, f"{book_at[1280]:.0f} px")
check("the Match word never shrinks, the meter does",
      re.search(r"\.ab-match-word\{[^}]*flex:none", html) is not None
      and re.search(r"\.ab-mmeter\{[^}]*flex:0 1 auto;overflow:hidden;min-width:0", html) is not None)
match_cells = re.findall(r'<td class="ab-hidemd ab-match"[^>]*>(.*?)</td>', all_html)
check("every Match cell with a meter carries its word",
      all(re.search(r'<span class="ab-match-word ab-conf-\w+">(High|Medium|Low)</span></span>$', c)
          for c in match_cells if "ab-mmeter" in c) and any("ab-mmeter" in c for c in match_cells),
      str([c for c in match_cells if "ab-mmeter" in c][:2])[:300])
check("?status=all: flat table, every book rendered", 'class="ab-group"' not in all_html and
      all(t in all_html for t in ("The Burning Room", "61 Hours", "Quoted", "Very Long Title", "Project Hail Mary")))
check("?status=all: no unexpanded {placeholders}",
      not re.search(r"\{(version|rows|headers|styles|script|colgroup|query|series_item|allclear)\}", all_html))
# ---- ingest rows (0.16.0): one badge per ingest row in the table (no shelf card since 0.21.0),
# the container source path in its panel, and Delete marked so it opens the remove-from-Audiobind modal
badges = html.count('class="ab-ingest-badge"')
check("ingest badge on the row (and nowhere else)", badges == 1, f"found {badges}")
check("ingest badge uses a numeric ref", "&#8675; ingest" in html)
check("ingest detail shows the container source dir",
      'Ingest source (read-only, still seeding): <span class="dim"><code>'
      "/ingest/Andy Weir - Project Hail Mary [TRACKER] {B08G9PRS1K}</code>" in html)
markers = html.count('data-source="ingest"')
check("ingest Delete controls marked (kebab + panel) and no library row marked", markers == 2, f"found {markers}")
check("detail panels rendered", all_html.count('class="dd"') == len(BOOKS) + len(SERIES_BOOKS)
      and html.count('class="dd"') == 4)
check("no CDN references", "http://" not in html + all_html and "https://" not in html + all_html)

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
def post_unit(payload: dict) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/unit", headers={"Content-Type": "application/json", "Cookie": COOKIE},
                data=json.dumps(payload).encode()), timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        answer = json.loads(exc.read() or b"{}")
        exc.close()
        return exc.code, answer


# 0.19.0-dev13: an ingest Delete removes only the converted copy and forgets (dismisses) the row;
# the torrent keeps seeding. Unconfirmed it changes nothing; confirmed it runs on a throwaway row
# so the rest of this script still sees the seeded ingest book.
status_code, answer = post_unit({"action": "delete", "path": INGEST_KEY})
check("Delete on an ingest row needs confirmation",
      status_code == 400 and state.get(INGEST_KEY) is not None and not state.is_dismissed(INGEST_KEY))
THROWAWAY = "ingest:Throwaway Torrent [TRACKER]"
state.save(UnitState(path=THROWAWAY, status="review", confidence="LOW", title="Throwaway", source="ingest"))
status_code, answer = post_unit({"action": "delete", "path": THROWAWAY, "confirm": True})
check("Delete on an ingest row forgets it and keeps the torrent seeding",
      status_code == 200 and answer.get("dismissed") is True and answer.get("source_kept") is True
      and "still seeding in qBittorrent" in answer.get("source_kept_reason", "")
      and state.get(THROWAWAY) is None and state.is_dismissed(THROWAWAY), f"{status_code} {answer}")
state.undismiss(THROWAWAY)
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
      "Jack Reacher &mdash; you're 3 of 5" in all_html and 'href="/series#jack-reacher"' in all_html)
page = get("/series")
check("series: no unexpanded {placeholders}",
      not re.search(r"\{(version|styles|series_script|series_item|body|rail_total)\}", page))
check("series: Series tab is the active one", 'ab-active" href="/series" aria-current="page"' in page
      and 'ab-active ab-alert" href="/series"' not in page)
check("series: card shows owned of total", 'of 5 owned' in page and "2 MISSING" in page)
check("series: missing list heading", "Missing from Jack Reacher &mdash; 2 books" in page)
check("series: row copy", "Not in your library" in page and "I have this" in page
      and "Copy Audible link" in page and "next gap: #4 Personal" in page)
check("series: unnumbered entries hidden behind a toggle", '<details class="ab-series-extra">' in page)
check("series: deep-link target present", 'id="ab-series-jack-reacher"' in page)
check("series: covers go through the local proxy", '/cover?asin=B0000000J4' in page)
external = set(re.findall(r"https?://[^\"' <>]+", page))
check("series: the only external URLs are Audible book links",
      all(u.startswith("https://www.audible.com/pd/") for u in external), str(sorted(external))[:300])
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

# ---- For you (0.17.0-dev1): suggestions from owned authors; again a faked catalog
print("for you page:")
FAKE_BY_AUTHOR = {
    "Lee Child": [catalog.Product(asin="B00MAKEME1", title="Make Me", authors=["Lee Child"],
                                  narrators=["Dick Hill"], hours=13.1, year="2015",
                                  image="https://m.media-amazon.com/images/I/makeme.jpg")],
    # one owned (title + author) and one already wishlisted: neither may be suggested
    "Michael Connelly": list(FAKE_RESULTS),
}
recs_calls: list[dict] = []


def fake_recs_query(**kw):
    recs_calls.append(kw)
    return list(FAKE_BY_AUTHOR.get(kw.get("author") or "", []))


catalog.query = fake_recs_query
before_refresh = get("/discover/for-you")
check("for you: no unexpanded {placeholders}",
      not re.search(r"\{(version|styles|discover_script|discover_item|series_item|disc_tabs|disc_body)\}",
                    before_refresh))
check("for you: Discover shows search and recommendations together",
      'name="title"' in before_refresh and 'id="ab-recs"' in before_refresh and ">For you</a>" not in before_refresh)
check("for you: a page load never queries the catalog", recs_calls == [])
check("for you: Refresh button and status bar", 'id="ab-recs"' in before_refresh
      and 'onclick="abRecsRefresh(true)">Refresh</button>' in before_refresh)
status, _, queued = fetch("/api/discover/recommendations", json.dumps({"action": "refresh", "force": True}).encode(),
                          {"Content-Type": "application/json", "Cookie": COOKIE})
check("for you: Refresh queues a background refresh", status == 200 and json.loads(queued).get("queued") is True)
discover.RECS_REFRESHER.join(10)
check("for you: one refresh stays within the call cap", 0 < len(recs_calls) <= discover.MAX_CALLS,
      f"{len(recs_calls)} calls")
recs_page = get("/discover/for-you")
check("for you: section and seed headings", "<h3>More by authors you own</h3>" in recs_page
      and "<h4>Lee Child<span>you own 4</span></h4>" in recs_page)
check("for you: a suggestion with Get this",
      'data-asin="B00MAKEME1"' in recs_page and "abWant(this,'for_you')\">Get this</button>" in recs_page)
check("for you: owned and wishlisted books are not suggested",
      'data-asin="B00LIBRARY"' not in recs_page and 'data-asin="B00NEWBOOK"' not in recs_page)
check("for you: nothing was added on its own", json.loads(get("/api/wishlist")).get("count") == 1)
check("for you: no CDN references", "http://" not in recs_page and "https://" not in recs_page)
node_check("for you", recs_page, "foryou.js")
recs_api = json.loads(get("/api/discover/recommendations"))
check("/api/discover/recommendations responds", recs_api.get("fetched_at") and recs_api.get("stale") is False)

# ---- series completion (0.17.0-dev1): Want / Want all missing on the Series page
print("series completion:")
spage = get("/series")
smissing = re.search(r'<section class="ab-series-panel" id="ab-series-missing-jack-reacher".*?</section>', spage, re.S)
smissing = smissing.group(0) if smissing else ""
check("series: card names the source of the total",
      '<div class="ab-series-src">total from the Audible catalog &#183; checked just now</div>' in spage)
check("series: each missing row has a Want button", smissing.count('onclick="abSeriesWant(this)">Want</button>') == 2)
check("series: one Want all missing button",
      'data-series="jack-reacher"' in smissing and '>Want all 2 missing</button>' in smissing)
check("series: missing rows show sequence and runtime",
      '<span class="ab-series-seq">4</span>' in smissing and "12.9 hrs" in smissing)
node_check("series completion", spage, "series-want.js")
before_all = json.loads(get("/api/wishlist")).get("count")
status, _, wanted_all = fetch("/api/wishlist", json.dumps({"series": "jack-reacher", "missing": "all"}).encode(),
                              {"Content-Type": "application/json", "Cookie": COOKIE})
wanted_all = json.loads(wanted_all or "{}")
check("Want all missing adds every missing entry",
      status == 200 and wanted_all.get("added") == ["B0000000J4", "B0000000J5"]
      and wanted_all.get("count") == before_all + 2)
spage = get("/series")
check("series: after Want all, the panel says they are all wanted",
      "All 2 on your wishlist" in spage and 'onclick="abSeriesWantAll(this)"' not in spage)
check("series: the wishlist lists them from the series", "Personal" in get("/discover/wishlist"))

# ---- Settings (0.21.0-dev5: four tabs; each check reads the tab it belongs to, inside its visible panel)
print("settings page:")


def active_panel(page: str) -> str:
    """The one tab panel the page shows (the others are in the page, hidden, for the one Save)."""
    found = re.search(r'<div class="ab-tabpanel" id="ab-tab-(\w+)" data-tab="\w+">', page)
    if not found:
        return ""
    rest = page[found.start():]
    end = re.search(r'<div class="ab-tabpanel" id=|<div class="ab-editbar"|</section>\s*</main>', rest[1:])
    return rest[: end.start() + 1] if end else rest


settings_page = get("/settings")
library_tab = active_panel(settings_page)
account_page = get("/settings?tab=account")
account_tab = active_panel(account_page)
advanced_tab = active_panel(get("/settings?tab=advanced"))
tab_bar = settings_page.split('<nav class="chips ab-tabs" aria-label="Settings sections">')[1].split("</nav>")[0]
check("settings: the tab bar lists Library, Watcher, Account, Advanced in order, Library marked on by default",
      re.findall(r'href="/settings\?tab=(\w+)"', tab_bar) == ["library", "watcher", "account", "advanced"]
      and '<a class="chip on" href="/settings?tab=library" aria-current="page">Library</a>' in tab_bar
      and tab_bar.count(" on\"") == 1, tab_bar[:300])
check("settings: ?tab=account marks Account and shows its panel",
      '<a class="chip on" href="/settings?tab=account" aria-current="page">Account</a>' in account_page
      and 'id="ab-tab-account" data-tab="account">' in account_page)
check("settings: an unknown tab opens Library", 'id="ab-tab-library" data-tab="library">' in get("/settings?tab=nonsense"))
check("settings: no unexpanded {placeholders}",
      not re.search(r"\{(styles|settings_tabs|library_panel|watcher_panel|advanced_panel|account_panel|savebar_hidden|"
                    r"settings_script|account_script|acct_user|acct_min)\}", settings_page + account_page))
check("settings: one form, one Save settings, every setting in it exactly once",
      settings_page.count(">Save settings</button>") == 1
      and settings_page.count('<button class="ab-save" type="submit">Save settings</button>') == 1
      and (lambda form: sorted(re.findall(r'data-setting="(\w+)"', form)) == sorted(set(re.findall(r'data-setting="(\w+)"', form)))
           and len(re.findall(r'data-setting="', form))
           == len(re.findall(r'data-setting="', re.sub(r"<script>.*?</script>", "", settings_page, flags=re.S))))(
          settings_page.split('<form id="ab-settings-form"')[1].split("</form>")[0]))
check("settings: Account tab names the signed-in user",
      'class="ab-acct"' in account_tab and '<b id="ab-acct-user">verify</b>' in account_tab)
check("settings: change-password and change-username forms",
      'data-acct="password"' in account_tab and 'data-acct="username"' in account_tab
      and account_tab.count('name="current"') == 2)
check("settings: reset-password hint", "docker exec -it audiobind audiobind reset-password" in account_tab)
check("settings: sign-out is a POST form to /logout", 'method="post" action="/logout"' in account_tab)
check("settings: the Save bar is hidden on the Account tab (nothing to save there)",
      '<div class="ab-editbar" style="margin-top:8px;" hidden>' in account_page)
check("settings: output format select with the three choices and the player note",
      'data-setting="output_format"' in library_tab and '<option value="M4B + MP3"' in library_tab
      and '<option value="MP3"' in library_tab and "Audiobookshelf" in library_tab)
check("settings: Advanced holds the region and the container-only list (the egress pin is on Watcher)",
      all(t in advanced_tab for t in ('data-setting="region"', "<code>TRACKER_PROXY</code>",
                                      "<code>TRACKER_HOME_IP</code>"))
      and 'data-setting="expected_egress"' not in advanced_tab
      and 'data-setting="output_format"' not in advanced_tab and 'data-setting="qbit_url"' not in advanced_tab)
check("settings: the egress pin is on the Watcher tab",
      'data-setting="expected_egress"' in active_panel(get("/settings?tab=watcher")))
# 0.21.0-dev5: the Watcher fields hold typed URLs (the tracker, qBittorrent), so "no CDN" means what it
# guards: nothing is loaded from another host - no external src/href, CSS url() or @import
EXTERNAL_LOAD = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://|@import""", re.I)
check("settings: no CDN references", not EXTERNAL_LOAD.search(settings_page))
node_check("settings", settings_page, "settings.js")
check("settings: Library card with the layout warning, Preview, Apply and the confirm modal",
      'id="library"' in library_tab and "Match Audiobookshelf layout" in library_tab
      and "may treat a renamed folder as a new book and lose its listening progress" in library_tab
      and 'onclick="abAbsPreview()"' in library_tab and 'onclick="abAbsAsk()"' in library_tab
      and 'id="ab-modal"' in settings_page)

# ---- the wishlist watcher (0.18.0-dev1): state chips, Retry, Settings > Watcher. The web server
# never starts the watcher (only the daemon does) and the tracker client factory is replaced, so
# nothing here can reach a tracker or qBittorrent.
print("wishlist watcher:")
from audiobind import watcher                                # noqa: E402

watcher.make_tracker = lambda: (_ for _ in ()).throw(AssertionError("verify-page must not build a tracker client"))
watcher.make_qbit = lambda: (_ for _ in ()).throw(AssertionError("verify-page must not build a qBittorrent client"))
CONFIG.tracker_session = ""
check("watcher: off without a tracker session", not watcher.configured() and not watcher.WATCHER.alive)
off_settings = get("/settings?tab=watcher")
check("watcher: Settings section, off, is one short block",
      'id="watcher"' in off_settings and "Acquisition source (optional)" in off_settings
      and all(f"<code>{v}</code>" in off_settings for v in ("TRACKER_SESSION", "QBIT_URL", "QBIT_USER", "QBIT_PASS"))
      and 'id="ab-watch-run"' not in off_settings and 'id="ab-watch-toggle"' not in off_settings)
notracker_pages = get("/") + get("/discover") + get("/discover/for-you") + get("/discover/wishlist") + get("/series")
check("no tracker: no wishlist tab, rail count, Get this or Want anywhere",
      "/discover/wishlist" not in notracker_pages and 'id="ab-rail-wanted"' not in notracker_pages
      and ">Get this</button>" not in notracker_pages and ">Want</button>" not in notracker_pages
      and 'onclick="abSeriesWantAll(' not in notracker_pages and 'onclick="abSeriesWant(' not in notracker_pages)
check("no tracker: the ingest folder is said to work either way",
      "converted whether or not the watcher is configured" in get("/"))
status, _, refused = fetch("/api/watcher", json.dumps({"action": "run"}).encode(),
                           {"Content-Type": "application/json", "Cookie": COOKIE})
check("Run a cycle now is refused while the watcher is off", status == 409 and "TRACKER_SESSION" in refused)
CONFIG.tracker_session = "VERIFY-SESSION"
wl_rows = [("B0VERWANT1", "wanted", ""), ("B0VERDOWN1", "downloading", "sent to qBittorrent: #1 'x'"),
           ("B0VERDONE1", "downloaded", "downloaded - waiting for the converted book"),
           ("B0VERLIB01", "in_library", "in your library"), ("B0VERNF001", "not_found", "not found on the tracker in 3 searches"),
           ("B0VERERR01", "error", 'download of tracker torrent #9 failed <"boom">')]
for asin, wl_status, wl_note in wl_rows:
    state.wishlist_add({"asin": asin, "title": f"Verify {wl_status}", "authors": ["V. Writer"], "origin": "search"})
    if wl_status == "downloaded":  # only reachable through downloading
        state.wishlist_set_status(asin, "downloading")
    if wl_status != "wanted":
        state.wishlist_set_status(asin, wl_status, note=wl_note, checked=True)
wl_page = get("/discover/wishlist")
check("wishlist: one chip per state", all(f'class="ab-disc-pill ab-wl-{s}"' in wl_page for _, s, _ in wl_rows))
check("wishlist: downloaded row shows the DOWNLOADED badge and its note, no Retry",
      ">DOWNLOADED</span>" in wl_page and "downloaded - waiting for the converted book" in wl_page)
check("wishlist: Retry only on not found / error", wl_page.count('onclick="abRetry(this)">Retry</button>') == 2)
check("wishlist: notes escaped", 'failed &lt;"boom"&gt;' in wl_page and '<"boom">' not in wl_page)
check("wishlist: last checked shown", "never checked" in wl_page and "checked just now" in wl_page)
check("wishlist: rail item present with the tracker configured", 'href="/wishlist"' in get("/discover"))
check("wishlist: no unexpanded {placeholders}",
      not re.search(r"\{(version|styles|discover_script|discover_item|series_item|disc_tabs|disc_body)\}", wl_page))
check("wishlist: no external URLs", "http://" not in wl_page and "https://" not in wl_page)
node_check("wishlist watcher", wl_page, "wishlist-watcher.js")
status, _, retried = fetch("/api/wishlist/retry", json.dumps({"asin": "B0VERERR01"}).encode(),
                           {"Content-Type": "application/json", "Cookie": COOKIE})
check("Retry puts an error row back to wanted", status == 200 and json.loads(retried)["item"]["status"] == "wanted")
CONFIG.tracker_session, CONFIG.qbit_user, CONFIG.qbit_pass = "VERIFY-SECRET-COOKIE", "admin", "VERIFY-SECRET-PASS"
on_settings = get("/settings?tab=watcher")
check("watcher: enabled section with Run and Pause", '<span class="ok">Enabled</span>' in on_settings
      and 'onclick="abWatcher(\'run\')" title=' in on_settings and "Pause watcher" in on_settings)
status, _, paused = fetch("/api/watcher", json.dumps({"action": "pause"}).encode(),
                          {"Content-Type": "application/json", "Cookie": COOKIE})
check("Pause is persisted", status == 200 and state.get_meta(watcher.META_PAUSED) is True)
paused_settings = get("/settings?tab=watcher")
check("watcher: paused section offers Resume", "Resume watcher" in paused_settings)
watcher_served = on_settings + paused_settings + get("/api/watcher") + get("/discover/wishlist")
check("watcher: the session and QBIT_PASS are never served",
      "VERIFY-SECRET-COOKIE" not in watcher_served and "VERIFY-SECRET-PASS" not in watcher_served)

# ---- 0.21.0-dev5: the watcher's connection settings on the page
print("watcher connection settings:")
watch_block = on_settings[on_settings.index('<section class="ab-acct ab-watch" id="watcher"'):]
watch_block = watch_block[:watch_block.index("</section>")]
check("watcher: Where it looks / How often it looks / Test the connection, no filled button in the panel",
      all(t in on_settings for t in ("Where it looks", "How often it looks", "Test the connection"))
      and all(f'data-setting="{k}"' in watch_block for k in config.WATCHER_SETTING_KEYS)
      and watch_block.count('class="ab-save"') == 0, f"{watch_block.count('class=\"ab-save\"')} filled buttons in the panel")
check("watcher: the secrets are empty password boxes that say a value is set",
      watch_block.count('type="password" class="ab-in" value="" data-initial="" placeholder="set - leave blank to keep it"') == 2
      and watch_block.count("data-clear") == 2,
      str(re.findall(r'<input type="password"[^>]*>', watch_block))[:300])
check("watcher: proxy and home IP are named as container-only",
      "<code>TRACKER_PROXY</code> and <code>TRACKER_HOME_IP</code>" in on_settings)


def post_json(path: str, payload: dict):
    return fetch(path, json.dumps(payload).encode(), {"Content-Type": "application/json", "Cookie": COOKIE})


status, _, saved = post_json("/api/settings", {"settings": {"tracker_session": "VERIFY-GUI-SESSION",
                                                            "qbit_pass": "VERIFY GUI PASS", "qbit_category": "books"}})
check("saving a session and a password from the page", status == 200
      and config.setting_effective("tracker_session") == "VERIFY-GUI-SESSION"
      and config.setting_effective("qbit_pass") == "VERIFY GUI PASS", f"{status} {saved}")
status, _, reset_answer = post_json("/api/settings", {"reset": True})
gui_served = get("/settings") + get("/api/state") + get("/api/watcher") + saved + reset_answer
check("the secrets saved from the page are never served (page, /api/state, /api/watcher, save and reset answers)",
      not any(secret in gui_served for secret in ("VERIFY-GUI-SESSION", "VERIFY GUI PASS", "VERIFY-SECRET-COOKIE",
                                                  "VERIFY-SECRET-PASS")))
check("Reset to defaults leaves the connection settings alone",
      config.setting_effective("tracker_session") == "VERIFY-GUI-SESSION" and config.is_custom("qbit_category"))
status, _, _ = post_json("/api/settings", {"settings": {"tracker_session": ""}})
check("a blank secret keeps the saved one", status == 200 and config.setting_effective("tracker_session") == "VERIFY-GUI-SESSION")
status, _, bad = post_json("/api/settings", {"settings": {"tracker_url": "not a url", "watch_interval": "often"}})
check("a bad URL and a non-number are refused with a plain message",
      status == 400 and "Tracker URL:" in bad and "Check the wishlist every:" in bad, f"{status} {bad}")
status, _, _ = post_json("/api/settings", {"reset_keys": ["tracker_session", "qbit_pass", "qbit_category"]})
check("Reset to container value hands each back", status == 200
      and config.setting_effective("tracker_session") == "VERIFY-SECRET-COOKIE" and not config.is_custom("qbit_category"))
for route in ("/api/watcher/test-qbit", "/api/watcher/test-tracker"):
    status, headers, _ = fetch(route, headers={"Cookie": COOKIE})
    check(f"{route}: GET is refused (POST only)", status == 405 and headers.get("Allow") == "POST", str(status))
    status, _, _ = fetch(route, b"{}", {"Content-Type": "application/json"})
    check(f"{route}: needs a signed-in session", status in (401, 403), str(status))
    status, _, answer = post_json(route, {})
    check(f"{route}: fails closed with a plain message when the target cannot be reached",
          status == 502 and json.loads(answer).get("ok") is False and json.loads(answer).get("message"), f"{status} {answer}")
check("watcher settings: no unexpanded {placeholders}",
      not re.search(r"\{(watcher_section|watcher_script|account_section)\}", on_settings))
check("watcher settings: no CDN references", not EXTERNAL_LOAD.search(on_settings))
node_check("watcher settings", on_settings, "watcher-settings.js")
CONFIG.tracker_session, CONFIG.qbit_user, CONFIG.qbit_pass = "", "", ""

creds = state.get_credentials()
served = html + settings_page + get("/api/state") + get("/api/units")
check("no password, hash or salt in any served page",
      "verify-pass-1" not in served and creds.password_hash not in served and creds.salt not in served)

server.shutdown()
state.close()
shutil.rmtree(TMP, ignore_errors=True)
print("\nFAILURES:", problems if problems else "none")
sys.exit(1 if problems else 0)
