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
  `duration` key is the app adopting the book, not a failed write.
