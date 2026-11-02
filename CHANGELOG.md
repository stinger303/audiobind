# Changelog

All notable changes to this project are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
