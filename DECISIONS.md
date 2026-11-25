# DECISIONS

Append-only log of choices that had an alternative and a reason. Newest first.
**Rule:** never edit history. To change a past call, add a new entry that supersedes it and name the one it replaced.

**Belongs here:** a safety posture, a build process, a support boundary, an approach rejected on evidence.
**Does not belong here:** individual fixes and version bumps — those go in CHANGELOG.md with their commit SHAs.

---

## 2026-11-22 · Nothing destructive happens on trust (0.26.0 safety release)

- **Decision:** every irreversible action must *prove* what it is about to touch before it acts. Delete refuses when the folder's contents no longer match the audited row ("re-check the book") instead of removing something it cannot prove. Publication writes intent + backup paths + expected identities **before** moving anything, and startup finishes or restores an interrupted run; an unreconcilable attempt is quarantined with a plain reason and a dismiss action, not a permanent scan stop.
- **Why:** fourteen audit findings, two of which could remove a book the user never deleted. A wrong deletion is the only failure class this app cannot apologize for.
- **Replaced:** delete-believes-the-row, recursive purge of the staged folder, and publication that stranded files on crash.
- **Related:** one writer at a time across processes via a kernel-held per-book lock; layout moves journaled and **bound to the preview the user saw** — if the plan changed, apply refuses and asks for a fresh preview. `9e850d5` `b8b936e` `6c1486c` `8294202` `e61d636` `8beaedc`
- **Copy decision:** the permanence is stated before the button, and the button says `DELETE PERMANENTLY` (P0-13). Wording that softens an irreversible action is a safety defect, not a style choice. `f05a7ca`

## 2026-11-22 · An unknown value is not a poisoned value

- **Decision:** a product with no runtime is treated as *unknown* and routed to Review, not as bad data to be rejected.
- **Why:** conflating "we could not determine this" with "this is wrong" silently discards good books. Unknown must be visible, not decisive. `4d65e75`

## 2026-11-21 · Python 3.10 support is proven with a real interpreter, not a linter

- **Decision:** keep `requires-python >= 3.10`, and prove it by building CPython 3.10 from source and importing the app under it. The ruff gate with its accepted baseline is now part of `AGENTS.md`'s definition of done.
- **Why:** a backslash inside an f-string expression was a `SyntaxError` on 3.10 while every gate was green at that commit — the suite ran on a newer interpreter, so the declared floor was never exercised.
- **Rejected:** dropping 3.10, or trusting `ruff`'s syntax pass as proof. `0bfb74c` `650dd62` `6b48b71`

## 2026-11-21 · Codex is the overflow builder, in a worktree, behind the same gates

- **Decision:** Codex CLI builds on the overflow lane (`dispatch-codex.sh`) next to Claude Code. Every build runs in a worktree on `pass/<id>`, never the primary checkout. Merge back with `--ff-only` only after the report is read.
- **Why:** Claude quota is the bottleneck, and the worktree is the only isolation boundary available — there is no unprivileged user namespace on this host, so Codex runs without a sandbox and the checkout itself must be the wall.
- **Standing consequence:** a Codex self-report is not proof. Gates get run by the orchestrator, same as with Claude.

## 2026-11-21 · Two stages by default: plan, then build

- **Decision:** a read-only plan naming, per fix, the test that proves it — then a build authorized only after that plan is approved. Plan approval is not build approval; build is not push.
- **Why:** "the suite is green" has twice been true of broken code here. Naming the proving test before writing the fix is what caught it.

---

## Standing rules

1. **Never auto-fix an ambiguous matching case.** Surface it for the owner; a wrong-edition match is his call, not the agent's.
2. **One commit per fix, finding ID in the message.** A quota wall must not lose work.
3. **A brief carries captured numbers, never paraphrases.** Every number in a brief comes from `capture_live_case.py` or it does not go in.
