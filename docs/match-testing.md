# Audiobind: the match-test programme

[← Back to development notes](development.md)

The matcher decides which Audible edition a folder becomes, and at HIGH it converts without asking.
This programme is how every revision proves it did not get worse. It lives in `tests/matchbench/`
and runs as part of the normal suite (`tests/test_matchbench.py`), so CI fails on it like on any
other test.

```bash
python -m tests.matchbench              # run offline, print the scoreboard, exit 1 if the gate fails
python -m tests.matchbench check -v     # ... and list the rows declared unlabelled
python -m tests.matchbench results      # per-row CSV (add --all for the adversarial cases)
python -m tests.matchbench accept --why "what changed and why it is right"
python -m tests.matchbench import --db /path/to/audiobind.db   # merge new library rows into the corpus
python -m tests.matchbench refresh      # fetch lookups the frozen cache lacks (network, additions only)
python -m tests.matchbench drift        # re-fetch every frozen answer live; report what would change
python -m tests.matchbench calibrate    # how well the synthetic catalog model matches real answers
```

## What it runs

| File | What it is | Who changes it |
| --- | --- | --- |
| `corpus.csv` | every row of the owner's library (123): key, shape, files, hours, the DB's tier/ASIN/pin | `import` |
| `labels.csv` | the written labels and failure-class tags; every row the DB cannot label must appear here | a person |
| `adversarial.json` | 42 synthetic cases for the failure classes we have paid for, with a catalog model | a person |
| `known_defects.csv` | every wrong answer on the real corpus, named; each must keep reproducing | a person |
| `baseline.csv` | the accepted per-row result, each row with the reason it was accepted | `accept` |
| `docs/match-catalog-cache.json` | the frozen catalog: real Audible/Audnex answers, trimmed | `refresh` |

Each corpus row is rebuilt into the `BookUnit` the scanner would name (`units._name_units`, ingest
cleaning included), matched with `matching.match_unit`, and then put through the two pipeline steps
that change a tier: the ingest embedded-tag retry (`pipeline._match_embedded` /
`_embedded_is_better`, when the tags were recorded) and the COLLECTION tier
(`looks_like_collection`). Pinned rows are matched **without** their pin: the pin is the owner's label,
and the question is whether the matcher would have found it.

The bench observes, it never steers: `_score_reading` and `_pick_and_margin` are wrapped only to
record the candidate pool and the ranking. Every catalog read goes to the frozen cache;
`urllib.request.urlopen` raises; the region is pinned to `us`. A cache miss is an admission failure,
not a quiet "no match".

## The stages

* **A – corpus admitted.** Every row is graded or listed as skipped with its reason; every row has a
  label or an explicit `unlabelled` line with why; no orphan labels; no catalog lookup missing.
* **B – retrieval recall.** For each row labelled with an edition: was it in the pool the matcher
  built (and at what rank)? Reported as in-pool, @1, @3, @5. This is the stage the grade-only harness
  could not see: *Rise of Empire* graded HIGH on a pool of one.
* **C – pick accuracy.** Did it pick a labelled edition, overall and given the edition was in the pool.
* **D – grade safety.** Wrong answers by tier. A wrong answer at HIGH fails the build unless it is in
  `known_defects.csv`; every listed defect must still reproduce, so a fix forces the list to change.
* **E – explanation.** The grade must agree with the reasons the panel shows: a HIGH with any limiting
  reason, a MEDIUM/LOW with none, a NONE that had candidates but no rejection note, a knockoff above
  LOW, or a foreign/adaptation hit at HIGH is a violation. The reasons (`limiting_reasons`) mirror
  `grade()`'s gates; a test checks the mirror against `grade()` on a 2,560-point grid, so a change to
  the thresholds shows up as a named change, not a silent drift.

Then the **baseline diff**. Decision columns (label, pick, tier, in-pool, rank, verdict, reasons) must
equal `baseline.csv`; score columns only drift and are counted. A difference fails the gate until
someone runs `accept --why ...`, which writes the reason beside each changed row, so the pull request
shows old value, new value and why in one diff. `accept` refuses a run with a new wrong-at-HIGH, an
explanation violation, an admission problem or a failing adversarial case, and refuses a retrieval or
pick regression without `--allow-regression` (the owner's call, made visibly).

## Labels, and how strong each kind is

| Source | Meaning | Strength |
| --- | --- | --- |
| `owner` | written in `labels.csv` by the owner | ground truth |
| `pin` | the owner hand-set this ASIN in the app | ground truth |
| `review` | a reviewer's reading of titles and runtimes, waiting for the owner | evidence, not truth |
| `db-approved` | converted at MEDIUM/LOW, so a person pressed Convert | usually right (one is not: *Island of Glass* was converted as *The Missing*) |
| `db-auto` | converted at HIGH with nobody looking | **the matcher's own past answer** - consistency, not correctness |

The scoreboard reports every stage per source. Read `db-auto` accuracy as "did not change its mind",
never as "is right". To add a label, add a line: `key,expect,source,classes,note` where `expect` is an
ASIN (or several, `A|B`), `none` (no edition is right: box sets, non-books), `not:ASIN` (known wrong,
right answer unknown) or `unlabelled`. Then `accept` the verdict change it causes.

## Adversarial cases

Written as what correct behaviour **is**, before running them. A case that fails and is accepted as a
real defect carries `known_gap` with its verified cause; the gate asserts it keeps failing. Classes:
same title / different production (dramatized, GraphicAudio, full cast, audio drama, split
dramatizations), summaries / study guides / review junk, foreign editions of English books and
genuinely foreign libraries (German, Spanish, French with accents), box sets, series echoes, series
numbers, misspelled authors, title/author swaps, volumes vs books (split halves, novellas vs omnibus),
one weak candidate, same title by two authors, narrator editions, torrent noise, ASIN hints, embedded
tags, co-authors, abridged vs complete.

Their catalog is `ModelCatalog`: strict title filter, strict author filter, keywords anywhere.
`calibrate` measures it against the real frozen answers: it returns about 57 % of what Audible
returned, because the real search is fuzzier. So synthetic cases test the matcher against a model
of retrieval; real retrieval evidence comes only from the frozen corpus.

## Refreshing the catalog is a reviewed act

`refresh` only fills gaps (never overwrites) and its commit should stand alone, listing what was
added. `drift` re-fetches every frozen answer into memory (or `--out` a scratch file) and reports
which answers changed and which outcomes would move - run it before trusting a refresh, and on a
schedule if you want to see Audible add a Dramatized Adaptation before a user does.
