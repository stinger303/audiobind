# Audiobind Backlog

Ideas folded in from a side-by-side test of **Listenarr** (grabber) and
**Chaptarr** (Readarr-fork library manager) installed on Tower
2026-11-08. Items are research, not commitments — pull them into a version
scope one at a time.

## v0.13 candidates (from Chaptarr / Listenarr)

1. **Chapter preservation on M4B conversion** (Chaptarr).
   The merger already joins split files into one M4B. Delta: when the source
   files carry chapter tags, keep them end-to-end; when they don't, synthesize
   chapters from the Audible chapter data for the matched ASIN.
   _Lands in:_ the encode/remux pipeline.

2. **Narrator-aware metadata** (Chaptarr).
   Add a narrator field to the metadata editor and a `narrator` naming token.
   Narrators are the audiobook equivalent of "artist" and both arrs treat them
   as first-class; our editor currently doesn't.
   _Lands in:_ metadata editor + naming scheme.

3. **Multi-edition as first-class data** (Chaptarr).
   Wrong-edition tolerance and the "Lock this book to one exact Audible
   entry" flow already exist — the upgrade is an explicit edition model: one
   title, several Audible entries, each tracked as an edition with its own
   ASIN. Locking stops being a hack and becomes the data structure.
   _Lands in:_ review queue + ASIN lock.

4. **Named quality profiles** (both, Listenarr's "Prefer M4B container").
   Replace the flat settings list (auto-convert tier, bitrate floor) with
   selectable named profiles, each defining tier, bitrate floor, and output
   container preference. Familiar shape for any *arr user.
   _Lands in:_ settings page.

5. **Metadata consensus before confidence scoring** (Chaptarr).
   Today an Audible match is single-source with a confidence rating. The
   Chaptarr idea: query a second metadata source and only flag
   MEDIUM/low-confidence when the two disagree — fewer false review-queue
   entries.
   _Lands in:_ catalog matching (needs a second metadata provider).

## Explicitly not pulled

- **Grabber features** (indexers, download clients, monitoring): Audiobind is
  a ripper/converter, not a download manager — that's Listenarr/Chaptarr's
  job, already running alongside.
- **Discord notifications** (Listenarr): deferred by decision in v0.12.0.
- **PostgreSQL backend** (Chaptarr): overkill at our library scale.
