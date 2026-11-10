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

## Explicitly not pulled

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
