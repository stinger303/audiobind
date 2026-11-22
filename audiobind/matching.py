"""Pick the right catalog entry for a unit, and say how sure we are."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import catalog, markers
from .catalog import Product
from .units import BookUnit
from .util import norm, sim, title_sim


@dataclass
class Match:
    product: Product | None = None
    score: float = 0.0
    margin: float = 0.0
    title_sim: float = 0.0
    author_sim: float = 0.0
    author_known: bool = False
    author_from_series: bool = False
    asin_override: bool = False
    #: catalog language of the matched product (grading caps non-English at MEDIUM)
    language: str = "english"
    #: set when a user-pinned ASIN had no catalog data; nothing is matched then (no search)
    asin_pin_failed: str = ""
    #: set when a user-pinned ASIN had no catalog data for PIN_GIVE_UP_AFTER checks in a row: the
    #: pin stopped blocking and this match is a normal search (the pipeline never auto-converts it)
    pin_abandoned: str = ""
    confidence: str = "NONE"
    #: True when the scorer applied the non-English x0.85 penalty to this product
    language_penalized: bool = False
    #: (your files - catalog) / catalog as a whole percent; None when either runtime is unknown
    runtime_delta_pct: int | None = None
    #: what the search compared against (the folder's title/author guesses), for the evidence panel
    query_title: str = ""
    query_author: str = ""
    #: other candidates the search scored highly (other editions of the same work);
    #: [{"asin", "title", "authors", "score", "hours", "title_sim", "author_sim", "language",
    #: "language_penalized", "runtime_delta_pct", "knockoff", "corroborated"}], best first,
    #: chosen one excluded
    alternates: list[dict] = field(default_factory=list)
    #: the chosen product is a summary / review / study guide / zero-runtime catalog stub
    #: (see is_knockoff); grading never lets it past LOW
    knockoff: bool = False
    #: Audnex agreed with the chosen product (True), has a record that disagrees (False), or gave
    #: no evidence either way (None): never asked because nothing competed for the margin, no
    #: record, or the lookup failed
    corroborated: bool | None = None
    #: set when the best candidate's title did not resemble the wanted one (below TITLE_FLOOR):
    #: nothing is matched then, and this is the note that names what was rejected and why
    rejected: str = ""
    #: share of the folder title's tokens covered by the window behind floor_sim, measured when
    #: the title score (whole-string readings) is below floor_sim - a window matched better than
    #: any whole reading; 1.0 otherwise. Below MIN_HIGH_WINDOW_COVERAGE the grade is never HIGH
    title_window_coverage: float = 1.0
    #: set when the chosen product is an adaptation (Dramatized Adaptation, GraphicAudio, full
    #: cast, audio drama - see adaptation_label) and nothing the folder said asks for one: the
    #: phrase naming it ("a Dramatized Adaptation"). grade() never says HIGH for it
    unasked_adaptation: str = ""
    #: the other direction: the folder names an adaptation ("(Full Cast)") and the chosen product
    #: carries no adaptation marker - the phrase the folder asked for ("a full-cast production").
    #: grade() never says HIGH for it either
    missing_adaptation: str = ""
    edition_conflict: str = ""
    #: set when the chosen product contradicts something the folder states explicitly (another
    #: series position: "catalog hit is Book 1 of its series and your folder says Book 2"): every
    #: other candidate was vetoed too, so this is the best of a bad pool. grade() holds it at LOW
    veto: str = ""

    @property
    def asin(self) -> str:
        return self.product.asin if self.product else ""


#: the best candidate's title must resemble the best token window of the wanted title at least this much (the same
#: ``floor_sim`` the scorer uses for title_sim) or the unit is no match at all - never a LOW match with another book's title
TITLE_FLOOR = 0.60

def window_sim(wanted: str, candidate: str) -> float:
    """Best ``sim`` of ``candidate`` against any run of consecutive tokens of ``wanted``.

    The windows include the whole string, so this is never below ``sim(wanted, candidate)``; it
    keeps a long or mangled folder name from dragging a correct title under TITLE_FLOOR.
    """
    tokens = (wanted or "").split()
    return max(
        (sim(" ".join(tokens[i:j]), candidate)
         for i in range(len(tokens)) for j in range(i + 1, len(tokens) + 1)),
        default=0.0,
    )


#: the catalog title alone must score this before title + subtitle is scored at all: a subtitle
#: that merely names the query ("... Hunting Lee Child's Jack Reacher ...") never carries a title
SUBTITLE_GATE = 0.60
#: "Count to Ten aka Private Delhi": an alternate name inside a catalog title
_AKA_RE = re.compile(r"\s+(?:aka|a\.k\.a\.?)\s+", re.IGNORECASE)


def strip_brackets(text: str) -> str:
    """``text`` with bracketed / braced / parenthetical groups removed ("Lock In (Narrated by X)" -> "Lock In")."""
    return re.sub(r"\s+", " ", _BRACKETED.sub(" ", text or "")).strip(" -_.")


def catalog_titles(title: str) -> list[str]:
    """The catalog title, plus each side of an "aka" in it, each scored on its own - and each of
    those again with its bracketed / parenthetical groups removed, so a narrator or edition tag
    on the catalog side never keeps a plain folder name from matching."""
    title = (title or "").strip()
    sides = [s.strip() for s in _AKA_RE.split(title)] if _AKA_RE.search(title) else []
    out: list[str] = []
    for t in [title] + [s for s in sides if s and s != title]:
        for v in (t, strip_brackets(t)):
            if v and v not in out:
                out.append(v)
    return out


def catalog_full_titles(product: Product) -> list[str]:
    """Title + subtitle, and the same with brackets removed. Only ever scored behind SUBTITLE_GATE,
    which is measured on the title alone, so a stripped subtitle never stands in for a title."""
    full = f"{product.title} {product.subtitle}".strip()
    out = [full]
    stripped = strip_brackets(full)
    if stripped and stripped != full:
        out.append(stripped)
    return out


def floor_candidates(wanted: str, product: Product) -> list[str]:
    """The catalog strings ``floor_sim`` scores: the title (and each "aka" side of it), then
    title + subtitle only when the title alone already scored SUBTITLE_GATE. ``window_coverage``
    walks this same list, so it can never describe a subtitle the floor score did not see."""
    candidates = catalog_titles(product.title)
    if product.subtitle and title_alone(wanted, product) >= SUBTITLE_GATE:
        candidates = candidates + [f for f in catalog_full_titles(product) if f not in candidates]
    return candidates


def floor_sim(wanted: str, product: Product) -> float:
    """The title score TITLE_FLOOR is applied to: the best window of ``wanted`` against any of
    ``floor_candidates``.

    Only the floor gate uses this. It is never the title score: a catalog title equal to one
    window of the folder name ("Dune" inside "Dune Messiah") would score 1.0 and beat the real book.
    """
    return max((window_sim(wanted, c) for c in floor_candidates(wanted, product)), default=0.0)


def title_alone(wanted: str, product: Product) -> float:
    """How well the catalog title by itself (or one "aka" side of it) matches the best window of
    ``wanted`` - the SUBTITLE_GATE measure. A window, so "Moneyball The Art of Winning" still lets
    "Moneyball" + its subtitle count; a decoy whose title appears nowhere in the query never does.

    A catalog answer with no usable title (blank, whitespace, None) scores 0.0: it never opens the
    subtitle gate, and it must not raise and take the rest of the pool down with it."""
    return max((window_sim(wanted, t) for t in catalog_titles(product.title)), default=0.0)


def window_coverage(wanted: str, product: Product) -> float:
    """Share of ``wanted``'s tokens covered by the window that gives ``floor_sim`` (1.0 = whole string).

    Same windows, same candidates (``floor_candidates``) as ``floor_sim``: a subtitle behind
    SUBTITLE_GATE that the floor score never saw cannot widen the window either.
    """
    tokens = (wanted or "").split()
    if not tokens:
        return 1.0
    best, best_len = -1.0, len(tokens)
    for candidate in floor_candidates(wanted, product):
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                score = sim(" ".join(tokens[i:j]), candidate)
                # ties go to the longer window: the measure must not invent a short match
                if score > best or (score == best and j - i > best_len):
                    best, best_len = score, j - i
    return best_len / len(tokens)


#: bracketed release tags and parentheticals: "[TRACKER]", "{Narrator}", "(Unabridged)"
_BRACKETED = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)")
#: a leading sequence: "01 - Title", "3. Title"
_LEADING_SEQ = re.compile(r"^\s*\d{1,3}(?:\.\d)?\s*(?:-|–|—|\.)\s*(?=\S)")
#: "<Series> NN - <Title>"
_SERIES_SEQ_TITLE = re.compile(r"^(?P<x>.+?)\s+\d{1,3}(?:\.\d)?\s*(?:-|–|—|\.)\s*(?P<y>\S.*)$")
#: "Private 18 Private Rome": a bare "<word> NN <rest>", no dash
_WORD_SEQ_REST = re.compile(r"^(?P<x>[A-Za-z]+)\s+\d{1,3}(?:\.\d)?\s+(?P<y>[A-Za-z].*)$")
#: "<Title> - <Series>, Book N", "<Title> - <Series> - Book N", "<Title> — <Series> Book N"
_SERIES_BOOK_TAIL = re.compile(
    r"^(?P<t>\S.*?)\s+[-–—]\s+(?P<s>[^-–—]*[A-Za-z][^-–—]*?)\s*(?:,|\s[-–—])?\s*"
    r"\b(?:book|volume|vol\.?)\s*\d{1,3}(?:\.\d)?\s*$",
    re.IGNORECASE,
)
#: the series part and the title part must be this alike before the title part stands alone
SERIES_ECHO_SIM = 0.8


def _echoes(series: str, title: str) -> bool:
    """The series part is echoed by the title part: alike, or every series word is in the title
    ("Private" in "Private Dublin" - sim no longer gives one word inside two a containment boost)."""
    words = set(norm(series).split())
    return sim(series, title) >= SERIES_ECHO_SIM or bool(words) and words <= set(norm(title).split())


def title_variants(title: str) -> list[str]:
    """The query title plus its normalised readings, each scored whole-string against a candidate.

    a. the raw title; b. bracketed groups and parentheticals removed; c. a leading sequence
    (``NN - `` / ``NN. ``) removed; d. ``<X> NN - <Y>`` with X echoed in Y -> Y, the real title
    ("Dungeon Crawler Carl 01 - Dungeon Crawler Carl" -> "Dungeon Crawler Carl"); e. a bare
    ``<word> NN <rest>`` with a rest of two words or more -> rest ("Private 18 Private Rome" ->
    "Private Rome"); f. a trailing `` - <Series>, Book N`` tail dropped ("Rise of Empire -
    Riyria Revelations, Book 2" -> "Rise of Empire").
    """
    raw = (title or "").strip()
    variants = [raw]
    stripped = strip_brackets(raw)
    variants.append(stripped)
    variants.append(_LEADING_SEQ.sub("", stripped).strip())
    echo = _SERIES_SEQ_TITLE.match(stripped)
    if echo and _echoes(echo.group("x"), echo.group("y")):
        variants.append(echo.group("y").strip())
    bare = _WORD_SEQ_REST.match(stripped)
    if bare and len(bare.group("y").split()) >= 2:
        variants.append(bare.group("y").strip())
    tail = _SERIES_BOOK_TAIL.match(stripped)
    if tail:
        variants.append(tail.group("t").strip())
    out: list[str] = []
    for v in variants:
        if v and v not in out:
            out.append(v)
    return out


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
          "xi": 11, "xii": 12}
_POSITION_NUMBER = r"\d{1,3}(?:\.\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) \
    + "|" + "|".join(sorted(_ROMAN, key=len, reverse=True))
#: an explicit series position: "Book 2", "Book Two", "Vol. II", "Volume 3", "Tome 1". A range
#: ("Book 1-3", "Books 1 to 3": a box set) is never one position; "#1" and "No. 1" are not read
#: ("#1 New York Times bestseller", "Private: No. 1 Suspect")
_POSITION_RE = re.compile(
    r"(?<![a-z0-9])(?:book|bk|volume|vol|tome)\.?\s*(?P<n>" + _POSITION_NUMBER + r")(?![a-z0-9]|\.\d)"
    r"(?!\s*(?:-|–|—|to|through|thru|&|and)\s*(?:\d|" + "|".join(_NUMBER_WORDS) + r")\b)",
    re.IGNORECASE,
)
#: a bare number ending a title ("Chronicles of the Northern Kingdom 2"): only weak evidence of a
#: position - it counts in a conflict only when the other side states a position explicitly
_TRAILING_NUMBER_RE = re.compile(r"(?<=[A-Za-z])[\s,]+(?P<n>\d{1,3}(?:\.\d+)?)\s*$")


def _position(token: str) -> str:
    token = token.lower()
    if token in _NUMBER_WORDS:
        return str(_NUMBER_WORDS[token])
    if token in _ROMAN:
        return str(_ROMAN[token])
    value = float(token)
    return str(int(value)) if value == int(value) else f"{value:g}"


def series_positions(*texts: str) -> tuple[set[str], set[str]]:
    """(explicit, weak) series positions named in ``texts``: "Book 2" is explicit, a bare trailing
    number ("Kingdom 2") weak. Positions are canonical ("03", "Three", "III" -> "3")."""
    strong: set[str] = set()
    weak: set[str] = set()
    for text in texts:
        text = text or ""
        strong.update(_position(m.group("n")) for m in _POSITION_RE.finditer(text))
        trailing = _TRAILING_NUMBER_RE.search(strip_brackets(text).strip(" -_.,;:"))
        if trailing:
            weak.add(_position(trailing.group("n")))
    return strong, weak


def position_conflict(wanted: tuple[str, ...], product: Product) -> str:
    """The veto text when ``product`` is another volume than the one ``wanted`` (the folder's own
    words) names, else "".

    The product's positions come from its title, subtitle and catalog sequence (explicit). A
    conflict needs an explicit position on at least one side, a position on both, and no position
    in common: a folder that names no position never vetoes anything, and a number that is part of a
    title ("Fahrenheit 451") is only weak evidence."""
    q_strong, q_weak = series_positions(*wanted)
    c_strong, c_weak = series_positions(product.title, product.subtitle)
    sequence = (product.sequence or "").strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d+)?", sequence):
        c_strong.add(_position(sequence))
    elif sequence:
        c_strong.update(series_positions(sequence)[0])
    if not (q_strong or c_strong):
        return ""
    q, c = q_strong | q_weak, c_strong | c_weak
    if not q or not c or q & c:
        return ""

    def books(nums: set[str]) -> str:
        return " / ".join(f"Book {n}" for n in sorted(nums, key=float))

    return f"catalog hit is {books(c)} of its series and your folder says {books(q)}"


def title_score(wanted: str, product: Product) -> float:
    """The title score: best WHOLE-STRING ``title_sim`` of any variant against the catalog title
    (or one "aka" side of it), then title + subtitle only when the title alone (``title_alone``)
    reached SUBTITLE_GATE."""
    variants = title_variants(wanted)
    best = max((title_sim(v, t) for v in variants for t in catalog_titles(product.title)), default=0.0)
    if product.subtitle and max(best, title_alone(wanted, product)) >= SUBTITLE_GATE:
        best = max([best] + [title_sim(v, f) for v in variants for f in catalog_full_titles(product)])
    return best


#: the scorer's multiplier for a product whose catalog language is not English
LANGUAGE_PENALTY = 0.85


def language_penalized(language: str | None) -> bool:
    """The scorer's language rule: anything not starting with "english" (even blank) is penalized."""
    return not (language or "").lower().startswith("english")


def runtime_delta_pct(local_hours: float | None, catalog_hours: float | None) -> int | None:
    """(local - catalog) / catalog as a whole percent - the same measure runtime_ok checks.

    None when either side is unknown (0 or missing), so the panel never shows a made-up delta.
    """
    try:
        local, cat = float(local_hours or 0), float(catalog_hours or 0)
    except (TypeError, ValueError):
        return None
    if local <= 0 or cat <= 0:
        return None
    return int(round((local - cat) / cat * 100))


def infer_author_from_series(series: str) -> str:
    """'Jack Reacher Series' -> 'Lee Child', from the modal author of a catalog search."""
    import re
    from collections import Counter

    query = re.sub(r"(?i)\b(series|novels|collection|complete|box|set)\b", " ", series or "").strip()
    if not query:
        return ""
    counts: Counter[str] = Counter()
    for product in catalog.search(title=query, limit=10):
        for name in product.authors:
            counts[name] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _same_work(a: Product, b: Product) -> bool:
    """Same title and lead author. An adaptation marker in the title is not part of the work:
    "Rise of Empire [Dramatized Adaptation]" is another production of "Rise of Empire"."""
    return (_work_title(a.title) == _work_title(b.title)
            and norm((a.authors or [""])[0]) == norm((b.authors or [""])[0]))


#: title words that mark a catalog entry as something written *about* a book, not an edition of it
_KNOCKOFF_WORDS = re.compile(r"\b(summarized|summary|study\s+guide|workbook)\b", re.IGNORECASE)
#: "[Review] The Butcher's Masquerade ...", "Review: Atomic Habits"
_REVIEW_PREFIX = re.compile(r"^\W*(\[\s*review\s*\]|review\s*:)", re.IGNORECASE)


def _field(product, name: str) -> str:
    if isinstance(product, dict):
        return str(product.get(name) or "")
    return str(getattr(product, name, "") or "")


def _runtime_min(product) -> float:
    """Catalog runtime in minutes; 0 when missing. Takes a Product or a raw catalog dict."""
    try:
        if isinstance(product, dict):
            if product.get("runtime_length_min") is not None:
                return float(product.get("runtime_length_min") or 0)
            return float(product.get("hours") or 0) * 60
        return float(getattr(product, "hours", 0) or 0) * 60
    except (TypeError, ValueError):
        return 0.0


def is_knockoff(product, peers=()) -> bool:
    """True when a catalog candidate is a knockoff, not an edition of the work.

    A knockoff is a summary / review / study guide / workbook (by its title, or its subtitle:
    a knockoff that copies the real title verbatim carries the marker there), or an entry with
    no runtime while at least one of ``peers`` (the other candidates of the same search) has a
    real one: a zero-runtime hit beside real editions is catalog junk. Pure: no catalog calls.
    ``product`` and ``peers`` may be Products or raw catalog dicts.
    """
    for text in (_field(product, "title"), _field(product, "subtitle")):
        if _KNOCKOFF_WORDS.search(text) or _REVIEW_PREFIX.search(text):
            return True
    if _runtime_min(product) <= 0:
        return any(peer is not product and _runtime_min(peer) > 0 for peer in peers)
    return False


#: words on a catalog title / subtitle that mark an adaptation of the book rather than a reading
#: of it, each with the phrase the panel note uses. Separators may be spaces, dots, underscores
#: or dashes ("Full.Cast", "GraphicAudio")
_ADAPTATION_MARKERS = (
    (re.compile(r"(?<![a-z0-9])dramati[sz]ed[\W_]+adaptation(?![a-z0-9])", re.IGNORECASE),
     "a Dramatized Adaptation"),
    (re.compile(r"(?<![a-z0-9])graphic[\W_]*audio(?![a-z0-9])", re.IGNORECASE), "a GraphicAudio production"),
    (re.compile(r"(?<![a-z0-9])full[\W_]+cast(?![a-z0-9])", re.IGNORECASE), "a full-cast production"),
    (re.compile(r"(?<![a-z0-9])audio[\W_]+dramas?(?![a-z0-9])", re.IGNORECASE), "an audio drama"),
)


def adaptation_label(*texts: str) -> str:
    """The phrase for the first adaptation marker found in ``texts`` ("a Dramatized Adaptation"),
    or "" when none carries one."""
    for pattern, label in _ADAPTATION_MARKERS:
        if any(pattern.search(t or "") for t in texts):
            return label
    return ""


#: a bracketed / parenthesised group, for dropping the ones that only carry an adaptation marker
_GROUP = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")


def _work_title(title: str) -> str:
    """``norm(title)`` without its adaptation markers: a group that carries one is dropped
    whole ("[Dramatized Adaptation]", "(Audio Drama)"), a bare marker phrase is dropped."""
    text = _GROUP.sub(lambda g: " " if adaptation_label(g.group(0)) else g.group(0), title or "")
    for pattern, _label in _ADAPTATION_MARKERS:
        text = pattern.sub(" ", text)
    return norm(text)


def unasked_adaptation(product: Product, wanted: tuple[str, ...]) -> str:
    """``adaptation_label`` of the product's title / subtitle, unless ``wanted`` (the folder's
    own words) carries any adaptation marker too - then "". Any marker counts as asking: the
    GraphicAudio productions are the ones Audible titles "[Dramatized Adaptation]"."""
    label = adaptation_label(product.title, product.subtitle)
    if not label or adaptation_label(*wanted):
        return ""
    return label


def missing_adaptation(product: Product, wanted: tuple[str, ...]) -> str:
    """``adaptation_label`` of ``wanted`` (the folder's own words) when the product's title /
    subtitle carries no adaptation marker at all, else "": the folder asked for a production the
    pick is not. The mirror of ``unasked_adaptation`` - any marker on the product counts."""
    label = adaptation_label(*wanted)
    if not label or adaptation_label(product.title, product.subtitle):
        return ""
    return label


#: margins below this are what grade() acts on (0.05 HIGH, 0.10 title-only MEDIUM), so only
#: rivals this close to the best are worth a second-source check
CORROBORATION_WINDOW = 0.10
#: how many close rivals are checked against Audnex at most (each is one memoised lookup)
CORROBORATION_MAX_RIVALS = 3
#: Audnex and the catalog must agree on the runtime within this fraction when both list one
CORROBORATION_RUNTIME_TOLERANCE = 0.05


def corroborated(product: Product) -> bool | None:
    """Does Audnex (the second catalog source) agree with this candidate? Three answers:

    True when Audnex has a record for the ASIN whose title and author match the candidate and
    whose runtime is consistent with it; False when it has a record that disagrees (evidence);
    None when it could not say - the lookup failed or there is no record (some ASINs legitimately
    have none in a region). "Could not check" is never "does not exist": None never removes a
    rival and never widens a margin. Callers only use this to break a contest between
    candidates, never to demote a lone match.
    """
    try:
        record = catalog.book(product.asin)
    except Exception:  # a lookup failure is "unknown", never a crash in matching
        return None
    if not isinstance(record, dict) or not record.get("asin"):
        return None
    if sim(product.title, str(record.get("title") or "")) < 0.85:
        return False
    names = [str(a.get("name") or "") for a in (record.get("authors") or []) if isinstance(a, dict)]
    if product.authors and not any(sim(a, b) >= 0.85 for a in product.authors for b in names):
        return False
    try:
        record_min = float(record.get("runtimeLengthMin") or 0)
    except (TypeError, ValueError):
        record_min = 0.0
    catalog_min = _runtime_min(product)
    if record_min > 0 and catalog_min > 0:
        if abs(record_min - catalog_min) / catalog_min > CORROBORATION_RUNTIME_TOLERANCE:
            return False
    return True


def _product_from_book(book: dict) -> Product:
    """A Product from an Audnex book record."""
    series = book.get("seriesPrimary") or {}
    return catalog.Product(
        asin=book["asin"],
        title=book.get("title", ""),
        subtitle=book.get("subtitle") or "",
        authors=[a.get("name", "") for a in (book.get("authors") or []) if a.get("name")],
        narrators=[n.get("name", "") for n in (book.get("narrators") or []) if n.get("name")],
        series=series.get("name", "") or "",
        sequence=str(series.get("position", "") or ""),
        hours=round(float(book.get("runtimeLengthMin") or 0) / 60, 2),
        language=book.get("language") or "english",
        year=(book.get("releaseDate") or "")[:4],
        publisher=book.get("publisherName") or "",
        image=book.get("image") or "",
    )


def match_from_asin(asin: str, local_hours: float = 0.0) -> Match:
    """Build a confident match from an ASIN the user supplied."""
    book = catalog.book(asin)
    if not book.get("asin"):
        return Match()
    product = _product_from_book(book)
    match = Match(
        product=product,
        score=1.0,
        margin=1.0,
        title_sim=1.0,
        author_sim=1.0,
        author_known=True,
        asin_override=True,
        language=product.language,
        # a pin is never scored, so no penalty was applied to it
        language_penalized=False,
        runtime_delta_pct=runtime_delta_pct(local_hours, product.hours),
    )
    match.confidence = "HIGH"
    return match


#: the swapped title/author reading is searched only when the primary reading's best score is under this
ARBITRATE_BELOW = 0.90


#: a hand-set ASIN with no catalog data on this many checks in a row stops blocking a normal match
PIN_GIVE_UP_AFTER = 3


def match_unit(
    unit: BookUnit, asin_override: str = "", local_hours: float = 0.0, pin_failures: int = 0
) -> Match:
    """``local_hours`` (the probed length of the files) only feeds the evidence panel's
    runtime deltas; it never changes a score or the confidence grade.

    ``pin_failures`` is how many checks in a row before this one found no catalog data for
    ``asin_override``. The pin is always tried first; a live pin always wins.
    """
    if asin_override:
        forced = match_from_asin(asin_override, local_hours)
        if forced.product:
            return forced
        if pin_failures + 1 < PIN_GIVE_UP_AFTER:
            # a dead pin (or a catalog hiccup) must never fall through to a search
            # that could auto-convert a different edition - review instead
            return Match(asin_pin_failed=asin_override)
        # dead on PIN_GIVE_UP_AFTER checks in a row: match normally so the panel offers a real
        # candidate instead of the same dead-pin message forever (never auto-converted)
        match = _search_match(unit, local_hours)
        match.pin_abandoned = asin_override
        return match
    return _search_match(unit, local_hours)


def _search_match(unit: BookUnit, local_hours: float) -> Match:
    """The catalog search match for a unit with no (usable) ASIN pin."""
    author, title = unit.author_guess, unit.title_guess
    author_from_series = False
    if not author and unit.series_guess:
        author = infer_author_from_series(unit.series_guess)
        author_from_series = bool(author)

    hint = unit.asin_hint
    scored = _score_reading(title, author, local_hours, hint)
    alt_title = unit.alt_title_guess
    if alt_title and _top_score(scored) < ARBITRATE_BELOW:
        # "Past Tense - Lee Child": both sides look like names, but only a weak primary reading
        # pays for the swapped one; a confident reading never does. The swapped reading must
        # score strictly higher to win
        alt_author = unit.alt_author_guess
        alt_scored = _score_reading(alt_title, alt_author, local_hours, hint)
        if _top_score(alt_scored) > _top_score(scored):
            title, author, scored = alt_title, alt_author, alt_scored
            author_from_series = False

    if not scored:
        return Match(author_from_series=author_from_series, query_title=title, query_author=author)

    products = [s["product"] for s in scored.values()]
    # the folder's own words: its name, the scanner's title reading(s) and the reading searched
    # (for an ingest retry, title_guess is the embedded-tag title)
    wanted = (unit.name, unit.title_guess, unit.alt_title_guess, title)
    asks_adaptation = bool(adaptation_label(*wanted))
    # read before any bracket removal: "(Abridged)", "[Synopsis]", "(Box Set)" never reach a score
    wanted_markers = markers.edition_markers(*wanted)
    for entry in scored.values():
        entry["knockoff"] = is_knockoff(entry["product"], products)
        entry["corroborated"] = None  # None = never asked Audnex
        # the production the folder did not ask for: an adaptation for a plain folder, a plain
        # edition for a folder that names an adaptation
        entry["unwanted_production"] = (
            bool(adaptation_label(entry["product"].title, entry["product"].subtitle)) != asks_adaptation
        )
        # another edition than the folder's (a box set, an abridgment, a summary, or the plain book
        # for a folder that names one): ranked below every candidate that agrees, never
        # corroborated into first place, and graded LOW if it still wins
        # A knockoff's summary marker is the knockoff gate's (is_knockoff reads the raw strings
        # too, and holds it at LOW); only the other markers are compared for it
        found = markers.edition_markers(entry["product"].title, entry["product"].subtitle)
        asked = wanted_markers
        if entry["knockoff"]:
            found, asked = found - {markers.DERIVATIVE}, asked - {markers.DERIVATIVE}
        entry["edition_conflict"] = markers.edition_mismatch(asked, found)
        # another volume than the one the folder names explicitly: ranked below every candidate
        # that is not vetoed, never corroborated into first place, and graded LOW if it still wins
        entry["veto"] = position_conflict(wanted, entry["product"])
    _rank_productions(scored.values())
    ranked = sorted(scored.values(), key=_rank_key, reverse=True)
    best, margin = _pick_and_margin(ranked)

    floor_score = floor_sim(title, best["product"])
    # defensive: when a window of the folder title matched better than any whole-string reading,
    # record how much of the title that window covers; grade() refuses HIGH below 60%
    coverage = 1.0
    if best["title_sim"] < round(floor_score, 3):
        coverage = window_coverage(title, best["product"])
    if floor_score < TITLE_FLOOR:
        return Match(
            author_from_series=author_from_series, query_title=title, query_author=author,
            rejected=(f"no catalog match: closest was '{best['product'].title}' "
                      f"(title {floor_score:.2f}, needs {TITLE_FLOOR:.2f})"),
        )

    match = Match(
        product=best["product"],
        score=best["score"],
        margin=margin,
        title_sim=best["title_sim"],
        author_sim=best["author_sim"],
        author_known=bool(author),
        author_from_series=author_from_series,
        language=best["product"].language,
        language_penalized=best["language_penalized"],
        runtime_delta_pct=best["runtime_delta_pct"],
        query_title=title,
        query_author=author,
        knockoff=best["knockoff"],
        corroborated=best["corroborated"],
        title_window_coverage=round(coverage, 3),
        unasked_adaptation=unasked_adaptation(best["product"], wanted),
        missing_adaptation=missing_adaptation(best["product"], wanted),
        edition_conflict=best.get("edition_conflict", ""),
        veto=best.get("veto", ""),
    )
    match.confidence = grade(match)
    # keep the runners-up (other editions of the same work, near misses) so the
    # detail panel can offer one-click "use this edition" ASIN pins
    match.alternates = [
        {
            "asin": s["product"].asin,
            "title": s["product"].title,
            "authors": list(s["product"].authors or []),
            "score": s["score"],
            # the evidence behind the score, so the panel can say why each one lost
            "hours": s["product"].hours,
            "title_sim": s["title_sim"],
            "author_sim": s["author_sim"],
            "language": s["product"].language,
            "language_penalized": s["language_penalized"],
            "runtime_delta_pct": s["runtime_delta_pct"],
            # badged in the panel; a knockoff never counted against the pick's margin
            "knockoff": s["knockoff"],
            "corroborated": s["corroborated"],
        }
        for s in [s for s in ranked if s is not best][:4]
    ]
    return match


#: the most catalog searches one reading of a unit may make, the keyword fallback included
MAX_QUERIES = 8
#: how many title_variants readings are each asked of the catalog
MAX_VARIANT_QUERIES = 4
#: rip noise that never appears in a catalog title; "read by ..." / "narrated by ..." run to the end
_RIP_NOISE_RE = re.compile(
    r"\bchapterized\b|\b\d+\s*kbps\b|\bby\s+chapter\b|\(\s*mp3\s*\)|\b(?:read|narrated)\s+by\b.*$",
    re.IGNORECASE,
)


def strip_rip_noise(title: str) -> str:
    """"Killing Floor Chapterized 64kbps (Read by Dick Hill)" -> "Killing Floor"."""
    text = _RIP_NOISE_RE.sub(" ", title or "")
    text = re.sub(r"\(\s*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -_.,;(")


def search_queries(title: str, author: str) -> tuple[list[dict], int, str]:
    """The catalog searches for one reading: (queries, how many lead with variant readings, the
    cleaned title the keyword fallback uses).

    In order, deduplicated: each title_variants reading (at most MAX_VARIANT_QUERIES), the
    rip-noise-stripped best reading, title + author for the raw title and the best reading, then
    the author alone. Title queries are trimmed so the list plus the one keyword fallback never
    exceed MAX_QUERIES. Audible's ``title=`` filter is strict ("Private 20 - Private Dublin" finds
    nothing), so the readings are what find the book.
    """
    titled: list[dict] = []

    def add(query: dict) -> None:
        if query not in titled:
            titled.append(query)

    cleaned = ""
    n_variants = 0
    if title:
        variants = title_variants(title)[:MAX_VARIANT_QUERIES]
        for variant in variants:
            add({"title": variant})
        n_variants = len(titled)
        cleaned = strip_rip_noise(variants[-1]) or variants[-1]
        add({"title": cleaned})
        if author:
            add({"title": title, "author": author})
            add({"title": cleaned, "author": author})
    tail = [{"author": author}] if author else []
    titled = titled[: MAX_QUERIES - 1 - len(tail)]  # one slot stays free for the keyword fallback
    return titled + tail, min(n_variants, len(titled)), cleaned


def _score_product(product: Product, title: str, author: str, local_hours: float) -> dict:
    # whole-string over a few normalised readings of the folder title: release tags and a
    # "Series NN - Title" echo are dropped, but a catalog title that is only one window of
    # the folder name ("Dune" in "Dune Messiah") never scores as an exact match
    t_sim = title_score(title, product)
    a_sim = max([sim(author, a) for a in product.authors] or [0.0]) if author else 0.0
    # without a known author the title is the only evidence we have
    total = 0.60 * t_sim + 0.40 * a_sim if author else t_sim
    penalized = language_penalized(product.language)
    if penalized:
        total *= LANGUAGE_PENALTY
    return {
        "product": product,
        "score": round(total, 3),
        "title_sim": round(t_sim, 3),
        "author_sim": round(a_sim, 3),
        "language_penalized": penalized,
        "runtime_delta_pct": runtime_delta_pct(local_hours, product.hours),
        # tie-breakers only: the raw whole-string title, then the runtime gap
        "whole_sim": sim(title, product.title) if title else 0.0,
    }


def _hint_entry(asin: str, title: str, author: str, local_hours: float) -> dict | None:
    """The scored entry for an ASIN found in the torrent name, or None.

    A candidate, never a pin: it enters the pool at score 1.0 only when the catalog knows the ASIN
    and its title clears TITLE_FLOOR against the wanted one; grade() still decides. Only the total
    is boosted: title_sim stays the real similarity, so a barely-matching hint is graded on it.
    """
    try:
        book = catalog.book(asin)
    except Exception:  # a lookup failure is "no hint", never a crash in matching
        return None
    if not isinstance(book, dict) or not book.get("asin"):
        return None
    product = _product_from_book(book)
    if not title or floor_sim(title, product) < TITLE_FLOOR:
        return None
    entry = _score_product(product, title, author, local_hours)
    entry.update(score=1.0)
    return entry


def _score_reading(title: str, author: str, local_hours: float, asin_hint: str = "") -> dict[str, dict]:
    """Every candidate the catalog offers for one (title, author) reading, scored."""
    scored: dict[str, dict] = {}
    if asin_hint:
        entry = _hint_entry(asin_hint, title, author, local_hours)
        if entry:
            scored[entry["product"].asin] = entry
    queries, n_variants, cleaned = search_queries(title, author)
    titled_found = 0
    for i, query in enumerate(queries):
        found = catalog.search(limit=10, **query)
        if "title" in query:
            titled_found += len(found)
        for product in found:
            if product.asin not in scored:
                scored[product.asin] = _score_product(product, title, author, local_hours)
        # the early stop waits for every variant reading: a 0.95 on the raw title must not
        # keep the cleaner readings from being asked
        if i + 1 >= n_variants and _top_score(scored) >= 0.95 and len(scored) >= 3:
            break
    if title and cleaned and titled_found == 0 and len(queries) < MAX_QUERIES:
        for product in catalog.search(limit=10, keywords=cleaned):
            if product.asin not in scored:
                scored[product.asin] = _score_product(product, title, author, local_hours)
    return scored


def _top_score(scored: dict[str, dict]) -> float:
    return max((s["score"] for s in scored.values()), default=-1.0)


def _rank_productions(entries) -> None:
    """Set each entry's ``rank_score``: its score, except that a production the folder did not
    ask for (``unwanted_production``) never ranks above the wanted production of the same work.

    Its rank score is held to the best wanted, non-knockoff same-work peer's score, and
    _rank_key breaks the then-equal scores toward the wanted one: a GraphicAudio dramatization
    whose "Book 1" subtitle out-scores the plain edition's "Volume 1" still sorts just below it
    for a plain folder - and stays above everything the plain edition beats. Against a
    different work only an exact score tie is broken this way. The score itself is untouched.
    """
    entries = list(entries)
    for entry in entries:
        entry["rank_score"] = entry["score"]
        if not entry.get("unwanted_production"):
            continue
        peers = [
            p["score"]
            for p in entries
            if not p.get("unwanted_production") and not p.get("knockoff")
            and _same_work(p["product"], entry["product"])
        ]
        if peers:
            entry["rank_score"] = min(entry["score"], max(peers))


def _rank_key(entry: dict) -> tuple:
    """A candidate of another edition (``edition_conflict``) or another series volume (``veto``)
    below every other; then rank score (see _rank_productions); equal scores go to the production
    the folder asked for, then the closer raw title, then the closer runtime."""
    delta = entry.get("runtime_delta_pct")
    return (
        not (entry.get("edition_conflict") or entry.get("veto")),
        entry.get("rank_score", entry["score"]),
        not entry.get("unwanted_production", False),
        entry.get("whole_sim", 0.0),
        -abs(delta) if delta is not None else float("-inf"),
    )


#: grade() never says HIGH when the floor was only reached by a window covering less of the title
MIN_HIGH_WINDOW_COVERAGE = 0.60


def _pick_and_margin(ranked: list[dict]) -> tuple[dict, float]:
    """The best candidate and its margin over the strongest rival that counts.

    Rivals that never count: other editions of the same work (duplicate editions are not
    ambiguity) and knockoffs (catalog junk is not ambiguity either). With no rival left the
    margin is the best score itself - decisive.

    When rivals sit within CORROBORATION_WINDOW of the best, the best and those rivals are
    checked against Audnex. Only evidence moves anything: a best that Audnex contradicts
    (False) gives way to a corroborated contender, and a rival Audnex contradicts never eats a
    corroborated best's margin. An unknown answer (None: no record, lookup failed) displaces no
    leader, removes no rival and widens no margin. With no close rival nothing is looked up, so a
    missing Audnex record can never demote a lone match.
    """
    best = ranked[0]

    def rivals_of(pick: dict) -> list[dict]:
        return [
            s
            for s in ranked
            if s is not pick and not s["knockoff"] and not _same_work(s["product"], pick["product"])
        ]

    rivals = rivals_of(best)
    if not best["knockoff"]:
        close = [r for r in rivals if best["score"] - r["score"] < CORROBORATION_WINDOW]
        close = close[:CORROBORATION_MAX_RIVALS]
        if close:
            for entry in [best, *close]:
                entry["corroborated"] = corroborated(entry["product"])
            if best["corroborated"] is False:
                # Audnex contradicts the leader; a corroborated contender takes its place. An
                # unknown leader stays: proving a rival exists says nothing about the query
                backed = [r for r in close
                          if r["corroborated"] and not (r.get("edition_conflict") or r.get("veto"))]
                if backed:
                    best = backed[0]  # highest-scoring corroborated contender
                    rivals = rivals_of(best)
            if best["corroborated"]:
                # rivals Audnex contradicts step aside; unknown and unchecked (farther) ones still
                # count - only evidence removes a rival
                rivals = [r for r in rivals if r["corroborated"] is not False]
    top_rival = max((r["score"] for r in rivals), default=0.0)
    # a pick ranked first for something other than its score (it agrees with the folder's edition
    # markers, a higher-scoring rival does not) is not ahead of that rival: margin 0, never negative
    return best, round(max(best["score"] - top_rival, 0.0), 3)


def grade(match: Match) -> str:
    """HIGH needs title *and* author corroborated plus a clear win over other works."""
    if not match.product:
        return "NONE"
    if match.knockoff or match.edition_conflict or match.veto:
        # the best hit is a summary / review / zero-runtime stub, another edition than the folder's
        # (box set, abridgment), or it contradicts what the folder states (another volume): a person
        # decides. LOW, not MEDIUM, so no auto-convert tier (not even "medium") converts it unseen
        return "LOW"
    # a foreign-language hit must never auto-convert at HIGH confidence: it still has to clear
    # the normal title / author / margin gates, and is then capped at MEDIUM. The scorer's rule,
    # so grade, scorer and panel agree: a blank language is not confirmed English and is capped too
    foreign = language_penalized(match.language)
    title, author = match.title_sim, match.author_sim
    window_only = match.title_window_coverage < MIN_HIGH_WINDOW_COVERAGE
    # an adaptation the folder never asked for (a GraphicAudio [Dramatized Adaptation] found for a
    # plain folder) is the right book in a different production: capped at MEDIUM, like a foreign
    # hit. At the default AUTO_CONVERT=high a MEDIUM waits for the operator's click, so a silent
    # wrong conversion becomes a visible choice. Not LOW: title and author genuinely match, so it
    # stays a useful suggestion. A folder that names the adaptation is not capped. The other
    # direction costs the same: a folder that names an adaptation and a plain pick
    # (missing_adaptation) is the right book in the wrong production too - MEDIUM at most
    adaptation = bool(match.unasked_adaptation or match.missing_adaptation)
    if match.author_known:
        if (title >= 0.88 and author >= 0.85 and match.margin >= 0.05 and not window_only
                and not foreign and not adaptation):
            return "HIGH"
        # MEDIUM is one config change from auto-converting: a catalog title that is only one
        # window of the folder name is held at LOW here too. A foreign hit may still reach MEDIUM
        if title >= 0.85 and author >= 0.60 and match.margin > 0 and not window_only:
            return "MEDIUM"
        return "LOW"
    # title-only: an exact title with a decisive margin, author taken from the catalog (never a
    # window-only one, for the same reason as above)
    if title >= 0.96 and match.margin >= 0.10 and not window_only:
        return "MEDIUM"
    return "LOW"


def runtime_ok(local_hours: float, catalog_hours: float, tolerance: float) -> bool:
    """False when the file is a different edition (radio drama, abridged, omnibus).

    A catalogue answer with no runtime is **not** agreement: nothing corroborates the edition, so
    the book is held for a person instead of written unseen. (``is_knockoff`` only flags a
    zero-runtime candidate when a real edition sits beside it, so a lone no-runtime answer used to
    reach HIGH and convert with the one guard against a wrong edition silently disabled.)
    """
    if catalog_hours <= 0:
        return False
    return abs(local_hours - catalog_hours) / catalog_hours <= tolerance
