"""Pick the right catalog entry for a unit, and say how sure we are."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import catalog
from .catalog import Product
from .units import BookUnit, author_like, split_author_title
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
    #: set when a user-pinned ASIN had no catalog data and the search was used instead
    asin_pin_failed: str = ""
    confidence: str = "NONE"
    #: other candidates the search scored highly (other editions of the same work);
    #: [{"asin", "title", "authors", "score"}], best first, chosen one excluded
    alternates: list[dict] = field(default_factory=list)

    @property
    def asin(self) -> str:
        return self.product.asin if self.product else ""


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


def match_from_asin(asin: str) -> Match:
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
    match = Match(product=product, score=1.0, margin=1.0, title_sim=1.0, author_sim=1.0,
                  author_known=True, asin_override=True)
    match.confidence = "HIGH"
    return match


def match_unit(unit: BookUnit, asin_override: str = "") -> Match:
    if asin_override:
        forced = match_from_asin(asin_override)
        if forced.product:
            return forced
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
            title_sim = max(sim(title, product.title),
                            sim(title, f"{product.title} {product.subtitle}".strip()))
            author_sim = max([sim(author, a) for a in product.authors] or [0.0]) if author else 0.0
            # without a known author the title is the only evidence we have
            total = 0.60 * title_sim + 0.40 * author_sim if author else title_sim
            if not product.language.lower().startswith("english"):
                total *= 0.85
            scored[product.asin] = {"product": product, "score": round(total, 3),
                                    "title_sim": round(title_sim, 3),
                                    "author_sim": round(author_sim, 3)}
        if scored and max(s["score"] for s in scored.values()) >= 0.95 and len(scored) >= 3:
            break

    if not scored:
        fallback = Match(author_from_series=author_from_series)
        fallback.asin_pin_failed = asin_override
        return fallback

    ranked = sorted(scored.values(), key=lambda s: s["score"], reverse=True)
    best = ranked[0]
    # margin over the best *different* work: duplicate editions are not ambiguity
    rivals = [s["score"] for s in ranked[1:] if not _same_work(s["product"], best["product"])]
    margin = round(best["score"] - (max(rivals) if rivals else 0.0), 3)

    match = Match(
        product=best["product"], score=best["score"], margin=margin,
        title_sim=best["title_sim"], author_sim=best["author_sim"],
        author_known=bool(author), author_from_series=author_from_series,
    )
    match.confidence = grade(match)
    match.asin_pin_failed = asin_override
    # keep the runners-up (other editions of the same work, near misses) so the
    # detail panel can offer one-click "use this edition" ASIN pins
    match.alternates = [
        {"asin": s["product"].asin, "title": s["product"].title,
         "authors": list(s["product"].authors or []), "score": s["score"]}
        for s in ranked[1:5] if s["product"].asin != best["product"].asin
    ]
    return match


def grade(match: Match) -> str:
    """HIGH needs title *and* author corroborated plus a clear win over other works."""
    if not match.product:
        return "NONE"
    title, author = match.title_sim, match.author_sim
    if match.author_known:
        if title >= 0.88 and author >= 0.85 and match.margin >= 0.05:
            return "HIGH"
        if title >= 0.85 and author >= 0.60:
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
