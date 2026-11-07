# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.0] - 2026-11-07

### Added
- The dashboard now carries a stats block alongside the status cards: total hours of audio
  converted, total GB written to the output tree, and a library progress bar (converted vs
  total discovered units) with the same card styling. The numbers are computed from the state
  database on each render - output sizes are already recorded at publish time, so nothing scans
  the output tree - and the same block is served on `/api/state` as `stats`.
- The dashboard table gained a search box (title, author or path) and sortable columns; the
  same match and sort rules run server-side in `web.search_matches()`/`web.sort_key()`.
- Cover art appears in the table and the detail panel, served by a caching `/cover` proxy so
  the upstream catalog is hit once per ASIN rather than once per page view.
- The detail panel is now a metadata editor: title, author, series and ASIN can be overridden
  per book, and the overrides feed matching on the next scan.
- A prefetch lookahead: while the current book encodes, one background thread does the next
  book's catalog lookups and leaves the results in the catalog's URL cache, so the wait
  disappears from the main loop. The thread is read-only against the state and any failure is
  logged and retried on the normal path.

### Changed
- The test suite grew from 48 tests in 0.9.3 to 183, covering the new UI, the stats
  computation, the cover proxy and the prefetcher.

### Fixed
- The two tests that serve real HTTP endpoints bound fixed ports (8096/8098) and could fail
  with `OSError: [Errno 98] Address already in use` when a listener from a previous run or
  test lingered. They now ask the kernel for a free ephemeral port (`web_port = 0`, the bound
  port is written back to `CONFIG`), so the port is never contended.

## [0.9.3] - 2026-11-01

### Added
- The status page now declares a favicon and theme colour, so the browser tab shows the Audiobind
  icon instead of a blank page glyph. The icon is served from the container at `/favicon.svg`, and
  the image carries `icon.svg` so it works without internet access.

## [0.9.2] - 2026-11-01

### Fixed
- **The daemon could wedge and stop converting anything.** The daemon thread, the web handlers and
  the manual Retry/Convert actions all shared one SQLite connection with no lock and implicit
  transactions, which surfaced as `sqlite3.OperationalError: cannot start a transaction within a
  transaction` and left 21 books sitting in `ready` forever. Writes are now serialised behind an
  `RLock`, the connection runs in autocommit with a 30 s busy timeout, and there is a test that
  hammers it from four threads.
- Only one book is encoded at a time: a manual action now waits for the daemon's current book
  instead of running a second ffmpeg alongside it.

## [0.9.1] - 2026-11-01

### Fixed
- Books recorded before 0.9.0 keep their runtime comparison in the note rather than the detail
  blob, so their "why it is held" explanation only showed half the story. Both numbers are now
  recovered from the note.

## [0.9.0] - 2026-11-01

### Added
- **"Why it is held / in review" and "What to do"** at the top of every book panel, in plain
  English. A held book now shows both runtimes and the percentage difference and explains that it
  is usually a different edition or missing parts; a review book names the actual reason (no
  catalog match, weak match, unreadable source files, looks like several books) and the specific
  next step - Retry, paste an ASIN, Convert anyway, or split the folder.
- The catalog comparison (matched title, ASIN, runtime) is stored with each book, so the
  explanation survives restarts and does not depend on re-querying Audible.

## [0.8.1] - 2026-11-01

### Fixed
- An expanded book panel closed itself after a few seconds: the page refreshed on a fixed 10 second
  timer. Open panels are now remembered in `sessionStorage` and restored after any refresh, and the
  page only reloads when the status counts actually change.

## [0.8.0] - 2026-11-01

### Added
- **Per-book actions**, so `review` is no longer a dead end. Expand a row and you get:
  - **Retry now** - re-audit and convert under the normal rules (useful after you fix a source file
    or change `AUTO_CONVERT`).
  - **Convert anyway** - the operator override: ignore the confidence tier, the runtime hold and
    the damaged-source hold. The output is still verified before it is published.
  - **Use this ASIN** - pin the book to a specific Audible entry when the automatic match picked
    the wrong edition; the override is stored and used on every later scan.
- `POST /api/unit {"path": ..., "action": "retry" | "force" | "set_asin", "asin": ...}`.

### Fixed
- Rows recorded before 0.7.0 showed no size in the detail panel even though the columns held it.

## [0.7.0] - 2026-11-01

### Added
- **Filter chips by status** (`all`, `converted`, `ready`, `review`, `held`, `failed`,
  `interrupted`) with live counts, so the books waiting for review are reachable instead of being
  buried below the fold.
- `/api/units?status=ready` returns the filtered list as JSON for scripting.
- The detail panel now shows the full source path, not just its size and codec.

### Fixed
- The table silently showed only the newest 40 rows even when there were far more books. It now
  lists every matching unit (capped at 400 with an explicit note), sorted by title when filtered.

## [0.6.1] - 2026-11-01

### Fixed
- The row expander used a Unicode triangle that renders as a tofu box in the default UI font;
  it is now a plain `+` / `-`.
- Rows recorded before 0.6.0 say "codec not recorded" instead of inventing `0 MB / unknown codec`.

## [0.6.0] - 2026-11-01

### Added
- **Before/after detail on every row.** Click a book in the activity table to expand a panel
  showing what it was (parts, files, size, runtime, codec/channels), what it became (file, size,
  runtime, chapters and where they came from, full path) and the decision (match confidence, ASIN,
  notes). Stored as JSON on the unit, so it survives restarts.

### Fixed
- **A book that is already an AAC `.m4b`/`.m4a` is no longer re-encoded.** It is remuxed
  (`-c:a copy`), so chapters, tags and cover are rewritten while the audio samples are untouched.
  Anything not AAC (e.g. ALAC, or mp3 that only pretends to be m4b) still converts normally.

### Changed
- **Default bitrate is now 128 kbps** (was 64), and it acts as a *floor*: a source richer than the
  floor keeps its own bitrate (a 256 kbps mp3 stays 256 kbps), anything below is lifted to it, and
  the result is capped at 320 kbps.

## [0.5.3] - 2026-11-01

### Fixed
- A book with unreadable source files is now held for review **before** encoding, instead of
  spending minutes producing a short file that then fails verification (`1984` hit this: 168 files,
  one unreadable, output came out at 5.2 h against 9.7 h of sources).
- When the merged output is shorter than its sources, the error now says so in hours and quotes
  the first ffmpeg complaint, instead of a bare pair of second counts.

## [0.5.2] - 2026-11-01

### Fixed
- `last scan` now updates at the start of a pass as well as after each book, so it reads fresh
  while a long scan is still running instead of showing the previous run's age.

## [0.5.1] - 2026-11-01

### Fixed
- The control bar collapsed into colored slivers: the new progress track used the class `bar`,
  which is already the class of the toolbar container, so `height:8px; overflow:hidden` clipped
  every button. The track is now `.ptrack` and the toolbar is `.toolbar`.
- `last scan` no longer shows a stale time during a long pass - the heartbeat is updated after
  every book, not only when the whole scan ends.

## [0.5.0] - 2026-11-01

### Added
- **Progress bar for the book being encoded.** ffmpeg runs with `-progress pipe:1` and the
  percentage, realtime speed, elapsed time and ETA are published on the status page, refreshed
  every 2 seconds without reloading the page. The same values are in `/api/state` under
  `progress` for scripting.

### Fixed
- `render()` shadowed its `state` argument while building the progress text, which would have
  broken the activity table.

## [0.4.0] - 2026-11-01

### Added
- **Stop now** button. Unlike pause (finish this book, start nothing new), stop aborts the running
  ffmpeg immediately. Stopping is safe by construction: the encode writes into the work directory
  and only moves into the library after verification, so an aborted conversion leaves the partial
  file in the work directory, which is deleted. The book is marked `interrupted` and retried on the
  next scan from scratch (nothing is resumed, so a half-written file can never be published).
- The page names the book currently converting, so pause-vs-stop is an informed choice.

### Changed
- The activity table's `hours` column is now `runtime` - it is the on-disk running time of the
  source files, used to prove a multi-part set is complete.

## [0.3.0] - 2026-11-01

### Added
- **Dry-run toggle on the status page.** `DRY_RUN` no longer requires editing the container; the
  button switches between deciding-only and writing, and the choice survives a restart. The
  environment variable remains the default for a fresh install.
- The button turns amber when dry run is on (clicking it arms real writes) and red when writing.

### Changed
- `pipeline` reads the effective dry-run state from the run controls rather than the environment.

## [0.2.0] - 2026-11-01

### Added
- **Controls on the status page**: `Scan now`, `Pause after this book` and `Resume`. Pausing stops
  the daemon before it starts another book (the one in flight finishes), and the paused state is
  remembered across container restarts.
- `POST /api/control` with `{"action": "scan" | "pause" | "resume"}` for scripting.
- The page shows a running/paused pill and refreshes itself every 10 seconds.

## [0.1.4] - 2026-11-01

### Added
- Every release tag now publishes a GitHub Release whose body is this changelog section,
  generated by `scripts/release_notes.py` in the build workflow.
- New application icon: a tetra-teal spined book with a waveform across the cover, verified
  legible at 32 px.

## [0.1.3] - 2026-11-01

### Fixed
- Dry runs no longer record books as `converted`. They get their own `ready` status, shown as a
  separate card on the status page, so a dry-run pass cannot be mistaken for real output.

## [0.1.2] - 2026-11-01

### Fixed
- `audiobind health` prints "no scan recorded yet" instead of the nonsensical "infs" before the
  first scan completes.

## [0.1.1] - 2026-11-01

### Fixed
- `audiobind version` and `audiobind health` no longer fail when no volumes are mounted. The
  output-path write check now runs only for the commands that actually write (`audit`, `scan`,
  `convert`, `daemon`), which is what a container healthcheck needs.

## [0.1.0] - 2026-11-01

First public release.

### Added
- Book *unit* discovery: sibling `Part N` / `Disc N` folders (including CamelCase names such as
  `StoneColdUnabridgedPart2`) are grouped into one book, and a bare `Disc1` keys on its parent so
  a 9-disc set does not become nine one-hour books.
- Catalog verification against the public Audible search API plus Audnex: title, author, narrator,
  series, ASIN, genres, description, cover.
- Confidence grading (`HIGH` / `MEDIUM` / `LOW` / `COLLECTION` / `NONE`) with the match margin
  measured against the best *different* work, so duplicate editions do not count as ambiguity.
- Completeness proof: summed runtime is compared with the catalog runtime; a set that is short is
  reported, never silently merged.
- Merge and convert to a single m4b (`ffmpeg` concat, AAC 64 kbps mono by default) with Audnex
  chapter marks scaled onto the real timeline, embedded cover art and a full tag set.
- Publishing to `Author/Title/Title.m4b` or `Author/Series/NN - Title/`, with `cover.jpg` and an
  Audiobookshelf-compatible `metadata.json`.
- SQLite manifest so re-runs are idempotent, plus a status page and JSON API on port 8080.
- Unraid template, GitHub Actions image build to GHCR, and unit tests for parsing and grading.
