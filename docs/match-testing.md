# Audiobind: the match-test programme

[← Back to development notes](development.md)

The matcher decides which Audible edition a folder becomes, and at HIGH it converts without asking.
This programme is how every revision proves it did not get worse. It lives in `tests/matchbench/`
and runs as part of the normal suite (`tests/test_matchbench.py`), so CI fails on it like on any
other test.

```bash
python -m tests.matchbench              # run offline, print the scoreboard, exit 1 if the gate fails
python -m tests.matchbench check -v     # ... and list weak labels and full out-of-scope reasons
python -m tests.matchbench results      # per-row CSV (add --all for the adversarial cases)
python -m tests.matchbench accept --why "what changed and why it is right"
python -m tests.matchbench labels --write   # add DB-labelled rows, rewrite the evidence columns
python -m tests.matchbench mutate       # move each gate constant; pre-release check (~50 s)
python -m tests.matchbench mutate --check   # ... and exit 1 if the profile differs from mutations.csv
python -m tests.matchbench mutate --cases   # flip every adversarial expectation; which ones does the gate notice (~1 s)
python -m tests.matchbench mutate --cases --check   # ... and exit 1 if that differs from case_mutations.csv
python -m tests.matchbench import --db /path/to/audiobind.db   # merge new library rows into the corpus
python -m tests.matchbench refresh      # fetch lookups the frozen cache lacks (network, additions only)
python -m tests.matchbench drift        # re-fetch every frozen answer live; report what would change
python -m tests.matchbench calibrate    # how well the synthetic catalog model matches real answers
```

## What it runs

| File | What it is | Who changes it |
| --- | --- | --- |
| `corpus.csv` | every row of the owner's library (123): key, shape, files, hours, the DB's tier/ASIN/pin | `import` |
| `labels.csv` | one line per corpus row: the label, its `label_source`, and its evidence tuple | a person (hand columns); `labels --write` (evidence columns) |
| `adversarial.json` | 51 synthetic cases for the failure classes we have paid for, with a catalog model | a person |
| `known_defects.csv` | every wrong answer on the real corpus, named; each must keep reproducing | a person |
| `baseline.csv` | the accepted per-row result and decision, each row with the reason it was accepted | `accept` |
| `mutations.csv` | the recorded mutation profile: what moving each constant does | `mutate --record` |
| `case_mutations.csv` | the recorded case profile: what flipping each adversarial expectation does | `mutate --cases --record` |
| `docs/match-catalog-cache.json` | the frozen catalog: real Audible/Audnex answers, trimmed | `refresh` |

Each corpus row is rebuilt into the `BookUnit` the scanner would name (`units._name_units`, ingest
cleaning included) and matched with `matching.match_unit`. It then goes through the pipeline steps
that decide what happens to the book: the ingest embedded-tag retry (`pipeline._match_embedded` /
`_embedded_is_better`, when the tags were recorded), the COLLECTION tier (`looks_like_collection`),
and `pipeline.choose_action` with the configured runtime tolerance. Pinned rows are matched
**without** their pin: the pin is the owner's label, and the question is whether the matcher would
have found it.

The bench observes, it never steers: `_score_reading` and `_pick_and_margin` are wrapped only to
record the candidate pool and the ranking. Every catalog read goes to the frozen cache;
`urllib.request.urlopen` raises; the region is pinned to `us`.

## The stages

* **A – corpus admitted.** Every corpus row has exactly one line in `labels.csv`, no orphans, no
  unlabelled rows. A catalog lookup missing from the frozen cache is a **bench error on the row that
  made it** (verdict `error`, tier `ERR`), never a quiet NONE. A stored `null` is a real frozen
  answer (Audnex had no record); only an absent URL is a miss.
* **L – label evidence.** Every label's evidence is recomputed and checked (see below). A
  contradicted label fails the gate.
* **B – retrieval recall.** For each row labelled with an edition: was it in the pool the matcher
  built, and at what rank? Reported as in-pool, @1, @3, @5.
* **C – pick accuracy.** Did it pick a labelled edition? Quoted over the **trusted** sources first;
  see "Reading the provenance split".
* **D – grade safety.** Wrong answers by tier. A wrong answer at HIGH fails the build unless it is in
  `known_defects.csv`; every listed defect must still reproduce, so a fix forces the list to change.
* **E – explanation.** The grade must agree with the reasons the panel shows: a HIGH with any
  limiting reason, a MEDIUM/LOW with none, a NONE that had candidates but no rejection note, a
  knockoff above LOW, or a foreign/adaptation hit at HIGH is a violation. The reasons mirror
  `grade()`; a test checks the mirror against `grade()` on an 8,000-point grid.
* **F – decision.** What the pipeline would do with each row. See "What `would_convert_wrong` means".

Then the **baseline diff**. Decision columns (label, pick, tier, in-pool, rank, verdict, reasons,
decision) must equal `baseline.csv`; score columns only drift and are counted. One score column is a
cost: `searches`, the `catalog.search` calls a row made. The `search cost` line prints the run total
against the baseline's and the rows that moved. It never fails the gate, but without it a matcher
that searched every query twice (425 → 822 searches) read as "0 score-only drift". On the frozen
corpus a *new* query already fails, as a cache miss; a repeated query, and any extra search in an
adversarial case, did not show anywhere. A difference fails the
gate until someone runs `accept --why ...`, which writes the reason beside each changed row. The
pull request then shows old value, new value and why in one diff. A changed label is shown as
`LABEL old -> new` and is never counted as a matcher regression.

The baseline **fails closed**. If `baseline.csv` lacks a column the run produces, holds a row the
corpus does not, or misses a row the corpus has, the gate fails as `STALE` and lists the rows. It
never compares the intersection. `accept` refuses a run with a new wrong-at-HIGH, a
would_convert_wrong, a label contradiction, an explanation violation, an admission problem or a
failing adversarial case. It refuses a retrieval or pick regression without `--allow-regression`
(the owner's call, made visibly).

## Labels: provenance and evidence

Each line of `labels.csv` has hand columns (`key, expect, label_source, classes, note`) and
evidence columns the bench recomputes on every run. A stored evidence value that differs from the
recomputed one fails the gate as stale evidence. `labels --write` rewrites the evidence columns and
adds lines for new rows the DB can label. It never touches a hand column.

| `label_source` | Meaning | Strength |
| --- | --- | --- |
| `owner` | the owner confirmed it, written here | ground truth |
| `pin` | the owner hand-set this ASIN in the app | ground truth (the owner typed it) |
| `db-approved` | converted at MEDIUM/LOW, so a person pressed Convert (assumes AUTO_CONVERT=high at the time) | a person accepted it |
| `db-auto` | converted at HIGH with nobody looking | **the matcher's own past answer** - consistency, not correctness |
| `inferred` | a reviewer's reading of the evidence columns | evidence, never the owner's word |
| `out-of-scope` | run and decided, never graded; the `note` says why, with the numbers | - |

`expect` is an ASIN (or several, `A|B`, any of which is right), `none` (no edition is right: box
sets, non-books), `not:ASIN` (known wrong, right answer unknown), or empty for out-of-scope.
`pin`/`db-*` lines must agree with the DB facts in `corpus.csv`. Only the owner may override a pin.

**The evidence tuple** (`db_label, local_h, label_asin, label_h, delta_pct, runtime, folder_author,
label_author, author_match, folder_title, label_title, title_match, narrator, corroborated_by,
evidence`) is everything a person needs to audit a label without rerunning anything. The match
columns are word containment: the share of the label's words found in the folder's words. The
comparison is order-free and stopword-free, and fuzzy for words of four or more letters (difflib
>= 0.8, so "Bldacci" counts). It is the bench's own measure, deliberately not the matcher's scorer.

**Contradiction check** (each one fails the gate, printed with the numbers):

* runtime: the label's catalog runtime is outside `LABEL_RUNTIME_TOLERANCE` (35 %) of the files — a
  *label* tolerance, deliberately wider than the decision tolerance (`MAX_RUNTIME_DELTA`), because a
  label can be right while the runtime disagrees: the owner's rips are shortened by up to a fifth. A
  label at 17 % is evidence, not a contradiction; tightening the decision tolerance must never
  invalidate the corpus;
* author: the folder (or its recorded tags) names an author, and at most half of the label author's
  words are in it;
* title: at most half of the label title's words are in the folder name;
* one ASIN labels two rows whose titles are not the same book;
* for a `not:` label: the excluded edition agrees on every measure (it *is* the book, as far as the
  evidence can tell).

On top of that, an **`inferred` edition label must be corroborated**: runtime within 5 %, or
mirroring a trusted label on the same files (`corroborated_by`). An inferred label with no catalog
evidence fails. An inferred or out-of-scope line may override a DB label **only** when the DB
label's own evidence contradicts it. The scoreboard prints each such override with its reason. A
label that is inside tolerance but not corroborated is listed as **weak** (`check -v` names them).
Weak owner, pin and DB labels stand, but nothing independent backs them.

### Reading the provenance split

```
  PROVENANCE SPLIT  n  in-pool  right   pick%  wrong@H  wrong@M  wrong@L  writes  wrong-writes
    pin               15       13     13   86.7%  ...
    db-approved       14       14     14  100.0%  ...
    db-auto           67       67     67  100.0%  ...
    inferred          18        8      5   62.5%  ...
    = trusted         29       27     27   93.1%  ...
```

* Quote **`= trusted`** (owner + pin + db-approved) as the app's accuracy, with its row count.
  `= owner+db-appr.` is printed too, for the strict reading.
* **db-auto at 100 % means the matcher has not changed its mind**, not that it is right. It cannot be
  anything else: the label *is* the matcher's earlier answer.
* **inferred** is low because the reviewer labelled the hard rows; read it as a defect list, not a rate.
* `writes` counts the rows the pipeline would convert unseen. The F line repeats it by source: in
  this corpus most unseen writes are graded only by db-auto labels.

## What `would_convert_wrong` means

The damage a matcher does comes from one decision: **HIGH (an auto-convert tier) + runtime
agreement → convert**. Stage F computes, per row, what the pipeline would do:

| decision | when |
| --- | --- |
| `convert` / `remux` | `choose_action` says convert (or remux: a single AAC .m4b/.m4a) and the tier is in `effective_auto_tiers()` |
| `hold` | HIGH/MEDIUM but `Audit.runtime_matches` is False (configured tolerance) |
| `review` | everything else: MEDIUM at AUTO_CONVERT=high, LOW, COLLECTION, NONE |

Codecs are not in the corpus. A single `.m4b`/`.m4a` is taken as AAC (remux), anything else as MP3.

* **`would_convert_wrong`**: the row would be written into the library unseen, and the pick is not
  the labelled edition (or any pick for a `none` label, or the excluded ASIN for a `not:` label).
  This is the only number that measures real damage. **The gate requires 0.**
* **`would_hold_wrong`**: the labelled edition was picked and the runtime check held it anyway.
* **held a wrong pick**: the runtime check stopped a wrong HIGH/MEDIUM pick. It is the net working.
* **unseen writes the labels cannot grade**: a convert on an out-of-scope or `not:` row; listed.
* **runtime unknown**: either side is 0. `runtime_ok` treats an unknown as **not** agreement, so such
  a book is held, never written unseen; the counter still lists them (and an unseen write with an
  unknown runtime fails the gate, which is now unreachable by construction).
  `adv:unknown_runtime_lone_candidate` is the case that keeps it that way: a lone no-runtime answer
  grades HIGH and must still be held.
  *Production exposure, measured 0.22.0-dev5 (read-only).* No state-DB copy was reachable, so this is
  measured on the DB's last export (`corpus.csv`, 2026-11-16) and the frozen catalogue. Of 122
  matched rows (DB recorded an ASIN), 120 have their product in the catalogue and **0** carry runtime
  0; no row has 0 local hours. The other 2 cannot be checked offline (`B0BCDTCVGV` Unlocked, review;
  `B014JWR5I4` The Guilty, a pinned HIGH conversion), so the held-instead-of-written rate is 0/120,
  at most 2/122 (1.6 %). The bench's own picks today: 0 of 114 have an unknown runtime. No-runtime
  records do exist in the catalogue: 44 of 958 ASINs (4.6 %), each in one corpus pool, none picked.
  The rule has no pin exemption (`Audit.runtime_matches`), so a hand-set ASIN whose record has no
  runtime is held too. Re-measure on a DB copy with `SELECT path, asin FROM units WHERE asin != ''`
  joined to each product's catalogue runtime (`detail.match.hours` = 0).
* **if AUTO_CONVERT=medium**: wrong picks that would be written at the medium setting.
* **F's reach**: graded rows whose pool holds a wrong candidate inside the tolerance. F can only ever
  fire there; a bad pick anywhere else is held by the runtime check.

The tolerance printed in the header comes from the real path (env default, `MAX_RUNTIME_DELTA`, or a
settings override), never a constant. A different value in your environment changes decisions, and
the baseline diff will show it.

Rows whose ingest retry could not be reproduced (embedded tags were never recorded) are listed as
"decided on the folder reading only": the retry could still replace their NONE/LOW in the real app.
The tags cannot be rebuilt from the state DB: the app stores the tag text only when the tags *won*
(`name_source = embedded`, then `query_title`/`query_author` are the tags). When they lose, the
only trace is a note naming the retry's pick; when the retry finds nothing, there is no trace. Of
the 7 such rows today, 3 are pinned (a live pin skips the search, so the app never ran the retry),
2 were MEDIUM in the DB (the retry only runs on NONE/LOW), and 2 (`22 Midnight Line`, `In Too
Deep`) recorded LOW with the same ASIN the bench picks: the retry ran and did not replace them.
Only the files themselves (or the rip's `metadata.json`) could supply the tag text.

An adversarial case can say which reading must win with `"embedded": true|false`: the retry's
match must (or must not) replace the folder's.

## Mutation tests: which constants are load-bearing

`mutate` re-runs the whole bench once per (constant, value). Each constant is moved to three values
below and three above its default. The bench takes about 0.5 s per run, so the full sweep is under a
minute. It is meant for a pre-release check, **not the CI gate**.

```bash
python -m tests.matchbench mutate                # print the sweep
python -m tests.matchbench mutate --only grade   # just grade()'s gates
python -m tests.matchbench mutate --record       # write mutations.csv (after an intended change)
python -m tests.matchbench mutate --check        # exit 1 if anything moved since the record
```

For each value it prints the number of flipped rows (real/adversarial), bench errors, pick accuracy
(all and trusted), wrong@HIGH, would_convert_wrong, would_hold_wrong, the adversarial spec count,
**catalogue searches per run** and example rows. Each constant is then classified:

* **load-bearing**: the nearest step either way changes outcomes;
* **slack**: only farther steps do (the corpus has no case near the threshold);
* **one-sided**: only moving it one way changes anything;
* **cost-only**: no outcome changes at any value, but the number of catalogue searches does. This is
  a *guard*, not dead code: `ARBITRATE_BELOW` decides whether the swapped title/author reading is
  paid for at all, and at 0.70 it makes 19 fewer searches per run than at 0.90 with every decision
  identical. Worth knowing before anyone "simplifies" it away;
* **NO EFFECT on corpus or adversarial**: no outcome *and* no search change — dead code or a missing
  test case. Either is worth knowing. Currently none: `PIN_GIVE_UP_AFTER` became load-bearing when
  the bench learnt to model a hand-set ASIN, and `EMBEDDED_SCORE_EDGE` when three cases put the
  folder and tag readings both at LOW, 0.116 and 0.098 apart (`adv:embedded_edge_just_over` /
  `_just_under`), plus one where the tags are worse and must never win at any edge.

A decision-only sweep cannot see a cost guard, so the search counter exists to stop the sweep calling
one dead. `searches` counts calls to `catalog.search` per row (both the frozen-corpus and the model
path); it is recorded in `mutations.csv` like any other column.

`LABEL_RUNTIME_TOLERANCE` is swept too, although it is the bench's own constant: it moves no
decision, only how many labels their own runtime contradicts (the `label_contradicted` column, which
counts as an outcome for the classification, because each one fails the gate). Measured at 0.22.0-dev5:
the largest label runtime gap on the corpus is −21 % (Echo Burning, db-auto), so the count is 6 at
0.15, 1 at 0.20 and 0 from 0.25 up. At 0.15, the decision tolerance, the 6 would include the owner's
pin on The Power of Habit (+17 %). That is why the two tolerances must stay separate. The corpus cannot choose
between 0.25 and 0.45. The upper side is held by `test_a_contradicted_label_in_labels_csv_fails_the_gate_with_the_numbers`,
whose −38 % label must stay contradicted.

grade()'s gates are inline literals, so they are moved through `grade_with`, a mirror that a test
checks against `grade()` on the grid. Before any gate is moved, `mutate` also checks that the mirror
reproduces the corpus exactly. The runtime tolerance is moved through the settings path
(`config._EFFECTIVE["runtime_delta"]`), as the settings page stores it.

Caveat: a mutation that changes which **queries** the matcher makes (e.g. `LANGUAGE_PENALTY` above
0.85, a wide `CORROBORATION_WINDOW`) hits responses the frozen cache does not have. Those rows show
up as bench errors (`err`), unmeasured, never as a silent NONE.

## Meta-mutation: which expectations are load-bearing

`mutate` moves the code and asks whether the cases notice. `mutate --cases` moves the **cases** and
asks whether the bench notices. Every expectation of every adversarial case is flipped, one at a
time, against the unchanged matcher, and re-evaluated through `evaluate_case` itself:

| expectation | flipped to |
| --- | --- |
| `asin` / `asin_not` / `none` | a label that turns the verdict on the observed pick around (another edition is right; the excluded edition is right; the pick is right) |
| `tier`, `decision` | every other value |
| `max_tier` / `min_tier` | the opposite bound, one rank past it (`max_tier: HIGH` and `min_tier: NONE` have no flip: they are true of everything) |
| `wrong_max_tier` | one rank under a wrong pick's tier |
| `must_pick`, `in_pool`, `no_write`, `no_pick`, `embedded` | the opposite assertion (the flags are two-way: `false` asserts the opposite, absent asserts nothing) |

A flip is **noticed** when the gate's own lines for that case change: the case fails, a known gap
"now passes" or "fails differently", a known defect "no longer reproduces", a wrong@HIGH appears.
The baseline diff is counted apart. It records what the code did and `accept` rewrites it, so an
expectation that only the baseline notices says nothing a blank case would not.

* **load-bearing**: the gate notices every flip.
* **partly**: it notices some; **masked**: the problem list changes but the gate's verdict does not.
* **baseline-only**: only the baseline's label and verdict columns move.
* **decoration**: nothing moves.

A case with no load-bearing expectation is **vacuous**, and the command exits 1. The suite runs the
profile on every run (`TestCaseMutation`) and compares it with `case_mutations.csv`, so a new
expectation that tests nothing fails CI until it is recorded and explained.

Measured at 0.22.0-dev6, 51 cases: no vacuous case. Before the fixes, 24 of 120 expectations were not
load-bearing. 15 of them sat in the 12 known-gap cases, for one reason: the gate only asked a gap to
*keep failing*. A gap accepted as "right book, graded LOW" could turn into "wrong book" and the spec
said nothing (stages D and F excuse a known gap too). Three changes closed that:

* `gap_fails`: each known gap lists the exact problems it fails with. The gate fails when a gap fails
  any other way. A gap without it, or a `gap_fails` without `known_gap`, fails admission.
* `must_pick` on the six gap cases whose label nothing consumed, and `in_pool: false` on the two
  regional cases (their premise: the right edition is unreachable).
* An expectation key no check reads (a misspelt `max_teir`) fails admission. It used to pass.

After: 119 of 128 load-bearing. The other 9 are accepted and pinned by a test: a label (`asin_not`,
`none`) in a passing case whose `max_tier` already forbids everything the label could flag. The label
names the wrong edition for the scoreboard's wrong@tier columns; the ceiling is what gates.

## Independent replay: a second path to the same answer

Both kinds of mutation test the bench against itself. `docs/match-replay.py` checks it against
something else: it re-decides every corpus row **without importing `tests/matchbench`** and compares
ASIN, tier and decision with `baseline.csv`.

```bash
python3 docs/match-replay.py          # summary and every disagreement; exit 1 on any
python3 docs/match-replay.py --rows   # ... and one line per row
```

The two paths are built differently on purpose:

| step | bench | replay |
| --- | --- | --- |
| the unit | a `BookUnit` built by hand, then `units._name_units` | the corpus laid out as folders and empty files in a temp dir, walked by `units.discover_units` / `discover_ingest_units` |
| match, retry, COLLECTION, action | each pipeline step re-enacted in `run_unit` / `_finish` / `decide` | `pipeline.audit_unit` itself, with ffprobe and the tag reader stood in for |
| decision | `decide` | `process_unit`'s rule, restated in the script |

A disagreement is a finding for the owner, with both values. Neither side is edited to agree. A row
the scanner does not produce, or a catalog answer the cache lacks, is reported as `NOT REPLAYED`,
never skipped. `tests/test_match_replay.py` asserts agreement on every suite run, and that the
replay moves when the app does (a 20 % tolerance, a stricter title floor, a changed baseline value).

Measured at 0.22.0-dev6: 123 of 123 rows replayed, 0 disagreements.

Agreement proves nothing about what both paths have to assume, because the corpus records nothing
better: a pinned row is matched without its pin; codecs are guessed from the extension and a
folder's files are taken as MP3; embedded tags exist only for the rows where they won; a
part-bucket's folder names are rebuilt from its key; the catalog is the frozen one.

## Adversarial cases

Written as what correct behaviour **is**, before running them. A case that fails and is accepted as a
real defect carries `known_gap` with its verified cause and `gap_fails` with the exact problems; the
gate asserts it keeps failing, and in that way. Each gap
now also shows its decision (e.g. the box-set-as-book-1 HIGH is held by runtime). Classes: same
title / different production (dramatized, GraphicAudio, full cast, audio drama, split
dramatizations), summaries / study guides / review junk, foreign editions of English books and
genuinely foreign libraries (German, Spanish, French with accents), box sets, series echoes, series
numbers, misspelled authors, title/author swaps, volumes vs books (split halves, novellas vs
omnibus), one weak candidate, same title by two authors, narrator editions, torrent noise, ASIN
hints, embedded tags, co-authors, abridged vs complete, regional editions.

**Regional editions** are the class behind the owner's only real wrong write (`Jack Reacher
Series/10 The Hard Way`). The right edition (UK, Jeff Harding, 11.83 h) exists in Audible, but no US
search returns it, so the pool holds only another production of the same book. Two cases pin the
search answers to that one production. `regional_edition_unreachable_runtime_holds` uses the real
numbers (Dick Hill, 14.23 h, −18 %): the pick is wrong at HIGH (a named known defect) and the runtime
check must hold it (`decision: hold`; it converts again at a 20 % tolerance).
`regional_edition_unreachable_runtime_agrees` is the same failure with the returned production at
−6 %. It converts, and is a `known_gap`: title, author and runtime cannot tell two productions of
one book apart, and only retrieving the regional edition fixes it. `no_write: true` is the
expectation for "never written unseen, however it is stopped".

Their catalog is `ModelCatalog`: strict title filter, strict author filter, keywords anywhere.
`calibrate` measures it against the real frozen answers. It returns about 57 % of what Audible
returned, because the real search is fuzzier. So synthetic cases test the matcher against a model of
retrieval; real retrieval evidence comes only from the frozen corpus.

## Refreshing the catalog is a reviewed act

`refresh` only fills gaps (never overwrites), and its commit should stand alone, listing what was
added. `drift` re-fetches every frozen answer into memory (or `--out` a scratch file) and reports
which answers changed and which outcomes would move. Run it before trusting a refresh, and on a
schedule if you want to see Audible add a Dramatized Adaptation before a user does.
