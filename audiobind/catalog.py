"""Audible catalog search and Audnex metadata - both free, no key required."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .config import effective_val

USER_AGENT = "audiobind/0.1 (+https://github.com/stinger303/audiobind)"
AUDIBLE = "https://api.audible.com/1.0/catalog/products"
AUDNEX = "https://api.audnex.us"

_lock = threading.Lock()
_last_call = 0.0

#: Successful responses are memoised by URL so a prefetched lookup is free when the main loop
#: asks for it a moment later. Failures are never cached - they must be retried.
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 900
CACHE_MAX_ENTRIES = 256


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


def cache_get(url: str):
    with _cache_lock:
        entry = _cache.get(url)
        if entry is None:
            return None
        stored_at, payload = entry
        if time.time() - stored_at > CACHE_TTL_SECONDS:
            _cache.pop(url, None)
            return None
        return payload


def cache_put(url: str, payload: dict) -> None:
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest = sorted(_cache.items(), key=lambda item: item[1][0])[: len(_cache) // 4 or 1]
            for key, _value in oldest:
                _cache.pop(key, None)
        _cache[url] = (time.time(), payload)


@dataclass
class Product:
    asin: str = ""
    title: str = ""
    subtitle: str = ""
    authors: list[str] = field(default_factory=list)
    narrators: list[str] = field(default_factory=list)
    series: str = ""
    sequence: str = ""
    hours: float = 0.0
    language: str = ""
    year: str = ""
    publisher: str = ""
    summary: str = ""
    #: catalog cover art, kept with the match so the UI never has to search again
    image: str = ""

    @staticmethod
    def from_audible(raw: dict) -> Product:
        series = raw.get("series") or []
        summary = re_strip(raw.get("merchandising_summary") or "")
        return Product(
            asin=raw.get("asin", ""),
            title=raw.get("title", ""),
            subtitle=raw.get("subtitle") or "",
            authors=[a.get("name", "") for a in (raw.get("authors") or []) if a.get("name")],
            narrators=[n.get("name", "") for n in (raw.get("narrators") or []) if n.get("name")],
            series=series[0].get("title", "") if series else "",
            sequence=str(series[0].get("sequence", "") or "") if series else "",
            hours=round(float(raw.get("runtime_length_min") or 0) / 60, 2),
            language=raw.get("language", ""),
            year=(raw.get("release_date") or "")[:4],
            publisher=raw.get("publisher_name", ""),
            summary=summary,
            image=product_image(raw),
        )


def product_image(raw: dict) -> str:
    """The best product_images URL an Audible search returned (we ask for 500 and 1000)."""
    images = raw.get("product_images") or {}
    for size in ("1000", "500"):
        url = images.get(str(size))
        if url:
            return str(url)
    for value in images.values():  # whatever size the API did send
        if value:
            return str(value)
    return ""


def re_strip(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", " ", html or "").strip()


def _get_json(url: str, tries: int = 3, timeout: int = 25) -> dict | None:
    """GET with a global ~0.6 s politeness gap and exponential backoff.

    A successful response is memoised, so a prefetched lookup costs the main loop nothing.
    """
    global _last_call
    cached = cache_get(url)
    if cached is not None:
        return cached
    for attempt in range(tries):
        with _lock:
            wait = 0.6 - (time.time() - _last_call)
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            if isinstance(payload, dict):
                cache_put(url, payload)
            return payload
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None
            time.sleep(1.2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            time.sleep(1.2 * (attempt + 1))
    return None


def search(title: str | None = None, author: str | None = None, limit: int = 10) -> list[Product]:
    params = {
        "num_results": limit,
        "products_sort_by": "Relevance",
        "response_groups": "product_desc,media,series,contributors,product_extended_attrs",
        "image_sizes": "500,1000",
    }
    if title:
        params["title"] = title
    if author:
        params["author"] = author
    data = _get_json(AUDIBLE + "?" + urllib.parse.urlencode(params)) or {}
    return [Product.from_audible(p) for p in (data.get("products") or [])]


def book(asin: str) -> dict:
    eff_region = effective_val("region")
    return _get_json(f"{AUDNEX}/books/{asin}?region={eff_region}") or {}


def chapters(asin: str) -> list[dict]:
    eff_region = effective_val("region")
    data = _get_json(f"{AUDNEX}/books/{asin}/chapters?region={eff_region}") or {}
    return data.get("chapters") or []


def chapter_ms(chapter: dict) -> int:
    """Audnex returns camelCase (lengthMs); older payloads used length_ms."""
    return chapter.get("lengthMs") or chapter.get("length_ms") or 0


def download(url: str, destination: str) -> bool:
    if not url:
        return False
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
        if len(data) < 2000:
            return False
        with open(destination, "wb") as handle:
            handle.write(data)
        return True
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def product_images(asin: str) -> dict:
    """The Audible catalog's image URLs for one ASIN, memoised like every other lookup.

    The ASIN-derived Amazon URL pattern only resolves for some ASINs - for the rest it
    returns a 43-byte 1x1 tracking image with HTTP 200, which is not a cover. Asking the
    catalog for ``product_images`` is the reliable source, and ``_get_json``'s memoisation
    makes the repeat lookups free.
    """
    data = _get_json(f"{AUDIBLE}/{asin}?response_groups=media&image_sizes=500,1000") or {}
    product = data.get("product") or {}
    return product.get("product_images") or {}


def cover_url(product: dict, asin: str) -> str:
    """Prefer the Audnex image, fall back to the Audible product image."""
    if product.get("image"):
        return product["image"]
    images = product.get("product_images") or {}
    for size in ("1000", "500"):
        if images.get(size):
            return images[size]
    return f"https://m.media-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_.jpg"
