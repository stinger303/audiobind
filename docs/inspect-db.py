#!/usr/bin/env python3
"""Show any non-success rows in the Audiobind manifest with their stored note."""
import sqlite3

DB = "/mnt/user/appdata/audiobind/audiobind.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row

print("=== status counts ===")
for r in c.execute("select status, count(*) n from units group by status order by n desc"):
    print(f"  {r['status']:12} {r['n']}")

for want in ("failed", "interrupted", "held"):
    rows = list(c.execute("select path, confidence, title, hours, note from units where status = ?", (want,)))
    if not rows:
        continue
    print(f"\n=== {want} ({len(rows)}) ===")
    for r in rows[:8]:
        print(f"  {r['path'][:60]}")
        print(f"     conf={r['confidence']} hours={r['hours']} title={r['title']}")
        print(f"     note={r['note'][:200]}")
