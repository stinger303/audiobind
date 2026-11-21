# Working in audiobind

Merge, convert and correctly tag audiobook libraries. Python 3.10+, **stdlib only** (no runtime
deps), `ffmpeg`/`ffprobe` required. Version lives in `audiobind/__init__.py` **and** `pyproject.toml`.

## Where you are

- Dev checkout in the Hermes container: `/opt/data/audiobind` (branch `main`).
- Live instance: Tower, `http://192.168.5.2:8686`, container `audiobind`, library mounted **read-only**.
- Check `git status` and the branch **before** editing. Preserve unrelated changes in the tree.

## Commands

```bash
python3 -m unittest discover -s tests -v      # full suite, ~5 min — run backgrounded, don't block a turn
python3 -m tests.matchbench                   # match gate; exit 1 = FAIL. Required if a change can move a match
python3 -m tests.matchbench mutate            # pre-release constant sweep, ~50 s
python3 docs/verify-page.py                   # renders the web UI; last line must read "FAILURES: none"
ruff check .                                  # line-length 110, target py310, select E,F,W,I,UP,B
docker build -t audiobind:dev .               # CI: .github/workflows/docker.yml
```

## Definition of done

A task is done only when **all** of these are true, and the report carries the evidence:

1. Full suite run for real: the actual test count and `OK`. "Tests pass" is not evidence.
2. `docs/verify-page.py` ends with `FAILURES: none`.
3. `python3 -m tests.matchbench` exits 0 whenever matching, tiering or unit-selection changed.
4. `ruff check .` reports zero `invalid-syntax` findings, and the only `E501` line-too-long findings
   are the pre-existing baseline. The baseline before f310 was 1132 E501, 29 I001, 20 F401, 8 B905,
   5 invalid-syntax, and 4 F541. The five syntax findings are historical and the gate is zero.
   Cosmetic drift is tracked, not fixed, so a bug fix can land without reformatting 1,100 lines.
5. One commit per fix, the finding ID in the message, tree otherwise clean.
6. `CHANGELOG.md` entry and a version bump in **both** files when releasing.

Report **which** target, cases and checks ran, the result, and the report path. A self-report is not
proof — paste counts and command output.

## Hard stops — never do these without an explicit owner go

- Push, tag, release, or edit `templates/audiobind.xml` / `ca_profile.xml`. See `docs/PUBLISHING.md`.
- Deploy to Tower or restart the live container.
- Auto-fix an **ambiguous** match. Surface the case with its evidence; the owner decides.
- Point a browser check at the live instance or real library data. Use a throwaway build and share.
- Write to the source library root. It is read-only by design.
- Add `+use_metadata_tags` to any ffmpeg command — the muxer silently drops cover art and still exits 0.
- Add a dependency, or automate a tracker call beyond its documented authorized endpoints.
- Retry a test or run blindly after an uncertain result — reconcile what actually happened first.
- Treat finishing a prerequisite as permission for the next approval stage. It is not.

## Repo map

| file | owns |
|---|---|
| `pipeline.py` | scan → match → convert orchestration, unit lifecycle |
| `matching.py` | candidate scoring and tiering (HIGH/MEDIUM/LOW/NONE) |
| `units.py` | multi-file / collection grouping |
| `state.py` | SQLite state (WAL); all writes go through it |
| `catalog.py` | Audnex metadata and cover fetch |
| `encode.py` | ffmpeg invocation, chapters, m4b assembly, verification |
| `web.py` | HTTP server and UI |

## Conventions

- Read `docs/development.md` "Notes on correctness" **before** touching `encode.py`, ffmpeg args or
  chapters — those notes are silent-data-loss traps (`+use_metadata_tags`, Audnex camelCase,
  chapter-timing 0.85–1.20 window).
- Match changes must pass the programme in `docs/match-testing.md`; a scoreboard without provenance
  is not accuracy.
- Keep secrets, local plans and generated evidence out of commits.
- Update the doc whose subject you changed: `docs/install.md`, `using.md`, `acquisition.md`,
  `configuration.md`, `development.md`.
