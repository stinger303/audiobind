#!/usr/bin/env python3
"""Independent replay: re-decide the corpus on a second path and compare it with the bench's baseline.

The match-test programme (``python -m tests.matchbench``, docs/match-testing.md) is one instrument.
If it rebuilt a unit wrongly, or decided a row with a rule the app does not have, every number it
prints would be wrong together and its own baseline would agree with it. This script is the second
opinion. It shares **no code** with the bench - only the app's own modules and two data files:

    tests/matchbench/corpus.csv      the rows (the DB export: key, shape, files, parts, hours, tags)
    tests/matchbench/baseline.csv    what the bench accepted per row (pick, tier, decision)
    docs/match-catalog-cache.json    the frozen catalog answers

Where the bench builds each ``BookUnit`` by hand and re-enacts the audit step by step, the replay
goes the long way round through the app:

1. **The scanner names the units.** The corpus keys are laid out as real folders and empty files in
   a temporary directory, and ``units.discover_units`` / ``units.discover_ingest_units`` walk it.
   Grouping, keys, torrent-name cleaning and the name guesses are whatever the scanner does to a
   real tree. A corpus row the scan does not produce is reported, never skipped.
2. **The pipeline audits them.** ``pipeline.audit_unit`` runs on each unit: ``matching.match_unit``,
   the ingest embedded-tag retry, the COLLECTION tier and ``pipeline.choose_action`` with
   ``Audit.runtime_matches`` (the configured tolerance). Only its inputs from the disk are stood in
   for: ffprobe (each file gets an equal share of the row's recorded hours) and the tag reader (the
   tags the corpus recorded, or none).
3. **The decision is process_unit's rule**, restated here in four lines (``decide``): a convert
   action is taken unseen only at a tier in ``effective_auto_tiers()``.
4. ``matching.grade`` is called again on every match and must return the tier ``match_unit`` gave.

Then ASIN, tier and decision are compared, row by row, with ``baseline.csv``. A disagreement is a
finding for the owner: this script only reports. It never writes a file.

What the two paths still share, because the corpus holds nothing better (so agreement proves
nothing about these):
  * a pinned row is matched without its pin (the pin is the label);
  * codecs are not recorded: a single .m4b/.m4a is taken as AAC, anything else as MP3. The files
    inside a folder are not recorded either, so a folder's files are named NNN.mp3;
  * embedded tags exist only for the rows where they won; elsewhere the retry reads no tags;
  * a part-bucket's real folder names are not in its key; they are rebuilt from the key;
  * the frozen catalog and the ``us`` region.

Usage:
    python3 docs/match-replay.py            # summary + every disagreement; exit 1 on any
    python3 docs/match-replay.py --rows     # ... and one line per row
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import urllib.request
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from audiobind import catalog, config, ingest, matching, pipeline, units  # noqa: E402
from audiobind.probe import Probe  # noqa: E402

CORPUS = REPO / "tests" / "matchbench" / "corpus.csv"
BASELINE = REPO / "tests" / "matchbench" / "baseline.csv"
CACHE = REPO / "docs" / "match-catalog-cache.json"
REGION = "us"
#: the baseline's adversarial rows are synthetic cases with their own catalog model: not corpus rows
ADVERSARIAL_PREFIX = "adv:"
#: a second bucket beside a part-bucket row, so the scanner sees the shared parent its key implies
SCAFFOLD = "zz replay scaffold"
COMPARED = ("asin", "tier", "decision")


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ------------------------------------------------------------------------------ the tree
def _touch(directory: Path, count: int, durations: dict, seconds: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        path = directory / f"{i + 1:03d}.mp3"
        path.touch()
        durations[str(path)] = seconds


def lay_out(row: dict, library: Path, ingest_root: Path, durations: dict) -> str:
    """Create the folders and empty files this corpus row describes. Returns "" or why it cannot.

    ``durations`` gets every created file's share of the row's hours (what ffprobe would report).
    """
    key = row["key"]
    n_files = max(int(row.get("files") or 1), 1)
    n_parts = max(int(row.get("parts") or 1), 1)
    seconds = float(row.get("hours") or 0) * 3600 / n_files
    is_ingest = key.startswith(units.RESERVED_KEY_PREFIX)
    root = ingest_root if is_ingest else library
    rel = key[len(units.RESERVED_KEY_PREFIX):] if is_ingest else key
    if not is_ingest and rel.startswith("./" + units.RESERVED_KEY_PREFIX):
        rel = rel[2:]  # a library folder literally named "ingest:..." is keyed "./ingest:..."
    shape = row.get("shape") or "folder"
    if shape == "file":
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        durations[str(path)] = seconds * n_files
        return ""
    if n_files < n_parts:
        return f"{n_files} file(s) cannot fill {n_parts} parts"
    share = [n_files // n_parts + (1 if i < n_files % n_parts else 0) for i in range(n_parts)]
    if shape == "bucket":
        # "<parent>#<key>": several part-buckets share <parent>; this one's folders reduce to <key>
        if "#" not in rel:
            return "a bucket row without '#' in its key"
        parent, bucket = rel.rsplit("#", 1)
        if n_parts < 2:
            return "a bucket row with one part (the scanner only keys a multi-part bucket)"
        for i in range(n_parts):
            _touch(root / parent / f"{bucket} Disc {i + 1}", share[i], durations, seconds)
        for i in range(2):
            _touch(root / parent / f"{SCAFFOLD} Disc {i + 1}", 1, durations, 0.0)
        return ""
    if shape != "folder":
        return f"unknown shape {shape!r}"
    if n_parts == 1:
        _touch(root / rel, n_files, durations, seconds)
    else:
        for i in range(n_parts):
            _touch(root / rel / f"Disc {i + 1}", share[i], durations, seconds)
    return ""


# ----------------------------------------------------------------------------- the catalog
class Frozen:
    """catalog._get_json, answered from the frozen cache. A URL the cache lacks is a miss, counted
    per row, answered None. Chapter lookups are not part of a match (audit_unit only counts them
    for the panel): answered None and not counted."""

    def __init__(self, data: dict):
        self.data = data
        self.misses: list[str] = []

    def __call__(self, url: str, *_args, **_kwargs):
        if url in self.data:
            return self.data[url]
        if "/chapters" not in url:
            self.misses.append(url)
        return None


def _offline(*_args, **_kwargs):
    raise RuntimeError("match-replay is offline: something tried to open a URL")


# ----------------------------------------------------------------------------- the decision
def decide(audit: pipeline.Audit) -> str:
    """process_unit's rule for an unforced, readable unit, in the baseline's four words."""
    converts = audit.action in ("merge+convert", "convert", "remux")
    if converts and audit.confidence in config.effective_auto_tiers() and not audit.match.pin_abandoned:
        return "remux" if audit.action == "remux" else "convert"
    if audit.action == "hold":
        return "hold"
    return "review"


@dataclass
class Row:
    key: str
    asin: str = ""
    tier: str = ""
    decision: str = ""
    action: str = ""
    name_source: str = ""
    #: this row could not be replayed at all (why); it is a finding, never a silent skip
    problem: str = ""
    #: observations that are not disagreements
    notes: list[str] = field(default_factory=list)


@dataclass
class Replay:
    rows: list[Row]
    tolerance: float
    auto_tiers: list[str]
    #: units the scan produced that no corpus row describes (the scaffold excluded)
    strays: list[str]


def replay(corpus_path: Path = CORPUS, cache_path: Path = CACHE) -> Replay:
    corpus = sorted(read_csv(corpus_path), key=lambda r: r["key"])
    frozen = Frozen(json.loads(cache_path.read_text(encoding="utf-8")))
    real_effective = catalog.effective_val
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="match-replay-") as tmp, ExitStack() as stack:
        library, ingest_root = Path(tmp) / "library", Path(tmp) / "ingest"
        library.mkdir()
        ingest_root.mkdir()
        durations: dict[str, float] = {}
        cannot = {row["key"]: lay_out(row, library, ingest_root, durations) for row in corpus}

        found = {u.state_key: u for u in units.discover_units(library, [])}
        found.update({u.state_key: u for u in units.discover_ingest_units(ingest_root, [])})

        def fake_probe(path, timeout: int = 60, raise_on_timeout: bool = False) -> Probe:
            codec = "aac" if Path(path).suffix.lower() in (".m4b", ".m4a") else "mp3"
            # the corpus records no audio shape: every file is taken as the default configured one
            # (2 ch / 44100 Hz), so a single .m4b stays the remux case (AB-MONO-CAP reads both figures)
            return Probe(duration=durations.get(str(path), 0.0), codec=codec, channels="2",
                         sample_rate="44100")

        tags = {"now": ("", "")}
        stack.enter_context(mock.patch.object(pipeline, "probe", fake_probe))
        stack.enter_context(mock.patch.object(ingest, "embedded_names", lambda _unit: tags["now"]))
        stack.enter_context(mock.patch.object(catalog, "_get_json", frozen))
        stack.enter_context(mock.patch.object(
            catalog, "effective_val", lambda name: REGION if name == "region" else real_effective(name)))
        stack.enter_context(mock.patch.object(urllib.request, "urlopen", _offline))
        catalog.cache_clear()
        stack.callback(catalog.cache_clear)

        for row in corpus:
            out = Row(key=row["key"])
            rows.append(out)
            if cannot[row["key"]]:
                out.problem = f"not laid out: {cannot[row['key']]}"
                continue
            unit = found.get(row["key"])
            if unit is None:
                out.problem = "the scanner did not produce a unit with this key"
                continue
            tags["now"] = (row.get("tag_author") or "", row.get("tag_title") or "")
            before = len(frozen.misses)
            audit = pipeline.audit_unit(unit)  # no pin: the pin is the owner's label
            missed = frozen.misses[before:]
            if missed:
                out.problem = f"{len(missed)} catalog lookup(s) missing from the frozen cache, first: {missed[0]}"
                continue
            hours = float(row.get("hours") or 0)
            if abs(audit.hours - hours) > 0.005:
                out.problem = f"rebuilt hours {audit.hours} differ from the row's {hours}"
                continue
            if unit.n_files != int(row.get("files") or 1) or len(unit.parts) != max(int(row.get("parts") or 1), 1):
                out.problem = (f"rebuilt {unit.n_files} file(s) in {len(unit.parts)} part(s), "
                               f"the row says {row.get('files')} in {row.get('parts')}")
                continue
            out.asin, out.tier, out.action = audit.match.asin, audit.confidence, audit.action
            out.decision, out.name_source = decide(audit), audit.name_source
            regraded = matching.grade(audit.match)
            if regraded != audit.match.confidence:
                out.notes.append(f"grade() says {regraded}, match_unit said {audit.match.confidence}")
            if row.get("pin"):
                out.notes.append("pinned in the app; replayed without the pin")
            if unit.source == "ingest" and not any(tags["now"]) and audit.match.confidence in ("NONE", "LOW"):
                out.notes.append("embedded-tag retry had no recorded tags to read")
        strays = sorted(k for k in found if k not in cannot and SCAFFOLD not in k)
    tolerance = float(config.effective_val("max_runtime_delta"))
    return Replay(rows, tolerance, sorted(config.effective_auto_tiers()), strays)


# ------------------------------------------------------------------------------ comparing
def load_baseline(path: Path = BASELINE) -> dict[str, dict]:
    """The bench's accepted corpus rows, by key, in the replay's column names."""
    return {
        r["key"]: {"asin": r["pick"], "tier": r["tier"], "decision": r["decision"]}
        for r in read_csv(path) if not r["key"].startswith(ADVERSARIAL_PREFIX)
    }


def compare(result: Replay, baseline: dict[str, dict]) -> list[str]:
    """Every way the replay and the baseline differ, one line each. [] = the two paths agree."""
    out = []
    seen = set()
    for row in result.rows:
        seen.add(row.key)
        if row.problem:
            out.append(f"{row.key}: NOT REPLAYED - {row.problem}")
            continue
        for note in row.notes:
            if note.startswith("grade()"):
                out.append(f"{row.key}: GRADE - {note}")
        base = baseline.get(row.key)
        if base is None:
            out.append(f"{row.key}: in the corpus, not in the baseline")
            continue
        for col in COMPARED:
            mine, theirs = getattr(row, col), base[col]
            if mine != theirs:
                out.append(f"{row.key}: {col.upper()} replay {mine or '-'} != baseline {theirs or '-'}")
    for key in sorted(set(baseline) - seen):
        out.append(f"{key}: in the baseline, not in the corpus")
    for key in result.strays:
        out.append(f"{key}: the scanner produced a unit no corpus row describes")
    return out


def report(result: Replay, differences: list[str], show_rows: bool = False) -> str:
    done = [r for r in result.rows if not r.problem]
    lines = [
        f"match-replay: {len(result.rows)} corpus rows, {len(done)} replayed; "
        f"runtime tolerance {result.tolerance:.0%}, auto-convert tiers {','.join(result.auto_tiers) or 'none'}",
        "  tier/decision: " + ", ".join(
            f"{t}/{d} {n}" for (t, d), n in sorted(Counter((r.tier, r.decision) for r in done).items())),
        f"  embedded tags won: {sum(1 for r in done if r.name_source == pipeline.NAME_SOURCE_EMBEDDED)}; "
        f"replayed without a pin: {sum(1 for r in done if any('pinned' in n for n in r.notes))}; "
        f"retry with no recorded tags: {sum(1 for r in done if any('retry' in n for n in r.notes))}",
    ]
    if show_rows:
        lines += [f"  {r.tier or '-':10} {r.decision or '-':7} {r.asin or '-':10} {r.key}"
                  + (f"   [{r.problem}]" if r.problem else "") for r in result.rows]
    if differences:
        lines.append(f"REPLAY: {len(differences)} DISAGREEMENT(S) with tests/matchbench/baseline.csv")
        lines += ["  " + d for d in differences]
        lines.append("  Each line is a finding. Decide which side is right; do not edit either to agree.")
    else:
        lines.append(f"REPLAY: AGREES - asin, tier and decision equal baseline.csv on all {len(done)} rows")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rows", action="store_true", help="also print one line per corpus row")
    args = ap.parse_args(argv)
    result = replay()
    differences = compare(result, load_baseline())
    print(report(result, differences, args.rows))
    loaded = sorted(m for m in sys.modules if m == "tests" or m.startswith("tests."))
    if loaded:
        print(f"REPLAY: NOT INDEPENDENT - loaded {', '.join(loaded)}")
        return 1
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
