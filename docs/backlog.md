# Audiobind Backlog

Ideas folded in from a side-by-side test of **Listenarr** (grabber) and
**Chaptarr** (Readarr-fork library manager) installed on Tower
2026-11-08. Items are research, not commitments — pull them into a version
scope one at a time.

## v0.13 candidates (from Chaptarr / Listenarr) — decided 2026-11-08

1. **Chapter preservation on M4B conversion** (Chaptarr) — SHIPPED in v0.13.0.
   Sources with embedded chapter tags keep them end-to-end (offset-accumulated
   across parts, titles preserved). Priority over Audible chapter data for
   single-part m4b/mp3 sources; Audnex wins for multi-part and other
   containers; per-file fallback unchanged.

2. **Narrator-aware metadata** (Chaptarr) — SHIPPED in v0.13.0 (scoped).
   The narrator tag and the metadata editor field already existed; what was
   missing was surfacing: the detail panel now shows "Narrated by …" and the
   editor's narrator box pre-fills from the match. The filing layout stays
   Author/Title, so there is no narrator naming token — not worth restructuring
   existing libraries for.

3. **Multi-edition as first-class data** (Chaptarr) — SHIPPED in v0.13.0
   (scoped). The top runners-up from the matching search are persisted per
   unit; the detail panel lists them as "Other editions seen" with one-click
   "Use this edition" ASIN pins (the existing lock flow). A full edition
   model with per-edition history remains out of scope.

4. **Named quality profiles** (both) — SHIPPED in v0.13.0. Settings page gains
   a Balanced / High / Source-match select that presets the auto-convert tier
   and bitrate floor client-side, flipping to Custom when those fields are
   edited. Container preference not included — output is always m4b.

5. **Metadata consensus before confidence scoring** (Chaptarr) — DEFERRED.
   No second audiobook-grade metadata provider exists; Open Library
   cross-checks would increase false review flags, not reduce them. Revisit
   if a second provider is added.

## Decisions 2026-11-09 (owner)

- **Item 1 (multi-region fallback): REJECTED** — owner saw the mockup, "don't like
  the multi-region at all". Move to explicitly-not-pulled.
- **Item 2 ("Why this match?"): APPROVED** — owner wants it live. Mockup
  `/opt/data/audiobind-designs/mock-2-why-this-match.html` is the acceptance bar
  (real scores rendered from the live matcher on The Power of Habit).
- **Item 3 (series shelf + gaps): APPROVED with revised placement** — two surfaces:
  (a) a new rail tab directly under Library (working title "Series"), and
  (b) a hook inside the book detail panel — a "See what you're missing" row that
  jumps into that book's series. Gap-detection behavior as described: the shelf
  reads the library's series/sequence data and asks Audible for the full series
  list, then shows owned vs missing with covers, runtimes and ASINs.
  Placement mockup `/opt/data/audiobind-designs/mock-3b-series-placement.html`
  APPROVED by owner 2026-11-09 ("looks great") — build cleared.

## Explicitly not pulled (owner decisions)

- **Multi-region Audible fallback search** (owner, 2026-11-09): rejected after
  seeing the mockup — not the direction he wants. Not revisiting without a new ask.
- **Grabber features** (indexers, download clients, monitoring): Audiobind is
  a ripper/converter, not a download manager — that's Listenarr/Chaptarr's
  job, already running alongside.
- **Discord notifications** (Listenarr): deferred by decision in v0.12.0.
- **PostgreSQL backend** (Chaptarr): overkill at our library scale.

## Candidate tooling (not a feature — decision logged)

- **Paper MCP** (paper.design/docs/mcp) — noted 2026-11-09. Design canvas whose
  files are HTML/CSS; an MCP server lets Claude Code read frames and turn them
  into code (one-click install for Claude Code). NOT adopted for 0.15.x: the
  existing mockup loop (self-contained HTML built from real data in
  audiobind-designs, browser-rendered, owner approves pixels) is cheaper and
  proven, and the real UI is Python-embedded strings in web.py, which no design
  tool ports automatically. Reconsider for the next major UI redesign or any
  new project with a marketing/landing page. Setup path if adopted: Paper
  Desktop on the owner's Windows laptop, "Add to Claude", plus a stdio MCP
  config for the container's Claude Code pointing at the desktop CLI.

## UX sequence (decided 2026-11-09, owner approved)

From the UX research digest (/opt/data/audiobind-designs/ux-research-2026-11.md).
Order fixed by owner: sequence below, then re-look at the remainder. Every step:
mockup -> owner pixel approval -> Claude Code build -> verify -> banked deploy;
rollback = prior image tag on Tower + docs/rollback.sh (written and tested
BEFORE the first deploy in this series); each feature one commit.

1. **Release "queue hygiene":** P2 auto-lock on human-confirmed fields (S) +
   P5/P6 table semantics kit (tabular-nums, em-dash cells, relative-time
   cutoffs, one labeled status dot per row) + anti-pattern fixes that ride
   along: visible kebab menu replaces hover-only row actions; minimum text
   sizes become a verification gate. Anti-patterns are acceptance criteria, not
   separate items.
2. **Release 2:** P3 per-field match/apply in review (M) — designed to live in
   the detail panel (no new modals).
3. **Release 3:** P1 persisted filter/sort URL state + P10 saved smart filters
   as rail items (M).
4. **Release 4 (re-evaluate):** P4 remap rules from corrections (L) — needs
   release 2's field-level correction records. Owner re-look happens after 3.
Remaining research patterns (P7 frozen header, P8 LQIP covers, P9 audit log,
P11 decimal sequencing, P12 raw-data escape hatch) revisit after release 3.

## Candidate direction: Amazon/ASIN + review UX — noted 2026-11-09

Owner asked for "pick an ASIN from Amazon as well as Audible" and general
improvements. Reality check first: Amazon has no free product API (PA-API
needs an Associates account with qualifying sales; scraping breaks constantly),
but Amazon audiobook listings usually carry the SAME ASIN as the Audible
edition, and the catalog stack already covers the gap for free. These are the
shaped versions of the idea:

1. **Multi-region Audible fallback search** — lands candidate v0.15.x. Today
   search and ASIN lookup use one effective region, so titles sold only in
   other stores (UK/DE/AU/CA editions, foreign-language editions) come back
   "no match". api.audible.com and Audnex both accept a marketplace/region
   param: on a miss, fan out to the other stores, badge each candidate row
   with its region, and pin across regions. Covers most of what "search
   Amazon" would give, with zero new dependencies.

2. **"Why this match?" score panel** — the owner reviews ambiguous matches
   personally; today the review shows candidates but not WHY one won. Show
   the score components per candidate: title similarity, author similarity,
   runtime delta vs source duration, series/sequence agreement. Turns gut-feel
   approval into informed approval.

3. **Not-on-Audible ASIN pinning** — the literal ask, shaped honestly: allow
   pinning an ASIN that has NO Audnex record (Amazon-only listing, withdrawn
   title). Take title/author from folder name + the manual metadata editor,
   reuse the existing Amazon image-URL fallback for cover, skip chapter
   lookup with a visible "no chapter data found" state instead of failing.

4. **Chapter timeline editor** — when Audnex chapters disagree with the
   source there is only an automatic fallback. A small editor in the detail
   panel (shift/merge/split boundaries, rename, apply-on-encode) fixes bad
   disc breaks by hand. Largest of the batch; scope it as its own version.

5. **Already-in-library guard at match time** — warn when the winning
   candidate's ASIN (or title+author after normalization) matches a book
   already published, before the encode wastes an hour on a duplicate rip.

6. **Delete undo / trash** — Delete is real rm today. A 5-minute undo window
   (or move-to-trash folder on the same share) would have saved the
   sibling-delete scare fixed in 0.14.4 from ever mattering.

7. **Review-queue keyboard flow** — j/k to move, 1..9 to pick a candidate,
   enter to approve. The owner hand-reviews matches; the queue is exactly the
   screen where keyboard speed compounds.

8. **ntfy completion notifications** — self-hosted push on book published /
   review waiting / failure. Discord was explicitly deferred by decision; ntfy
   is a separate, self-hosted channel and needs its own owner decision.

9. **Series shelf grouping** — optional shelf mode grouping published books by
   series with sequence badges, reading the series data the matcher already
   persists.

## MAM acquisition pipeline + wishlist/discovery — planned 2026-11-10 (owner approved)

Phase plan agreed with owner. Trigger stays human (a MAM bookmark or a
wishlist click); everything after the trigger is automatic. Ratio safety is
MAM's server-side "Require FL Wedge" preference (owner configured 2026-11-10:
Require mode, size-above 1 MiB, auto-apply left empty) — never rely on
client-side wedge application (Prowlarr regression history). Prowlarr is
deliberately NOT part of this: bookmarks RSS feeds qBittorrent natively, and
the wishlist watcher talks to MAM's official API directly.

### Phase 1 — ingest plumbing (Audiobind v0.16 "ingest")

- New read-only watched root: mount `/mnt/user/cache_plex/torrents` :ro as
  `/ingest` (qBittorrent has no library mount and cannot write the media
  tree; converting in place from a read-only mount keeps seeding intact by
  construction). Env `INGEST_DIR`, off by default when unset.
- Daemon treats each direct child directory of /ingest as a candidate unit —
  same unit model and matcher as the library; publish to OUTPUT_ROOT as
  today. Never write to /ingest (it is :ro anyway).
- UI: ingest rows on the table page (torrent name, match confidence,
  convert/skip actions, source dir shown in the detail panel); unmatched
  ingest rows land on the needs-attention shelf. Statuses carry a distinct
  source marker so library rows and ingest rows never blur.
- Runtime controls per standing rule: pause/dry-run already gate the whole
  loop; no ingest-specific switch without an owner ask.
- Deploy includes container recreate with the new mount + template
  (`my-Audiobind.xml`) kept in step; owner approves the recreate.

### Phase 1 acceptance test (before trusting the chain)

Bookmark one audiobook -> wedge auto-applies -> RSS -> qBittorrent auto-
download -> appears as ingest row -> converts -> published. Verify on MAM
stats that zero download bytes counted (the Require-wedge feature is marked
in-development by MAM itself; a blocked download is the expected failure
mode, a ratio hit is the bug).

### Phase 2 — discovery: search + recommendations (Audiobind)

1. **Search button** — queries the Audible catalog API we already use
   (multi-region per backlog item 1); results page with a "Get this" button
   that adds to the wishlist.
2. **Recommendations from the owned library** — local computation over
   persisted metadata (ASIN, authors, narrators, series, genres):
   more-by-this-author, same-narrator, same-genre. Candidates come from
   Audible catalog lookups, deduped against the library (already-in-library
   guard reused). Owner picks from suggestions; nothing auto-added.
3. **Series completion** — extends the series feature: "10 of 20 owned"
   shows the missing sequels individually or a "want them all" action; both
   add to the wishlist.

### Phase 3 — wishlist watcher (separate component, builds nothing fragile)

- Wishlist page in Audiobind: the "books to get" list, each row showing
  status (wanted / found on MAM / bookmarked / downloaded+published).
- Watcher loop: for each wanted title, search MAM's official API through the
  VPN (join `network: container:gluetun`, same netns pattern as
  qBittorrent/firefox — MAM sessions are IP-locked), auto-bookmark the best
  edition, and stop there: the Phase 1 conveyor (bookmark -> RSS ->
  download -> convert) does the rest. Research step first: confirm the MAM
  API supports bookmarking and what rate limits apply; if it does not,
  watcher degrades to a "found on MAM — click to bookmark" list, still
  useful.
- A downloaded wishlist row resolves automatically when the book appears in
  the published library (ASIN match).
