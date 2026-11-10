"""Pick the right catalog entry for a unit, and say how sure we are."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import catalog
from .catalog import Product
from .units import BookUnit
from .util import norm, sim


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
    #: "language_penalized", "runtime_delta_pct"}], best first, chosen one excluded
    alternates: list[dict] = field(default_factory=list)

    @property
    def asin(self) -> str:
        return self.product.asin if self.product else ""


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
    return norm(a.title) == norm(b.title) and norm((a.authors or [""])[0]) == norm((b.authors or [""])[0])


def match_from_asin(asin: str, local_hours: float = 0.0) -> Match:
    """Build a confident match from an ASIN the user supplied."""
    book = catalog.book(asin)
    if not book.get("asin"):
        return Match()
    series = book.get("seriesPrimary") or {}
    product = catalog.Product(
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


def match_unit(unit: BookUnit, asin_override: str = "", local_hours: float = 0.0) -> Match:
    """``local_hours`` (the probed length of the files) only feeds the evidence panel's
    runtime deltas; it never changes a score or the confidence grade."""
    if asin_override:
        forced = match_from_asin(asin_override, local_hours)
        if forced.product:
            return forced
        # a dead pin (or a catalog hiccup) must never fall through to a search
        # that could auto-convert a different edition - review instead
        return Match(asin_pin_failed=asin_override)
    author, title = unit.author_guess, unit.title_guess
    author_from_series = False
    if not author and unit.series_guess:
        author = infer_author_from_series(unit.series_guess)
        author_from_series = bool(author)

    queries: list[dict] = []
    if title:
        queries.append({"title": title})
        words = title.split()
        if len(words) > 5:
            queries.append({"title": " ".join(words[:5])})
    if author and title:
        queries.append({"title": title, "author": author})
    if author:
        queries.append({"author": author})

    scored: dict[str, dict] = {}
    for query in queries:
        for product in catalog.search(limit=10, **query):
            if product.asin in scored:
                continue
            title_sim = max(
                sim(title, product.title), sim(title, f"{product.title} {product.subtitle}".strip())
            )
            author_sim = max([sim(author, a) for a in product.authors] or [0.0]) if author else 0.0
            # without a known author the title is the only evidence we have
            total = 0.60 * title_sim + 0.40 * author_sim if author else title_sim
            penalized = language_penalized(product.language)
            if penalized:
                total *= LANGUAGE_PENALTY
            scored[product.asin] = {
                "product": product,
                "score": round(total, 3),
                "title_sim": round(title_sim, 3),
                "author_sim": round(author_sim, 3),
                "language_penalized": penalized,
                "runtime_delta_pct": runtime_delta_pct(local_hours, product.hours),
            }
        if scored and max(s["score"] for s in scored.values()) >= 0.95 and len(scored) >= 3:
            break

    if not scored:
        return Match(author_from_series=author_from_series, query_title=title, query_author=author)

    ranked = sorted(scored.values(), key=lambda s: s["score"], reverse=True)
    best = ranked[0]
    # margin over the best *different* work: duplicate editions are not ambiguity
    rivals = [s["score"] for s in ranked[1:] if not _same_work(s["product"], best["product"])]
    margin = round(best["score"] - (max(rivals) if rivals else 0.0), 3)

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
        }
        for s in ranked[1:5]
        if s["product"].asin != best["product"].asin
    ]
    return match


def grade(match: Match) -> str:
    """HIGH needs title *and* author corroborated plus a clear win over other works."""
    if not match.product:
        return "NONE"
    if not (match.language or "english").lower().startswith("english"):
        # a foreign-language hit must never auto-convert at HIGH confidence
        return "MEDIUM"
    title, author = match.title_sim, match.author_sim
    if match.author_known:
        if title >= 0.88 and author >= 0.85 and match.margin >= 0.05:
            return "HIGH"
        if title >= 0.85 and author >= 0.60 and match.margin > 0:
            return "MEDIUM"
        return "LOW"
    # title-only: an exact title with a decisive margin, author taken from the catalog
    if title >= 0.96 and match.margin >= 0.10:
        return "MEDIUM"
    return "LOW"


def runtime_ok(local_hours: float, catalog_hours: float, tolerance: float) -> bool:
    """False when the file is a different edition (radio drama, abridged, omnibus)."""
    if catalog_hours <= 0:
        return True
    return abs(local_hours - catalog_hours) / catalog_hours <= tolerance
