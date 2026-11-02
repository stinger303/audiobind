"""Command line entry points."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from . import __version__, daemon
from .config import CONFIG
from .pipeline import audit_unit, process_unit
from .state import State
from .units import discover_units


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _find(units, wanted: str):
    exact = [u for u in units if u.rel_path == wanted]
    if exact:
        return exact[0]
    partial = [u for u in units
               if wanted.lower() in u.rel_path.lower() or wanted.lower() in u.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        sys.exit(f"no unit matching {wanted!r}")
    sys.exit(f"ambiguous: {[u.rel_path for u in partial][:5]}")


def cmd_audit(args) -> int:
    state = State(CONFIG.state_dir)
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    print(f"{len(units)} unit(s) under {CONFIG.library_root}")
    rows = []
    for unit in units:
        audit = audit_unit(unit)
        row = {
            "rel_path": unit.rel_path, "parts": len(unit.parts), "files": unit.n_files,
            "hours": audit.hours, "author_guess": unit.author_guess,
            "title_guess": unit.title_guess, "confidence": audit.confidence,
            "score": audit.match.score, "margin": audit.match.margin,
            "asin": audit.match.asin,
            "match_title": audit.match.product.title if audit.match.product else "",
            "match_authors": "; ".join(audit.match.product.authors) if audit.match.product else "",
            "match_narrators": "; ".join(audit.match.product.narrators) if audit.match.product else "",
            "match_hours": audit.match.product.hours if audit.match.product else "",
            "chapters": audit.chapters_available, "action": audit.action,
            "notes": "; ".join(audit.notes),
        }
        rows.append(row)
        print(f"  {audit.confidence:10} {audit.hours:>7.2f}h {unit.n_files:>4}f  {unit.rel_path[:64]}")

    out = Path(CONFIG.state_dir) / "audit.csv"
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["rel_path"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nreport: {out}")
    summary = {}
    for row in rows:
        summary[row["confidence"]] = summary.get(row["confidence"], 0) + 1
    print("confidence:", summary)
    return 0


def cmd_scan(args) -> int:
    state = State(CONFIG.state_dir)
    result = daemon.scan_once(state, force=args.force)
    print(json.dumps(result))
    return 0


def cmd_convert(args) -> int:
    state = State(CONFIG.state_dir)
    units = discover_units(CONFIG.library_root, CONFIG.ignore_globs)
    unit = _find(units, args.unit)
    print(f"unit: {unit.rel_path}  ({len(unit.parts)} parts, {unit.n_files} files)")
    audit = process_unit(state, unit, log=print)
    return 0 if audit.confidence in ("HIGH", "MEDIUM") else 1


def cmd_daemon(args) -> int:
    daemon.run()
    return 0


def cmd_health(args) -> int:
    state = State(CONFIG.state_dir)
    age = state.heartbeat_age()
    limit = CONFIG.scan_interval_min * 60 * 2.5 + 300
    if age <= limit:
        print(f"ok ({age:.0f}s since last scan)")
        return 0
    print(f"stale ({age:.0f}s since last scan, limit {limit:.0f}s)")
    return 1


def cmd_version(args) -> int:
    print(f"audiobind {__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audiobind",
                                     description="Merge, convert and correctly tag audiobooks.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit", help="report what is on disk and how confident the match is").set_defaults(func=cmd_audit)
    scan = sub.add_parser("scan", help="one pass: convert new books that are safe to convert")
    scan.add_argument("--force", action="store_true", help="reconsider units already seen")
    scan.set_defaults(func=cmd_scan)
    convert = sub.add_parser("convert", help="process a single unit")
    convert.add_argument("--unit", required=True, help="path relative to LIBRARY_ROOT (substring ok)")
    convert.set_defaults(func=cmd_convert)
    sub.add_parser("daemon", help="scan forever + status page").set_defaults(func=cmd_daemon)
    sub.add_parser("health", help="exit 0 when the daemon is alive").set_defaults(func=cmd_health)
    sub.add_parser("version").set_defaults(func=cmd_version)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    # version/health must work even with no volumes mounted (healthchecks, `docker run ... version`)
    if args.command not in ("version", "health"):
        try:
            CONFIG.preflight()
        except RuntimeError as exc:
            print(f"audiobind: {exc}", file=sys.stderr)
            return 2
    return args.func(args)
