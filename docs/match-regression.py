#!/usr/bin/env python3
"""Match regression harness: grade every book in a state database, against a frozen catalog.

Repo tooling, not a test. For every ``units`` row with a path, rebuild the ``BookUnit`` the
scanner would have built (``units._name_units``: the same name cleaning and guessing, no new
parser), call ``matching.match_unit(unit, row.asin_override, local_hours=row.hours)`` and write
one CSV line per book:

    path, asin, title, tier, t_sim, a_sim, margin, coverage

``tier`` is ``match_unit``'s own grade. The pipeline layers two things on top that this harness
does not reproduce (it never sees the files): the ingest embedded-tag retry and the COLLECTION
tier (``looks_like_collection``). The sanity summary on stderr compares against the database's
``confidence`` column and names every row where the two can legitimately differ.

Every catalog response (Audible search and Audnex book lookups, failures included) is cached in
``--cache`` and served from there when present, so a before-run and an after-run see identical
catalog data and the diff measures the code, not Audible. ``--offline`` refuses to fetch: a
cache miss is then a catalog failure, and the run reports how many there were.

Rows that are skipped are listed on stderr with the reason - a silent drop would make the
before/after gate a lie:
  * ingest rows (``ingest:`` keys): their names go through the torrent cleaner and the pipeline
    retries them with the files' embedded tags, which this harness cannot read;
  * keyed part-bucket rows (``<parent>#<key>``): the bucket's display name is not in the key.

Usage:
    python3 docs/match-regression.py [--db PATH] [--cache PATH] [--out CSV] [--offline]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from audiobind import catalog, matching, units  # noqa: E402
from audiobind.units import BookUnit, Part  # noqa: E402

DEFAULT_DB = os.environ.get("AUDIOBIND_DB") or "/opt/data/scratch/ab_baseline.db"
DEFAULT_CACHE = str(REPO / "docs" / "match-catalog-cache.json")
FIELDS = ["path", "asin", "title", "tier", "t_sim", "a_sim", "margin", "coverage"]
#: a fake library root; nothing is read from it, it only anchors the relative paths
ROOT = Path("/library")

#: the raw Audible product keys Product.from_audible reads (product_images is reduced to a stub)
_PRODUCT_KEYS = (
    "asin", "title", "subtitle", "authors", "narrators", "series", "runtime_length_min",
    "language", "release_date", "publisher_name",
)
#: the Audnex book keys _product_from_book / corroborated read
_BOOK_KEYS = (
    "asin", "title", "subtitle", "authors", "narrators", "seriesPrimary", "runtimeLengthMin",
    "language", "releaseDate", "publisherName",
)


def _trim(url: str, payload):
    """Keep only what matching reads, so the committed cache stays small and reviewable."""
    if not isinstance(payload, dict):
        return payload
    if url.startswith(catalog.AUDIBLE):
        products = []
        for raw in payload.get("products") or []:
            if isinstance(raw, dict):
                products.append({k: raw[k] for k in _PRODUCT_KEYS if k in raw})
        return {"products": products}
    if "/chapters" not in url:
        return {k: payload[k] for k in _BOOK_KEYS if k in payload}
    return payload


class FrozenCatalog:
    """Wraps catalog._get_json: cache hit -> cached payload (None included); miss -> fetch once."""

    def __init__(self, path: str, offline: bool):
        self.path, self.offline = path, offline
        self.data: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        self.hits = self.misses = self.fetched = 0
        self._real = catalog._get_json

    def __call__(self, url: str, *args, **kwargs):
        if url in self.data:
            self.hits += 1
            return self.data[url]
        self.misses += 1
        if self.offline:
            return None
        payload = _trim(url, self._real(url, *args, **kwargs))
        self.fetched += 1
        self.data[url] = payload
        return payload

    def install(self) -> None:
        catalog._get_json = self

    def save(self) -> None:
        if self.offline or not self.fetched:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=0, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)


def rebuild_unit(rel: str) -> tuple[BookUnit | None, str]:
    """The BookUnit the scanner would have named for library key ``rel``, or (None, why not)."""
    if rel.startswith(units.RESERVED_KEY_PREFIX):
        return None, "ingest row (torrent-name cleaning + embedded-tag retry need the files)"
    if "#" in rel:
        return None, "keyed part-bucket row (the bucket's display name is not in the key)"
    on_disk = rel[2:] if rel.startswith("./" + units.RESERVED_KEY_PREFIX) else rel
    path = ROOT / on_disk
    unit = BookUnit(path=str(path), name=path.name, parent=str(path.parent),
                    parts=[Part(name=path.name, files=[])])
    units._name_units([unit], ROOT)
    if unit.rel_path != rel:
        return None, f"rebuilt key {unit.rel_path!r} differs from the row's"
    return unit, ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--out", default="-", help="CSV path, '-' for stdout")
    ap.add_argument("--offline", action="store_true", help="never fetch; a cache miss is a catalog failure")
    args = ap.parse_args(argv)

    frozen = FrozenCatalog(args.cache, args.offline)
    frozen.install()

    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT path, confidence, asin, asin_override, hours FROM units "
        "WHERE path IS NOT NULL AND path != '' ORDER BY path"
    ).fetchall()

    out_rows, skipped, db_tiers, diffs = [], [], Counter(), []
    try:
        for row in rows:
            unit, why = rebuild_unit(row["path"])
            if unit is None:
                skipped.append((row["path"], row["confidence"], why))
                continue
            match = matching.match_unit(unit, row["asin_override"] or "", local_hours=row["hours"] or 0.0)
            out_rows.append({
                "path": row["path"],
                "asin": match.asin,
                "title": match.product.title if match.product else "",
                "tier": match.confidence,
                "t_sim": f"{match.title_sim:.3f}",
                "a_sim": f"{match.author_sim:.3f}",
                "margin": f"{match.margin:.3f}",
                "coverage": f"{match.title_window_coverage:.3f}",
            })
            db_tiers[row["confidence"]] += 1
            if (row["confidence"], row["asin"] or "") != (match.confidence, match.asin):
                diffs.append((row["path"], row["confidence"], row["asin"], match.confidence, match.asin))
    finally:
        frozen.save()

    fh = sys.stdout if args.out == "-" else open(args.out, "w", newline="", encoding="utf-8")
    try:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)
    finally:
        if fh is not sys.stdout:
            fh.close()

    err = sys.stderr
    tiers = Counter(r["tier"] for r in out_rows)
    print(f"rows in db: {len(rows)}   graded: {len(out_rows)}   skipped: {len(skipped)}", file=err)
    print("tiers (harness):  " + ", ".join(f"{k} {v}" for k, v in sorted(tiers.items())), file=err)
    print("tiers (db, same rows): " + ", ".join(f"{k} {v}" for k, v in sorted(db_tiers.items())), file=err)
    print(f"catalog: {frozen.hits} cached, {frozen.misses} missed, {frozen.fetched} fetched"
          f"{' (offline: misses were failures)' if args.offline else ''}", file=err)
    for path, conf, why in skipped:
        print(f"SKIP  [{conf}] {path}: {why}", file=err)
    for path, old_t, old_a, new_t, new_a in diffs:
        print(f"DB≠   {path}: db {old_t} {old_a or '-'} -> harness {new_t} {new_a or '-'}", file=err)
    return 0


if __name__ == "__main__":
    sys.exit(main())
