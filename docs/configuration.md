# Audiobind: configuration

[← Back to the README](../README.md)

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LIBRARY_ROOT` | `/library` | Source library (writable so Delete can fully remove a book) |
| `OUTPUT_ROOT` | `/output` | Where finished books go |
| `STATE_DIR` | `/config` | SQLite manifest + `audit.csv` |
| `LOG_FILE` | `<STATE_DIR>/audiobind.log` | Rotating log file (5 MB x 3 backups) written alongside stdout, so it survives a container recreate. Set to an empty string to turn the file log off. An unwritable path logs one warning and carries on with stdout only |
| `AUTO_CONVERT` | `high` | `high`, `medium` or `off` |
| `SCAN_INTERVAL_MIN` | `30` | Poll interval |
| `OUTPUT_FORMAT` | `M4B` | `M4B`, `MP3` or `M4B + MP3`: what each book is published as (also in Settings). See [Output format](#output-format) |
| `AUDIO_BITRATE` | `128k` | Minimum AAC bitrate. A source that is already richer keeps its own bitrate (256k mp3 stays 256k); everything is capped at 320k |
| `AUDIO_CHANNELS` | `2` | 1 = mono, 2 = stereo. Stereo keeps the source layout; mono halves the file size |
| `SAMPLE_RATE` | `44100` | Output sample rate |
| `JOBS` | `1` | Books converted in parallel |
| `NICE` | `10` | ffmpeg CPU priority |
| `MANUAL_WORKERS` | `2` | Manual actions (Retry, Convert anyway, Use this ASIN, Re-check) allowed at once; more get "another action is already running" |
| `MAX_RUNTIME_DELTA` | `0.15` | Hold when runtime differs by more than this fraction. 15% because a US/UK edition pair of the same book commonly differs by 16–21%: at 25% the wrong edition converted silently |
| `REGION` | `us` | Audible region (`us`, `ca`, `uk`, `au`, `fr`, `de`, `it`, `es`, `jp`, `in`) |
| `DRY_RUN` | `false` | Decide and log, write nothing. `true` always starts dry; the UI switch can turn it off afterwards |
| `WATCH_DRY_RUN` | `false` | The [watcher's](acquisition.md#the-wishlist-watcher-private-tracker) own dry run, separate from `DRY_RUN`: search the tracker and note what it would download, never download. `true` always starts the watcher dry; the Settings > Watcher switch can turn it off afterwards |
| `WEB_ENABLED` | `true` | Status page |
| `WEB_PORT` | `8080` | Status page port |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names / IPs the web UI answers to (ports are ignored). **Add the LAN IP and hostname you browse to**, e.g. `192.168.1.10,tower,tower.local`; any other `Host` gets a 403. This blocks DNS-rebinding attacks from websites you visit. Keep `localhost,127.0.0.1` in the list for scripts on the server itself |
| `TRUSTED_PROXY` | `false` | `true` only behind your own reverse proxy: `X-Forwarded-Host` / `-For` / `-Proto` are then believed, **but only from the peers listed in `TRUSTED_PROXY_IPS`** (since 0.19.0-dev5 `TRUSTED_PROXY=true` alone trusts nobody). `X-Forwarded-Host` is checked against `ALLOWED_HOSTS` instead of `Host`; `X-Forwarded-Proto: https` adds `Secure` to the session cookie |
| `TRUSTED_PROXY_IPS` | *(empty)* | Comma-separated IPs / CIDRs of your reverse proxy, e.g. `172.18.0.0/16`. Forwarded headers from any other peer are ignored. **Migrating:** if you set `TRUSTED_PROXY=true` before, add this too or the proxy's headers stop being honoured |
| `MAX_CONNECTIONS` | `64` | Concurrent web connections served at once; more get a `503` and are closed. Request headers must arrive within 10 s |
| `ALLOW_ROOT` | *(unset)* | The entrypoint refuses `PUID=0` / `PGID=0` unless this is `1` |
| `AUTH_TOKEN` | *(unset)* | **Deprecated - use the account instead (see [Signing in](install.md#signing-in)).** A shared password for the web UI and API. It only applies while no account exists: you sign in with any username and the token as the password (session cookie, 7 days, `HttpOnly`, `SameSite=Strict`), and scripts send `Authorization: Bearer <token>` to `/api/...`. Five failed sign-ins from one IP lock it out for 10 minutes. While it is set, the first-run setup page is skipped. Once an account exists, the token no longer signs in |
| `NOTIFY_URL` | | Optional webhook for a per-scan summary |
| `TRACKER_*` | *(unset)* | **The [wishlist watcher's](acquisition.md#the-wishlist-watcher-private-tracker) settings** - the site's URL, paths and cookie, the proxy, the egress guard. All are listed with their defaults in that section. Unset = the watcher is off: no thread, no network |
| `IP_UPDATER` | `off` | The [allowed-IP updater](acquisition.md#when-the-egress-ip-drifts-ip_updater): `off`, `notify` or `auto`. **Settable on the page:** Settings > Watcher > Allowed-IP updater; a value saved there wins, and this variable is still honoured as the default until you do (Reset to container value goes back to it). `off` is the behaviour before it existed: no extra request, no extra log line; the page shows only the select and a line saying the updater is off. `notify` logs a drift away from `TRACKER_EXPECTED_EGRESS_IP` once per new address, shows it on Settings > Watcher and offers **Use the current IP**. `notify` is read-only: it never calls the update endpoint. `auto` on MyAnonamouse sets the session's dynamic seedbox IP (`dynamicSeedbox.php`), confirms it with `jsonIp.php`, then re-pins. It needs "Allow session to set dynamic seedbox IP" enabled for the session and `TRACKER_HOME_IP` set, and changes the IP at most once an hour. On other trackers, or without a session, `auto` acts like `notify` ([details](ip-updater.md)). Any other value here is a startup error; the page only offers the three |
| `WATCH_INTERVAL_MIN` | `60` | Minutes between watcher cycles (10-1440) |
| `WATCH_MAX_PER_CYCLE` | `5` | Wishlist books one watcher cycle searches at most (1-20) |
| `WATCH_MAX_INCOMPLETE` | `10` | A cycle adds no torrent while this many in `QBIT_CATEGORY` are still below 99.9 % (`0` = no limit) |
| `QBIT_URL` | `http://localhost:8083` | qBittorrent WebUI the watcher hands torrents to |
| `QBIT_CATEGORY` | `audiobooks` | The qBittorrent category torrents are added to; its save path is your ingest folder |
| `QBIT_USER` / `QBIT_PASS` | *(unset)* | qBittorrent WebUI login. Without them the watcher stays disabled (a warning in the log and on Settings) |
| `DOWNLOAD_CLIENT` | `qbittorrent` | Which client the watcher hands torrents to: `qbittorrent`, `deluge`, `transmission` or `rtorrent`. The default is exactly the behaviour before this setting existed; any other value is a startup error. `QBIT_CATEGORY` is used by every client (Deluge: Label-plugin label, lower-cased; Transmission: label; rTorrent: `custom1`, the ruTorrent label). See [Download clients](#download-clients) |
| `DELUGE_URL` / `DELUGE_PASS` | *(unset)* | Deluge web UI, e.g. `http://deluge:8112`, and its password (the web UI has no user name). Read only when `DOWNLOAD_CLIENT=deluge`; both are needed |
| `TRANSMISSION_URL` | *(unset)* | Transmission RPC, e.g. `http://transmission:9091/transmission/rpc` (a bare `http://host:9091` gets that path added). Read only when `DOWNLOAD_CLIENT=transmission` |
| `TRANSMISSION_USER` / `TRANSMISSION_PASS` | *(unset)* | Optional RPC login, only if Transmission asks for one |
| `RTORRENT_URL` | *(unset)* | `scgi://host:port` (rTorrent's `network.scgi.open_port`) or `http(s)://host/RPC2` (an XML-RPC mount behind a web server). Read only when `DOWNLOAD_CLIENT=rtorrent` |
| `RTORRENT_USER` / `RTORRENT_PASS` | *(unset)* | Optional basic-auth login for an `http(s)` `/RPC2` mount. SCGI has none |
| `PUID` / `PGID` | `99` / `100` | Ownership of written files |

## Download clients

The wishlist watcher hands each torrent it fetches to one download client. qBittorrent is the
default and nothing changes for it. To use another, set `DOWNLOAD_CLIENT` and that client's
settings in the container, or choose it under **Settings > Watcher > Download client**. That
section shows the chosen client's fields, its label, and a **Test connection** button, which saves
what you typed, logs in, asks for the version and logs out. A value saved on the page wins over the
container's. The passwords are never shown again once saved.

| Client | Address | Needs |
|---|---|---|
| qBittorrent | `QBIT_URL` | `QBIT_USER`, `QBIT_PASS`; the category's save path is the ingest folder |
| Deluge 2.x | `DELUGE_URL` (web UI) | `DELUGE_PASS`; the **Label** plugin enabled (the test refuses without it); the label's move-completed folder (or download location) is the ingest folder |
| Transmission 3.0+ | `TRANSMISSION_URL` (RPC) | Login only if set in Transmission; this host in its RPC whitelist. Transmission does not move a download by label, so its download folder must be the ingest folder |
| rTorrent 0.9 | `RTORRENT_URL` (`scgi://` or `http(s)://.../RPC2`) | Nothing for SCGI, which has **no authentication**: expose that port only to trusted containers |

Every client behaves the same way for the watcher. The login comes before any tracker download, so
a client that is down never costs a freeleech credit. A duplicate or invalid torrent is a refusal,
and that row shows the error. A client that cannot be reached leaves the cycle with "... is not
available", never with a crash. The Deluge, Transmission and rTorrent adapters are tested against
simulated clients only, not real ones. Run **Test connection** before you rely on one.

## Output format

Every book is one file. By default that is an `.m4b`. Set `OUTPUT_FORMAT` (or Settings > Output
format) to `MP3` for one `.mp3` per book with the chapters, tags and cover embedded, or to
`M4B + MP3` for both in the same folder (the m4b is the archival copy and is the book's main
file). A book is published only after every file has been verified; if the second encode of
`M4B + MP3` fails, nothing is published and the book is retried.

Player note: MP3 chapter marks are ID3 CHAP frames. ffprobe-based players (Audiobookshelf,
Plex) read them; Apple's Music/iTunes does not, so use the m4b there.

## Folder layout

Books are filed under `OUTPUT_ROOT` the way Audiobookshelf expects:

| Book | Folder |
|---|---|
| standalone | `Author/Title/Title.m4b` |
| in a series, with a sequence | `Author/Series/3 - Title/Title.m4b` |
| in a series, no sequence | `Author/Series/Title/Title.m4b` |
| several authors | `Terry Pratchett & Neil Gaiman/Good Omens/Good Omens.m4b` |

Every author is in the folder name, joined with ` & `; a book with none is filed under
`Unknown`. The sequence is used as the catalog (or your override) gives it - `1`, `12`, `2.5` -
with no zero padding. Narrator, ASIN, series and sequence are all in the file's tags, so the
folder names carry no `{narrator}` braces or `[ASIN]` brackets. Characters that cannot be in a
file name are replaced, and nothing in the metadata can place a file outside `OUTPUT_ROOT`.

### Match Audiobookshelf layout (Settings > Library)

Books converted before 0.19.0-dev14 keep their old folders (for example `Author/Series/03 - Title/`,
only the first author, or a series book without a sequence filed as `Author/Title/`). New
conversions never move them. **Settings > Library > Match Audiobookshelf layout** moves them on
request:

- **Preview** lists every move (old -> new) and every book it will leave alone, with the reason.
  Nothing is written.
- **Apply** asks for confirmation, then moves each book's folder (or, when only the file name is
  wrong, renames the file) and points the book's row at the new file.

> This moves and renames folders inside /output. Audiobookshelf may treat a renamed folder as a
> new book and lose its listening progress.

Each target is worked out from the book's own `metadata.json` (title, authors, series and
sequence), so it lands where a fresh conversion would put it; anything missing there comes from
the metadata stored with the book's row. A book is skipped, never overwritten, when its new folder
already exists, the new path would leave `OUTPUT_ROOT` (a symlinked folder is resolved first), its
published file is missing or outside `OUTPUT_ROOT`, another Audiobind book is filed in the same
folder (for a file rename too), or the folder also holds audio that belongs to other books. Folders left empty by a move are removed. A failure on one book is
reported and the run carries on. Every move is recorded in `STATE_DIR/abs-layout-<UTC time>.json`
(`from`/`to` for each folder and renamed file) so it can be undone by hand. The action is refused
while a book is being encoded, and a scan never runs it.

Scripted equivalent (`dry_run` defaults to true; the real run needs `"confirm": true`):

```bash
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"dry_run":true}' localhost:8080/api/library/abs-layout
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"dry_run":false,"confirm":true}' localhost:8080/api/library/abs-layout
```

### Upgrading from a release before 0.19.0-dev14

- **Converted books stay where they are.** Upgrading moves nothing: books converted before
  0.19.0-dev14 keep their old folders until you run **Match Audiobookshelf layout**.
- **Run Match Audiobookshelf layout first** - Preview, then Apply - **before** any **Re-match all…**
  or **Retry** on converted books. Since 0.19.0-dev15 a re-check leaves a converted book whose file
  still exists alone and logs `already converted at <old path> - run Match Audiobookshelf layout to
  move it`, but **Convert anyway** really does encode the book again into the new layout, leaving
  a second copy and the old folder behind.
- **Audiobookshelf may lose listening progress.** It may treat a renamed or moved folder as a new
  book. Note where you are in books you are listening to, or let Audiobookshelf rescan and check
  before you delete anything there.
