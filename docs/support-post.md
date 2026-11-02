# Unraid forum support post

Title: **Audiobind - merge, verify and tag audiobook libraries**

---

**Audiobind** fixes an audiobook library instead of making you curate it by hand.

Point it at a folder of audiobooks and a folder to write to. On a timer it works out what each
book actually is, merges the ones spread over dozens of files into a single tagged `.m4b`, and
files the result into a clean `Author/Title` library.

### The problem it solves

Every audiobook collection eventually looks like this:

```
Michael Lewis- Moneyball/
  Moneyball/1-01 1A.mp3 ... 195 files
  Moneyball [Disc 2]/
  Moneyball [Disc 3]/
George Orwell - 1984/Disc1 ... Disc9
Vince Flynn Novels 14 books Complete Collection/  (33 files, 172 hours)
```

One book in eight folders, another book spread over nine discs, and a folder containing fourteen
books at once.

### What it does

1. **Groups parts into books** - `Part 1..3`, `Disc 1..9`, and CamelCase names like
   `StoneColdUnabridgedPart2` collapse into one unit.
2. **Verifies against the Audible catalog** - title, author, narrator, series, ASIN.
3. **Proves completeness** - total runtime is compared with the catalog runtime. A set that is an
   hour short gets reported, not merged.
4. **Merges and converts** - one `.m4b`, AAC 64 kbps mono, real chapter marks from Audnex, cover
   art and full tags embedded.
5. **Files it** as `Author/Title/Title.m4b` or `Author/Series/03 - Title/`, with an
   Audiobookshelf-compatible `metadata.json`.

### What it will not do

* It never modifies your source files - the input is mounted read-only, and nothing is deleted.
* It will not convert anything it is unsure about. Partial matches, ambiguous titles, and books
  whose runtime disagrees with the catalog by more than 25% (usually the wrong edition) are
  marked for review on the status page.

### Install

Community Applications: search for **Audiobind**. Manual template:
`https://raw.githubusercontent.com/stinger303/audiobind/main/templates/audiobind.xml`

Set the library path (read-only), the output path, and leave `AUTO_CONVERT` on `high` for the
first run so you can check the results before letting it run unattended.

### Notes

* Internet access is required for the catalog lookups (Audible search + Audnex, both free and
  keyless). No account or API key needed.
* Mono 64 kbps is deliberate: roughly 3x smaller than the source with no perceptible difference
  for spoken word.
* Bug reports and feature requests: please reply here, or open an issue on GitHub.
