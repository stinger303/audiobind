"""The match bench: replay a labelled corpus through the real matcher against a frozen catalog.

Deterministic and offline. Every catalog answer comes from ``catalog.json`` (real Audible and
Audnex responses, trimmed to what matching reads) or, for the synthetic adversarial cases, from a
small rule-based catalog model (``adversarial.json``). Nothing here changes how the app matches:
the bench builds each unit with the scanner's own naming code, calls ``matching.match_unit`` and
layers on the pipeline steps that decide what happens to a book (the ingest embedded-tag retry, the
COLLECTION tier, and ``choose_action`` with the configured runtime tolerance) using the pipeline's
own functions. It only *observes*: two module functions (``_score_reading``, ``_pick_and_margin``)
are wrapped to record the candidate pool and ranking.

Stages, each with its own verdict in the scoreboard:
  A  corpus admitted   every row has exactly one label line; no catalog lookup missing from the
                       frozen cache, per row (a miss is a bench error on that row, never a NONE)
  L  label evidence    every label carries its provenance and the evidence behind it, recomputed
                       on each run; a label its own evidence contradicts fails the gate
  B  retrieval recall  was a labelled edition in the candidate pool the matcher built
  C  pick accuracy     given the pool, did it pick a labelled edition - reported per label source,
                       and over the trusted sources (owner, pin, db-approved) on their own
  D  grade safety      wrong answers by tier; wrong-at-HIGH outside known_defects.csv fails
  E  explanation       the grade agrees with the evidence the panel shows (reason invariants)
  F  decision          what the pipeline would do: convert / remux / hold / review. A wrong pick
                       that would be written into the library (would_convert_wrong) fails the gate
and a diff against ``baseline.csv``: any decision change fails until accepted with a reason, and a
baseline that disagrees with the corpus about which rows exist fails outright.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from audiobind import __version__, catalog, config, ingest, matching, pipeline, units  # noqa: E402
from audiobind.catalog import Product  # noqa: E402
from audiobind.units import AUDIO_EXT, BookUnit, Part, looks_like_collection  # noqa: E402
from audiobind.util import norm  # noqa: E402

CORPUS = HERE / "corpus.csv"
LABELS = HERE / "labels.csv"
KNOWN_DEFECTS = HERE / "known_defects.csv"
BASELINE = HERE / "baseline.csv"
ADVERSARIAL = HERE / "adversarial.json"
MUTATIONS = HERE / "mutations.csv"
#: the frozen catalog: one file for the bench and the older docs/ tooling
CATALOG = REPO / "docs" / "match-catalog-cache.json"

LIBRARY_ROOT = Path("/library")
INGEST_ROOT = Path("/ingest")
REGION = "us"

TIER_RANK = {"NONE": 0, "LOW": 1, "COLLECTION": 1, "MEDIUM": 2, "HIGH": 3}

# ---------------------------------------------------------------------------- label sources
#: strongest first. owner: the owner confirmed it, in labels.csv. pin: the owner hand-set this ASIN
#: in the app. db-approved: converted at MEDIUM/LOW, so a person pressed Convert (AUTO_CONVERT=high).
#: db-auto: converted at HIGH with nobody looking - the matcher's own past answer. inferred: a
#: reviewer's reading of the evidence columns, never the owner's word.
SOURCES = ("owner", "pin", "db-approved", "db-auto", "inferred")
#: a row the bench runs but never grades, with the reason in its note
OUT_OF_SCOPE = "out-of-scope"
LABEL_SOURCES = SOURCES + (OUT_OF_SCOPE,)
#: the sources a person stands behind: the accuracy quoted as the app's comes from these alone
TRUSTED = ("owner", "pin", "db-approved")
DB_SOURCES = ("pin", "db-approved", "db-auto")

CORPUS_FIELDS = [
    "key", "source", "shape", "name", "files", "parts", "hours", "status", "db_tier", "db_asin", "pin",
    "rec_title", "rec_author", "name_source", "tag_author", "tag_title",
]
#: written by a person
LABEL_HAND_FIELDS = ["key", "expect", "label_source", "classes", "note"]
#: recomputed from corpus.csv and the frozen catalog on every run; a stored value that differs is
#: stale and fails admission (`labels --write` rewrites these columns, never the hand columns)
EVIDENCE_FIELDS = [
    "db_label", "local_h", "label_asin", "label_h", "delta_pct", "runtime",
    "folder_author", "label_author", "author_match", "folder_title", "label_title", "title_match",
    "narrator", "corroborated_by", "evidence",
]
LABEL_FIELDS = LABEL_HAND_FIELDS + EVIDENCE_FIELDS
RESULT_FIELDS = [
    "key", "label", "src", "pick", "tier", "in_pool", "rank", "verdict", "reasons", "decision",
    "t_sim", "a_sim", "margin", "coverage", "pool", "rt_delta", "searches", "flags", "title", "accepted_because",
]
#: a change in any of these is a decision change: it fails the gate until accepted
DECISION_FIELDS = ("label", "pick", "tier", "in_pool", "rank", "verdict", "reasons", "decision")
#: these only drift; reported, never gated. ``searches`` (catalog.search calls per row) is a cost, not an
#: outcome: making every search twice nearly doubles the run's searches (425 -> 822) with every decision
#: identical, which the gate used to report as "0 score-only drift". On the frozen corpus a *new* query
#: already fails (a cache miss is a bench error), but a repeated query and every adversarial case (the
#: model catalog answers anything) were invisible. It is shown, with the run total, and never fails the
#: build: a cost is the owner's trade to make, not a correctness failure.
SCORE_FIELDS = ("t_sim", "a_sim", "margin", "coverage", "pool", "rt_delta", "searches")

#: grade()'s gates, mirrored so a non-HIGH grade can be explained and so ``mutate`` can move the
#: inline literals grade() keeps. test_matchbench checks grade_with(GRADE_GATES) against grade()
#: itself on a grid of evidence, so the mirror cannot quietly fall behind.
GRADE_GATES = {
    "high_title": 0.88, "high_author": 0.85, "high_margin": 0.05,
    "medium_title": 0.85, "medium_author": 0.60,
    "title_only": 0.96, "title_only_margin": 0.10,
}
_ACTIVE_GATES = dict(GRADE_GATES)

# --------------------------------------------------------------------- label-evidence rules
#: an inferred label must agree with the files this closely (the same 5 % the matcher uses to call
#: two catalog records one edition). Owner, pin and DB labels are only *reported* when weaker.
INFERRED_RUNTIME_BAND = 0.05
#: how far a label's own runtime may sit from the files before the label is called contradicted.
#: Deliberately wider than the decision tolerance, because they answer different questions: the
#: decision asks "may this be written unseen?" and must be strict; the label asks "is this edition
#: plausible at all?". The owner's rips are shortened by up to a fifth (a Dick Hill book whose files
#: are 17% shorter than the US edition is still the right book), so a 17% gap is evidence, not a
#: contradiction - tightening the decision tolerance must never invalidate his labels.
LABEL_RUNTIME_TOLERANCE = 0.35
#: share of the label's words found in the folder's words: at CONTRADICTS or below (at most half of
#: them: "Private Dublin" for "Private Beijing") the label contradicts the folder; at CORROBORATES or
#: above it corroborates it. Tokens match fuzzily (difflib >= 0.8,
#: words of 4+ letters) so a misspelling ("Bldacci", "Steeleart") still counts.
MATCH_CONTRADICTS = 0.5
MATCH_CORROBORATES = 0.8
_STOPWORDS = frozenset({"the", "a", "an", "of", "and"})
_AUTHOR_SPLIT = re.compile(r"\s*(?:,|&|;|\band\b|\bwith\b)\s*", re.IGNORECASE)


# ------------------------------------------------------------------------------- csv helpers
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if not (row.get("key") or "").startswith("#")]


def csv_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return next(csv.reader(fh), [])


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
    """Stands in for catalog._get_json. Offline: a miss is recorded (and counted per call, so the
    row that caused it can be named) and answered with None. A stored ``null`` is a real frozen
    answer (Audnex had no record), not a miss. ``fetch`` (only ever from the refresh command)
    fetches a miss once and keeps it."""

    def __init__(self, data: dict, fetch=None):
        self.data = data
        self.fetch = fetch
        self.used: set[str] = set()
        self.misses: list[str] = []
        self.miss_events = 0
        self.added: list[str] = []

    def __call__(self, url: str, *args, **kwargs):
        if url in self.data:
            self.used.add(url)
            return self.data[url]
        if self.fetch is None:
            self.miss_events += 1
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


def catalog_index(data: dict) -> dict[str, Product]:
    """Every product the frozen cache knows, by ASIN (search results first, then Audnex records).
    Read straight from the data: building it never counts as a lookup or a miss."""
    index: dict[str, Product] = {}
    for url in sorted(data):
        payload = data[url]
        if not isinstance(payload, dict):
            continue
        if url.startswith(catalog.AUDIBLE):
            for raw in payload.get("products") or []:
                if isinstance(raw, dict) and raw.get("asin"):
                    index.setdefault(raw["asin"], Product.from_audible(raw))
        elif payload.get("asin") and "/chapters" not in url:
            index.setdefault(payload["asin"], matching._product_from_book(payload))
    return index


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


# ---------------------------------------------------------------------- configured decision
def runtime_tolerance() -> tuple[float, str]:
    """The tolerance the pipeline holds books on, through the pipeline's own path, and its source."""
    tol = float(config.effective_val("max_runtime_delta"))
    if config.is_custom("max_runtime_delta"):
        where = "settings override"
    elif os.environ.get("MAX_RUNTIME_DELTA"):
        where = "MAX_RUNTIME_DELTA env"
    else:
        where = "default"
    return tol, where


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


# ------------------------------------------------------------------------------ the labels
@dataclass
class Label:
    kind: str  # "asin" | "none" | "not" | "out" | "unlabelled" (an admission failure)
    asins: tuple[str, ...] = ()
    src: str = ""
    classes: tuple[str, ...] = ()
    note: str = ""
    #: what the state DB alone says for this row (pin / db-approved / db-auto), or None
    db: "Label | None" = None

    def text(self) -> str:
        if self.kind == "asin":
            return "|".join(self.asins)
        if self.kind == "not":
            return "not:" + "|".join(self.asins)
        if self.kind == "out":
            return OUT_OF_SCOPE
        return self.kind

    @property
    def graded(self) -> bool:
        return self.kind in ("asin", "none", "not")


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
    """Each corpus key's label from its one line in labels.csv, and the structural problems.

    Evidence rules (contradictions, overrides of a DB label) need the run and are checked later,
    in ``check_labels``."""
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
    labels: dict[str, Label] = {}
    for row in corpus:
        key = row["key"]
        db = db_label(row)
        line = by_key.get(key)
        if line is None:
            problems.append(f"{key!r} has no line in labels.csv (`labels --write` adds the rows the DB "
                            "labels; any other row needs a label or out-of-scope with the reason)")
            labels[key] = Label("unlabelled", src="unlabelled", db=db)
            continue
        src = (line.get("label_source") or "").strip()
        kind, asins = parse_expect(line.get("expect", ""))
        classes = tuple(c for c in (line.get("classes") or "").split(";") if c)
        note = (line.get("note") or "").strip()
        if src not in LABEL_SOURCES:
            problems.append(f"labels.csv: {key!r} has label_source {src!r}; one of {', '.join(LABEL_SOURCES)}")
            labels[key] = Label("unlabelled", src="unlabelled", classes=classes, note=note, db=db)
            continue
        if src == OUT_OF_SCOPE:
            if kind:
                problems.append(f"labels.csv: {key!r} is out-of-scope but carries expect {line.get('expect')!r}")
            if not note:
                problems.append(f"labels.csv: {key!r} is out-of-scope without a reason in its note")
            labels[key] = Label("out", (), src, classes, note, db)
            continue
        if kind in ("", "unlabelled"):
            problems.append(f"labels.csv: {key!r} ({src}) has no expect: an ASIN, 'none', 'not:ASIN', "
                            "or make the row out-of-scope with the reason")
            labels[key] = Label("unlabelled", src="unlabelled", classes=classes, note=note, db=db)
            continue
        label = Label(kind, asins, src, classes, note, db)
        if src in DB_SOURCES:
            if db is None or db.src != src or kind != "asin" or asins != db.asins:
                said = f"{db.src} {db.text()}" if db else "nothing"
                problems.append(f"labels.csv: {key!r} says {src} {label.text()} but the DB says {said}")
        elif src == "inferred" and not note:
            problems.append(f"labels.csv: {key!r} is inferred without a note saying what was read")
        if db is not None and db.src == "pin" and src not in ("pin", "owner"):
            problems.append(f"labels.csv: {key!r} overrides the owner's pin {db.text()} as {src}; only "
                            "the owner can")
        labels[key] = label
    return labels, problems


# ---------------------------------------------------------------------------- label evidence
def _num(token: str) -> str:
    return (token.lstrip("0") or "0") if token.isdigit() else token


def _words(text: str) -> list[str]:
    return [_num(t) for t in norm(text).split() if t not in _STOPWORDS]


def _word_found(word: str, hay: set[str]) -> bool:
    if word in hay:
        return True
    if word.isdigit() or len(word) < 4:
        return False
    return any(len(h) >= 4 and SequenceMatcher(None, word, h).ratio() >= 0.8 for h in hay)


def containment(needle: str, hay: str) -> float | None:
    """Share of ``needle``'s words found in ``hay`` (fuzzy, order-free); None when it has none."""
    words = _words(needle)
    if not words:
        return None
    pool = set(_words(hay))
    return sum(_word_found(w, pool) for w in words) / len(words)


def folder_authors(row: dict, unit: BookUnit | None) -> list[str]:
    """Every author name the folder (or its recorded tags) states, split on co-author joins."""
    raw = [row.get("tag_author") or ""]
    if unit is not None:
        raw += [unit.author_guess or "", unit.alt_author_guess or ""]
    names: list[str] = []
    for text in raw:
        for name in _AUTHOR_SPLIT.split(text or ""):
            name = name.strip(" ()-")
            if name and norm(name) and name not in names:
                names.append(name)
    return names


def folder_titles(row: dict, unit: BookUnit | None) -> list[str]:
    out: list[str] = []
    cands = [row.get("tag_title") or ""]
    if unit is not None:
        cands = [unit.title_guess or "", unit.alt_title_guess or ""] + cands
    for text in cands:
        if text and text not in out:
            out.append(text)
    return out


def _label_titles(product: Product) -> list[str]:
    title = product.title or ""
    out = [title, title.split(":")[0], matching.strip_brackets(title)]
    return [t for i, t in enumerate(out) if t and t not in out[:i]]


@dataclass
class Evidence:
    fields: dict
    verdict: str  # corroborated | consistent | contradicted | unverifiable | n/a
    contradictions: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)
    runtime_corroborates: bool = False


def _fmt(x: float | None) -> str:
    return "" if x is None else f"{x:.2f}"


def measure(row: dict, unit: BookUnit | None, product: Product, tolerance: float) -> dict:
    """The evidence tuple for one catalog product against one corpus row."""
    local = float(row.get("hours") or 0)
    cat = float(product.hours or 0)
    delta = None if local <= 0 or cat <= 0 else (local - cat) / cat
    if delta is None:
        runtime = "unknown"
    elif not matching.runtime_ok(local, cat, tolerance):
        runtime = "contradicts"
    elif abs(delta) <= INFERRED_RUNTIME_BAND:
        runtime = "corroborates"
    else:
        runtime = "consistent"
    names = folder_authors(row, unit)
    a_match = None
    if names and product.authors:
        a_match = max(max(containment(a, n) or 0.0, containment(n, a) or 0.0)
                      for a in product.authors for n in names)
    hay = " ".join(([unit.name] if unit is not None else []) + folder_titles(row, unit))
    t_match = max((containment(t, hay) or 0.0 for t in _label_titles(product)), default=0.0)
    narr = "; ".join(product.narrators[:2])
    if narr and unit is not None and any((containment(n, unit.name) or 0) >= 1.0 for n in product.narrators):
        narr += " [named in folder]"
    return {
        "label_asin": product.asin, "label_h": f"{cat:.2f}" if cat > 0 else "",
        "delta_pct": "" if delta is None else f"{round(delta * 100):+d}", "runtime": runtime,
        "folder_author": "; ".join(names), "label_author": "; ".join(product.authors[:3]),
        "author_match": _fmt(a_match), "folder_title": "; ".join(folder_titles(row, unit)),
        "label_title": product.title, "title_match": _fmt(t_match), "narrator": narr,
        "_delta": delta, "_a": a_match, "_t": t_match,
    }


def _contradictions(m: dict, tolerance: float) -> list[str]:
    out = []
    if m["runtime"] == "contradicts":
        out.append(f"runtime: files {m['_local']}h vs {m['label_asin']} {m['label_h']}h ({m['delta_pct']}%, "
                   f"tolerance {tolerance:.0%})")
    if m["_a"] is not None and m["_a"] <= MATCH_CONTRADICTS:
        out.append(f"author: folder '{m['folder_author']}' vs '{m['label_author']}' (match {m['_a']:.2f})")
    if m["_t"] <= MATCH_CONTRADICTS:
        out.append(f"title: folder '{m['folder_title']}' vs '{m['label_title']}' (match {m['_t']:.2f})")
    return out


def _weaknesses(m: dict) -> list[str]:
    out = []
    if m["runtime"] == "consistent":
        out.append(f"runtime {m['delta_pct']}%")
    elif m["runtime"] == "unknown":
        out.append("runtime unknown")
    if m["_a"] is not None and MATCH_CONTRADICTS < m["_a"] < MATCH_CORROBORATES:
        out.append(f"author {m['_a']:.2f}")
    if MATCH_CONTRADICTS < m["_t"] < MATCH_CORROBORATES:
        out.append(f"title {m['_t']:.2f}")
    return out


def label_evidence(row: dict, unit: BookUnit | None, label: Label, index: dict[str, Product],
                   tolerance: float) -> Evidence:
    """The evidence behind ``label`` on ``row``, and whether it corroborates or contradicts it.

    asin label: contradicted when the runtime is outside the configured tolerance, the folder names
    an author the label's authors do not resemble, or the label's title is not in the folder's words.
    not label: contradicted when the excluded edition agrees on every measure (it *is* the book, as
    far as the evidence can tell). none / out-of-scope: no edition to measure."""
    f = {k: "" for k in EVIDENCE_FIELDS}
    db = label.db
    f["db_label"] = f"{db.src}:{db.text()}" if db else ""
    f["local_h"] = f"{float(row.get('hours') or 0):.2f}"
    f["folder_author"] = "; ".join(folder_authors(row, unit))
    f["folder_title"] = "; ".join(folder_titles(row, unit))
    if label.kind not in ("asin", "not"):
        f["evidence"] = "n/a"
        return Evidence(f, "n/a")
    known = [index[a] for a in label.asins if a in index]
    if not known:
        f["label_asin"] = label.asins[0] if label.asins else ""
        f["evidence"] = "unverifiable: not in the frozen catalog"
        return Evidence(f, "unverifiable")
    measured = []
    for product in known:
        m = measure(row, unit, product, tolerance)
        m["_local"] = f["local_h"]
        rank = (m["runtime"] != "contradicts", -abs(m["_delta"]) if m["_delta"] is not None else -9.0, m["_t"])
        measured.append((rank, m))
    measured.sort(key=lambda x: x[0], reverse=True)
    m = measured[0][1]  # an asin label at its best edition; a not label at its strongest rival
    f.update({k: v for k, v in m.items() if not k.startswith("_")})
    contra = _contradictions(m, tolerance)
    weak = _weaknesses(m)
    rt_ok = m["runtime"] == "corroborates"
    if label.kind == "asin":
        if contra:
            ev = Evidence(f, "contradicted", contra, weak, rt_ok)
        elif not weak:
            ev = Evidence(f, "corroborated", [], [], rt_ok)
        else:
            ev = Evidence(f, "consistent", [], weak, rt_ok)
    else:
        if not contra and not weak:
            ev = Evidence(f, "contradicted",
                          [f"the excluded {m['label_asin']} agrees on every measure: runtime {m['delta_pct']}%, "
                           f"title {m['_t']:.2f}, author {_fmt(m['_a']) or 'n/a'}"], [], rt_ok)
        else:
            ev = Evidence(f, "consistent", [], contra + weak, rt_ok)
    f["evidence"] = ev.verdict + (": " + "; ".join(ev.contradictions or ev.weak) if (ev.contradictions or ev.weak) else "")
    return ev


def same_book(a: dict, b: dict, units_by_key: dict) -> bool:
    """Two rows hold the same book when one's title is (nearly) all inside the other's name."""
    def words(row):
        unit = units_by_key.get(row["key"])
        return " ".join(([unit.name] if unit else []) + folder_titles(row, unit))
    ta = " ".join(folder_titles(a, units_by_key.get(a["key"]))) or words(a)
    tb = " ".join(folder_titles(b, units_by_key.get(b["key"]))) or words(b)
    return max(containment(ta, words(b)) or 0, containment(tb, words(a)) or 0) >= MATCH_CORROBORATES


@dataclass
class LabelCheck:
    evidence: dict[str, Evidence]
    failures: list[str]
    db_overrides: list[str]
    contradictions: list[str]
    unverifiable_db: list[str]


def check_labels(corpus: list[dict], labels: dict[str, Label], label_rows: list[dict],
                 units_by_key: dict, index: dict[str, Product],
                 tolerance: float = LABEL_RUNTIME_TOLERANCE) -> LabelCheck:
    """Stage L: recompute every label's evidence and apply the rules (see docs/match-testing.md).

    ``tolerance`` is the *label* tolerance, not the decision tolerance: a label may be right while
    the runtime disagrees (shortened rips), so the two are deliberately different constants.
    """
    rows = {r["key"]: r for r in corpus}
    lines = {r["key"]: r for r in label_rows}
    evidence = {k: label_evidence(rows[k], units_by_key.get(k), labels[k], index, tolerance) for k in rows}
    failures: list[str] = []
    overrides: list[str] = []
    contradictions: list[str] = []
    unverifiable_db: list[str] = []
    # corroborated_by: an inferred edition label that mirrors a trusted label on the same files
    for key, label in labels.items():
        if label.src != "inferred" or label.kind != "asin":
            continue
        local = float(rows[key].get("hours") or 0)
        for other, ol in sorted(labels.items()):
            if other != key and ol.src in TRUSTED and ol.kind == "asin" and set(ol.asins) & set(label.asins) \
                    and abs(float(rows[other].get("hours") or 0) - local) <= 0.01:
                evidence[key].fields["corroborated_by"] = other
                break
    for key in sorted(rows):
        label, ev = labels[key], evidence[key]
        if ev.verdict == "contradicted":
            for c in ev.contradictions:
                contradictions.append(f"{key} [{label.src} {label.text()}]: {c}")
                failures.append(f"L contradiction: {key} [{label.src} {label.text()}]: {c}")
        if ev.verdict == "unverifiable":
            if label.src in DB_SOURCES or label.src == "owner":
                unverifiable_db.append(f"{key} [{label.src} {label.text()}]: {ev.fields['evidence']}")
            else:
                failures.append(f"L {key} [{label.src} {label.text()}]: {ev.fields['evidence']} - an "
                                "inferred label needs evidence")
        if label.src == "inferred" and label.kind == "asin" and ev.verdict != "contradicted" \
                and not ev.runtime_corroborates and not ev.fields["corroborated_by"]:
            failures.append(f"L {key} [inferred {label.text()}] is not corroborated: runtime "
                            f"{ev.fields['delta_pct'] or 'unknown'}% is outside the {INFERRED_RUNTIME_BAND:.0%} "
                            "band and no trusted row shares its files")
        db = label.db
        if db is not None and label.src not in (db.src, "owner"):
            db_ev = label_evidence(rows[key], units_by_key.get(key), db, index, tolerance)
            if db_ev.verdict == "contradicted":
                overrides.append(f"{key}: the DB's {db.src} {db.text()} is contradicted "
                                 f"({'; '.join(db_ev.contradictions)}) -> {label.src} {label.text()}")
            else:
                failures.append(f"L {key}: {label.src} {label.text()} overrides the DB's {db.src} {db.text()}, "
                                f"but the evidence does not contradict the DB's label ({db_ev.verdict})")
    # one ASIN, two different books
    by_asin: dict[str, list[str]] = defaultdict(list)
    for key, label in sorted(labels.items()):
        if label.kind == "asin":
            for a in label.asins:
                by_asin[a].append(key)
    for asin, keys in sorted(by_asin.items()):
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if not same_book(rows[a], rows[b], units_by_key):
                    msg = f"{asin} labels two different books: {a!r} and {b!r}"
                    contradictions.append(msg)
                    failures.append(f"L contradiction: {msg}")
    # stale evidence columns
    for key in sorted(rows):
        line = lines.get(key)
        if line is None:
            continue
        stale = [c for c in EVIDENCE_FIELDS if (line.get(c) or "") != evidence[key].fields[c]]
        if stale:
            c = stale[0]
            failures.append(f"L stale evidence: {key}: {len(stale)} column(s), e.g. {c} csv={line.get(c)!r} "
                            f"now={evidence[key].fields[c]!r} (`python -m tests.matchbench labels --write`)")
    return LabelCheck(evidence, failures, overrides, contradictions, unverifiable_db)


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
        #: catalog searches this row made. Not an outcome, but the thing a "cost guard" constant
        #: moves: ARBITRATE_BELOW decides whether a swapped reading is paid for at all, and with no
        #: outcome change the sweep used to call that dead code.
        self.searches = 0

    @contextlib.contextmanager
    def watching(self):
        real_score, real_pick = matching._score_reading, matching._pick_and_margin
        real_search = catalog.search

        def search(*args, **kwargs):
            self.searches += 1
            return real_search(*args, **kwargs)

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

        with patched(catalog, search=search), \
                patched(matching, _score_reading=score_reading, _pick_and_margin=pick_and_margin):
            yield self


def grade_with(match: matching.Match, gates: dict) -> str:
    """grade() with its literal gates taken from ``gates`` (the mirror ``mutate`` moves)."""
    if not match.product:
        return "NONE"
    if match.knockoff:
        return "LOW"
    foreign = not (match.language or "english").lower().startswith("english")
    title, author = match.title_sim, match.author_sim
    window_only = match.title_window_coverage < matching.MIN_HIGH_WINDOW_COVERAGE
    adaptation = bool(match.unasked_adaptation)
    if match.author_known:
        if (title >= gates["high_title"] and author >= gates["high_author"] and match.margin >= gates["high_margin"]
                and not window_only and not foreign and not adaptation):
            return "HIGH"
        if title >= gates["medium_title"] and author >= gates["medium_author"] and match.margin > 0 and not window_only:
            return "MEDIUM"
        return "LOW"
    if title >= gates["title_only"] and match.margin >= gates["title_only_margin"] and not window_only:
        return "MEDIUM"
    return "LOW"


def limiting_reasons(match: matching.Match) -> list[str]:
    """Why ``match`` is not HIGH, from the evidence the detail panel shows ([] = nothing stops it)."""
    if not match.product:
        if match.rejected:
            return ["rejected"]
        return ["pin-failed"] if match.asin_pin_failed else ["no-candidates"]
    g = _ACTIVE_GATES
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
        if match.title_sim < g["high_title"]:
            reasons.append("title")
        if match.author_sim < g["high_author"]:
            reasons.append("author")
        if match.margin <= 0:
            reasons.append("tie")
        elif match.margin < g["high_margin"]:
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


# --------------------------------------------------------------------------------- decision
CONVERT_ACTIONS = ("convert", "merge+convert", "remux")


def decide(unit: BookUnit, match: matching.Match, tier: str, hours: float) -> tuple[str, str, str, str]:
    """What the pipeline would do: (decision, choose_action's action, runtime state, delta).

    The runtime test is ``pipeline.Audit.runtime_matches`` itself (configured tolerance via
    effective_val, matching.runtime_ok); the action is ``pipeline.choose_action``; a convert action
    becomes ``convert``/``remux`` only for a tier in ``effective_auto_tiers()``, as in the daemon.
    Codecs are not in the corpus: a single .m4b/.m4a is taken as AAC (the remux case), else MP3.
    runtime state: agree / disagree / unknown (either side 0 - runtime_ok passes those silently).
    """
    if not match.product:
        return "review", "review", "", ""
    audit = pipeline.Audit(unit=unit, hours=hours, match=match)
    cat = float(match.product.hours or 0)
    if hours <= 0 or cat <= 0:
        state, delta = "unknown", ""
    else:
        state = "agree" if audit.runtime_matches else "disagree"
        delta = f"{round((hours - cat) / cat * 100):+d}"
    files = unit.files
    codecs = {"aac"} if len(files) == 1 and Path(files[0]).suffix.lower() in (".m4b", ".m4a") else {"mp3"}
    action = pipeline.choose_action(unit, tier, audit.runtime_matches, codecs)
    if action in CONVERT_ACTIONS and tier in config.effective_auto_tiers():
        decision = "remux" if action == "remux" else "convert"
    elif action == "hold":
        decision = "hold"
    else:
        decision = "review"
    if match.pin_abandoned:
        # pipeline.py: the operator asked for one edition; a search made after giving up on that
        # dead pin may have found another, so it is never converted unseen whatever the tier says
        decision = "review"
    return decision, action, state, delta


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
    #: a bench error on this row (a catalog lookup missing from the frozen cache): never graded
    error: str = ""
    decision: str = ""
    action: str = ""
    runtime: str = ""  # agree | disagree | unknown | "" (no product)
    rt_delta: str = ""
    #: candidates in the pool that are wrong by the label yet inside the runtime tolerance: the
    #: picks the runtime check could not stop. Only on rows with traps can stage F ever fire
    traps: int = 0
    #: catalog searches this row made (a cost, not an outcome - see Capture.searches)
    searches: int = 0
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
        if self.error:
            return "error"
        if self.skipped:
            return "skipped"
        if kind == "asin":
            return "right" if pick in self.label.asins else ("wrong" if pick else "missed")
        if kind == "none":
            return "wrong" if pick and TIER_RANK[self.tier] >= TIER_RANK["MEDIUM"] else "right"
        if kind == "not":
            return "wrong" if pick in self.label.asins else "unknown"
        if kind == "out":
            return OUT_OF_SCOPE
        return "unlabelled"

    @property
    def writes(self) -> bool:
        """The pipeline would write this pick into the library unseen."""
        return self.decision in ("convert", "remux")

    @property
    def damage(self) -> bool:
        """would_convert_wrong: written into the library unseen, and wrong."""
        return self.writes and self.verdict == "wrong"


def run_unit(unit: BookUnit, hours: float, tags: tuple[str, str] | None,
             asin_override: str = "", pin_failures: int = 0) -> tuple[matching.Match, Capture, list[str]]:
    """match_unit plus the pipeline's ingest retry, as audit_unit runs them (no files read).

    ``asin_override``/``pin_failures`` model a hand-set ASIN, so the dead-pin rules
    (``PIN_GIVE_UP_AFTER``: block the search, then abandon the pin and never auto-convert) are
    reachable at all. The corpus never passes them: a pinned row is matched *without* its pin,
    because the pin is the owner's label and the question is whether the matcher would have found it.
    """
    flags: list[str] = []
    with Capture().watching() as cap:
        match = matching.match_unit(unit, local_hours=hours, asin_override=asin_override,
                                    pin_failures=pin_failures)
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


def evaluate_row(row: dict, label: Label, frozen: FrozenCatalog | None = None) -> Outcome:
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
    before = frozen.miss_events if frozen else 0
    match, cap, run_flags = run_unit(unit, hours, tags)
    out.flags += run_flags
    _finish(out, unit, match, cap, hours)
    missed = (frozen.miss_events if frozen else 0) - before
    if missed:
        out.error = f"{missed} catalog lookup(s) missing from the frozen cache"
    if row.get("pin"):
        out.flags.append("pinned-in-app")
    rec = row.get("rec_title") or ""
    if rec and row.get("name_source") == "folder" and "embedded-tags" not in out.flags and rec != match.query_title:
        out.flags.append("reading-drift")
    return out


def _finish(out: Outcome, unit: BookUnit, match: matching.Match, cap: Capture, hours: float) -> None:
    out.unit, out.match = unit, match
    out.pool, out.ranked, out.counted_rivals = list(cap.pool), list(cap.ranked), cap.counted_rivals
    out.searches = cap.searches
    tier = match.confidence
    avg = hours / len(unit.files) if unit.files else 0
    if tier in ("LOW", "NONE") and looks_like_collection(unit, avg):
        tier = "COLLECTION"
    out.tier = tier
    out.reasons = limiting_reasons(match)
    out.explain = explanation_problems(match, match.confidence, out.reasons, out.pool)
    out.decision, out.action, out.runtime, out.rt_delta = decide(unit, match, tier, hours)
    if match.product and tier == "HIGH" and out.counted_rivals == 0:
        out.flags.append("lone-high")
    if len(out.pool) == 1:
        out.flags.append("one-candidate")


def evaluate_corpus(corpus: list[dict], labels: dict[str, Label], frozen: FrozenCatalog | None = None) -> list[Outcome]:
    return [evaluate_row(row, labels[row["key"]], frozen) for row in sorted(corpus, key=lambda r: r["key"])]


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
    pin = str(case.get("pin") or "")
    pin_failures = int(case.get("pin_failures") or 0)
    with patched(catalog, search=model.search, book=model.book):
        match, cap, flags = run_unit(unit, hours, tags, asin_override=pin, pin_failures=pin_failures)
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
    #: the tier says how sure the matcher is; the decision says whether it writes. A case that must
    #: never convert (unknown runtime, unconfirmed edition) can only be written as a decision.
    if "decision" in exp and out.decision != exp["decision"]:
        problems.append(f"decision {out.decision}, want {exp['decision']}")
    #: "never written unseen", however it is stopped: held by the runtime, or below an auto-convert tier
    if exp.get("no_write") and out.writes:
        problems.append(f"would {out.decision} {out.pick or 'nothing'} unseen")
    if exp.get("in_pool") and not out.in_pool:
        problems.append("labelled edition never reached the candidate pool")
    #: a hand-set ASIN with no catalog data must block the search entirely: a pick here means the
    #: dead pin fell through to a search that could offer (and convert) a different edition
    if exp.get("no_pick") and out.pick:
        problems.append(f"picked {out.pick} while the pinned ASIN was unresolved")
    #: whether the ingest retry's embedded-tag match replaced the folder's (pipeline._embedded_is_better):
    #: the only way to say "the tags must NOT win here" when both readings find something
    if "embedded" in exp and ("embedded-tags" in out.flags) != bool(exp["embedded"]):
        problems.append("the embedded-tag match replaced the folder's" if exp["embedded"] is False
                        else "the embedded-tag match did not replace the folder's")
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
        "tier": "ERR" if out.error else ("SKIP" if out.skipped else out.tier),
        "in_pool": ("1" if out.in_pool else "0") if out.label.kind == "asin" else "",
        "rank": str(out.rank) if out.label.kind == "asin" else "",
        "verdict": out.verdict,
        "reasons": ";".join(out.reasons) if m else "",
        "decision": out.decision,
        "t_sim": f"{m.title_sim:.3f}" if m else "",
        "a_sim": f"{m.author_sim:.3f}" if m else "",
        "margin": f"{m.margin:.3f}" if m else "",
        "coverage": f"{m.title_window_coverage:.3f}" if m else "",
        "pool": str(len(out.pool)) if m else "",
        "rt_delta": out.rt_delta,
        "searches": str(out.searches),
        "flags": ";".join(sorted(set(out.flags))),
        "title": (m.product.title if m and m.product else "") or (out.error or out.skipped or ""),
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
    labels: LabelCheck | None = None
    tolerance: float = 0.0
    tolerance_source: str = ""
    auto_tiers: tuple[str, ...] = ()

    @property
    def outcomes(self) -> list[Outcome]:
        return self.corpus + self.adversarial

    def rows(self) -> list[dict]:
        return [result_row(o) for o in self.outcomes]


def count_traps(out: Outcome, local: float, index: dict[str, Product], tolerance: float) -> int:
    """Pool candidates the label calls wrong whose runtime the pipeline would accept."""
    if not out.label.graded or out.error or local <= 0:
        return 0
    n = 0
    for asin in out.pool:
        if (out.label.kind == "asin" and asin in out.label.asins) or (out.label.kind == "not" and asin not in out.label.asins):
            continue
        p = index.get(asin)
        if p is not None and p.hours > 0 and matching.runtime_ok(local, p.hours, tolerance):
            n += 1
    return n


def run_bench(catalog_data: dict | None = None, fetch=None, labels_path: Path = LABELS) -> tuple[Run, FrozenCatalog]:
    corpus = read_csv(CORPUS)
    label_rows = read_csv(labels_path)
    labels, problems = resolve_labels(corpus, label_rows)
    header = csv_header(labels_path)
    missing_cols = [c for c in LABEL_FIELDS if c not in header]
    if missing_cols:
        problems.append(f"labels.csv lacks column(s) {', '.join(missing_cols)}")
    defects = {row["key"]: row for row in read_csv(KNOWN_DEFECTS)}
    for key in defects:
        if not key.startswith("adv:") and key not in labels:
            problems.append(f"known_defects.csv: {key!r} is not in the corpus")
    spec = load_adversarial()
    frozen = FrozenCatalog(load_catalog() if catalog_data is None else catalog_data, fetch)
    tolerance, where = runtime_tolerance()
    with sealed(frozen):
        real = evaluate_corpus(corpus, labels, frozen)
        adv = evaluate_adversarial(spec)
    gaps = {"adv:" + c["name"]: c["known_gap"] for c in spec["cases"] if c.get("known_gap")}
    units_by_key = {o.key: o.unit for o in real if o.unit is not None}
    index = catalog_index(frozen.data)
    hours = {r["key"]: float(r.get("hours") or 0) for r in corpus}
    for o in real:
        o.traps = count_traps(o, hours.get(o.key, 0.0), index, tolerance)
    # read at call time, not as check_labels' default (bound once at import): `mutate` moves it
    lc = check_labels(corpus, labels, label_rows, units_by_key, index, tolerance=LABEL_RUNTIME_TOLERANCE)
    run = Run(real, adv, problems, list(frozen.misses), len(frozen.data), len(frozen.used), defects, gaps,
              lc, tolerance, where, tuple(sorted(config.effective_auto_tiers(), key=lambda t: -TIER_RANK[t])))
    return run, frozen


# --------------------------------------------------------------------------------- scoring
def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.1f}%)" if d else f"{n}/0"


@dataclass
class Score:
    graded: int = 0
    skipped: int = 0
    errors: int = 0
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
    decisions: Counter = field(default_factory=Counter)
    unlabelled: int = 0
    out_of_scope: int = 0
    convert_wrong: int = 0
    convert_unverified: int = 0
    convert_unchecked: int = 0
    hold_wrong: int = 0
    hold_saved: int = 0
    medium_wrong: int = 0  # wrong and would convert if AUTO_CONVERT=medium

    def add(self, out: Outcome) -> None:
        if out.error:
            self.errors += 1
            return
        if out.skipped:
            self.skipped += 1
            return
        self.graded += 1
        self.tiers[out.tier] += 1
        self.decisions[out.decision] += 1
        verdict = out.verdict
        if out.writes:
            if verdict == "wrong":
                self.convert_wrong += 1
            elif verdict != "right":
                self.convert_unverified += 1
            if out.runtime == "unknown":
                self.convert_unchecked += 1
        if out.decision == "hold" and verdict == "right":
            self.hold_wrong += 1
        if out.decision == "hold" and verdict == "wrong":
            self.hold_saved += 1
        if verdict == "wrong" and out.action in CONVERT_ACTIONS and out.tier in ("HIGH", "MEDIUM"):
            self.medium_wrong += 1
        if out.label.kind == "out":
            self.out_of_scope += 1
            return
        if out.label.kind == "unlabelled":
            self.unlabelled += 1
            return
        self.labelled += 1
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
    #: catalog searches per run, now and in the baseline, and the rows whose count moved: score-only,
    #: shown beside the drift count and never a failure (see SCORE_FIELDS)
    searches: int = 0
    searches_base: int = 0
    search_moves: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


#: failures `accept` may waive by writing a new baseline (everything else it refuses)
WAIVABLE = ("baseline:", "B/C ")


def load_baseline(path: Path = BASELINE) -> dict[str, dict]:
    return {row["key"]: row for row in read_csv(path)}


def describe(row: dict) -> str:
    if not row:
        return "(absent)"
    return (f"{row.get('tier', '')} {row.get('pick') or '-'} {row.get('verdict', '')}"
            f"{' pool' if row.get('in_pool') == '1' else ''}{' rank ' + row['rank'] if row.get('rank') else ''}"
            f"{' -> ' + row['decision'] if row.get('decision') else ''}"
            f" t={row.get('t_sim') or '-'} a={row.get('a_sim') or '-'} m={row.get('margin') or '-'}"
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
            if new["label"] != old.get("label", "") or new["src"] != old.get("src", ""):
                # the ground truth moved, not the matcher: shown, accepted with a reason, never a regression
                changes.append(f"{key}: LABEL {old.get('src', '')} {old.get('label', '')} -> {new['src']} "
                               f"{new['label']}: {describe(old)} -> {describe(new)}")
                continue
            changes.append(f"{key}: {describe(old)} -> {describe(new)}")
            if old.get("in_pool") == "1" and new.get("in_pool") != "1":
                regressions.append(f"{key}: labelled edition left the candidate pool")
            if old.get("verdict") == "right" and new.get("verdict") != "right":
                regressions.append(f"{key}: was right, now {new['verdict']}")
            if old.get("verdict") != "wrong" and new.get("verdict") == "wrong":
                regressions.append(f"{key}: now wrong at {new['tier']}")
            if old.get("decision") not in ("convert", "remux") and new.get("decision") in ("convert", "remux") \
                    and new.get("verdict") != "right":
                regressions.append(f"{key}: now written into the library unseen ({new['verdict']})")
        elif any(new[f] != old.get(f, "") for f in SCORE_FIELDS):
            drift += 1
    return changes, regressions, drift


def search_cost(rows: list[dict], baseline: dict[str, dict]) -> tuple[int, int, list[str]]:
    """(searches this run, searches in the baseline, the rows whose count moved) - over the rows both
    have, so a STALE baseline (reported on its own) does not read as a cost change."""
    def n(row):
        return int(row.get("searches") or 0)
    now = {r["key"]: r for r in rows}
    both = sorted(set(now) & set(baseline))
    moves = [f"{k}: {baseline[k].get('searches') or '-'} -> {now[k]['searches']}" for k in both
             if now[k].get("searches", "") != baseline[k].get("searches", "")]
    return sum(n(now[k]) for k in both), sum(n(baseline[k]) for k in both), moves


def stale_baseline(rows: list[dict], baseline: dict[str, dict], header: list[str]) -> list[str]:
    """Fail closed: a baseline whose rows or columns disagree with this run is not compared."""
    out = []
    if not baseline:
        return ["baseline: STALE - baseline.csv is missing or empty"]
    missing_cols = [c for c in RESULT_FIELDS if c not in header]
    if missing_cols:
        out.append(f"baseline: STALE - baseline.csv predates column(s) {', '.join(missing_cols)}")
    now = {r["key"] for r in rows}
    extra, absent = sorted(set(baseline) - now), sorted(now - set(baseline))
    if extra or absent:
        msg = f"baseline: STALE - {len(absent)} row(s) not in the baseline, {len(extra)} baseline row(s) not run"
        msg += "".join(f"\n      + {k}" for k in absent[:20]) + "".join(f"\n      - {k}" for k in extra[:20])
        out.append(msg)
    return out


def gate(run: Run, baseline: dict[str, dict], header: list[str] | None = None) -> Gate:
    g = Gate()
    header = csv_header(BASELINE) if header is None else header
    # A: admission - every row labelled, every lookup frozen; a missed lookup names its row
    g.failures += [f"A {p}" for p in run.admission]
    if run.misses:
        g.failures.append(f"A {len(run.misses)} catalog lookup(s) not in the frozen cache "
                          "(run `python -m tests.matchbench refresh`, a reviewed act)")
    for out in run.outcomes:
        if out.error:
            g.failures.append(f"A {out.key}: {out.error} - a bench error, not a NONE")
    # L: label evidence
    if run.labels is not None:
        g.failures += run.labels.failures
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
    # F: decision - nothing wrong is written into the library unseen; no unseen write goes unchecked
    for out in run.corpus:
        if out.damage:
            g.failures.append(f"F would_convert_wrong: {out.key} -> {out.decision} {out.pick} "
                              f"'{out.match.product.title}' ({out.tier}, runtime {out.rt_delta}%), "
                              f"want {out.label.text()} ({out.label.src})")
        if out.writes and out.runtime == "unknown":
            g.failures.append(f"F {out.key} would {out.decision} with the runtime unknown on one side")
    for out in run.adversarial:
        if out.damage and out.key not in run.known_gaps:
            g.failures.append(f"F would_convert_wrong (adversarial): {out.key} -> {out.decision} {out.pick}")
    # adversarial expectations; known gaps must keep failing
    for out in run.adversarial:
        gap = run.known_gaps.get(out.key)
        if gap and not out.expect_problems:
            g.failures.append(f"adversarial {out.key} now passes: move it out of known_gap ({gap})")
        if not gap and out.expect_problems:
            g.failures.append(f"adversarial {out.key}: {'; '.join(out.expect_problems)}")
    # baseline: fail closed on a stale one, then every decision change must be accepted
    rows = run.rows()
    g.failures += stale_baseline(rows, baseline, header)
    g.changes, g.regressions, g.drift = compare(rows, baseline)
    g.searches, g.searches_base, g.search_moves = search_cost(rows, baseline)
    if g.changes:
        g.failures.append(f"baseline: {len(g.changes)} decision change(s) not accepted "
                          "(`python -m tests.matchbench accept --why ...`)")
    # recall and pick over the rows whose label did not move (a withdrawn label is not a regression)
    now_rows = {r["key"]: r for r in rows}
    same = {k: r for k, r in baseline.items()
            if k in now_rows and r.get("label") == now_rows[k]["label"] and r.get("src") == now_rows[k]["src"]}
    base_score, cur_score = _baseline_score(same), _baseline_score({k: now_rows[k] for k in same})
    for name in ("recall", "pick"):
        if baseline and cur_score[name] < base_score[name]:
            g.failures.append(f"B/C {name} fell from {base_score[name]} to {cur_score[name]} "
                              "(rows with an unchanged label)")
    return g


def _baseline_score(baseline: dict[str, dict]) -> dict:
    rows = [r for k, r in baseline.items() if not k.startswith("adv:")]
    return {"recall": sum(r.get("in_pool") == "1" for r in rows),
            "pick": sum(r.get("verdict") == "right" and r.get("in_pool") != "" for r in rows)}


# ----------------------------------------------------------------------------- scoreboard
def _src_line(name: str, s: Score) -> str:
    acc = f"{100.0 * s.right / s.asin_labelled:5.1f}%" if s.asin_labelled else "    -"
    return (f"    {name:<16}{s.labelled:>4}  {s.in_pool:>7}  {s.right:>5}  {acc}  {s.wrong['HIGH']:>7}  "
            f"{s.wrong['MEDIUM']:>7}  {s.wrong['LOW'] + s.wrong['COLLECTION']:>7}  "
            f"{s.decisions['convert'] + s.decisions['remux']:>7}  {s.convert_wrong:>10}")


def scoreboard(run: Run, g: Gate, verbose: bool = False) -> str:
    lines: list[str] = []
    w = lines.append
    real = score(run.corpus)
    adv = score(run.adversarial)
    trusted = score(run.corpus, lambda o: o.label.src in TRUSTED)
    skipped = [o for o in run.corpus if o.skipped]
    out_rows = [o for o in run.corpus if o.label.kind == "out"]
    lc = run.labels
    w(f"MATCH BENCH  audiobind {__version__}  ·  corpus {len(run.corpus)} real rows + "
      f"{len(run.adversarial)} adversarial cases  ·  frozen catalog {run.cache_size} responses "
      f"({run.cache_used} used, {len(run.misses)} missing)")
    w(f"  runtime tolerance {run.tolerance:.0%} ({run.tolerance_source})  ·  auto-convert tiers: "
      f"{', '.join(run.auto_tiers) or 'none'}")
    w("")
    w(f"A corpus admitted   {real.graded} graded ({real.labelled} labelled, {real.out_of_scope} out of scope), "
      f"{real.skipped} skipped, {real.errors} bench error(s), {real.unlabelled} unlabelled   "
      f"admission problems: {len(run.admission)}")
    if lc is not None:
        verdicts = Counter(lc.evidence[o.key].verdict for o in run.corpus if o.key in lc.evidence)
        w(f"L label evidence    " + "  ".join(f"{v} {verdicts[v]}" for v in (
            "corroborated", "consistent", "contradicted", "unverifiable", "n/a")) +
          f"   failures: {len(lc.failures)}")
    w(f"B retrieval recall  in pool {pct(real.in_pool, real.asin_labelled)}   "
      f"@1 {pct(real.at1, real.asin_labelled)}   @3 {pct(real.at3, real.asin_labelled)}   "
      f"@5 {pct(real.at5, real.asin_labelled)}")
    w(f"C pick accuracy     TRUSTED {pct(trusted.right, trusted.asin_labelled)} (owner+pin+db-approved)   "
      f"all labels {pct(real.right, real.asin_labelled)} (includes db-auto and inferred - not the app's accuracy)")
    w(f"D grade safety      wrong@HIGH {real.wrong['HIGH']} (known defects {len(run.known_defects)})   "
      f"wrong@MEDIUM {real.wrong['MEDIUM']}   wrong@LOW/COLL {real.wrong['LOW'] + real.wrong['COLLECTION']}   "
      f"right@HIGH {real.right_by_tier['HIGH']}   right@MEDIUM {real.right_by_tier['MEDIUM']}   "
      f"right but LOW {real.right_by_tier['LOW'] + real.right_by_tier['COLLECTION']}")
    w(f"E explanation       {sum(len(o.explain) for o in run.corpus)} violation(s) on the corpus, "
      f"{sum(len(o.explain) for o in run.adversarial)} on adversarial")
    unknown_rt = [o for o in run.corpus if o.runtime == "unknown"]
    w(f"F decision          would_convert_wrong {real.convert_wrong}   would_hold_wrong {real.hold_wrong}   "
      f"held a wrong pick {real.hold_saved}   unverified writes {real.convert_unverified}   "
      f"runtime unknown {len(unknown_rt)}   (if AUTO_CONVERT=medium: wrong writes {real.medium_wrong})")
    writes = Counter(o.label.src for o in run.corpus if o.writes)
    w(f"  unseen writes     {sum(writes.values())}, graded by: trusted {sum(writes[s] for s in TRUSTED)}  inferred "
      f"{writes['inferred']}  db-auto {writes['db-auto']} (the matcher's own past answer - consistency, not "
      f"correctness)  ungraded {sum(writes.values()) - sum(writes[s] for s in SOURCES)}")
    reach = Counter(o.label.src for o in run.corpus if o.traps)
    w(f"  F's reach         {sum(reach.values())} graded rows hold a wrong candidate inside the tolerance (only there "
      f"can a bad pick convert): trusted {sum(reach[s] for s in TRUSTED)}  inferred {reach['inferred']}  "
      f"db-auto {reach['db-auto']}")
    w("  decisions (real)  " + "  ".join(f"{d} {real.decisions[d]}" for d in ("convert", "remux", "hold", "review")))
    w("  tiers (real)      " + "  ".join(f"{t} {real.tiers[t]}" for t in ("HIGH", "MEDIUM", "LOW", "COLLECTION", "NONE")))
    flags = Counter(f for o in run.corpus for f in set(o.flags))
    w("  risk flags        " + "  ".join(f"{k} {flags[k]}" for k in (
        "lone-high", "one-candidate", "retry-unreproducible", "reading-drift", "approx-name", "embedded-tags")))
    w("")
    w("  PROVENANCE SPLIT  n  in-pool  right   pick%  wrong@H  wrong@M  wrong@L  writes  wrong-writes")
    for src in SOURCES:
        s = score(run.corpus, lambda o, src=src: o.label.src == src)
        if s.labelled or src in TRUSTED:
            w(_src_line(src, s))
    w(_src_line("= trusted", trusted))
    od = score(run.corpus, lambda o: o.label.src in ("owner", "db-approved"))
    w(_src_line("= owner+db-appr.", od))
    w(f"    {'out-of-scope':<16}{len(out_rows):>4}  (run and decided, never graded)")
    w(f"  -> quote the TRUSTED row as the app's accuracy: {pct(trusted.right, trusted.asin_labelled)} over "
      f"{trusted.asin_labelled} rows. db-auto is the matcher agreeing with its own past answers; inferred is a "
      "reviewer's reading.")
    w("")
    if lc is not None:
        if lc.contradictions:
            w("  label contradictions (each fails the gate)")
            for c in lc.contradictions:
                w(f"    {c}")
            w("")
        if lc.db_overrides:
            w("  DB labels overridden because their own evidence contradicts them")
            for c in lc.db_overrides:
                w(f"    {c}")
            w("")
        if lc.unverifiable_db:
            w("  owner/DB labels with no catalog evidence (the label stands; nothing corroborates it)")
            for c in lc.unverifiable_db:
                w(f"    {c}")
            w("")
        weak = Counter(o.label.src for o in run.corpus if lc.evidence[o.key].verdict == "consistent"
                       and o.label.kind == "asin")
        if weak:
            w("  weak edition labels (inside tolerance, not corroborated): " +
              "  ".join(f"{s} {weak[s]}" for s in SOURCES if weak[s]))
            if verbose:
                for o in run.corpus:
                    ev = lc.evidence[o.key]
                    if ev.verdict == "consistent" and o.label.kind == "asin":
                        w(f"    {o.key} [{o.label.src}]: {'; '.join(ev.weak)}")
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
      f"(still failing: {sum(1 for o in gaps if o.expect_problems)}); wrong@HIGH {adv.wrong['HIGH']}; "
      f"would_convert_wrong {adv.convert_wrong} (held a wrong pick {adv.hold_saved})")
    for o in gaps:
        w(f"    gap  {o.key}: {run.known_gaps[o.key]} -> {'; '.join(o.expect_problems) or 'PASSES NOW'}"
          f" [decision {o.decision}{' ' + o.rt_delta + '%' if o.rt_delta else ''}]")
    w("")
    if run.known_defects:
        w("  known defects (real corpus, listed in known_defects.csv)")
        for key, d in sorted(run.known_defects.items()):
            o = next((x for x in run.corpus if x.key == key), None)
            w(f"    {key}: {d.get('symptom', '')}" + (f" [decision {o.decision}]" if o else ""))
        w("")
    wrong_med = [o for o in run.corpus if o.verdict == "wrong" and o.tier == "MEDIUM"]
    if wrong_med:
        w("  wrong at MEDIUM (one config change from auto-converting) and what the runtime check does")
        for o in wrong_med:
            w(f"    {o.key}: picked {o.pick} '{o.match.product.title}' want {o.label.text()} ({o.label.src}) "
              f"-> runtime {o.runtime} {o.rt_delta}% -> {o.decision}")
        w("")
    held_right = [o for o in run.corpus if o.decision == "hold" and o.verdict == "right"]
    if held_right:
        w("  would_hold_wrong (the right edition picked, held by the runtime check)")
        for o in held_right:
            w(f"    {o.key}: {o.pick} {o.tier} runtime {o.rt_delta}% ({o.label.src})")
        w("")
    if unknown_rt:
        w("  runtime unknown on one side (runtime_ok passes these; listed, never silently passed)")
        for o in unknown_rt:
            w(f"    {o.key}: {o.pick} {o.tier} -> {o.decision}")
        w("")
    unverified = [o for o in run.corpus if o.writes and o.verdict not in ("right", "wrong")]
    if unverified:
        w("  unseen writes the labels cannot grade")
        for o in unverified:
            w(f"    {o.key}: {o.decision} {o.pick} {o.tier} runtime {o.rt_delta}% ({o.verdict})")
        w("")
    retry = [o for o in run.corpus if "retry-unreproducible" in o.flags]
    if retry:
        w("  decided on the folder reading only (embedded tags never recorded, so the ingest retry that "
          "could replace a NONE/LOW is not modelled)")
        for o in retry:
            w(f"    {o.key}: {o.tier} -> {o.decision} ({o.label.src} {o.label.text()})")
        w("")
    missed_pool = [o for o in run.corpus if o.label.kind == "asin" and not o.skipped and not o.in_pool]
    if missed_pool:
        w("  retrieval misses (labelled edition never in the pool)")
        for o in missed_pool:
            w(f"    {o.key}: want {o.label.text()} ({o.label.src}); pool {len(o.pool)}, picked {o.pick or '-'} {o.tier}")
        w("")
    if out_rows:
        w("  out of scope (run, decided, never graded)")
        for o in out_rows:
            note = o.label.note if verbose else (o.label.note[:110] + ("…" if len(o.label.note) > 110 else ""))
            w(f"    {o.key}: {o.tier} {o.pick or '-'} -> {o.decision} - {note}")
        w("")
    if skipped:
        w("  skipped rows")
        for o in skipped:
            w(f"    {o.key}: {o.skipped}")
        w("")
    w(f"  baseline          {len(g.changes)} decision change(s), {len(g.regressions)} regression(s), "
      f"{g.drift} score-only drift")
    delta = g.searches - g.searches_base
    w(f"  search cost       {g.searches} catalog searches per run (baseline {g.searches_base}"
      f"{f', {delta:+d} ({100.0 * delta / g.searches_base:+.0f}%)' if delta and g.searches_base else ''}), "
      f"{len(g.search_moves)} row(s) moved - score-only, never fails the gate")
    for line in g.search_moves[:10 if not verbose else None]:
        w(f"    cost    {line}")
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
    g = gate(run, baseline, csv_header(path))
    hard = [f for f in g.failures if not f.startswith(WAIVABLE)]
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
    stale = [f for f in g.failures if "STALE" in f]
    return True, (f"baseline written: {len(rows)} rows, {len(g.changes)} change(s) accepted"
                  + "".join(f"\n  (was {f})" for f in stale))


# ------------------------------------------------------------------------ labels --write
def write_labels(path: Path = LABELS) -> str:
    """Add a line for every row the DB labels and lacks one; rewrite every evidence column.

    Never touches expect / label_source / classes / note of an existing line: those are a person's."""
    corpus = read_csv(CORPUS)
    lines = {r["key"]: r for r in read_csv(path)}
    added = []
    for row in corpus:
        db = db_label(row)
        if row["key"] not in lines and db is not None:
            lines[row["key"]] = {"key": row["key"], "expect": db.text(), "label_source": db.src,
                                 "classes": "", "note": ""}
            added.append(row["key"])
    write_csv(path, LABEL_FIELDS, [lines[k] for k in sorted(lines)])
    run, _ = run_bench()
    changed = 0
    for key, line in lines.items():
        ev = run.labels.evidence.get(key)
        if ev is None:
            continue
        for c in EVIDENCE_FIELDS:
            if (line.get(c) or "") != ev.fields[c]:
                changed += 1
            line[c] = ev.fields[c]
    write_csv(path, LABEL_FIELDS, [lines[k] for k in sorted(lines)])
    missing = sorted({r["key"] for r in corpus} - set(lines))
    out = [f"labels.csv: {len(lines)} lines, {len(added)} added from the DB, {changed} evidence value(s) rewritten"]
    out += [f"  + {k}" for k in added]
    out += [f"  ! no line (needs a person): {k}" for k in missing]
    return "\n".join(out)


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
        lines.append("new rows need a line in labels.csv: `labels --write` adds the ones the DB labels")
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


# ---------------------------------------------------------------------------- mutation
@dataclass(frozen=True)
class Knob:
    """One constant ``mutate`` moves: where it lives, its default, the values tried (low to high)."""
    name: str
    default: float
    values: tuple
    kind: str  # "module" | "gate" | "tolerance"
    target: object = None
    attr: str = ""

    def current(self) -> float:
        if self.kind == "module":
            return getattr(self.target, self.attr)
        if self.kind == "gate":
            return GRADE_GATES[self.attr]
        return float(config.effective_val("max_runtime_delta"))

    @contextlib.contextmanager
    def set(self, value):
        global _ACTIVE_GATES
        if self.kind == "module":
            with patched(self.target, **{self.attr: value}):
                yield
        elif self.kind == "gate":
            old = _ACTIVE_GATES
            _ACTIVE_GATES = dict(old, **{self.attr: value})
            try:
                with patched(matching, grade=lambda m: grade_with(m, _ACTIVE_GATES)):
                    yield
            finally:
                _ACTIVE_GATES = old
        else:
            had = "runtime_delta" in config._EFFECTIVE
            old = config._EFFECTIVE.get("runtime_delta")
            config._EFFECTIVE["runtime_delta"] = int(round(value * 100))  # the settings path: a percent
            try:
                yield
            finally:
                if had:
                    config._EFFECTIVE["runtime_delta"] = old
                else:
                    config._EFFECTIVE.pop("runtime_delta", None)


def _g(name, default, values):
    return Knob(f"grade.{name}", default, values, "gate", attr=name)


#: every constant that decides a gate, each moved down and up (nearest values first on each side)
KNOBS: tuple[Knob, ...] = (
    Knob("TITLE_FLOOR", 0.60, (0.40, 0.50, 0.55, 0.65, 0.70, 0.80), "module", matching, "TITLE_FLOOR"),
    Knob("SUBTITLE_GATE", 0.60, (0.40, 0.50, 0.55, 0.65, 0.70, 0.80), "module", matching, "SUBTITLE_GATE"),
    Knob("MIN_HIGH_WINDOW_COVERAGE", 0.60, (0.30, 0.45, 0.55, 0.65, 0.75, 0.90), "module", matching,
         "MIN_HIGH_WINDOW_COVERAGE"),
    Knob("LANGUAGE_PENALTY", 0.85, (0.60, 0.75, 0.80, 0.90, 0.95, 1.00), "module", matching, "LANGUAGE_PENALTY"),
    _g("high_title", 0.88, (0.80, 0.84, 0.86, 0.90, 0.92, 0.96)),
    _g("high_author", 0.85, (0.70, 0.78, 0.82, 0.88, 0.92, 0.97)),
    _g("high_margin", 0.05, (0.0, 0.02, 0.04, 0.06, 0.10, 0.20)),
    _g("medium_title", 0.85, (0.70, 0.78, 0.82, 0.88, 0.92, 0.97)),
    _g("medium_author", 0.60, (0.30, 0.45, 0.55, 0.65, 0.75, 0.90)),
    _g("title_only", 0.96, (0.85, 0.90, 0.94, 0.98, 0.99, 1.00)),
    _g("title_only_margin", 0.10, (0.0, 0.04, 0.08, 0.12, 0.20, 0.40)),
    Knob("EMBEDDED_SCORE_EDGE", 0.10, (0.0, 0.02, 0.05, 0.15, 0.25, 0.50), "module", pipeline, "EMBEDDED_SCORE_EDGE"),
    Knob("PIN_GIVE_UP_AFTER", 3, (1, 2, 4, 6, 10), "module", matching, "PIN_GIVE_UP_AFTER"),
    Knob("runtime tolerance", 0.15, (0.02, 0.05, 0.10, 0.20, 0.25, 0.35), "tolerance"),
    Knob("ARBITRATE_BELOW", 0.90, (0.70, 0.80, 0.85, 0.95, 1.00), "module", matching, "ARBITRATE_BELOW"),
    Knob("CORROBORATION_WINDOW", 0.10, (0.0, 0.05, 0.08, 0.12, 0.20, 0.40), "module", matching, "CORROBORATION_WINDOW"),
    Knob("CORROBORATION_RUNTIME_TOLERANCE", 0.05, (0.0, 0.02, 0.04, 0.08, 0.15, 0.30), "module", matching,
         "CORROBORATION_RUNTIME_TOLERANCE"),
    # the bench's own label tolerance (stage L), not a matcher constant: it moves no decision, only how
    # many labels their own runtime evidence contradicts (each one fails the gate). 0.15 is the decision
    # tolerance, the value it must never be confused with
    Knob("LABEL_RUNTIME_TOLERANCE", 0.35, (0.15, 0.20, 0.25, 0.30, 0.45), "module", sys.modules[__name__],
         "LABEL_RUNTIME_TOLERANCE"),
)
MUTATION_FIELDS = ["constant", "default", "value", "real_flips", "adv_flips", "errors", "pick", "trusted_pick",
                   "wrong_high", "would_convert_wrong", "would_hold_wrong", "adv_spec", "searches",
                   "label_contradicted", "fingerprint", "examples"]


def _summary(run: Run) -> dict:
    real, adv = score(run.corpus), score(run.adversarial)
    trusted = score(run.corpus, lambda o: o.label.src in TRUSTED)
    lc = run.labels
    return {"pick": real.right, "trusted_pick": trusted.right, "wrong_high": real.wrong["HIGH"],
            "would_convert_wrong": real.convert_wrong, "would_hold_wrong": real.hold_wrong,
            "adv_spec": sum(1 for o in run.adversarial if not o.expect_problems),
            "searches": sum(o.searches for o in run.corpus) + sum(o.searches for o in run.adversarial),
            # corpus rows whose label its own evidence contradicts (stage L; each fails the gate)
            "label_contradicted": sum(1 for ev in lc.evidence.values() if ev.verdict == "contradicted") if lc else 0,
            "errors": real.errors + adv.errors}


def _decisions(run: Run) -> dict[str, tuple]:
    return {r["key"]: tuple(r[f] for f in DECISION_FIELDS) for r in run.rows()}


def mutation_profile(only: str = "", data: dict | None = None) -> tuple[list[dict], dict, list[str]]:
    """Re-run the bench once per (constant, value). Returns (rows, base summary, problems)."""
    data = load_catalog() if data is None else data
    base_run, _ = run_bench(dict(data))
    base, base_dec = _summary(base_run), _decisions(base_run)
    problems = []
    # the grade mirror must reproduce grade() on the corpus before any gate is moved through it
    with patched(matching, grade=lambda m: grade_with(m, GRADE_GATES)):
        mirror_run, _ = run_bench(dict(data))
    if _decisions(mirror_run) != base_dec:
        problems.append("grade_with(GRADE_GATES) does not reproduce grade() on the corpus: grade-gate "
                        "mutations are meaningless until the mirror is fixed")
    rows = []
    for knob in KNOBS:
        if only and only.lower() not in knob.name.lower():
            continue
        if knob.current() != knob.default:
            problems.append(f"{knob.name} is {knob.current()} in the code, the bench expects {knob.default}: "
                            "update KNOBS (and re-record) so the steps straddle the real value")
        for value in knob.values:
            with knob.set(value):
                run, _ = run_bench(dict(data))
            dec = _decisions(run)
            flips = sorted(k for k in dec if dec[k] != base_dec.get(k))
            real_flips = [k for k in flips if not k.startswith("adv:")]
            adv_flips = [k for k in flips if k.startswith("adv:")]
            s = _summary(run)
            rows.append({
                "constant": knob.name, "default": f"{knob.default:g}", "value": f"{value:g}",
                "real_flips": str(len(real_flips)), "adv_flips": str(len(adv_flips)), "errors": str(s["errors"]),
                **{k: str(s[k]) for k in ("pick", "trusted_pick", "wrong_high", "would_convert_wrong",
                                          "would_hold_wrong", "adv_spec", "searches", "label_contradicted")},
                "fingerprint": hashlib.sha1("\n".join(flips).encode()).hexdigest()[:10] if flips else "",
                "examples": ";".join(flips[:3]),
            })
    return rows, base, problems


def classify(knob_rows: list[dict], default: float, base_searches: int | None = None,
             base_contradicted: int | None = None) -> str:
    """load-bearing / one-sided / slack / cost-only / NO EFFECT, from the rows of one constant.

    A change in how many labels are contradicted counts as an outcome: it fails the gate as surely as
    a decision flip (LABEL_RUNTIME_TOLERANCE moves nothing else)."""
    def moved(r):
        labels_moved = (base_contradicted is not None and r.get("label_contradicted", "") != ""
                        and int(r["label_contradicted"]) != base_contradicted)
        return r["real_flips"] != "0" or r["adv_flips"] != "0" or r["errors"] != "0" or labels_moved
    below = [r for r in knob_rows if float(r["value"]) < default]
    above = [r for r in knob_rows if float(r["value"]) > default]
    near_b = below[-1] if below else None
    near_a = above[0] if above else None
    any_b, any_a = any(moved(r) for r in below), any(moved(r) for r in above)
    if not any_b and not any_a:
        # a third answer besides dead code and a missing case: the constant is a cost guard. It
        # cannot change an outcome it never needed to, but it changes how many catalog searches a
        # scan makes - which is a real price (rate limits, latency) and is invisible to a
        # decision-only sweep. ARBITRATE_BELOW is exactly this: it decides whether a swapped
        # reading is paid for at all.
        deltas = []
        if base_searches is not None:
            deltas = [int(r["searches"]) - base_searches for r in knob_rows if r.get("searches", "") != ""]
        if any(deltas):
            low, high = min(deltas), max(deltas)
            span = f"{low:+d}..{high:+d}" if low != high else f"{low:+d}"
            return f"cost-only: no outcome change, {span} searches per run"
        return "NO EFFECT on corpus or adversarial - dead code or a missing test case"
    if near_b and near_a and moved(near_b) and moved(near_a):
        return "load-bearing (the nearest step either way changes outcomes)"
    if any_b and any_a:
        return "slack (only farther steps change outcomes)"
    side = "lowering" if any_b else "raising"
    return f"one-sided: only {side} it changes anything"


def mutate(only: str = "", record: bool = False, check: bool = False) -> tuple[str, int]:
    rows, base, problems = mutation_profile(only)
    lines = [f"MUTATE  audiobind {__version__}  ·  base: pick {base['pick']}  trusted {base['trusted_pick']}  "
             f"wrong@HIGH {base['wrong_high']}  would_convert_wrong {base['would_convert_wrong']}  "
             f"would_hold_wrong {base['would_hold_wrong']}  adversarial spec {base['adv_spec']}  "
             f"searches {base['searches']}  labels contradicted {base['label_contradicted']}", ""]
    lines.append(f"  {'constant':<34}{'value':>7}  flips(real/adv)  err  pick  trust  w@H  conv_wrong  "
                 "hold_wrong  adv_spec  search  L-contra  examples")
    verdicts = []
    for knob in KNOBS:
        krows = [r for r in rows if r["constant"] == knob.name]
        if not krows:
            continue
        for r in krows:
            def d(k):
                delta = int(r[k]) - int(base[k])
                return f"{r[k]}{'(' + format(delta, '+d') + ')' if delta else ''}"
            lines.append(f"  {knob.name + ' (' + r['default'] + ')':<34}{r['value']:>7}  "
                         f"{r['real_flips']:>7}/{r['adv_flips']:<7}  {r['errors']:>3}  {d('pick'):>4}  "
                         f"{d('trusted_pick'):>5}  {d('wrong_high'):>3}  {d('would_convert_wrong'):>10}  "
                         f"{d('would_hold_wrong'):>10}  {d('adv_spec'):>8}  {d('searches'):>6}  "
                         f"{d('label_contradicted'):>8}  {r['examples'][:85]}")
        verdict = classify(krows, knob.default, int(base["searches"]), int(base["label_contradicted"]))
        verdicts.append((knob.name, verdict))
        lines.append(f"  -> {knob.name}: {verdict}")
        lines.append("")
    lines.append("  SUMMARY")
    for name, verdict in verdicts:
        lines.append(f"    {name:<34}{verdict}")
    for p in problems:
        lines.append(f"  PROBLEM {p}")
    code = 1 if problems else 0
    if record and not only:
        write_csv(MUTATIONS, MUTATION_FIELDS, rows)
        lines.append(f"recorded {len(rows)} mutation rows to {MUTATIONS.name}")
    if check:
        old = {(r["constant"], r["value"]): r for r in read_csv(MUTATIONS)}
        diffs = []
        for r in rows:
            o = old.get((r["constant"], r["value"]))
            if o is None:
                diffs.append(f"{r['constant']}={r['value']}: not recorded")
            elif any(o.get(k, "") != r[k] for k in MUTATION_FIELDS):
                diffs.append(f"{r['constant']}={r['value']}: recorded {o.get('real_flips')}/{o.get('adv_flips')} flips "
                             f"[{o.get('fingerprint')}], now {r['real_flips']}/{r['adv_flips']} [{r['fingerprint']}]")
        if not only:
            seen = {(r["constant"], r["value"]) for r in rows}
            diffs += [f"{c}={v}: recorded but no longer run" for c, v in old if (c, v) not in seen]
        lines.append(f"mutation profile vs {MUTATIONS.name}: {len(diffs)} difference(s)")
        lines += [f"  ~ {x}" for x in diffs]
        if diffs:
            code = 1
    return "\n".join(lines), code


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
