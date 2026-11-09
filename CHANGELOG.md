# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.13.1] - 2026-11-08

### Added
- **Scan progress is visible.** Pressing Scan now shows a spinning
  "Scanning n/m…" indicator in the controls row, and the Scan button disables
  and relabels itself for the duration. The page stops auto-refreshing while a
  scan pass runs (it used to reload on every change, fighting the scan), then
  refreshes once when the pass ends so the fresh results appear. A page loaded
  mid-scan shows the indicator immediately.

### Changed
- **Removed the "auto: high" tag** from next to the version badge. The tier
  belongs in Settings, where it is explained; the header just showed a code
  fragment nobody asked for.

## [0.13.0] - 2026-11-08

Four ideas from the Chaptarr / Listenarr comparison landed. The fifth
(metadata consensus) is deferred - see the backlog for why.

### Added
- **Chapters survive the merge when the sources have them.** The m4b builder
  now reads the chapter tags inside the source files themselves and uses them
  for the merged book's chapter list, keeping the publisher's own titles
  ("Chapter One" beats "01 - Part_01.mp3"). Across a multi-disc rip the
  markers stack onto one continuous timeline so disc two's chapter one starts
  where disc one ended. The rules, in order: source tags win for single-part
  m4b/mp3 sources; for multi-part merges or odd containers the Audible
  chapter data is preferred (per-file tags can repeat or drift at part
  boundaries); with no tags anywhere and no usable Audible data, it falls
  back to one chapter per file as before. The log line now says which won -
  `chapters: 31 from source-embedded (31 chapters)`, audnex, or per-file.
- **Other editions are one click away.** While matching, the top runners-up
  from the Audible search (usually other editions of the same book - unabridged
  vs abridged, different narrators) are now saved with the book. A new "Other
  editions seen" section in the book's detail panel lists them with a
  "Use this edition" button that locks the match to that ASIN and re-checks
  the book, reusing the existing pin flow.
- **Named quality profiles on the settings page.** A select at the top
  presets the two conversion settings: Balanced (HIGH tier, 128k floor),
  High (HIGH tier, 256k floor) and Source-match (HIGH tier, 64k floor, which
  keeps the source bitrate as-is). Editing either setting afterwards flips
  the profile to Custom so it is visible when the settings stopped being a
  preset. All six existing settings behave exactly as before.
- **Narrator now surfaces in the UI.** The detail panel's "The Audible match"
  block shows "Narrated by …" when the catalog knows the narrator, and the
  metadata editor's narrator box pre-fills its suggestion from the match
  (the narrator tag in the m4b and the editor field itself existed already -
  this closes the loop from match to editor).

### Fixed
- **The settings page's Save button works again.** The page's JavaScript was
  being served with doubled braces from a templating artifact, which made the
  whole script a syntax error - Save, Reset and toast feedback silently did
  nothing (the underlying POST handler was fine; the buttons just couldn't
  call it). The script is now plain JavaScript and parses.

## [0.12.5] - 2026-11-08

### Changed
- **The ASIN row speaks plain English.** "Pin an exact ASIN:" is now "Lock this
  book to one exact Audible entry:", the hint explains what locking actually does
  (every future scan checks this book against this exact entry instead of
  guessing), and the Edit ASIN / Use this ASIN tooltips say "lock" instead of
  "pin".
- **The README now tells the truth about source safety.** The beta disclaimer
  explains the real mechanics: the source library is only ever read, converted
  books are written to the separate output library, and the hard delete is the
  one destructive path. (A stale note claimed converted sources were moved to a
  trash folder - that behavior never shipped, and the README no longer claims it.)

## [0.12.4] - 2026-11-08

### Changed
- **The detail panel layout matches the approved mockup.** The cover now sits
  beside the explanation instead of below the buttons; "Why it's waiting for
  you" and "Your options" stack in one column next to it; Convert anyway is the
  filled amber primary and Retry now the ghost button; the ASIN pin row carries
  a "Pin an exact ASIN:" label, the current ASIN pre-filled, and the hint line
  underneath. Delete moves to its own row at the very bottom, away from the
  action buttons.
- All detail section headers (including Your source files / What you'll get /
  The Audible match) render in the amber accent, and the big cover grew to
  190px, as in the mockup.

## [0.12.3] - 2026-11-08

### Changed
- **The review panel speaks plain English.** "Why it is review / What to do /
  Before / After / Decision" are now "Why it's waiting for you / Your options /
  Your source files / What you'll get / The Audible match", with status-specific
  headers everywhere ("Why it failed", "Why it's paused", ...). The field copy
  reads like sentences: "32 audio files in 1 part", "31.79 hours of audio",
  "Audio format not detected in the files". The match block names the book and
  links the ASIN to Audible, and a LOW/MEDIUM/HIGH confidence meter sits under
  the explanation.
- **Every advice string was rewritten** in the approved plain-English voice
  (converted, ready/dry-run, interrupted, failed, held, unreadable sources,
  no-match, collection, low confidence, medium-confidence fallback, dead pinned
  ASIN, never-scanned).
- **Book action buttons now live inside the detail panel** - Convert anyway /
  Retry now / the ASIN pin row sit right under "Your options" instead of only
  on hover at the end of the table.

### Added
- **Manual re-checks that still need a decision re-open their panel.** Retry
  now and Use this ASIN run a manual re-check; if it still ends below your
  auto-convert level (or held/failed), the detail is flagged, the page reloads
  and the panel re-opens by itself with an amber banner saying what the
  re-check found and what to do next. If the re-check passes, the book simply
  starts converting and the panel stays closed.

### Fixed
- **A same-status re-check used to go unnoticed.** The page only reloaded when
  book counts changed, so a Retry now that ended in the same status refreshed
  nothing while a panel was open. The page now watches a change signature that
  moves on every row write.
- **Detail panels no longer persist across refreshes.** Every panel starts
  closed on page load; the only panels that open by themselves are the ones a
  manual re-check asked to re-open.

## [0.12.2] - 2026-11-08

### Changed
- **Both "Audiobind" names are links home.** The sidebar brand and the page
  title now take you back to the main library view from anywhere (Settings
  included) - they used to be dead text.
- The lone "auto: high" indicator that floated at the far right of the header
  now sits with the version badge instead of hanging by itself.

## [0.12.1] - 2026-11-08

### Fixed
- **Column sorting works again - on every column.** A shadowed variable in the
  row renderer replaced each book's five sort attributes with a literal `{}`,
  so clicking any column header (not just Status and Tier) silently sorted
  nothing. The reason-line block was overwriting the sort-attribute string with
  the parsed detail blob; it now uses its own variable. Regression test asserts
  every rendered row carries all five sort keys, with and without a reason line.

### Added
- **What the tiers mean, in Settings.** The Auto-convert tier dropdown now
  explains HIGH (strong title+author match with a clear margin - safe to
  auto-convert), MEDIUM (good match, weaker evidence), LOW (held for review -
  fix with Edit ASIN), and COLLECTION/NONE (never auto-converted).

## [0.12.0] - 2026-11-08

### Added
- **Settings page** at `/settings` with six controls: auto-convert tier, output quality floor,
  wrong-edition tolerance, scan interval, CPU priority (nice), and catalog region. Values are
  persisted in the database and override environment defaults. The daemon wakes early when the scan
  interval is lowered and every consumer reads the effective value.
- **Settings link** in the left rail (gear icon, below Library).
- **`/api/settings` endpoint** with server-side validation; `/api/state` now includes an effective
  `settings` object and uses the effective `auto_convert` for the top-bar indicator.
- **README installation section** with Unraid (manual template), `docker run`, and Docker Compose
  paths, plus a first-run paragraph.

### Changed
- **Row counter removed** from the toolbar next to the search box — the static "N books" / "N of M
  shown" label, the `.ab-shown` CSS, and the JS that updated it are gone. The empty-state message
  is unchanged.

## [0.11.8] - 2026-11-08

### Fixed
- **The expanded detail panel no longer forces the page sideways.** The detail
  row inherited the books table's no-wrap cell rule, so the Why/What text, the
  Before/After blocks and the metadata hint could not wrap and pushed the table
  wider than the screen. The detail row now wraps text normally; the compact
  book rows keep their no-wrap cells.

## [0.11.7] - 2026-11-08

### Fixed
- **The hover action buttons no longer clip at the screen edge.** The three
  row buttons (Retry now / Convert anyway / Edit ASIN) were wider than the
  actions cell at typical viewports, and the cell's hidden-overflow clipped
  them mid-word. They now wrap to a second line inside the cell when space is
  tight, with tighter padding so all three fit on one line at 1080p.

## [0.11.6] - 2026-11-08

### Fixed
- **The table can no longer grow past its columns.** The books table now uses
  `table-layout: fixed`, so long titles can never push the STATUS/TIER/RUNTIME
  columns (and the hover actions) off-screen again - the max-width cap alone did
  not hold under auto layout. The BOOK column is capped at `min(560px, 30vw)`
  with the title truncating on ellipsis, and the freed width flows into the
  actions column where the hover buttons sit, right of Last Scan like the
  mockup. The hover overlay is clipped inside its cell, so no width produces a
  horizontal scrollbar.

## [0.11.5] - 2026-11-08

### Fixed
- Rail footer now wraps long host paths inside the sidebar (overflow-wrap:anywhere, 10.5px).
- BOOK column constrained to 38%/520px; title and author truncate with ellipsis.
- Hover action buttons anchored inside a fixed-width last column (170px), no longer cause horizontal scroll.

### Changed
- Delete confirmation is now a shared modal overlay instead of an inline strip.
- Row-hover "Use this ASIN" renamed to "Edit ASIN"; clicking it opens the detail panel and focuses the ASIN input (shelf cards too). Panel button unchanged.

## [0.11.4] - 2026-11-08

### Fixed
- The rail footer still said the library was "(read-only)" after 0.11.3 made it
  read-write for the delete feature. The stale note is gone.

## [0.11.3] - 2026-11-08

### Added
- **A plain-English reason line under the title for problem rows.** Held, review
  and failed units now show the "why" text from their advice explanation directly
  in the books table (amber for held/review, red for failed), so you can see what
  is wrong without expanding the detail panel.
- **Delete a book — permanently.** A Delete button in the detail panel's action
  row opens a warning dialog ("This permanently deletes the book's source folder
  and its published files. This cannot be undone."); deleting requires clicking
  the red "Delete permanently" button. The deletion removes the book's source
  folder under the library and its published output folder under the output
  library, then drops the book from the state database. It refuses while the
  book is encoding and on any path that would escape the library or output
  roots. (The library mount must be read-write for the source deletion.)
- **Hover tooltips on every control.** Scan now, Pause, Stop, Resume, Dry run and
  the per-book actions each explain what they do before you click.
- **Host paths in the rail footer.** Optional `HOST_LIBRARY` / `HOST_OUTPUT`
  environment variables are shown when set, so the footer names the real Unraid
  folders instead of the container's `/library` and `/output`. The stale
  "(read-only)" note was removed - the library is mounted read-write since the
  delete feature exists.

### Changed
- **The books table is more compact.** The permanent action-buttons column is
  gone — Retry now / Convert anyway / Use this ASIN now appear on row hover as a
  compact overlay with a solid background, matching the approved mockup. The
  column no longer reserves ~350px of width, so the table fits at 1080p without
  horizontal scrolling.
- **The Dry run button sits with the other controls** (Scan now / Pause / Stop)
  instead of alone at the right edge of the control row.

### Fixed
- No fixes in this release.

### Added
- **The rail footer shows the real host paths.** The container only ever knows `/library` and
  `/output` - meaningless names on a NAS. Two optional environment variables, `HOST_LIBRARY`
  and `HOST_OUTPUT`, are now displayed in the rail footer when set (the Unraid template ships
  them with the actual share paths); without them the container paths are shown as before.

## [0.11.1] - 2026-11-08

### Added
- **Every manual action now answers.** Retry now / Convert anyway / Use this ASIN show a toast
  ("pinning B013ILGDHQ - re-checking…") immediately, and the button itself flips to "working…" —
  previously a click was followed by silence until the next render.
- **A pinned ASIN that Audible will not serve is explained, not swallowed.** Some ASINs are
  ghost catalog entries: the API returns `REGION_UNAVAILABLE` (checked across five regions) and
  no metadata at all. When a pin falls back to the search result, the book's Why/What panel now
  says so in plain English and notes that the pin stays saved.

### Fixed
- **The sticky table header stopped one column short.** The Book header cell carried the
  `ab-bookcell` class, which also styles the body's title cell as `display:flex` - the same
  class-collision family as the old `.bar` bug, and it broke the sticky bar's layout so it did
  not span the full width. The header cell now has no cell class and the rule is scoped to
  `td.ab-bookcell`.
- The rail's Review glyph was the invalid entity `&flag;` (rendered as literal "&flag;" text)
  and Library used the nonexistent `&boxur;`; both are real unicode glyphs now.
- The rail footer's paths are labelled (`library /library (read-only)`, `output → /output`)
  instead of two bare paths.

## [0.11.0] - 2026-11-08

### Added
- **Left sidebar rail** with live unit counts for Library, Review, Held, and Failed. Clicking
  an attention item sets the corresponding status filter; "Library" clears all filters.
- **Needs-attention shelf**: collapsible card strip showing held/review/failed units (failed
  first, capped at 12 with "+N more"), each with cover, explanation, and quick-action buttons
  (Retry now, Convert anyway, Use this ASIN). Collapsed state persists in localStorage.
- **Pinned now-encoding strip** showing live title, percent, speed, and ETA with Pause/Stop.
  Hidden entirely when no encode is running — no empty stub.
- **Hover row actions** on problem rows (held/review/failed) in the table: Retry now, Convert
  anyway, and Use this ASIN appear on row hover without needing to open the detail panel.

### Changed
- **Full page redesign** matching the approved dark-theme mockup: left sidebar + main content
  layout, CSS custom properties, Inter/JetBrains Mono approximated with offline-safe system
  font stacks (no Google Fonts — the container works without internet).
- Stats display replaced with a slim monospaced stats line (hours, GB, progress with mini bar).
- Filter chips restyled as rounded pills matching the mockup's toolbar.
- Status pills and tier badges redesigned with coloured dots and soft backgrounds.
- Detail panel restyled with darker background and tighter spacing; metadata editor unchanged.
- Search box and controls row restyled to match the new design vocabulary.

### Fixed
- All existing API endpoints, polling behaviour, sessionStorage persistence, and dry-run toggle
  preserved unchanged. Page polls every 2s with the same busy-guard rules (open panels, focused
  search, interactive elements prevent reload).

## [0.10.1] - 2026-11-07

### Fixed
- **Most book covers showed a placeholder.** The ASIN-derived Amazon URL pattern resolves for
  only some ASINs; the rest return a 43-byte 1x1 tracking pixel with HTTP 200, which the
  download correctly rejected but then remembered as a six-hour miss. `/cover` now asks the
  Audible catalog for the real `product_images` URL for the ASIN first (the lookup is
  memoised with the same 15-minute cache as every other catalog call, so repeats are free),
  and falls back to the pattern only when the catalog has nothing. Books whose match data
  already carries an image URL still use it without any lookup. Miss markers from the
  pattern-URL era were cleared so every cover gets a fresh attempt on first view.

## [0.10.0] - 2026-11-07

### Added
- **Search and column sort in the books table.** A live search box filters on title, author and
  path (case-insensitive; every whitespace-separated term must match), and the column headers
  sort on title, status, confidence tier, runtime and last scan, with the direction toggling on
  re-click and an arrow marking the active column. Search composes with the existing status
  chips. The rules are not JS-only: `web.search_matches()` and `web.sort_value()`/
  `web.sort_units()` run the same logic server-side, and the chosen column and direction are
  stored in the state database, so a restart restores them.
- **Cover art in the table (48px) and the detail panel (150px).** Covers come from the
  `cover.jpg` already published next to a finished m4b, or from the catalog image URL now
  persisted with the match data (`Product.image`); books matched before 0.10.0 fall back to the
  Amazon URL derived from their ASIN. Everything is served by `/cover`, which fetches an image
  at most once into `STATE_DIR/cover-cache` and serves it from disk forever after, so the page
  still renders with the network down. A failed fetch leaves a six-hour `.miss` marker so a dead
  cover is not re-requested on every poll, and a miss falls back to a neutral placeholder. Only
  an ASIN-shaped string can become a cache filename, and a published cover must resolve inside
  `OUTPUT_ROOT`.
- **A metadata editor in the detail panel.** Title, author(s), narrator(s), series and series
  index can be corrected per book and are stored as overrides in the units table, beside the
  ASIN override they follow; `save()` leaves the column alone, so a correction survives later
  rescans. The overrides are written into the catalog payload before anything reads it, so the
  m4b tags, `metadata.json` and the author/title filing layout all agree. Corrected fields are
  marked *custom* in the panel and the book carries a badge in the table. An empty box means "no
  override" rather than "blank this field", so clearing a box hands the field back to the
  catalog; the catalog value is shown as the box's placeholder. Saving is one request with
  inline feedback - no page reload, and no library scan.
- **A catalog lookahead during the encode.** While a book encodes, one background thread does
  the next eligible book's catalog search and chapter fetch, so the main loop does not wait on
  the network when it gets there. Successful catalog responses are memoised by URL (15 minute
  TTL, bounded at 256 entries; failures are never cached), which is where the pipeline already
  looks. Exactly one lookahead runs at a time, the thread only reads `State` through the shared
  `RLock` and writes nothing, and any failure is logged and forgotten - the normal path simply
  fetches again.
- **Stats cards on the dashboard**: total hours of audio converted, total GB written to the
  output tree, and a library progress bar (converted vs total discovered units) in the existing
  card style. The numbers are computed from the state database on each render - output sizes are
  recorded at publish time, so nothing scans the output tree - and the same block is served on
  `/api/state` as `stats`.

### Changed
- The page no longer reloads while a detail panel is open or while the search box is focused or
  non-empty, so a refresh cannot interrupt reading or typing; open rows persist in
  `sessionStorage`. Table columns have fixed widths so no header truncates.
- The stylesheet and the script moved out of the `PAGE` format template into their own
  constants. CSS and JS braces no longer have to be doubled, which is what produced the template
  bugs in the 0.9.x line. Every class added in 0.10.0 is `ab-` prefixed and a test enforces it.
- The test suite covers the new work: the search filter, the sort comparator, override
  application in tag and `metadata.json` writing, cover caching with the network down, prefetch
  thread safety under concurrent `State` access, and the stats computation. `docs/verify-page.py`
  additionally boots the real server and checks the served HTML and inline JS.

### Fixed
- **The test suite collected nothing outside the container.** `tests/` was not a package, so
  `python3 -m unittest discover` silently ran zero tests on Python 3.12+, where namespace-package
  discovery was removed. With `tests/__init__.py` in place the documented command collects the
  whole suite - 185 tests at this release.
- **Total GB written was understated by about 2.4%.** The stats block divided megabytes by 1024,
  but `out_mb` is recorded as bytes/1e6, so a GB here is 1000 MB; 2560 MB read as 2.50 GB
  instead of 2.56 GB.
- The library progress bar reused the live encode bar's `.ptrack` class. It now has its own
  `.ab-statbar`/`.ab-statfill`, so restyling the encode bar cannot deform a stat card - the same
  class collision that once crushed the toolbar.
- A second catalog lookahead could start while the first was still being spawned: the guard read
  `Thread.is_alive()`, which is still false between construction and `start()`. The busy flag is
  now set inside the lock before the thread starts.
- `State.last_run()` read the database without taking the shared lock. Harmless while only the
  daemon and the web handlers existed; a background prefetch thread makes it a real race.
- The two tests that serve real HTTP endpoints bound fixed ports (8096/8098) and could fail with
  `OSError: [Errno 98] Address already in use` when a listener from a previous run lingered. They
  now ask the kernel for a free ephemeral port.

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
