#!/usr/bin/env python3
"""Revert one MouseSearch-parity feature from the parity branch, newest first.

Usage:
    python3 scripts/revert-parity.py --list
    python3 scripts/revert-parity.py 1 --dry-run     # show what would happen
    python3 scripts/revert-parity.py 1               # revert, staged, not committed
    python3 scripts/revert-parity.py 1 --yes         # revert and commit it

Every parity feature lands as a contiguous run of commits on ``parity/multiclient``, so undoing one
is ``git revert`` over that run. The run is resolved from commit subjects, not from hashes, so a
rebase does not invalidate this script.

Why newest first: a later feature builds on an earlier one. Reverting feature 1 while feature 3 is
still applied will conflict, and resolving that by hand is how a rollback becomes an incident. See
docs/PARITY-ROLLBACK.md.

Exit codes: 0 reverted (or dry run), 1 refused (dirty tree, bad range, conflict), 2 usage.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

#: feature id -> (name, first commit's subject prefix, last commit's subject prefix)
FEATURES: dict[str, tuple[str, str, str]] = {
    "1": (
        "multi-client download adapters",
        "clients: base",
        "tests: conformance",
    ),
    "3": (
        "allowed-IP updater",
        "parity: ip-updater research",
        "parity: ip-updater docs",
    ),
    "4": (
        "Hardcover evidence",
        "parity: hardcover retrieval",
        "parity: hardcover rollback",
    ),
}

#: what to run afterwards; the same gates every parity commit had to pass
GATES = (
    "python3 -m unittest discover -s tests",
    "python3 -m tests.matchbench",
    "python3 -m tests.matchbench mutate --check",
    "python3 -m tests.matchbench mutate --cases --check",
)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(("git", *args), text=True, capture_output=True, check=check)


def subject_commit(prefix: str) -> str | None:
    """The newest commit on HEAD whose subject starts with ``prefix``."""
    out = git("log", "--format=%H%x00%s", "-n", "400").stdout
    for line in out.splitlines():
        sha, _, subject = line.partition("\x00")
        if subject.startswith(prefix):
            return sha
    return None


def resolve(feature: str) -> tuple[str, str, str]:
    """(name, first_sha, last_sha) for a feature id, or raise LookupError."""
    if feature not in FEATURES:
        raise LookupError(f"no parity feature {feature!r}; known: {', '.join(sorted(FEATURES))}")
    name, first_prefix, last_prefix = FEATURES[feature]
    first = subject_commit(first_prefix)
    last = subject_commit(last_prefix)
    if not first or not last:
        missing = first_prefix if not first else last_prefix
        raise LookupError(f"feature {feature}: no commit on this branch starts with {missing!r}")
    # last must be first or newer than it
    newer = git("rev-list", f"{first}..{last}", check=False).returncode == 0
    if not newer:
        raise LookupError(f"feature {feature}: {last_prefix!r} is not on top of {first_prefix!r}")
    return name, first, last


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Revert one parity feature, newest first.")
    parser.add_argument("feature", nargs="?", help="feature id (see --list)")
    parser.add_argument("--list", action="store_true", help="show the features this script knows")
    parser.add_argument("--dry-run", action="store_true", help="resolve the range and stop")
    parser.add_argument("--yes", action="store_true", help="commit the revert instead of leaving it staged")
    args = parser.parse_args(argv)

    if args.list or not args.feature:
        for fid, (name, first_prefix, last_prefix) in sorted(FEATURES.items()):
            print(f"{fid}  {name}  ({first_prefix!r} .. {last_prefix!r})")
        return 0

    if git("status", "--porcelain").stdout.strip():
        print("refusing: the working tree is dirty. Commit or stash first.", file=sys.stderr)
        return 1

    try:
        name, first, last = resolve(args.feature)
    except LookupError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1

    count = git("rev-list", "--count", f"{first}^..{last}").stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    print(f"feature {args.feature}: {name}")
    print(f"branch:  {branch}")
    print(f"commits: {count}  ({first[:7]}..{last[:7]})")
    git("log", "--oneline", f"{first}^..{last}", check=False)
    if args.dry_run:
        print("\ndry run: nothing changed.")
        return 0

    # git revert over a range applies the commits newest first, which is what a rollback needs.
    revert = git("revert", "--no-commit", f"{first}^..{last}", check=False)
    if revert.returncode != 0:
        git("revert", "--abort", check=False)
        git("reset", "--merge", check=False)
        print("refusing: the revert conflicted, and was aborted; nothing changed.\n", file=sys.stderr)
        print(revert.stderr.strip() or revert.stdout.strip(), file=sys.stderr)
        print("\nA conflict here means a later feature is still applied. Revert the newest feature "
              "first (docs/PARITY-ROLLBACK.md).", file=sys.stderr)
        return 1

    if args.yes:
        git("commit", "-m", f'revert: parity feature {args.feature} - {name}')
        print(f"\ncommitted the revert of feature {args.feature}.")
    else:
        print(f"\nfeature {args.feature} reverted and STAGED, not committed.")
        print(f"  inspect:  git diff --cached --stat")
        print(f"  commit:   git commit -m 'revert: parity feature {args.feature} - {name}'")
        print(f"  discard:  git reset --hard HEAD")
    print("\nbefore you push, the gates that feature had to pass:")
    for gate in GATES:
        print(f"  {gate}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
