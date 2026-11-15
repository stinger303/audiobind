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
| `AUDIO_CHANNELS` | `1` | 1 = mono |
| `SAMPLE_RATE` | `44100` | Output sample rate |
| `JOBS` | `1` | Books converted in parallel |
| `NICE` | `10` | ffmpeg CPU priority |
| `MANUAL_WORKERS` | `2` | Manual actions (Retry, Convert anyway, Use this ASIN, Re-check) allowed at once; more get "another action is already running" |
| `MAX_RUNTIME_DELTA` | `0.25` | Hold when runtime differs by more than this fraction |
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
| `WATCH_INTERVAL_MIN` | `60` | Minutes between watcher cycles (10-1440) |
| `WATCH_MAX_PER_CYCLE` | `5` | Wishlist books one watcher cycle searches at most (1-20) |
| `WATCH_MAX_INCOMPLETE` | `10` | A cycle adds no torrent while this many in `QBIT_CATEGORY` are still below 99.9 % (`0` = no limit) |
| `QBIT_URL` | `http://localhost:8083` | qBittorrent WebUI the watcher hands torrents to |
| `QBIT_CATEGORY` | `audiobooks` | The qBittorrent category torrents are added to; its save path is your ingest folder |
| `QBIT_USER` / `QBIT_PASS` | *(unset)* | qBittorrent WebUI login. Without them the watcher stays disabled (a warning in the log and on Settings) |
| `PUID` / `PGID` | `99` / `100` | Ownership of written files |

## Output format

Every book is one file. By default that is an `.m4b`. Set `OUTPUT_FORMAT` (or Settings > Output
format) to `MP3` for one `.mp3` per book with the chapters, tags and cover embedded, or to
`M4B + MP3` for both in the same folder (the m4b is the archival copy and is the book's main
file). A book is published only after every file has been verified; if the second encode of
`M4B + MP3` fails, nothing is published and the book is retried.

Player note: MP3 chapter marks are ID3 CHAP frames. ffprobe-based players (Audiobookshelf,
Plex) read them; Apple's Music/iTunes does not, so use the m4b there.
