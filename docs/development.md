# Audiobind: development

[← Back to the README](../README.md)

## Development

```bash
python -m unittest discover -s tests -v
python -m tests.matchbench          # the match-test programme's scoreboard and gate
docker build -t audiobind:dev .
```

Any change that can move a match must pass the match-test programme: see
[match-testing.md](match-testing.md).

## Notes on correctness

* **`+use_metadata_tags` must never be added to the ffmpeg command.** It makes the MP4 muxer drop
  the attached cover art while still exiting 0. `encode.py` verifies the output has a stream with
  `attached_pic=1` before publishing, precisely because of this.
* **Audnex chapters are camelCase** (`lengthMs`, `startOffsetMs`). Reading `length_ms` yields zero
  for every chapter and silently falls back to per-file chapters.
* **Chapter timings are only trusted when they fit.** Audnex timings are scaled onto the real
  duration only if `local / catalog` is within 0.85-1.20; otherwise per-file chapters are used,
  because the catalog entry is for a different edition.
* **The output is checked against a measured total, never a guessed one.** A source file ffprobe
  could not read has no length, so `ChapterPlan.total` is `None` and `encode` refuses the book
  before ffmpeg runs, naming the file. Summing it as 0 s would make the check blame the merge
  ("files were dropped") for a file that was never measured.
* **Audiobookshelf rewrites `metadata.json`** when it scans the new book. Losing our extra
  `duration` key is the app adopting the book, not a failed write. Publication checks existing
  audio and companions before encoding. Verified audio for the edition allows publication to
  refresh a regular `metadata.json` or `cover.jpg` even if another tool changed it.

* **Schema upgrades serialize writers.** Column inspection, additive migrations, backfills
  and the field-lock bootstrap marker commit in one SQLite write transaction.

* **Publication is journaled in the SQLite manifest.** A durable attempt precedes staging in the
  book folder. Once staging has finished, the attempt records the verified file identities and
  the converted row before any destination is renamed. Startup checks every pending attempt
  before discovery. A complete set is recorded and the intent is retired in one SQLite transaction.
  An incomplete set is rolled back using the original files and the attempt is retired after restore.
  A missing backup is detected before rollback changes any files. If recovery cannot finish,
  the intent is quarantined with a reason and the scan continues. Successful replacements keep
  their backups beside the book.

* **Media writers share claims in the state directory.** Each book has a kernel file lock,
  and encodes also claim the destination folder so two keys that resolve to the same folder
  cannot publish concurrently. Delete takes the book and destination claims through its state
  commit. Layout work needs the exclusive layout claim. The shared publication journal is
  serialized during settlement and file placement. Every encode cleans only its own scratch
  directory. A hard-killed attempt can leave scratch files for an operator to remove.

The scan resolves source audio before each media reader uses it. The resolved path must
stay within the unit's library or ingest root. Internal links work, and an escaping link
puts the book in review.

Catalogue responses are checked before caching and again when read from cache. A
malformed product invalidates its response so matching cannot choose from an incomplete
candidate list. During a scan the affected book goes to review with the field that failed
validation, and the scan continues.
