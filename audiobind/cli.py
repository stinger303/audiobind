"""Command line entry points."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import logging.handlers
import sys
from pathlib import Path

from . import __version__, daemon
from . import auth as auth_mod
from .config import CONFIG, effective_val, set_effective
from .pipeline import audit_unit, process_unit
from .state import State
from .units import discover_units


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3


def setup_logging(
    verbose: bool = False,
    log_file: str | None = None,
    max_bytes: int = LOG_MAX_BYTES,
    backups: int = LOG_BACKUPS,
) -> None:
    """Console (stdout) handler plus a rotating file handler (LOG_FILE, 5 MB x 3 backups).

    ``log_file`` None = the configured LOG_FILE; "" = no file log. A file that cannot be opened
    costs one console warning and the app carries on with stdout only.
    """
    level = logging.DEBUG if verbose else logging.INFO
    if log_file is None:
        log_file = CONFIG.log_file
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)
    root = logging.getLogger()
    # replace our own handlers from an earlier call; never touch foreign ones
    for handler in list(root.handlers):
        if getattr(handler, "_audiobind", False):
            root.removeHandler(handler)
            handler.close()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console._audiobind = True  # type: ignore[attr-defined]
    root.addHandler(console)
    root.setLevel(level)
    if not log_file:
        return
    try:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
    except OSError as exc:
        logging.getLogger("audiobind").warning(
            "log file %s cannot be opened (%s) - logging to stdout only", log_file, exc
        )
        return
    file_handler.setFormatter(formatter)
    file_handler._audiobind = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)


def _find(units, wanted: str):
    exact = [u for u in units if u.rel_path == wanted]
    if exact:
        return exact[0]
    partial = [u for u in units if wanted.lower() in u.rel_path.lower() or wanted.lower() in u.name.lower()]
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
            "rel_path": unit.rel_path,
            "parts": len(unit.parts),
            "files": unit.n_files,
            "hours": audit.hours,
            "author_guess": unit.author_guess,
            "title_guess": unit.title_guess,
            "confidence": audit.confidence,
            "score": audit.match.score,
            "margin": audit.match.margin,
            "asin": audit.match.asin,
            "match_title": audit.match.product.title if audit.match.product else "",
            "match_authors": "; ".join(audit.match.product.authors) if audit.match.product else "",
            "match_narrators": "; ".join(audit.match.product.narrators) if audit.match.product else "",
            "match_hours": audit.match.product.hours if audit.match.product else "",
            "chapters": audit.chapters_available,
            "action": audit.action,
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
    set_effective(state)
    age = state.heartbeat_age()
    limit = effective_val("scan_interval_min") * 60 * 2.5 + 300
    if age == float("inf"):
        print(f"stale (no scan recorded yet, limit {limit:.0f}s)")
        return 1
    if age <= limit:
        print(f"ok ({age:.0f}s since last scan)")
        return 0
    print(f"stale ({age:.0f}s since last scan, limit {limit:.0f}s)")
    return 1


def _read_secret(prompt: str, interactive: bool) -> str:
    """A hidden prompt on a terminal; one line of stdin otherwise (only reachable with
    --username, for scripted resets: ``printf '%s\\n' "$PW" | audiobind reset-password ...``)."""
    if interactive:
        return getpass.getpass(prompt)
    line = sys.stdin.readline()
    return line[:-1] if line.endswith("\n") else line


def cmd_reset_password(args) -> int:
    """Forgot the password: set a new username/password, replacing the account if there is
    one (that IS the reset) or creating it on a fresh database. Needs shell access to the
    container - there is deliberately no way to do this over HTTP without signing in."""
    interactive = sys.stdin.isatty()
    if not interactive and args.username is None:
        print(
            "audiobind: reset-password needs a terminal (docker exec -it ...). For a scripted "
            "reset pass --username and the new password on stdin.",
            file=sys.stderr,
        )
        return 2
    state = State(CONFIG.state_dir)
    try:
        current = state.get_credentials()
        if args.username is not None:
            username = args.username.strip()
        else:
            default = current.username if current else "admin"
            username = input(f"Username [{default}]: ").strip() or default
        problem = auth_mod.username_problem(username)
        if problem:
            print(f"audiobind: {problem}", file=sys.stderr)
            return 2
        password = _read_secret("New password: ", interactive)
        confirm = _read_secret("Repeat new password: ", interactive) if interactive else None
        problem = auth_mod.password_problem(password, confirm)
        if problem:
            print(f"audiobind: {problem} Nothing was changed.", file=sys.stderr)
            return 2
        state.set_credentials(username, auth_mod.hash_password(password))
    except (EOFError, KeyboardInterrupt):
        print("\naudiobind: cancelled - nothing was changed.", file=sys.stderr)
        return 1
    finally:
        state.close()
    verb = "replaced" if current else "created"
    print(f"Account {verb}: sign in as {username!r}. Every other signed-in browser is signed out.")
    if CONFIG.auth_token:
        print("AUTH_TOKEN no longer signs in now that an account exists; you can remove it.")
    return 0


def cmd_version(args) -> int:
    print(f"audiobind {__version__}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audiobind", description="Merge, convert and correctly tag audiobooks."
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("audit", help="report what is on disk and how confident the match is").set_defaults(
        func=cmd_audit
    )
    scan = sub.add_parser("scan", help="one pass: convert new books that are safe to convert")
    scan.add_argument("--force", action="store_true", help="reconsider units already seen")
    scan.set_defaults(func=cmd_scan)
    convert = sub.add_parser("convert", help="process a single unit")
    convert.add_argument("--unit", required=True, help="path relative to LIBRARY_ROOT (substring ok)")
    convert.set_defaults(func=cmd_convert)
    sub.add_parser("daemon", help="scan forever + status page").set_defaults(func=cmd_daemon)
    sub.add_parser("health", help="exit 0 when the daemon is alive").set_defaults(func=cmd_health)
    sub.add_parser("version").set_defaults(func=cmd_version)
    reset = sub.add_parser(
        "reset-password",
        help="set a new username/password (replaces the account; creates it on a fresh install)",
    )
    reset.add_argument("--username", help="the account's username (skips the username prompt)")
    reset.set_defaults(func=cmd_reset_password)

    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    # version/health must work even with no volumes mounted (healthchecks, `docker run ... version`);
    # reset-password only needs the state volume - a missing library must not stop a lockout fix
    if args.command not in ("version", "health", "reset-password"):
        try:
            CONFIG.preflight()
        except RuntimeError as exc:
            print(f"audiobind: {exc}", file=sys.stderr)
            return 2
    return args.func(args)
