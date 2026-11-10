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
