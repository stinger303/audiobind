"""The match bench: replay a labelled corpus through the real matcher against a frozen catalog.

Deterministic and offline. Every catalog answer comes from ``catalog.json`` (real Audible and
Audnex responses, trimmed to what matching reads) or, for the synthetic adversarial cases, from a
small rule-based catalog model (``adversarial.json``). Nothing here changes how the app matches:
the bench builds each unit with the scanner's own naming code, calls ``matching.match_unit`` and
layers on the two pipeline steps that decide a tier (the ingest embedded-tag retry and the
COLLECTION tier) using the pipeline's own functions. It only *observes*: two module functions
(``_score_reading``, ``_pick_and_margin``) are wrapped to record the candidate pool and ranking.

Stages, each with its own verdict in the scoreboard:
  A  corpus admitted   every row graded or skipped with a reason; every row labelled or declared
                       unlabelled; no catalog lookup missing from the frozen cache
  B  retrieval recall  was a labelled edition in the candidate pool the matcher built
  C  pick accuracy     given the pool, did it pick a labelled edition
  D  grade safety      wrong answers by tier; wrong-at-HIGH outside known_defects.csv fails
  E  explanation       the grade agrees with the evidence the panel shows (reason invariants)
and a diff against ``baseline.csv``: any decision change fails until accepted with a reason.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from audiobind import __version__, catalog, ingest, matching, pipeline, units  # noqa: E402
from audiobind.catalog import Product  # noqa: E402
from audiobind.units import AUDIO_EXT, BookUnit, Part, looks_like_collection  # noqa: E402
from audiobind.util import norm  # noqa: E402

CORPUS = HERE / "corpus.csv"
LABELS = HERE / "labels.csv"
KNOWN_DEFECTS = HERE / "known_defects.csv"
BASELINE = HERE / "baseline.csv"
ADVERSARIAL = HERE / "adversarial.json"
#: the frozen catalog: one file for the bench and the older docs/ tooling
CATALOG = REPO / "docs" / "match-catalog-cache.json"

LIBRARY_ROOT = Path("/library")
INGEST_ROOT = Path("/ingest")
REGION = "us"

TIER_RANK = {"NONE": 0, "LOW": 1, "COLLECTION": 1, "MEDIUM": 2, "HIGH": 3}
#: label sources, strongest first. owner: labels.csv, the owner said so. pin: the owner hand-set
#: this ASIN in the app. review: a reviewer's reading of titles, waiting for the owner.
#: db-approved: converted at MEDIUM/LOW, so a person clicked Convert (AUTO_CONVERT=high).
#: db-auto: converted at HIGH with no person involved - the matcher's own past answer.
SOURCES = ("owner", "pin", "review", "db-approved", "db-auto")
#: the label groups the scoreboard reports separately
GROUPS = {"owner": "human", "pin": "human", "db-approved": "human", "review": "review", "db-auto": "auto"}

CORPUS_FIELDS = [
    "key", "source", "shape", "name", "files", "parts", "hours", "status", "db_tier", "db_asin", "pin",
    "rec_title", "rec_author", "name_source", "tag_author", "tag_title",
]
LABEL_FIELDS = ["key", "expect", "source", "classes", "note"]
RESULT_FIELDS = [
    "key", "label", "src", "pick", "tier", "in_pool", "rank", "verdict", "reasons",
    "t_sim", "a_sim", "margin", "coverage", "pool", "flags", "title", "accepted_because",
]
#: a change in any of these is a decision change: it fails the gate until accepted
DECISION_FIELDS = ("label", "pick", "tier", "in_pool", "rank", "verdict", "reasons")
#: these only drift; reported, never gated
SCORE_FIELDS = ("t_sim", "a_sim", "margin", "coverage", "pool")

#: grade()'s HIGH gates for a known author, mirrored so a non-HIGH grade can be explained.
#: test_matchbench checks this mirror against grade() itself on a grid of evidence.
HIGH_TITLE, HIGH_AUTHOR, HIGH_MARGIN = 0.88, 0.85, 0.05


# ------------------------------------------------------------------------------- csv helpers
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if not (row.get("key") or "").startswith("#")]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------- frozen catalog
#: the raw Audible product keys Product.from_audible reads (images are not kept)
_PRODUCT_KEYS = (
    "asin", "title", "subtitle", "authors", "narrators", "series", "runtime_length_min",
    "language", "release_date", "publisher_name",
)
#: the Audnex book keys matching reads
_BOOK_KEYS = (
    "asin", "title", "subtitle", "authors", "narrators", "seriesPrimary", "runtimeLengthMin",
    "language", "releaseDate", "publisherName",
)


def trim(url: str, payload):
    """Keep only what matching reads, so the committed cache stays small and reviewable."""
    if not isinstance(payload, dict):
        return payload
    if url.startswith(catalog.AUDIBLE):
        products = [{k: raw[k] for k in _PRODUCT_KEYS if k in raw}
                    for raw in payload.get("products") or [] if isinstance(raw, dict)]
        return {"products": products}
    if "/chapters" not in url:
        return {k: payload[k] for k in _BOOK_KEYS if k in payload}
    return payload


class FrozenCatalog:
    """Stands in for catalog._get_json. Offline: a miss is recorded and answered with None.
    ``fetch`` (only ever from the refresh command) fetches a miss once and keeps it."""

    def __init__(self, data: dict, fetch=None):
        self.data = data
        self.fetch = fetch
        self.used: set[str] = set()
        self.misses: list[str] = []
        self.added: list[str] = []

    def __call__(self, url: str, *args, **kwargs):
        if url in self.data:
            self.used.add(url)
            return self.data[url]
        if self.fetch is None:
            if url not in self.misses:
                self.misses.append(url)
            return None
        payload = trim(url, self.fetch(url))
        self.data[url] = payload
        self.added.append(url)
        self.used.add(url)
        return payload


def load_catalog(path: Path = CATALOG) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_catalog(data: dict, path: Path = CATALOG) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=0, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _no_network(*_a, **_k):
    raise RuntimeError("the match bench is offline: something tried to reach the network")


@contextlib.contextmanager
def patched(target, **attrs):
    old = {name: getattr(target, name) for name in attrs}
    try:
        for name, value in attrs.items():
            setattr(target, name, value)
        yield
    finally:
        for name, value in old.items():
            setattr(target, name, value)


@contextlib.contextmanager
def sealed(get_json):
    """Every catalog read goes to ``get_json``; urlopen raises; the region is pinned."""
    import urllib.request

    real_effective = catalog.effective_val
    catalog.cache_clear()
    with patched(catalog, _get_json=get_json,
                 effective_val=lambda name: REGION if name == "region" else real_effective(name)), \
            patched(urllib.request, urlopen=_no_network):
        yield
    catalog.cache_clear()


# --------------------------------------------------------------------------- catalog model
def _tokens(text: str) -> set[str]:
    return set(norm(text).split())


class ModelCatalog:
    """A deterministic stand-in for Audible's search, for the synthetic cases only.

    ``title=`` matches when every query word is in the product title (Audible's title filter
    is strict: "Private 20 - Private Dublin" finds nothing); ``author=`` when every query word
    is in one author's name (a misspelled author finds nothing); ``keywords=`` when every word
    is somewhere in title, subtitle, authors or series. Results keep catalog order. A case may
    pin exact answers for a query instead (``answers``), e.g. a catalog that returns one weak hit.
    """

    def __init__(self, products: list[dict], answers: dict | None = None, audnex: dict | None = None):
        self.raw = products
        self.answers = answers or {}
        self.audnex = audnex or {}

    @staticmethod
    def query_key(**query) -> str:
        return json.dumps({k: v for k, v in sorted(query.items()) if v and k != "limit"}, sort_keys=True)

    def search(self, title=None, author=None, limit=10, keywords=None):
        key = self.query_key(title=title, author=author, keywords=keywords)
        if key in self.answers:
            return [Product.from_audible(p) for p in self.answers[key]][:limit]
        found = []
        for raw in self.raw:
            names = [a.get("name", "") for a in raw.get("authors") or []]
            if title and not _tokens(title) <= _tokens(raw.get("title", "")):
                continue
            if author and not any(_tokens(author) <= _tokens(n) for n in names):
                continue
            if keywords:
                series = " ".join(s.get("title", "") for s in raw.get("series") or [])
                hay = _tokens(" ".join([raw.get("title", ""), raw.get("subtitle", ""), series] + names))
                if not _tokens(keywords) <= hay:
                    continue
            found.append(Product.from_audible(raw))
        return found[:limit]

    def book(self, asin):
        return dict(self.audnex.get(asin) or {})


# ------------------------------------------------------------------------------ the corpus
@dataclass
class Label:
    kind: str  # "asin" | "none" | "not" | "unlabelled"
    asins: tuple[str, ...] = ()
    src: str = ""
    classes: tuple[str, ...] = ()
    note: str = ""

    def text(self) -> str:
        if self.kind == "asin":
            return "|".join(self.asins)
        if self.kind == "not":
            return "not:" + "|".join(self.asins)
        return self.kind


def parse_expect(expect: str) -> tuple[str, tuple[str, ...]]:
    expect = (expect or "").strip()
    if not expect:
        return "", ()
    if expect in ("none", "unlabelled"):
        return expect, ()
    if expect.startswith("not:"):
        return "not", tuple(a.strip() for a in expect[4:].split("|") if a.strip())
    return "asin", tuple(a.strip() for a in expect.split("|") if a.strip())


def db_label(row: dict) -> Label | None:
    """The label a state-DB row carries on its own, or None."""
    if row.get("pin"):
        return Label("asin", (row["pin"],), "pin")
    if row.get("status") == "converted" and row.get("db_asin"):
        src = "db-auto" if row.get("db_tier") == "HIGH" else "db-approved"
        return Label("asin", (row["db_asin"],), src)
    return None


def resolve_labels(corpus: list[dict], label_rows: list[dict]) -> tuple[dict[str, Label], list[str]]:
    """Each corpus key's effective label, and the admission problems (orphans, conflicts, gaps)."""
    problems: list[str] = []
    keys = {row["key"] for row in corpus}
    by_key: dict[str, dict] = {}
    for row in label_rows:
        key = row["key"]
        if key in by_key:
            problems.append(f"labels.csv: {key!r} is listed twice")
        by_key[key] = row
        if key not in keys:
            problems.append(f"labels.csv: {key!r} is not in the corpus (orphan label)")
        kind, _ = parse_expect(row.get("expect", ""))
        src = (row.get("source") or "").strip()
        if kind and kind != "unlabelled" and src not in ("owner", "review"):
            problems.append(f"labels.csv: {key!r} has source {src!r}; a written label is 'owner' or 'review'")
    labels: dict[str, Label] = {}
    for row in corpus:
        key = row["key"]
        auto = db_label(row)
        extra = by_key.get(key, {})
        classes = tuple(c for c in (extra.get("classes") or "").split(";") if c)
        note = extra.get("note") or ""
        kind, asins = parse_expect(extra.get("expect", ""))
        src = (extra.get("source") or "").strip()
        chosen = auto
        if kind == "unlabelled":
            if auto:
                problems.append(f"labels.csv: {key!r} is declared unlabelled but the DB labels it ({auto.src})")
            else:
                chosen = Label("unlabelled")
        elif kind:
            written = Label(kind, asins, src)
            if auto is None or SOURCES.index(src) < SOURCES.index(auto.src):
                chosen = written
        if chosen is None:
            problems.append(f"{key!r} has no label: add it to labels.csv (a label, or 'unlabelled' + why)")
            chosen = Label("unlabelled")
        chosen.classes, chosen.note = classes, note
        labels[key] = chosen
    return labels, problems


# --------------------------------------------------------------------------- rebuilding units
def _fake_files(base: Path, n: int) -> list[str]:
    return [str(base / f"{i:03d}.mp3") for i in range(1, max(n, 1) + 1)]


def rebuild_unit(row: dict) -> tuple[BookUnit | None, str, list[str]]:
    """The BookUnit the scanner would name for this corpus row: (unit, why not, flags)."""
    key, flags = row["key"], []
    n_files = int(row.get("files") or 1)
    n_parts = max(int(row.get("parts") or 1), 1)
    ingest_row = key.startswith(units.RESERVED_KEY_PREFIX)
    root = INGEST_ROOT if ingest_row else LIBRARY_ROOT
    rel = key[len(units.RESERVED_KEY_PREFIX):] if ingest_row else key
    if not ingest_row and rel.startswith("./" + units.RESERVED_KEY_PREFIX):
        rel = rel[2:]
    shape = row.get("shape") or "folder"
    bucket_key = ""
    if shape == "bucket":
        rel, bucket_key = rel.rsplit("#", 1)
        flags.append("approx-name")
    path = root / rel
    if shape == "file":
        parts = [Part(name=path.name, files=[str(path)])]
        unit = BookUnit(path=str(path), name=os.path.splitext(path.name)[0], parent=str(path.parent), parts=parts)
    else:
        per = [n_files // n_parts + (1 if i < n_files % n_parts else 0) for i in range(n_parts)]
        parts = [Part(name=f"Part {i + 1}" if n_parts > 1 else path.name,
                      files=_fake_files(path / f"p{i + 1}", per[i])) for i in range(n_parts)]
        name = (row.get("name") or bucket_key) if shape == "bucket" else path.name
        unit = BookUnit(path=str(path), name=name, parent=str(path.parent), parts=parts, key=bucket_key)
    units._name_units([unit], root, ingest=ingest_row)
    rebuilt = (units.RESERVED_KEY_PREFIX if ingest_row else "") + unit.rel_path
    if rebuilt != key:
        return None, f"rebuilt key {rebuilt!r} differs from the row's", flags
    if ingest_row:
        unit.source = "ingest"
    return unit, "", flags


# ---------------------------------------------------------------------------- observation
class Capture:
    """Records, per match_unit call, the candidate pool and the final ranking. Observes only."""

    def __init__(self):
        self.pool: list[str] = []
        self.ranked: list[str] = []
        self.counted_rivals = 0

    @contextlib.contextmanager
    def watching(self):
        real_score, real_pick = matching._score_reading, matching._pick_and_margin

        def score_reading(*args, **kwargs):
            scored = real_score(*args, **kwargs)
            for asin in scored:
                if asin not in self.pool:
                    self.pool.append(asin)
            return scored

        def pick_and_margin(ranked):
            best, margin = real_pick(ranked)
            self.ranked = [e["product"].asin for e in ranked]
            self.ranked.remove(best["product"].asin)
            self.ranked.insert(0, best["product"].asin)
            self.counted_rivals = sum(
                1 for e in ranked if e is not best and not e["knockoff"]
                and not matching._same_work(e["product"], best["product"]))
            return best, margin

        with patched(matching, _score_reading=score_reading, _pick_and_margin=pick_and_margin):
            yield self


def limiting_reasons(match: matching.Match) -> list[str]:
    """Why ``match`` is not HIGH, from the evidence the detail panel shows ([] = nothing stops it)."""
    if not match.product:
        if match.rejected:
            return ["rejected"]
        return ["pin-failed"] if match.asin_pin_failed else ["no-candidates"]
    reasons = []
    if match.knockoff:
        reasons.append("knockoff")
    if matching.language_penalized(match.language):
        reasons.append("foreign")
    if match.unasked_adaptation:
        reasons.append("adaptation")
    if match.title_window_coverage < matching.MIN_HIGH_WINDOW_COVERAGE:
        reasons.append("window-only")
    if not match.author_known:
        reasons.append("no-author")
    else:
        if match.title_sim < HIGH_TITLE:
            reasons.append("title")
        if match.author_sim < HIGH_AUTHOR:
            reasons.append("author")
        if match.margin <= 0:
            reasons.append("tie")
        elif match.margin < HIGH_MARGIN:
            reasons.append("margin")
    return reasons


def explanation_problems(match: matching.Match, tier: str, reasons: list[str], pool: list[str]) -> list[str]:
    """Stage E: the grade must agree with the reasons a person would be shown."""
    out = []
    if tier == "HIGH" and reasons:
        out.append(f"HIGH despite {','.join(reasons)}")
    if tier in ("MEDIUM", "LOW") and match.product and not reasons:
        out.append(f"{tier} with no reason shown")
    if match.confidence == "NONE" and match.product:
        out.append("NONE with a product")
    if match.confidence == "NONE" and not match.product and pool and not match.rejected and not match.asin_pin_failed:
        out.append("NONE with candidates but no rejection note")
    if match.knockoff and TIER_RANK[tier] > TIER_RANK["LOW"]:
        out.append(f"knockoff at {tier}")
    if ("foreign" in reasons or "adaptation" in reasons) and tier == "HIGH":
        out.append("capped production at HIGH")
    return out


# ------------------------------------------------------------------------------- evaluation
@dataclass
class Outcome:
    key: str
    label: Label
    unit: BookUnit | None = None
    match: matching.Match | None = None
    tier: str = "NONE"
    pool: list[str] = field(default_factory=list)
    ranked: list[str] = field(default_factory=list)
    counted_rivals: int = 0
    reasons: list[str] = field(default_factory=list)
    explain: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    skipped: str = ""
    expect_problems: list[str] = field(default_factory=list)  # adversarial expectations only

    @property
    def pick(self) -> str:
        return self.match.asin if self.match else ""

    @property
    def in_pool(self) -> bool:
        return self.label.kind == "asin" and any(a in self.pool for a in self.label.asins)

    @property
    def rank(self) -> int:
        if self.label.kind != "asin":
            return 0
        hits = [i for i, a in enumerate(self.ranked) if a in self.label.asins]
        return hits[0] + 1 if hits else 0

    @property
    def verdict(self) -> str:
        kind, pick = self.label.kind, self.pick
        if self.skipped:
            return "skipped"
        if kind == "asin":
            return "right" if pick in self.label.asins else ("wrong" if pick else "missed")
        if kind == "none":
            return "wrong" if pick and TIER_RANK[self.tier] >= TIER_RANK["MEDIUM"] else "right"
        if kind == "not":
            return "wrong" if pick in self.label.asins else "unknown"
        return "unlabelled"


def run_unit(unit: BookUnit, hours: float, tags: tuple[str, str] | None) -> tuple[matching.Match, Capture, list[str]]:
    """match_unit plus the pipeline's ingest retry, as audit_unit runs them (no files read)."""
    flags: list[str] = []
    with Capture().watching() as cap:
        match = matching.match_unit(unit, local_hours=hours)
    if unit.source == "ingest" and match.confidence in ("NONE", "LOW"):
        if tags is None:
            flags.append("retry-unreproducible")
        else:
            with Capture().watching() as retry_cap, patched(ingest, embedded_names=lambda _u: tags):
                retry = pipeline._match_embedded(unit, hours)
            if retry is not None and pipeline._embedded_is_better(retry, match):
                match, cap = retry, retry_cap
                flags.append("embedded-tags")
    return match, cap, flags


def evaluate_row(row: dict, label: Label) -> Outcome:
    out = Outcome(key=row["key"], label=label)
    unit, why, flags = rebuild_unit(row)
    out.flags = flags
    if unit is None:
        out.skipped = why
        return out
    hours = float(row.get("hours") or 0)
    tags = None
    if row.get("tag_author") or row.get("tag_title"):
        tags = (row.get("tag_author") or "", row.get("tag_title") or "")
    match, cap, run_flags = run_unit(unit, hours, tags)
    out.flags += run_flags
    _finish(out, unit, match, cap, hours)
    if row.get("pin"):
        out.flags.append("pinned-in-app")
    rec = row.get("rec_title") or ""
    if rec and row.get("name_source") == "folder" and "embedded-tags" not in out.flags and rec != match.query_title:
        out.flags.append("reading-drift")
    return out


def _finish(out: Outcome, unit: BookUnit, match: matching.Match, cap: Capture, hours: float) -> None:
    out.unit, out.match = unit, match
    out.pool, out.ranked, out.counted_rivals = list(cap.pool), list(cap.ranked), cap.counted_rivals
    tier = match.confidence
    avg = hours / len(unit.files) if unit.files else 0
    if tier in ("LOW", "NONE") and looks_like_collection(unit, avg):
        tier = "COLLECTION"
    out.tier = tier
    out.reasons = limiting_reasons(match)
    out.explain = explanation_problems(match, match.confidence, out.reasons, out.pool)
    if match.product and tier == "HIGH" and out.counted_rivals == 0:
        out.flags.append("lone-high")
    if len(out.pool) == 1:
        out.flags.append("one-candidate")


def evaluate_corpus(corpus: list[dict], labels: dict[str, Label]) -> list[Outcome]:
    return [evaluate_row(row, labels[row["key"]]) for row in sorted(corpus, key=lambda r: r["key"])]


# ---------------------------------------------------------------------- adversarial cases
def load_adversarial(path: Path = ADVERSARIAL) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def case_label(case: dict) -> Label:
    exp = case.get("expect") or {}
    classes = tuple(case.get("classes") or ())
    if exp.get("none"):
        return Label("none", (), "spec", classes, case.get("why", ""))
    if exp.get("asin"):
        asins = exp["asin"] if isinstance(exp["asin"], list) else [exp["asin"]]
        return Label("asin", tuple(asins), "spec", classes, case.get("why", ""))
    if exp.get("asin_not"):
        asins = exp["asin_not"] if isinstance(exp["asin_not"], list) else [exp["asin_not"]]
        return Label("not", tuple(asins), "spec", classes, case.get("why", ""))
    return Label("unlabelled", (), "spec", classes, case.get("why", ""))


def case_unit(case: dict) -> BookUnit:
    ingest_case = bool(case.get("ingest"))
    root = INGEST_ROOT if ingest_case else LIBRARY_ROOT
    path = root / case["folder"]
    n = int(case.get("files") or 1)
    if case.get("file"):
        unit = BookUnit(path=str(path), name=os.path.splitext(path.name)[0], parent=str(path.parent),
                        parts=[Part(name=path.name, files=[str(path)])])
    else:
        unit = BookUnit(path=str(path), name=path.name, parent=str(path.parent),
                        parts=[Part(name=path.name, files=_fake_files(path, n))])
    units._name_units([unit], root, ingest=ingest_case)
    if ingest_case:
        unit.source = "ingest"
    return unit


def evaluate_case(case: dict, products: dict) -> Outcome:
    label = case_label(case)
    out = Outcome(key="adv:" + case["name"], label=label)
    raws = [products[k] for k in case.get("catalog") or []]
    answers = {ModelCatalog.query_key(**json.loads(q)): [products[k] for k in keys]
               for q, keys in (case.get("answers") or {}).items()}
    model = ModelCatalog(raws, answers, {a: r for a, r in (case.get("audnex") or {}).items()})
    unit = case_unit(case)
    hours = float(case.get("hours") or 0)
    tags = tuple(case["tags"]) if case.get("tags") else None
    with patched(catalog, search=model.search, book=model.book):
        match, cap, flags = run_unit(unit, hours, tags)
    out.flags = flags
    _finish(out, unit, match, cap, hours)
    exp = case.get("expect") or {}
    problems = []
    # a wrong pick a person reviews anyway (LOW and below) is allowed unless the case says otherwise
    if out.verdict == "wrong" and TIER_RANK[out.tier] > TIER_RANK[exp.get("wrong_max_tier", "LOW")]:
        problems.append(f"picked {out.pick} at {out.tier}")
    if exp.get("must_pick") and out.verdict != "right":
        problems.append(f"picked {out.pick or 'nothing'}, want {label.text()}")
    if "tier" in exp and out.tier != exp["tier"]:
        problems.append(f"tier {out.tier}, want {exp['tier']}")
    if "max_tier" in exp and TIER_RANK[out.tier] > TIER_RANK[exp["max_tier"]]:
        problems.append(f"tier {out.tier}, want at most {exp['max_tier']}")
    if "min_tier" in exp and TIER_RANK[out.tier] < TIER_RANK[exp["min_tier"]]:
        problems.append(f"tier {out.tier}, want at least {exp['min_tier']}")
    if exp.get("in_pool") and not out.in_pool:
        problems.append("labelled edition never reached the candidate pool")
    out.expect_problems = problems
    return out


def evaluate_adversarial(spec: dict) -> list[Outcome]:
    products = spec["products"]
    return [evaluate_case(case, products) for case in spec["cases"]]


# ------------------------------------------------------------------------------- results
def result_row(out: Outcome) -> dict:
    m = out.match
    return {
        "key": out.key,
        "label": out.label.text(),
        "src": out.label.src,
        "pick": out.pick,
        "tier": out.tier if not out.skipped else "SKIP",
        "in_pool": ("1" if out.in_pool else "0") if out.label.kind == "asin" else "",
        "rank": str(out.rank) if out.label.kind == "asin" else "",
        "verdict": out.verdict,
        "reasons": ";".join(out.reasons) if m else "",
        "t_sim": f"{m.title_sim:.3f}" if m else "",
        "a_sim": f"{m.author_sim:.3f}" if m else "",
        "margin": f"{m.margin:.3f}" if m else "",
        "coverage": f"{m.title_window_coverage:.3f}" if m else "",
        "pool": str(len(out.pool)) if m else "",
        "flags": ";".join(sorted(set(out.flags))),
        "title": (m.product.title if m and m.product else "") or (out.skipped or ""),
    }


@dataclass
class Run:
    corpus: list[Outcome]
    adversarial: list[Outcome]
    admission: list[str]
    misses: list[str]
    cache_size: int
    cache_used: int
    known_defects: dict[str, dict]
    known_gaps: dict[str, str]

    @property
    def outcomes(self) -> list[Outcome]:
        return self.corpus + self.adversarial

    def rows(self) -> list[dict]:
        return [result_row(o) for o in self.outcomes]


def run_bench(catalog_data: dict | None = None, fetch=None) -> tuple[Run, FrozenCatalog]:
    corpus = read_csv(CORPUS)
    labels, problems = resolve_labels(corpus, read_csv(LABELS))
    defects = {row["key"]: row for row in read_csv(KNOWN_DEFECTS)}
    for key in defects:
        if not key.startswith("adv:") and key not in labels:
            problems.append(f"known_defects.csv: {key!r} is not in the corpus")
    spec = load_adversarial()
    frozen = FrozenCatalog(load_catalog() if catalog_data is None else catalog_data, fetch)
    with sealed(frozen):
        real = evaluate_corpus(corpus, labels)
        adv = evaluate_adversarial(spec)
    gaps = {"adv:" + c["name"]: c["known_gap"] for c in spec["cases"] if c.get("known_gap")}
    run = Run(real, adv, problems, list(frozen.misses), len(frozen.data), len(frozen.used), defects, gaps)
    return run, frozen


# --------------------------------------------------------------------------------- scoring
def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.1f}%)" if d else f"{n}/0"


@dataclass
class Score:
    graded: int = 0
    skipped: int = 0
    labelled: int = 0
    asin_labelled: int = 0
    in_pool: int = 0
    at1: int = 0
    at3: int = 0
    at5: int = 0
    right: int = 0
    wrong: Counter = field(default_factory=Counter)
    right_by_tier: Counter = field(default_factory=Counter)
    tiers: Counter = field(default_factory=Counter)
    unlabelled: int = 0

    def add(self, out: Outcome) -> None:
        if out.skipped:
            self.skipped += 1
            return
        self.graded += 1
        self.tiers[out.tier] += 1
        if out.label.kind == "unlabelled":
            self.unlabelled += 1
            return
        self.labelled += 1
        verdict = out.verdict
        if verdict == "wrong":
            self.wrong[out.tier] += 1
        if out.label.kind == "asin":
            self.asin_labelled += 1
            self.in_pool += out.in_pool
            self.at1 += out.rank == 1
            self.at3 += 0 < out.rank <= 3
            self.at5 += 0 < out.rank <= 5
            if verdict == "right":
                self.right += 1
                self.right_by_tier[out.tier] += 1

    @property
    def wrong_high(self) -> int:
        return self.wrong["HIGH"]

    def headline(self) -> dict:
        return {"recall": self.in_pool, "pick": self.right, "n": self.asin_labelled}


def score(outcomes: list[Outcome], pred=lambda o: True) -> Score:
    s = Score()
    for out in outcomes:
        if pred(out):
            s.add(out)
    return s


# ------------------------------------------------------------------------------ the gate
@dataclass
class Gate:
    failures: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    drift: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures


def load_baseline(path: Path = BASELINE) -> dict[str, dict]:
    return {row["key"]: row for row in read_csv(path)}


def describe(row: dict) -> str:
    if not row:
        return "(absent)"
    return (f"{row['tier']} {row['pick'] or '-'} {row['verdict']}"
            f"{' pool' if row.get('in_pool') == '1' else ''}{' rank ' + row['rank'] if row.get('rank') else ''}"
            f" t={row['t_sim'] or '-'} a={row['a_sim'] or '-'} m={row['margin'] or '-'}"
            f"{' [' + row['reasons'] + ']' if row.get('reasons') else ''}")


def compare(rows: list[dict], baseline: dict[str, dict]) -> tuple[list[str], list[str], int]:
    """(decision changes, regressions, score-drift count) of ``rows`` against ``baseline``."""
    changes, regressions, drift = [], [], 0
    now = {r["key"]: r for r in rows}
    for key in sorted(set(now) | set(baseline)):
        new, old = now.get(key, {}), baseline.get(key, {})
        if not old or not new:
            changes.append(f"{key}: {describe(old)} -> {describe(new)}")
            continue
        if any(new[f] != old.get(f, "") for f in DECISION_FIELDS):
            changes.append(f"{key}: {describe(old)} -> {describe(new)}")
            if old.get("in_pool") == "1" and new.get("in_pool") != "1":
                regressions.append(f"{key}: labelled edition left the candidate pool")
            if old.get("verdict") == "right" and new.get("verdict") != "right":
                regressions.append(f"{key}: was right, now {new['verdict']}")
            if old.get("verdict") != "wrong" and new.get("verdict") == "wrong":
                regressions.append(f"{key}: now wrong at {new['tier']}")
        elif any(new[f] != old.get(f, "") for f in SCORE_FIELDS):
            drift += 1
    return changes, regressions, drift


def gate(run: Run, baseline: dict[str, dict]) -> Gate:
    g = Gate()
    # A: admission
    g.failures += [f"A {p}" for p in run.admission]
    if run.misses:
        g.failures.append(f"A {len(run.misses)} catalog lookup(s) not in the frozen cache "
                          "(run `python -m tests.matchbench refresh`, a reviewed act)")
    for out in run.corpus:
        if out.skipped and out.label.kind not in ("unlabelled",):
            pass  # a skipped row is admitted: it is listed with its reason on every run
    # D: wrong at HIGH outside the known defects; every known defect must still reproduce
    for out in run.outcomes:
        listed = run.known_defects.get(out.key)
        if out.verdict == "wrong" and out.tier == "HIGH" and not listed and out.key not in run.known_gaps:
            g.failures.append(f"D wrong at HIGH: {out.key} picked {out.pick} ({out.match.product.title})")
        if listed and not (out.verdict == "wrong" and TIER_RANK[out.tier] >= TIER_RANK[listed.get("tier") or "HIGH"]):
            g.failures.append(f"D {out.key} is in known_defects.csv but no longer reproduces "
                              f"({out.verdict} at {out.tier}): take it out")
    # E: explanation
    for out in run.outcomes:
        for problem in out.explain:
            g.failures.append(f"E {out.key}: {problem}")
    # adversarial expectations; known gaps must keep failing
    for out in run.adversarial:
        gap = run.known_gaps.get(out.key)
        if gap and not out.expect_problems:
            g.failures.append(f"adversarial {out.key} now passes: move it out of known_gap ({gap})")
        if not gap and out.expect_problems:
            g.failures.append(f"adversarial {out.key}: {'; '.join(out.expect_problems)}")
    # B/C + baseline: every decision change must be accepted
    g.changes, g.regressions, g.drift = compare(run.rows(), baseline)
    if g.changes:
        g.failures.append(f"baseline: {len(g.changes)} decision change(s) not accepted "
                          "(`python -m tests.matchbench accept --why ...`)")
    base_score = _baseline_score(baseline)
    now = score(run.corpus)
    for name, cur, old in (("recall", now.in_pool, base_score["recall"]), ("pick", now.right, base_score["pick"])):
        if baseline and cur < old:
            g.failures.append(f"B/C {name} fell from {old} to {cur}")
    return g


def _baseline_score(baseline: dict[str, dict]) -> dict:
    rows = [r for k, r in baseline.items() if not k.startswith("adv:")]
    return {"recall": sum(r.get("in_pool") == "1" for r in rows),
            "pick": sum(r.get("verdict") == "right" and r.get("in_pool") != "" for r in rows)}


# ----------------------------------------------------------------------------- scoreboard
def scoreboard(run: Run, g: Gate, verbose: bool = False) -> str:
    lines: list[str] = []
    w = lines.append
    real = score(run.corpus)
    adv = score(run.adversarial)
    skipped = [o for o in run.corpus if o.skipped]
    unl = [o for o in run.corpus if o.label.kind == "unlabelled" and not o.skipped]
    w(f"MATCH BENCH  audiobind {__version__}  ·  corpus {len(run.corpus)} real rows + "
      f"{len(run.adversarial)} adversarial cases  ·  frozen catalog {run.cache_size} responses "
      f"({run.cache_used} used, {len(run.misses)} missing)")
    w("")
    w(f"A corpus admitted   {real.graded} graded, {real.skipped} skipped, {real.unlabelled} declared unlabelled"
      f"   admission problems: {len(run.admission)}")
    w(f"B retrieval recall  in pool {pct(real.in_pool, real.asin_labelled)}   "
      f"@1 {pct(real.at1, real.asin_labelled)}   @3 {pct(real.at3, real.asin_labelled)}   "
      f"@5 {pct(real.at5, real.asin_labelled)}")
    w(f"C pick accuracy     {pct(real.right, real.asin_labelled)}   given in pool {pct(real.right, real.in_pool)}")
    w(f"D grade safety      wrong@HIGH {real.wrong['HIGH']} (known defects {len(run.known_defects)})   "
      f"wrong@MEDIUM {real.wrong['MEDIUM']}   wrong@LOW/COLL {real.wrong['LOW'] + real.wrong['COLLECTION']}   "
      f"right@HIGH {real.right_by_tier['HIGH']}   right@MEDIUM {real.right_by_tier['MEDIUM']}   "
      f"right but LOW {real.right_by_tier['LOW'] + real.right_by_tier['COLLECTION']}")
    w(f"E explanation       {sum(len(o.explain) for o in run.corpus)} violation(s) on the corpus, "
      f"{sum(len(o.explain) for o in run.adversarial)} on adversarial")
    w("  tiers (real)      " + "  ".join(f"{t} {real.tiers[t]}" for t in ("HIGH", "MEDIUM", "LOW", "COLLECTION", "NONE")))
    flags = Counter(f for o in run.corpus for f in set(o.flags))
    w("  risk flags        " + "  ".join(f"{k} {flags[k]}" for k in (
        "lone-high", "one-candidate", "retry-unreproducible", "reading-drift", "approx-name", "embedded-tags")))
    w("")
    w("  by label source   n  in-pool  right  wrong@H  wrong@M  wrong@L")
    for src in SOURCES:
        s = score(run.corpus, lambda o, src=src: o.label.src == src)
        if s.labelled:
            w(f"    {src:<14}{s.labelled:>4}  {s.in_pool:>7}  {s.right:>5}  {s.wrong['HIGH']:>7}  "
              f"{s.wrong['MEDIUM']:>7}  {s.wrong['LOW'] + s.wrong['COLLECTION']:>7}")
    w("")
    by_class: dict[str, list[Outcome]] = defaultdict(list)
    for o in run.outcomes:
        for c in o.label.classes:
            by_class[c].append(o)
    w("  by failure class  n  real  right  wrong@H  wrong@M  expectation-fails")
    for c in sorted(by_class):
        os_ = by_class[c]
        s = score(os_)
        fails = sum(1 for o in os_ if o.expect_problems)
        nreal = sum(1 for o in os_ if not o.key.startswith("adv:"))
        w(f"    {c:<16}{len(os_):>3}  {nreal:>4}  {s.right:>5}  {s.wrong['HIGH']:>7}  {s.wrong['MEDIUM']:>7}  {fails:>6}")
    w("")
    passed = sum(1 for o in run.adversarial if not o.expect_problems)
    gaps = [o for o in run.adversarial if o.key in run.known_gaps]
    w(f"  adversarial       {passed}/{len(run.adversarial)} meet the spec; known gaps {len(gaps)} "
      f"(still failing: {sum(1 for o in gaps if o.expect_problems)}); wrong@HIGH {adv.wrong['HIGH']}")
    for o in gaps:
        w(f"    gap  {o.key}: {run.known_gaps[o.key]} -> {'; '.join(o.expect_problems) or 'PASSES NOW'}")
    w("")
    if run.known_defects:
        w("  known defects (real corpus, listed in known_defects.csv)")
        for key, d in sorted(run.known_defects.items()):
            w(f"    {key}: {d.get('symptom', '')}")
        w("")
    wrong_med = [o for o in run.corpus if o.verdict == "wrong" and o.tier == "MEDIUM"]
    if wrong_med:
        w("  wrong at MEDIUM (one config change from auto-converting)")
        for o in wrong_med:
            w(f"    {o.key}: picked {o.pick} '{o.match.product.title}' want {o.label.text()} ({o.label.src})")
        w("")
    missed_pool = [o for o in run.corpus if o.label.kind == "asin" and not o.skipped and not o.in_pool]
    if missed_pool:
        w("  retrieval misses (labelled edition never in the pool)")
        for o in missed_pool:
            w(f"    {o.key}: want {o.label.text()} ({o.label.src}); pool {len(o.pool)}, picked {o.pick or '-'} {o.tier}")
        w("")
    if skipped:
        w("  skipped rows")
        for o in skipped:
            w(f"    {o.key}: {o.skipped}")
        w("")
    if verbose and unl:
        w("  declared unlabelled")
        for o in unl:
            w(f"    {o.key}: {o.tier} {o.pick or '-'} - {o.label.note}")
        w("")
    w(f"  baseline          {len(g.changes)} decision change(s), {len(g.regressions)} regression(s), "
      f"{g.drift} score-only drift")
    for line in g.changes[:60]:
        w(f"    change  {line}")
    for line in g.regressions:
        w(f"    REGRESS {line}")
    w("")
    if g.ok:
        w("GATE: PASS")
    else:
        w(f"GATE: FAIL ({len(g.failures)})")
        for f in g.failures:
            w(f"  - {f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- acceptance
def accept(run: Run, why: str, allow_regression: bool = False, path: Path = BASELINE) -> tuple[bool, str]:
    """Write the run as the new baseline. Changed rows carry ``why``; unchanged rows keep theirs."""
    baseline = load_baseline(path)
    g = gate(run, baseline)
    hard = [f for f in g.failures if not f.startswith("baseline:") and not f.startswith("B/C ")]
    if hard:
        return False, "refusing to accept: the run fails gates that acceptance cannot waive:\n  " + "\n  ".join(hard)
    if g.regressions and not allow_regression:
        return False, ("refusing to accept regressions without --allow-regression:\n  "
                       + "\n  ".join(g.regressions))
    if not why.strip() and (g.changes or not baseline):
        return False, "--why is required: it is written beside every changed row"
    rows = run.rows()
    for row in rows:
        old = baseline.get(row["key"])
        changed = not old or any(row[f] != old.get(f, "") for f in DECISION_FIELDS)
        row["accepted_because"] = why if changed else old.get("accepted_because", "")
    write_csv(path, RESULT_FIELDS, rows)
    return True, f"baseline written: {len(rows)} rows, {len(g.changes)} change(s) accepted"


# ------------------------------------------------------------------------- corpus import
def corpus_from_db(db_path: str) -> list[dict]:
    """Corpus rows from a state database (read-only)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = []
    for r in db.execute("SELECT * FROM units WHERE path IS NOT NULL AND path != '' ORDER BY path"):
        detail = {}
        try:
            detail = json.loads(r["detail"] or "{}") or {}
        except ValueError:
            pass
        match = detail.get("match") or {}
        source = detail.get("source") or {}
        key = r["path"]
        ingest_row = key.startswith(units.RESERVED_KEY_PREFIX)
        files = int(source.get("files") or r["files"] or 1)
        ext = os.path.splitext(key)[1].lower()
        if "#" in key and not ingest_row:
            shape = "bucket"
        elif ingest_row and files == 1 and ext in AUDIO_EXT:
            shape = "file"
        else:
            shape = "folder"
        embedded = match.get("name_source") == "embedded"
        rows.append({
            "key": key, "source": "ingest" if ingest_row else "library", "shape": shape, "name": "",
            "files": str(files), "parts": str(int(source.get("parts") or 1)),
            "hours": f"{float(r['hours'] or 0):.2f}", "status": r["status"] or "", "db_tier": r["confidence"] or "",
            "db_asin": r["asin"] or "", "pin": r["asin_override"] or "",
            "rec_title": match.get("query_title") or "", "rec_author": match.get("query_author") or "",
            "name_source": match.get("name_source") or "",
            "tag_author": (match.get("query_author") or "") if embedded else "",
            "tag_title": (match.get("query_title") or "") if embedded else "",
        })
    return rows


def import_db(db_path: str, drop_missing: bool = False) -> str:
    """Merge a state DB into corpus.csv. New rows are added, changed DB facts updated; a hand-set
    ``name``/``shape``/tag column is kept. Rows absent from the DB stay unless ``drop_missing``."""
    old = {r["key"]: r for r in read_csv(CORPUS)}
    new = {r["key"]: r for r in corpus_from_db(db_path)}
    added = sorted(set(new) - set(old))
    missing = sorted(set(old) - set(new))
    merged = []
    for key in sorted(set(old) | set(new)):
        if key in new:
            row = dict(new[key])
            if key in old:
                for keep in ("name", "shape", "tag_author", "tag_title"):
                    if old[key].get(keep):
                        row[keep] = old[key][keep]
            merged.append(row)
        elif not drop_missing:
            merged.append(old[key])
    write_csv(CORPUS, CORPUS_FIELDS, merged)
    lines = [f"corpus: {len(merged)} rows ({len(added)} added, {len(missing)} not in this DB"
             f"{' - dropped' if drop_missing else ' - kept'})"]
    lines += [f"  + {k}" for k in added]
    lines += [f"  ? {k}" for k in missing]
    if added:
        lines.append("new rows need a label (or 'unlabelled') in labels.csv unless the DB labels them")
    return "\n".join(lines)


# ------------------------------------------------------------------------- calibration
def calibrate() -> str:
    """How closely ModelCatalog (the synthetic cases' search) reproduces the real frozen answers.

    Every frozen Audible search is replayed through the model over the products the frozen
    cache knows; real-in-model is the share of real results the model also returns."""
    import urllib.parse

    data = load_catalog()
    universe: dict[str, dict] = {}
    for url, payload in sorted(data.items()):
        for raw in (payload or {}).get("products") or [] if url.startswith(catalog.AUDIBLE) else []:
            universe.setdefault(raw["asin"], raw)
    model = ModelCatalog(list(universe.values()))
    hit = total = extra = exact = n = 0
    for url, payload in sorted(data.items()):
        if not url.startswith(catalog.AUDIBLE) or "?" not in url:
            continue
        q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        real = {p["asin"] for p in (payload or {}).get("products") or []}
        got = {p.asin for p in model.search(title=q.get("title"), author=q.get("author"),
                                             keywords=q.get("keywords"), limit=1000)}
        n += 1
        hit += len(real & got)
        total += len(real)
        extra += len(got - real)
        exact += (not real) == (not got)
    return (f"model vs {n} frozen searches: {pct(hit, total)} of real results also returned by the model; "
            f"{extra} model-only results; empty/non-empty agrees on {pct(exact, n)}")


# ---------------------------------------------------------------------- refresh and drift
def refresh() -> str:
    """Fetch only what the frozen cache is missing (additions only, never an overwrite)."""
    data = load_catalog()
    before = len(data)
    run, frozen = run_bench(data, fetch=_live_get)
    save_catalog(frozen.data)
    lines = [f"catalog: {before} -> {len(frozen.data)} responses ({len(frozen.added)} added)"]
    lines += [f"  + {u}" for u in frozen.added]
    return "\n".join(lines)


def _live_get(url: str):
    """The real catalog fetch, bypassing the bench's seal (refresh and drift only)."""
    import urllib.request

    with patched(urllib.request, urlopen=_REAL_URLOPEN):
        return _REAL_GET_JSON(url)


_REAL_GET_JSON = catalog._get_json
import urllib.request as _ur  # noqa: E402

_REAL_URLOPEN = _ur.urlopen


def drift(out_path: str = "") -> str:
    """Re-fetch every frozen response live and report what would change. Never writes the cache."""
    frozen = load_catalog()
    live, changed, failed = {}, [], []
    for url in sorted(frozen):
        payload = trim(url, _live_get(url))
        if payload is None and frozen[url] is not None:
            failed.append(url)
            live[url] = frozen[url]
            continue
        live[url] = payload
        if payload != frozen[url]:
            changed.append(url)
    run_frozen, _ = run_bench(frozen)
    run_live, live_cat = run_bench(dict(live))
    changes, regressions, _ = compare(run_live.rows(), {r["key"]: r for r in run_frozen.rows()})
    if out_path:
        save_catalog(live, Path(out_path))
    lines = [f"drift: {len(frozen)} responses re-fetched, {len(changed)} changed, {len(failed)} failed "
             f"(kept frozen), {len(live_cat.misses)} new lookups the live answers lead to"]
    lines += [f"  ~ {u}" for u in changed]
    lines += [f"  ! {u}" for u in failed]
    lines.append(f"outcomes that would change against the live catalog: {len(changes)}")
    lines += [f"  {c}" for c in changes]
    lines += [f"  REGRESS {r}" for r in regressions]
    return "\n".join(lines)
