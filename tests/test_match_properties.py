"""Property and edge tests on the matcher's public surface: match_unit, grade, choose_action, runtime_ok.

Seeded and deterministic, no network, a few seconds. Units and catalog answers are *generated* -
empty titles, unicode, zero and negative hours, titles that are substrings of each other, a
200-candidate pool, a product with no authors, one ASIN returned twice, products with no language -
and every generated point must satisfy:

  * no exception;
  * no grade outside HIGH / MEDIUM / LOW / NONE (COLLECTION is the pipeline's fifth tier);
  * ``grade()`` agrees with the panel's explanation mirror (``bench.grade_with`` and
    ``bench.limiting_reasons``, the reasons stage E checks): the mirror returns the same tier, and
    a match is HIGH exactly when the mirror has no limiting reason.

A violation is a **finding**, not a bug to fix here: matcher behaviour needs the owner's decision.
Each finding class is listed in ``KNOWN_FINDINGS`` with a minimal real input, and the suite asserts
both ways - every generated violation belongs to a listed class, and every listed class still
reproduces. So a new failure class fails CI, and so does a silent fix (the list must change with
it). Write-ups: /opt/data/audiobind-designs/claude-report-bench-meta-2.md.
"""

import itertools
import math
import random
import time
import traceback
import unittest
import urllib.request
from collections import Counter
from pathlib import Path
from unittest import mock

from audiobind import catalog, matching, pipeline, units
from audiobind.catalog import Product
from audiobind.matching import Match, grade, match_unit, runtime_ok
from audiobind.pipeline import choose_action
from audiobind.units import BookUnit, Part
from tests.matchbench import bench

SEED = 20261116
MATCH_TIERS = ("HIGH", "MEDIUM", "LOW", "NONE")
ALL_TIERS = MATCH_TIERS + ("COLLECTION",)
ACTIONS = ("review", "hold", "remux", "convert", "merge+convert")

# ------------------------------------------------------------------------------- vocabulary
TITLES = [
    "", " ", "Dune", "Dune Messiah", "Children of Dune", "The Dune", "dune", "DUNE",
    "The Hard Way", "Hard Way", "The Hard Way: A Jack Reacher Novel", "Jack Reacher 10 - The Hard Way",
    "1984", "2001 A Space Odyssey", "It", "A", "The", "The The",
    "Der Schwarm", "Les Misérables", "Cien años de soledad", "百年孤独", "Война и мир", "الف ليلة وليلة",
    "Ångström — Ünïcödé", "Café Noir", "Zero​Width", "📚 The Book 📚", "ǅ ﬁ ﬀ", "‮RTL override",
    "---", "()", "[]", "{}", "(((", "Dune (", "Dune [", "Dune [B0", "...", "_", "&", "'", "\t", "Line\nBreak",
    "Summary of Dune", "Dune: A Summary", "[Review] Dune", "Review: Dune", "Dune Study Guide", "Workbook for Dune",
    "Dune [Dramatized Adaptation]", "Dune (GraphicAudio)", "Dune: Full Cast", "Dune - An Audio Drama",
    "Dune Box Set", "Dune Trilogy", "Dune Books 1-3", "Dune, Book 1", "Dune (Unabridged)", "Dune Part 1",
    "Dune Chapterized 64kbps (Read by Scott Brick)", "Dune aka Arrakis", "Dune by Frank Herbert",
    "Frank Herbert - Dune", "Dune - Frank Herbert", "Past Tense - Lee Child", "01 Dune", "01 - Dune", "1. Dune",
    # long, but not as long as they could be: the window search is cubic in words (TestLongTitles
    # measures one folder name at the 255-byte cap), and a 400-word title takes ~30 s per match
    "Dune " * 25, "x" * 255, " ".join(f"w{i}" for i in range(30)),
]
AUTHORS = [
    "", " ", "Frank Herbert", "Herbert, Frank", "frank herbert", "F. Herbert", "Brian Herbert", "Lee Child",
    "Lee Child, Andrew Child", "Lee Child & Andrew Child", "Gabriel García Márquez", "村上 春樹", "Лев Толстой",
    "J.R.R. Tolkien", "Dr. Seuss", "Anonymous", "Various", "---", "()", "📚", "A" * 500, "Dune",
]
LANGUAGES = ["english", "English", "ENGLISH", "english (us)", "", None, "german", "spanish", " english", "englisch"]
#: catalogue runtimes: zero, negative, tiny, ordinary, absurd
CATALOG_HOURS = [0.0, -3.5, -0.0, 0.004, 0.5, 9.99, 10.0, 10.01, 11.5, 14.23, 100.0, 1e6]
LOCAL_HOURS = [0.0, -1.0, -0.0, 0.004, 9.0, 10.0, 11.83, 1e6]
ASINS = [f"B0{i:08d}" for i in range(400)]


def product(rng: random.Random, asin: str | None = None, title: str | None = None) -> Product:
    n_authors = rng.choice([0, 0, 1, 1, 1, 2, 3])
    return Product(
        asin=asin if asin is not None else rng.choice(ASINS),
        title=title if title is not None else rng.choice(TITLES),
        subtitle=rng.choice(["", "", "", "A Novel", "Dramatized Adaptation", "Summary", "Dune, Book 1", "Volume 1"]),
        authors=[rng.choice(AUTHORS) for _ in range(n_authors)],
        narrators=[rng.choice(AUTHORS) for _ in range(rng.choice([0, 1]))],
        series=rng.choice(["", "", "Dune", "Jack Reacher"]),
        sequence=rng.choice(["", "1", "10", "1.5", "x"]),
        hours=rng.choice(CATALOG_HOURS),
        language=rng.choice(LANGUAGES),
    )


def sparse_audible(rng: random.Random, asin: str) -> Product:
    """A product the way the app builds one from a search answer that omits most fields."""
    raw = {"asin": asin}
    if rng.random() < 0.8:
        raw["title"] = rng.choice(TITLES)
    if rng.random() < 0.5:
        raw["authors"] = rng.choice([[], None, [{"name": rng.choice(AUTHORS)}], [{}], [{"name": ""}]])
    if rng.random() < 0.5:
        raw["runtime_length_min"] = rng.choice([0, None, -30, 600, 599.5])
    if rng.random() < 0.5:
        raw["language"] = rng.choice([x for x in LANGUAGES if x is not None])
    if rng.random() < 0.3:
        raw["series"] = rng.choice([[], None, [{"title": "Dune", "sequence": "1"}], [{}]])
    return Product.from_audible(raw)


def audnex_record(rng: random.Random, asin: str) -> dict:
    """An Audnex book record; sparse ones included (a missing record is {})."""
    kind = rng.random()
    if kind < 0.35:
        return {}
    record = {"asin": asin, "title": rng.choice(TITLES)}
    if kind < 0.9:
        record["authors"] = [{"name": rng.choice(AUTHORS)} for _ in range(rng.choice([0, 1, 2]))]
        record["runtimeLengthMin"] = rng.choice([0, None, 600, 601, 853.8, -5])
        record["language"] = rng.choice([x for x in LANGUAGES if x is not None])
    if rng.random() < 0.3:
        record["seriesPrimary"] = rng.choice([None, {}, {"name": "Dune", "position": "1"}])
    return record


class FakeCatalog:
    """catalog.search / catalog.book over a generated pool. ``mode``: every search returns the whole
    pool ("all"), the products sharing a word with the query ("filter"), or nothing ("empty")."""

    def __init__(self, pool: list[Product], records: dict, mode: str = "all"):
        self.pool, self.records, self.mode = pool, records, mode
        self.searches = 0

    def search(self, title=None, author=None, limit=10, keywords=None):
        self.searches += 1
        if self.mode == "empty":
            return []
        if self.mode == "all":
            return list(self.pool)
        words = set(units.norm(" ".join(x for x in (title, author, keywords) if x)).split())
        return [p for p in self.pool if words & set(units.norm(f"{p.title} {' '.join(p.authors)}").split())]

    def book(self, asin):
        return dict(self.records.get(asin) or {})


def _offline(*_a, **_k):
    raise AssertionError("the property tests are offline: something tried to open a URL")


# ----------------------------------------------------------------------------------- units
def scanned_unit(name: str, n_files: int = 3, n_parts: int = 1, ingest: bool = False, ext: str = ".mp3") -> BookUnit:
    """The unit the scanner names for a folder (or bare file) called ``name``."""
    root = Path("/ingest" if ingest else "/library")
    path = root / name if name else root
    parts = [Part(name=f"Disc {i + 1}" if n_parts > 1 else path.name,
                  files=[str(path / f"p{i}" / f"{j:03d}{ext}") for j in range(n_files)]) for i in range(n_parts)]
    unit = BookUnit(path=str(path), name=path.name, parent=str(path.parent), parts=parts)
    units._name_units([unit], root, ingest=ingest)
    if ingest:
        unit.source = "ingest"
    return unit


def raw_unit(rng: random.Random) -> BookUnit:
    """A unit with its guesses set directly: readings the name parser may never produce."""
    title = rng.choice(TITLES)
    return BookUnit(
        path="/library/x", name=rng.choice(TITLES), parent="/library",
        parts=[Part(name="x", files=["/library/x/001.mp3"] * rng.choice([0, 1, 1, 30]))],
        author_guess=rng.choice(AUTHORS), title_guess=title,
        series_guess=rng.choice(["", "", "Jack Reacher Series", "Series", "📚"]),
        alt_author_guess=rng.choice(["", "", title]), alt_title_guess=rng.choice(["", "", rng.choice(AUTHORS)]),
        asin_hint=rng.choice(["", "", rng.choice(ASINS), "0000000000", "📚"]),
    )


def folder_name(rng: random.Random) -> str:
    title, author = rng.choice(TITLES), rng.choice(AUTHORS)
    shape = rng.randrange(7)
    name = [title, f"{author} - {title}", f"{title} - {author}", f"{author}/{title}", f"{title} by {author}",
            f"{author}/Series/{rng.randrange(40):02d} {title}", f"{title} [{rng.choice(ASINS)}]"][shape]
    return name.replace("\x00", "")


# ------------------------------------------------------------------------------ the checks
class Findings:
    """Every violation, by class, with the first input that showed it."""

    def __init__(self):
        self.count: Counter = Counter()
        self.first: dict[str, str] = {}
        self.points = 0   # Match objects graded against the mirror
        self.matches = 0  # match_unit calls

    def add(self, cls: str, detail: str) -> None:
        self.count[cls] += 1
        self.first.setdefault(cls, detail)

    def __str__(self) -> str:
        return "\n".join(f"{n:6} x {cls}\n         first: {self.first[cls][:600]}" for cls, n in sorted(self.count.items()))


def _raised_in(exc: BaseException) -> str:
    """The innermost app function on the traceback: which stage raised."""
    frame = traceback.extract_tb(exc.__traceback__)[-1]
    return f"{Path(frame.filename).name}:{frame.name}"


def language_class(language) -> str:
    return "blank" if not language else language


def check_grade(match: Match, found: Findings, origin: str) -> str:
    """grade() and its two mirrors on one Match. Returns the tier ("" when grade itself raised)."""
    found.points += 1
    brief = (f"{origin}: title={match.title_sim} author={match.author_sim} margin={match.margin} "
             f"coverage={match.title_window_coverage} known={match.author_known} knockoff={match.knockoff} "
             f"language={match.language!r} adaptation={match.unasked_adaptation!r} product={bool(match.product)}")
    try:
        tier = grade(match)
    except Exception as exc:  # noqa: BLE001 - any exception is the finding
        found.add(f"crash: grade raised {type(exc).__name__}", f"{brief} -> {exc}")
        return ""
    if tier not in MATCH_TIERS:
        found.add(f"grade outside the set: {tier!r}", brief)
    try:
        mirrored = bench.grade_with(match, bench.GRADE_GATES)
        reasons = bench.limiting_reasons(match)
    except Exception as exc:  # noqa: BLE001
        found.add(f"crash: the mirror raised {type(exc).__name__}", f"{brief} -> {exc}")
        return tier
    if mirrored != tier:
        found.add(f"mirror: grade_with says {mirrored}, grade says {tier}", brief)
    if tier == "HIGH" and reasons:
        lang = f" (language {language_class(match.language)})" if "foreign" in reasons else ""
        found.add(f"mirror: HIGH with limiting reason {','.join(reasons)}{lang}", brief)
    if tier != "HIGH" and not reasons:
        found.add(f"mirror: {tier} with no limiting reason", brief)
    return tier


def check_match(unit: BookUnit, fake: FakeCatalog, hours: float, found: Findings, origin: str,
                asin_override: str = "", pin_failures: int = 0) -> Match | None:
    where = (f"{origin}: name={unit.name[:80]!r} title_guess={unit.title_guess[:80]!r} "
             f"author_guess={unit.author_guess[:60]!r} hours={hours} pool={len(fake.pool)} mode={fake.mode} "
             f"pin={asin_override!r}/{pin_failures}")
    found.matches += 1
    try:
        match = match_unit(unit, asin_override=asin_override, local_hours=hours, pin_failures=pin_failures)
    except Exception as exc:  # noqa: BLE001
        found.add(f"crash: match_unit raised {type(exc).__name__} in {_raised_in(exc)}", f"{where} -> {exc}")
        return None
    if match.confidence not in MATCH_TIERS:
        found.add(f"match_unit tier outside the set: {match.confidence!r}", where)
    tier = check_grade(match, found, where)
    if tier and tier != match.confidence and not match.asin_override:
        found.add(f"match_unit says {match.confidence}, grade() of the same match says {tier}", where)
    if bool(match.product) != (match.confidence != "NONE"):
        found.add("a product at NONE, or a tier without a product", where)
    if match.product and not match.asin_override:
        reachable = {p.asin for p in fake.pool} | {unit.asin_hint}
        if match.product.asin not in reachable:
            found.add("picked an ASIN no search returned", where)
        if any(alt["asin"] == match.product.asin for alt in match.alternates):
            found.add("the pick is listed among its own alternates", where)
        if len(match.alternates) > 4:
            found.add("more than four alternates", where)
        for name in ("score", "margin", "title_sim", "author_sim", "title_window_coverage"):
            value = getattr(match, name)
            if not (isinstance(value, (int, float)) and math.isfinite(value)):
                found.add(f"non-finite {name}", where)
            elif not -1e-9 <= value <= 1.0 + 1e-9:
                found.add(f"{name} outside [0, 1]", f"{where} {name}={value}")
    # what the pipeline does next with it: the collection tier, the runtime check, the action
    try:
        avg = hours / len(unit.files) if unit.files else 0
        pipeline_tier = match.confidence
        if units.looks_like_collection(unit, avg) and pipeline_tier in ("LOW", "NONE"):
            pipeline_tier = "COLLECTION"
        audit = pipeline.Audit(unit=unit, hours=hours, match=match)
        ok = audit.runtime_matches
        action = choose_action(unit, pipeline_tier, ok, {"mp3"})
    except Exception as exc:  # noqa: BLE001
        found.add(f"crash: the decision raised {type(exc).__name__}", f"{where} -> {exc}")
        return match
    if pipeline_tier not in ALL_TIERS:
        found.add(f"pipeline tier outside the set: {pipeline_tier!r}", where)
    if action not in ACTIONS:
        found.add(f"unknown action {action!r}", where)
    if action in ("convert", "merge+convert", "remux") and not (pipeline_tier in ("HIGH", "MEDIUM") and ok is True):
        found.add("a convert action below MEDIUM or without runtime agreement", where)
    if action != "review" and match.product and match.product.hours <= 0 and action != "hold":
        found.add("a convert action on a catalogue answer with no runtime", where)
    return match


def explore(seed: int = SEED) -> Findings:
    rng = random.Random(seed)
    found = Findings()

    def run(unit, pool, records, mode, hours, origin, **pin):
        fake = FakeCatalog(pool, records, mode)
        with mock.patch.object(catalog, "search", fake.search), mock.patch.object(catalog, "book", fake.book), \
                mock.patch.object(urllib.request, "urlopen", _offline):
            match = check_match(unit, fake, hours, found, origin, **pin)
            crashing = [p for p in pool if p.subtitle and not (p.title or "").strip()]
            if match is None and crashing:
                # F2 hides everything else about this point: check the rest of it without those answers
                fake.pool = [p for p in pool if p not in crashing]
                match = check_match(unit, fake, hours, found, origin + "/without-blank-title", **pin)
            return match

    # 1. every edge title as a library folder and as an ingest folder, against three small pools
    exact = lambda t: [Product(asin="B0EXACT000", title=t, authors=["Frank Herbert"], hours=10.0, language="english")]  # noqa: E731
    for title in TITLES:
        for ingest in (False, True):
            for name in (title, f"Frank Herbert - {title}"):
                unit = scanned_unit(name.replace("\x00", ""), ingest=ingest)
                run(unit, [], {}, "empty", 10.0, "edge/no-answer")
                run(unit, exact(title), {}, "all", 10.0, "edge/exact")
                run(unit, [product(rng) for _ in range(5)], {}, "all", 10.0, "edge/random5")

    # 2. the brief's named shapes
    nested = [Product(asin=f"B0NEST{i:04d}", title=t, authors=["Frank Herbert"], hours=10.0, language="english")
              for i, t in enumerate(["Dune", "Dune Messiah", "Dune Messiah and Children of Dune", "Children of Dune",
                                     "The Dune", "Dun", "D", ""])]
    for name in ("Frank Herbert - Dune", "Frank Herbert - Dune Messiah", "Dune", "Dune Messiah", "Frank Herbert - D"):
        for hours in LOCAL_HOURS:
            run(scanned_unit(name), nested, {}, "all", hours, "substring-titles")
            run(scanned_unit(name), nested[::-1], {}, "filter", hours, "substring-titles/filter")
    for hours, cat_hours in itertools.product(LOCAL_HOURS, CATALOG_HOURS):
        lone = [Product(asin="B0HOURS000", title="Dune", authors=["Frank Herbert"], hours=cat_hours, language="english")]
        run(scanned_unit("Frank Herbert - Dune"), lone, {}, "all", hours, "hours")
    for authors in ([], [""], [" "], ["", "Frank Herbert"]):
        for name in ("Frank Herbert - Dune", "Dune"):
            lone = [Product(asin="B0NOAUTH00", title="Dune", authors=authors, hours=10.0, language="english")]
            run(scanned_unit(name), lone, {}, "all", 10.0, "empty-author-list")
            run(scanned_unit(name), lone + nested, {}, "all", 10.0, "empty-author-list/with-rivals")
    twin = Product(asin="B0TWICE000", title="Dune", authors=["Frank Herbert"], hours=10.0, language="english")
    other = Product(asin="B0TWICE000", title="Dune Messiah", authors=["Brian Herbert"], hours=3.0, language="german")
    for pool in ([twin, twin], [twin, Product(**vars(twin))], [twin, other], [other, twin], [twin, other] * 50):
        for name in ("Frank Herbert - Dune", "Dune Messiah"):
            run(scanned_unit(name), pool, {"B0TWICE000": {"asin": "B0TWICE000", "title": "Dune"}}, "all", 10.0,
                "asin-twice")
    for language in LANGUAGES:
        lone = [Product(asin="B0LANG0000", title="Dune", authors=["Frank Herbert"], hours=10.0, language=language)]
        run(scanned_unit("Frank Herbert - Dune"), lone, {}, "all", 10.0, "language/lone")
        run(scanned_unit("Frank Herbert - Dune"), lone + nested[1:4], {}, "all", 10.0, "language/with-rivals")

    # 3. a 200-candidate pool: random, near-identical, and all the same work
    for i in range(12):
        big = [product(rng, asin=ASINS[j]) for j in range(200)]
        run(scanned_unit(folder_name(rng), ingest=bool(i % 2)), big, {a: audnex_record(rng, a) for a in ASINS[:200]},
            "all", rng.choice(LOCAL_HOURS), "pool-200/random")
    clones = [Product(asin=ASINS[j], title="Dune", authors=["Frank Herbert"], hours=10.0 + j / 100, language="english")
              for j in range(200)]
    near = [Product(asin=ASINS[j], title=f"Dune {j}", authors=["Frank Herbert"], hours=10.0, language="english")
            for j in range(200)]
    for pool in (clones, near, clones + near):
        run(scanned_unit("Frank Herbert - Dune"), pool, {}, "all", 10.0, "pool-200/same-work")
        run(scanned_unit("Dune"), pool, {a: {"asin": a, "title": "Dune"} for a in ASINS[:200]}, "all", 10.0,
            "pool-200/same-work")

    # 4. random units against random pools, pins and ASIN hints included
    for i in range(700):
        size = rng.choice([0, 1, 1, 2, 3, 5, 10, 25])
        asins = rng.sample(ASINS, size) if rng.random() < 0.8 else [rng.choice(ASINS) for _ in range(size)]
        pool = [sparse_audible(rng, a) if rng.random() < 0.3 else product(rng, asin=a) for a in asins]
        records = {a: audnex_record(rng, a) for a in rng.sample(ASINS, 40)}
        kind = rng.random()
        if kind < 0.45:
            unit = scanned_unit(folder_name(rng), n_files=rng.choice([0, 1, 3, 40]), n_parts=rng.choice([1, 1, 2, 9]))
        elif kind < 0.8:
            unit = scanned_unit(folder_name(rng), n_files=rng.choice([1, 3]), ingest=True,
                                ext=rng.choice([".mp3", ".m4b"]))
        else:
            unit = raw_unit(rng)
        pin = {}
        if rng.random() < 0.2:
            pin = {"asin_override": rng.choice(ASINS + ["", "📚", " "]), "pin_failures": rng.choice([0, 1, 2, 3, 99, -1])}
        run(unit, pool, records, rng.choice(["all", "all", "filter", "empty"]), rng.choice(LOCAL_HOURS),
            f"random#{i}", **pin)

    # 5. grade() against its mirrors on generated evidence, well outside what the scorer emits
    some = Product(asin="B0", title="T", authors=["A"], hours=10.0)
    values = [-0.5, 0.0, 0.049, 0.05, 0.051, 0.099, 0.1, 0.59, 0.6, 0.601, 0.849, 0.85, 0.851, 0.879, 0.88,
              0.881, 0.959, 0.96, 0.961, 1.0, 1.5]
    for i in range(20000):
        check_grade(Match(
            product=None if rng.random() < 0.03 else some,
            title_sim=rng.choice(values) if rng.random() < 0.7 else rng.uniform(-0.2, 1.2),
            author_sim=rng.choice(values) if rng.random() < 0.7 else rng.uniform(-0.2, 1.2),
            margin=rng.choice(values) if rng.random() < 0.7 else rng.uniform(-0.2, 1.2),
            title_window_coverage=rng.choice([0.0, 0.3, 0.599, 0.6, 0.601, 1.0]),
            language=rng.choice(LANGUAGES), author_known=rng.random() < 0.6, knockoff=rng.random() < 0.1,
            unasked_adaptation=rng.choice(["", "", "", "a Dramatized Adaptation"]),
            rejected=rng.choice(["", "", "no catalog match"]), asin_pin_failed=rng.choice(["", "", "B0PIN"]),
        ), found, "grade-grid")
    return found


# ------------------------------------------------------------------------- known findings
def blank_language_match() -> tuple[Match, FakeCatalog]:
    """F1's minimal input: one catalogue answer, exact title and author, no language field."""
    fake = FakeCatalog([Product.from_audible({
        "asin": "B0BLANK000", "title": "Dune", "authors": [{"name": "Frank Herbert"}], "runtime_length_min": 600,
    })], {})
    with mock.patch.object(catalog, "search", fake.search), mock.patch.object(catalog, "book", fake.book):
        return match_unit(scanned_unit("Frank Herbert - Dune"), local_hours=10.0), fake


def blank_title_pool() -> list[Product]:
    """F2's minimal input: the real book, and beside it an answer with a subtitle but no title."""
    return [
        Product(asin="B0REALDUNE", title="Dune", authors=["Frank Herbert"], hours=10.0, language="english"),
        Product(asin="B0BLANKTTL", title="", subtitle="A Novel", authors=["Frank Herbert"], hours=10.0,
                language="english"),
    ]


#: finding class (as explore() names it) -> what it is. Each must still reproduce; nothing else may appear.
KNOWN_FINDINGS = {
    "mirror: HIGH with limiting reason foreign (language blank)":
        "F1: a catalogue answer with no language is English to grade() and foreign to the scorer, "
        "the panel and stage E",
    "crash: match_unit raised ValueError in matching.py:title_alone":
        "F2: a catalogue answer with a subtitle and a blank title raises out of match_unit",
}


class TestMatcherProperties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        started = time.monotonic()
        cls.found = explore()
        cls.seconds = time.monotonic() - started

    def test_no_generated_point_crashes_or_leaves_the_tier_set_beyond_the_known_findings(self):
        unknown = {c: n for c, n in self.found.count.items() if c not in KNOWN_FINDINGS}
        self.assertEqual(unknown, {}, f"\nnew finding class(es) - write them up, do not fix here:\n{self.found}")

    def test_every_known_finding_still_reproduces(self):
        gone = [c for c in KNOWN_FINDINGS if not self.found.count[c]]
        self.assertEqual(gone, [], "a known finding no longer reproduces: remove it from KNOWN_FINDINGS "
                                   "and say what fixed it")

    def test_the_run_is_large_seeded_and_fast(self):
        self.assertGreater(self.found.matches, 1700)
        self.assertGreater(self.found.points, 21000)  # graded points: 20,000 on the grid, the rest matches
        self.assertLess(self.seconds, 10.0)
        again = explore()
        self.assertEqual((again.points, dict(again.count), again.first), (self.found.points, dict(self.found.count),
                                                                         self.found.first))

    def test_the_checks_can_fail(self):
        some = Match(product=Product(asin="B0", title="T", authors=["A"]), title_sim=1.0, author_sim=1.0, margin=0.5,
                     author_known=True)
        for patch, expected in (
            (mock.patch.object(matching, "grade", lambda m: "MAYBE"), "grade outside the set: 'MAYBE'"),
            (mock.patch.object(bench, "grade_with", lambda m, g: "LOW"), "mirror: grade_with says LOW, grade says HIGH"),
            (mock.patch.object(bench, "limiting_reasons", lambda m: ["title"]), "mirror: HIGH with limiting reason title"),
            (mock.patch.object(bench, "grade_with", mock.Mock(side_effect=KeyError("x"))),
             "crash: the mirror raised KeyError"),
        ):
            found = Findings()
            with patch, mock.patch(f"{__name__}.grade", lambda m: matching.grade(m)):
                check_grade(some, found, "self-test")
            self.assertIn(expected, found.count, found.count)
        found = Findings()
        fake = FakeCatalog([], {}, "empty")
        with mock.patch(f"{__name__}.match_unit", mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertIsNone(check_match(scanned_unit("Dune"), fake, 10.0, found, "self-test"))
        self.assertEqual([c.split(" in ")[0] for c in found.count], ["crash: match_unit raised RuntimeError"])
        found = Findings()
        with mock.patch(f"{__name__}.match_unit", lambda *a, **k: Match(product=some.product, confidence="NONE")):
            check_match(scanned_unit("Dune"), fake, 10.0, found, "self-test")
        self.assertIn("a product at NONE, or a tier without a product", found.count)
        self.assertIn("picked an ASIN no search returned", found.count)

    def test_f1_blank_language_is_high_through_match_unit_while_the_panel_shows_a_language_penalty(self):
        match, _ = blank_language_match()
        self.assertEqual(match.language, "")
        self.assertEqual((match.confidence, grade(match)), ("HIGH", "HIGH"))
        # the scorer treated the same answer as not English: x0.85 on the score, flag set for the panel
        self.assertTrue(match.language_penalized)
        self.assertEqual(match.score, round(matching.LANGUAGE_PENALTY, 3))
        # stage E's reasons call it foreign, so the bench would fail this HIGH as an explanation violation
        reasons = bench.limiting_reasons(match)
        self.assertEqual(reasons, ["foreign"])
        self.assertEqual(bench.explanation_problems(match, "HIGH", reasons, ["B0BLANK000"]),
                         ["HIGH despite foreign", "capped production at HIGH"])
        # and it would be written unseen: runtime agrees, HIGH is an auto-convert tier
        unit = scanned_unit("Frank Herbert - Dune")
        self.assertEqual(choose_action(unit, match.confidence, runtime_ok(10.0, match.product.hours, 0.15), {"mp3"}),
                         "convert")


    def test_f2_a_blank_title_with_a_subtitle_raises_out_of_match_unit(self):
        def run(pool):
            fake = FakeCatalog(pool, {})
            with mock.patch.object(catalog, "search", fake.search), mock.patch.object(catalog, "book", fake.book):
                return match_unit(scanned_unit("Frank Herbert - Dune"), local_hours=10.0)

        real, blank = blank_title_pool()
        self.assertEqual((run([real]).confidence, run([real]).asin), ("HIGH", "B0REALDUNE"))
        for title in ("", " ", None):
            with self.subTest(title=title):
                blank.title = title
                try:
                    run([real, blank])
                except ValueError as exc:  # assertRaises would drop the traceback that names the stage
                    self.assertEqual(_raised_in(exc), "matching.py:title_alone")
                else:
                    self.fail("no longer raises: remove F2 from KNOWN_FINDINGS and say what fixed it")
        # the subtitle is what reaches the empty max(): without it, or with any title text, no crash
        blank.title, blank.subtitle = "", ""
        self.assertEqual(run([real, blank]).asin, "B0REALDUNE")
        blank.title, blank.subtitle = "()", "A Novel"
        self.assertEqual(run([real, blank]).asin, "B0REALDUNE")


class TestLongTitles(unittest.TestCase):
    """Not a crash and not a mirror disagreement, so not in KNOWN_FINDINGS: an observation with a bound.

    ``window_sim`` scores every run of consecutive words, so one match costs about words^3. A
    folder name is capped at 255 bytes by the filesystem (about 66 short words: under 0.3 s here); an
    embedded title tag, which the ingest retry searches with, has no cap.
    """

    def test_a_folder_name_at_the_filesystem_cap_still_matches_in_seconds(self):
        name = " ".join(f"w{i}" for i in range(69))[:255]
        self.assertGreater(len(name.encode()), 250)
        fake = FakeCatalog([Product(asin="B0LONG0000", title="Some Other Book", subtitle="A Novel",
                                    authors=["Frank Herbert"], hours=10.0, language="english")], {})
        started = time.monotonic()
        with mock.patch.object(catalog, "search", fake.search), mock.patch.object(catalog, "book", fake.book):
            match = match_unit(scanned_unit(name), local_hours=10.0)
        self.assertEqual(match.confidence, "NONE")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_the_window_count_is_quadratic_in_words(self):
        calls = []
        with mock.patch.object(matching, "sim", lambda a, b: calls.append(a) or 0.0):
            matching.window_sim(" ".join(f"w{i}" for i in range(40)), "Dune")
        self.assertEqual(len(calls), 40 * 41 // 2)


class TestChooseAction(unittest.TestCase):
    def test_every_combination_returns_a_known_action_and_never_converts_what_it_must_not(self):
        shapes = {
            "no files": BookUnit(path="/l/x", name="x", parent="/l", parts=[]),
            "empty part": BookUnit(path="/l/x", name="x", parent="/l", parts=[Part(name="x", files=[])]),
            "one m4b": BookUnit(path="/l/x.m4b", name="x", parent="/l", parts=[Part(name="x", files=["/l/x.m4b"])]),
            "one M4A": BookUnit(path="/l/x.M4A", name="x", parent="/l", parts=[Part(name="x", files=["/l/x.M4A"])]),
            "one mp3": BookUnit(path="/l/x", name="x", parent="/l", parts=[Part(name="x", files=["/l/x/1.mp3"])]),
            "no extension": BookUnit(path="/l/x", name="x", parent="/l", parts=[Part(name="x", files=["/l/x/1"])]),
            "unicode": BookUnit(path="/l/百", name="百", parent="/l", parts=[Part(name="百", files=["/l/百/📚.m4b"])]),
            "two parts": BookUnit(path="/l/x", name="x", parent="/l",
                                  parts=[Part(name="a", files=["/l/x/a/1.mp3"]), Part(name="b", files=["/l/x/b/1.m4b"])]),
        }
        tiers = ALL_TIERS + ("", "high", "Medium", "ERR", " HIGH", None)
        codec_sets = (set(), {"aac"}, {"mp3"}, {"aac", "mp3"}, {"AAC"}, {""})
        n = 0
        for (shape, unit), tier, ok, codecs in itertools.product(shapes.items(), tiers, (True, False), codec_sets):
            action = choose_action(unit, tier, ok, codecs)
            n += 1
            where = (shape, tier, ok, codecs)
            self.assertIn(action, ACTIONS, where)
            if tier not in ("HIGH", "MEDIUM"):
                self.assertEqual(action, "review", where)
            elif not ok:
                self.assertEqual(action, "hold", where)
            else:
                self.assertIn(action, ("remux", "convert", "merge+convert"), where)
                self.assertEqual(action == "remux", shape in ("one m4b", "one M4A", "unicode") and codecs == {"aac"}, where)
                self.assertEqual(action == "merge+convert", shape == "two parts", where)
        self.assertEqual(n, len(shapes) * len(tiers) * 2 * len(codec_sets))


class TestRuntimeOk(unittest.TestCase):
    FINITE = (-1e6, -10.0, -0.004, -0.0, 0.0, 0.004, 1.0, 8.5, 9.99, 10.0, 10.01, 11.5, 11.83, 14.23, 1e6)
    TOLERANCES = (-1.0, -0.0, 0.0, 0.05, 0.15, 0.2, 0.35, 1.0, 100.0)

    def test_it_is_a_bool_and_an_unknown_or_negative_catalogue_runtime_is_never_agreement(self):
        n = 0
        for local, cat, tol in itertools.product(self.FINITE, self.FINITE, self.TOLERANCES):
            ok = runtime_ok(local, cat, tol)
            n += 1
            self.assertIsInstance(ok, bool, (local, cat, tol))
            if cat <= 0:
                self.assertFalse(ok, (local, cat, tol))
            else:
                self.assertEqual(ok, abs(local - cat) / cat <= tol, (local, cat, tol))
                if local == cat and tol >= 0:
                    self.assertTrue(ok, (local, cat, tol))
                if tol < 0:
                    self.assertFalse(ok, (local, cat, tol))
        self.assertEqual(n, len(self.FINITE) ** 2 * len(self.TOLERANCES))

    def test_a_wider_tolerance_never_turns_agreement_into_disagreement(self):
        for local, cat in itertools.product(self.FINITE, self.FINITE):
            seen_ok = False
            for tol in sorted(self.TOLERANCES):
                ok = runtime_ok(local, cat, tol)
                self.assertFalse(seen_ok and not ok, (local, cat, tol))
                seen_ok = seen_ok or ok

    def test_non_finite_hours_never_raise_and_never_agree(self):
        for local, cat in itertools.product((math.nan, math.inf, -math.inf, 10.0), (math.nan, math.inf, -math.inf, 10.0)):
            if (local, cat) == (10.0, 10.0):
                continue
            ok = runtime_ok(local, cat, 0.15)
            self.assertIsInstance(ok, bool, (local, cat))
            self.assertFalse(ok, (local, cat))

    def test_the_delta_the_panel_shows_matches_the_check(self):
        for local, cat in itertools.product(self.FINITE, self.FINITE):
            delta = matching.runtime_delta_pct(local, cat)
            if local <= 0 or cat <= 0:
                self.assertIsNone(delta, (local, cat))
            else:
                self.assertEqual(delta, int(round((local - cat) / cat * 100)), (local, cat))


if __name__ == "__main__":
    _found = explore()
    print(f"{_found.matches} match_unit calls, {_found.points} graded points")
    print(_found)
