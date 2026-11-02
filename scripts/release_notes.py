#!/usr/bin/env python3
"""Write NOTES.md containing the CHANGELOG section for a release tag.

Usage: python3 scripts/release_notes.py v0.1.4
Falls back to a one-line body so a release is never blocked by a missing entry.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    tag = (sys.argv[1] if len(sys.argv) > 1 else "").lstrip("v") or "0.0.0"
    changelog = Path("CHANGELOG.md")
    body = f"Release {tag}"
    if changelog.exists():
        text = changelog.read_text(encoding="utf-8")
        pattern = rf"^## \[{re.escape(tag)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
        found = re.search(pattern, text, re.S | re.M)
        if found and found.group(1).strip():
            body = f"## {tag}\n\n" + found.group(1).strip() + "\n"
        else:
            print(f"::warning::no CHANGELOG.md section for {tag}")
    Path("NOTES.md").write_text(body + "\n", encoding="utf-8")
    print(body[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
