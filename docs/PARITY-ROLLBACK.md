# Rolling back a parity feature

MouseSearch parity lands on `parity/multiclient` as **one contiguous run of commits per feature**,
each behind a setting whose default is today's behaviour. That gives two ways out, and they are not
interchangeable:

| | when | how | what it costs |
|---|---|---|---|
| **Switch it off** | the feature misbehaves in production, you need it gone *now* | one setting, then restart the container | nothing; the code stays, the behaviour reverts |
| **Revert the code** | the feature is not wanted, or it is too broken to leave switched off | `scripts/revert-parity.py <n>` | a branch commit, then the full gate run |

Try the setting first. It is reversible in seconds and needs no build, no image, no deploy.

## Features

| id | feature | commits (subject range) | off state |
|---|---|---|---|
| 1 | multi-client download adapters (qBittorrent, Deluge, Transmission, rTorrent) | `clients: base` … `tests: conformance` | `DOWNLOAD_CLIENT=qbittorrent` (the default) |

Features 2–4 are not built yet; they get a row here when they land. The script resolves ranges from
commit subjects, not hashes, so rebasing the branch does not invalidate it.

## Switch it off

Feature 1 changes nothing for a qBittorrent install unless `DOWNLOAD_CLIENT` says otherwise, so the
off state is the default:

```bash
# in the container's settings, or Settings > Watcher > Download client
DOWNLOAD_CLIENT=qbittorrent
```

Restart. The watcher builds `make_qbit()` again, exactly as in 0.21.0. Leave the `DELUGE_*`,
`TRANSMISSION_*` and `RTORRENT_*` settings in place; they are only read when their client is chosen.

## Revert the code

```bash
cd /opt/data/audiobind
git checkout parity/multiclient && git pull
python3 scripts/revert-parity.py --list      # ids and the commit runs they map to
python3 scripts/revert-parity.py 1 --dry-run # show the range, change nothing
python3 scripts/revert-parity.py 1           # revert, staged but not committed
git diff --cached --stat                     # read it before you commit
python3 scripts/revert-parity.py 1 --yes     # or let it commit
```

The script refuses on a dirty tree, refuses when the range cannot be found, and **aborts the revert
and leaves the tree untouched** if it conflicts. Then run the gates it prints (suite, bench, both
mutation checks) and only push if they pass.

### Newest feature first

A later feature builds on an earlier one. Reverting feature 1 while feature 3 is applied conflicts,
and hand-resolving that is how a rollback turns into an incident. Revert the highest feature id
first, verify, then go down.

### What a revert does not do

* **No database change.** Feature 1 adds no columns and no tables: the new settings are rows in the
  existing settings table, and a reverted build ignores unknown keys. Nothing to migrate, either way.
* **No container change.** The image tag, the CA template and the volumes are untouched. Roll back a
  release with the image tag (`ghcr.io/stinger303/audiobind:<previous>`), not with this script.
* **Settings survive.** Rows written for Deluge, Transmission or rTorrent stay in the database. A
  later re-apply picks them up again; they do nothing in the meantime.

## Last resort: the whole feature line

If the branch itself is the problem, the branch base is the state to return to:

```bash
git checkout main                     # main is the pre-parity line
git branch -D parity/multiclient      # local only; the remote branch stays until you delete it
```

That drops every parity feature at once. It is the right move only when nothing on the branch is
worth keeping; otherwise revert per feature and keep the rest.
