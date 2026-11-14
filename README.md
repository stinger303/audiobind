# Audiobind

**Fix an audiobook library instead of curating it by hand.**

Audiobind walks a folder of audiobooks, works out what each book actually is, merges the ones that
are spread over dozens of files into a single tagged `.m4b`, and files the result into a clean
`Author/Title` library. It is built for the mess every audiobook collection eventually becomes:
`Part 1`, `Disc 3`, `StoneColdUnabridgedPart2`, three copies of the same book, and a folder called
`Vince Flynn Novels 14 books Complete Collection`.

```
library-in/                         library-out/
  Michael Lewis- Moneyball/           Michael Lewis/
    Moneyball/                          Moneyball/
      1-01 1A.mp3  ... 195 files           Moneyball.m4b   (one file, 16 chapters, cover)
    Moneyball [Disc 2]/                    cover.jpg
    Moneyball [Disc 3]/                    metadata.json
```

![Audiobind status page](docs/screenshot.png)

## Status: beta — read this before you point it at your library

Audiobind is a **beta**: a solo project with a single front-end developer, and the
code is written with heavy AI assistance (designed, reviewed and tested by a human,
but not hand-written line by line). It reads real audiobook libraries and writes to
a separate output library, and software like that can fail badly — **you may
experience data loss**. Audiobind reads your source library and writes converted books
to a separate output folder; the only thing that ever *deletes* source files is the
explicit Delete button (typed confirmation, no undo). Keep backups, run it against a
copy first, and pin a tagged release instead of `latest` if reproducible behavior
matters to you.

## How it works

1. **Group parts into books.** `Book/Disc 1..9` and `Book/Part 1..3` collapse into one unit. A
   book is a *unit*, never a directory.
2. **Verify against the Audible catalog.** Title, author, narrator, series, ASIN - via the public
   Audible search API and Audnex. Every candidate is scored, and the winner must beat the best
   *different* work by a margin before it is trusted. Knockoffs (summaries, reviews, study
   guides, zero-runtime stubs) never count against that margin, and when two candidates are
   neck and neck the one Audnex agrees with wins.
3. **Prove completeness.** The summed runtime of the parts is compared with the catalog runtime.
   A set that is an hour short is reported, not merged.
4. **Merge and convert.** One `ffmpeg` pass: concat demuxer, AAC, Audnex chapter marks scaled
   onto the real timeline, embedded cover, full tag set. The bitrate never drops below the
   configured floor (128 kbps by default) and a richer source keeps its own bitrate. A file that
   is already AAC in an MP4 container is remuxed with `-c:a copy` - no re-encode at all.
5. **File it.** `Author/Title/Title.m4b` or `Author/Series/03 - Title/Title.m4b`, plus a
   `cover.jpg` and an Audiobookshelf-compatible `metadata.json`.

### It is deliberately conservative

| Confidence | Meaning | Default behaviour |
|---|---|---|
| `HIGH` | title and author both corroborated, clear winner | converted |
| `MEDIUM` | exact title, author inferred from the catalog | converted (`AUTO_CONVERT=high` converts only HIGH) |
| `LOW` | weak or ambiguous match | left for review |
| `COLLECTION` | several books in one folder | left for review |
| held | runtime disagrees with the catalog by >25% - usually the wrong edition | not converted |

Converted books are written to the separate output library. The web UI's **Delete**
is the one destructive path — it permanently removes the source folder *and* the
published output, and requires typed confirmation. If you mount the library
read-only instead, Delete adapts: it removes the published output and forgets the
book, keeps the source folder untouched, and tells you so (the folder will
reappear on the next scan unless you remove it from the share).

Delete only removes what belongs to that one book. When two books' part folders share a
parent folder (`Author/Foo Part 1`, `Author/Bar Part 1`, ...), each book shows up on its own
row (`Author#foo`, `Author#bar`), and Delete removes only that book's part folders, never
the shared `Author` folder. If the folder you're deleting also holds another book's audio,
Delete refuses and nothing is removed.

## Installation

### Unraid (Community Applications)

Audiobind is pending publication in Community Applications. Until it is listed:

1. Copy the template into the Unraid template directory:
   ```bash
   cp templates/audiobind.xml /boot/config/plugins/dockerMan/templates-user/
   ```
2. In the Unraid web UI, go to **Apps** > **Settings** (gear icon) and check **"Allow install of second instance"**.
3. Click **Docker** > **Add Container** and select **audiobind** from the template dropdown.
4. Fill in the paths for your library (read-only), output directory, and appdata, then click **Apply**.

The template sets `PUID=99 PGID=100`, runs the container hardened (`--read-only --tmpfs /tmp
--cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID
--security-opt no-new-privileges:true`) and exposes port 8080 for the web UI. Set
**Allowed hosts** to your server's LAN IP / hostname, or the UI only answers on the server
itself.

### Docker run

```bash
docker run -d \
  --name audiobind \
  --restart unless-stopped \
  -e PUID=99 \
  -e PGID=100 \
  -e AUDIO_BITRATE=128k \
  -e AUTO_CONVERT=high \
  -e SCAN_INTERVAL_MIN=30 \
  -e REGION=us \
  -e DRY_RUN=false \
  -e ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.10,tower.local \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges:true \
  -v /path/to/audiobooks:/library:rw \
  -v /path/to/audiobooks-fixed:/output \
  -v /path/to/appdata/audiobind:/config \
  -p 8080:8080 \
  ghcr.io/stinger303/audiobind:0.15.1
```

### Docker Compose

```yaml
services:
  audiobind:
    image: ghcr.io/stinger303/audiobind:0.15.1
    restart: unless-stopped
    environment:
      - PUID=99
      - PGID=100
      - AUDIO_BITRATE=128k
      - AUTO_CONVERT=high
      - SCAN_INTERVAL_MIN=30
      - REGION=us
      - DRY_RUN=false
      - ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.10,tower.local
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    cap_add: [CHOWN, SETUID, SETGID]
    security_opt:
      - no-new-privileges:true
    volumes:
      - /path/to/audiobooks:/library:rw
      # - /path/to/audiobooks:/library:ro   # safer, but Delete can then no longer remove sources
      - /path/to/audiobooks-fixed:/output
      - /path/to/appdata/audiobind:/config
    ports:
      - "8080:8080"
```

### Hardening

The examples above run the container with a read-only root filesystem (`/tmp` is a
tmpfs; the app itself only writes to `/config`, `/output` and - for Delete -
`/library`), every Linux capability dropped except the three the entrypoint needs
to hand the files to `PUID`/`PGID` (`CHOWN`, `SETUID`, `SETGID`; the app runs
without any), and `no-new-privileges`. Audiobind feeds media from your library to
ffmpeg, and those files may come from anywhere, so these limits keep a malicious
file's reach small. `:latest` is rebuilt every week (no build cache) so it picks up
ffmpeg and base-image security patches. Version tags stay exactly as released: pin
one for reproducible installs and move to the next release for patches.

**Library mount: your call.** Mounting `/library` read-only (`:ro`) means Audiobind
can never change or delete your sources, at the cost of Delete no longer removing
the source folder (it still removes the output and forgets the book). Writable
(`:rw`) lets Delete remove sources too.

### First run

Open `http://<host>:8080`. The first time, Audiobind asks you to **create your account**:
a username (1-64 characters) and a password (at least 8 characters, typed twice). There is
exactly one account; once it exists the setup page is gone for good, and every page and
API call needs you signed in. Until then nothing but the setup page answers, so do this
right after you start the container, from your own network.

Then you get the status page. Books are discovered on the first scan (within `SCAN_INTERVAL_MIN` minutes). Review and held books need attention in the sidebar — you decide what to convert. See the [CHANGELOG](CHANGELOG.md) for what changed in each release.

### Signing in

* **Browser:** username and password on the sign-in page. A session lasts 7 days
  (`HttpOnly`, `SameSite=Strict` cookie). Five failed sign-ins from one IP lock that IP out
  for 10 minutes. A wrong username and a wrong password get the same answer.
* **Scripts:** send the account password as a Bearer token:
  `curl -H "Authorization: Bearer $AUDIOBIND_PASSWORD" localhost:8080/api/state`. Wrong
  passwords count towards the same lockout.
* **Change the password or username** in **Settings → Account**. Both need the current
  password. The browser you change it from stays signed in. Every other browser is
  signed out.
* The password is stored only as a salted PBKDF2-SHA256 hash (600,000 rounds) in the state
  database under `/config`. It is never logged.

**Forgot the password?** There is no reset link, on purpose. Reset it from a shell on the
server instead:

```bash
docker exec -it audiobind audiobind reset-password
```

It asks for a username (Enter keeps the current one) and the new password, typed twice and
hidden. Whatever account existed is replaced, and every browser is signed out. On a fresh
database it creates the account instead. `--username NAME` skips the username question.
Without a terminal (no `-it`) it refuses unless you pass `--username`, so a script can't
wipe your sign-in by accident. In that case the new password is read from the first line of
stdin: `printf '%s\n' "$NEW" | docker exec -i audiobind audiobind reset-password --username me`.

**Upgrading from `AUTH_TOKEN`:** existing installs keep working. While `AUTH_TOKEN` is set
and no account exists, the setup page is skipped. You sign in with any username (for example
`admin`) and the token as the password, and `Bearer <token>` still works for scripts. Create
the account in **Settings → Account** (it asks for the token to confirm) or with
`reset-password`. From then on the account is the only way in: `AUTH_TOKEN` stops working
and you can remove it.

## Controls in the web UI

The status page is also the control panel:

| Button | What it does |
|---|---|
| **Scan now** | skips the interval and starts a pass immediately |
| **Pause after this book** | finishes the book in flight, then stops before starting another |
| **Resume** | continues, and scans immediately |
| **Dry run: ON / OFF** | switches between deciding-only and writing. Persisted, so a restart keeps your choice; `DRY_RUN` is only the default for a fresh install |

Both toggles survive container restarts. Scripted equivalent:

```bash
AUTH="Authorization: Bearer $AUDIOBIND_PASSWORD"
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"pause"}'    localhost:8080/api/control
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"resume"}'   localhost:8080/api/control
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"scan"}'     localhost:8080/api/control
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"dry_run","value":false}' localhost:8080/api/control
```

Every POST must be sent as `Content-Type: application/json` (anything else gets a 415).
If the request carries an `Origin` or `Referer` header, it has to name this server's own
host, or `X-Forwarded-Host` when you're behind a reverse proxy. Anything else gets a 403. That
stops another website you visit from pressing these buttons for you. Scripts that send
neither header, like the `curl` lines above, keep working. They still need the Bearer
password (see [Signing in](#signing-in)). Keep the port on your LAN all the same.

### Series

The **Series** tab (`/series`) lists every series where you have two or more books.
Each one is checked against the Audible series order and shows what you're missing. The
amber number on the tab is the total missing across every series whose size Audible
reported. **I have this** marks a book you own without a rip, so it stops counting as
missing. **Copy Audible link** copies the book's Audible page address. Series lists are
fetched in the background, at most once a day per series. `/api/series` returns the same
data as JSON.

Each card shows "owned X of Y" and notes that the total comes from the Audible catalog, with
when it was checked. Audiobind never estimates a total. If Audible has no listing, or the
catalog couldn't be reached, the card shows **total unknown** and the reason. In the missing
list, **Want** puts one book on your wishlist. **Want all missing** adds every missing book in
the series in one click. Books already on the wishlist are skipped, and nothing is downloaded.
Want all uses the server's current missing list, so a book you got after the page loaded isn't
added.

```bash
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"series":"jack-reacher","missing":"all"}' localhost:8080/api/wishlist
```

### Discover and the wishlist

The **Discover** tab (`/discover`) searches the Audible catalog by title, with an optional
author. Each result shows its cover, authors, narrators, runtime, series and release year.
Books already in your library are marked **In your library**: same ASIN, same title and
author, or marked "I have this" on the Series page. **Get this** puts a book on your
wishlist. That's all it does: nothing is downloaded, and no tracker is contacted. The
[wishlist watcher](#the-wishlist-watcher-myanonamouse) picks books up from there when it is
on. The **Wishlist** tab lists it with each book's state and when it was last checked;
**Retry** and **Remove** act on one book. The number on the Discover tab is the wishlist size. Search uses
one catalog request at a time, with the same pacing as matching, and only the US store
(`api.audible.com`).

```bash
curl -H "$AUTH" 'localhost:8080/api/discover/search?title=project+hail+mary&author=weir'
curl -H "$AUTH" localhost:8080/api/wishlist
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"asin":"B08G9PRS1K"}' localhost:8080/api/wishlist
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"asin":"B08G9PRS1K"}' localhost:8080/api/wishlist/remove
```

Adding an ASIN that's already on the list changes nothing. If Audiobind has never shown an
ASIN (in a search, a suggestion or a series), send `"title"` (and `"authors"`) with it.

The **For you** tab (`/discover/for-you`) suggests books based on what you already own. It has
three sections: more by your top 5 authors, books read by your top 3 narrators, and your top 2
genres. Genres come from the `metadata.json` written beside each published book. Books you own
or already want are left out, and so are summaries and other knockoffs. Suggestions are stored,
so opening the page doesn't query the catalog. A refresh runs when nothing is stored yet, when
the stored suggestions are more than 7 days old, or when you press **Refresh**. One refresh
makes at most 10 catalog requests (one per author, narrator and genre). Nothing goes on the
wishlist until you press **Get this**.

```bash
curl -H "$AUTH" localhost:8080/api/discover/recommendations          # stored result only
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' \
     -d '{"action":"refresh","force":true}' localhost:8080/api/discover/recommendations
```

### The wishlist watcher (MyAnonamouse)

Off unless `MAM_ID` is set. With it set (and `QBIT_USER` / `QBIT_PASS` for the qBittorrent
WebUI), a background thread works through the wishlist every `WATCH_INTERVAL_MIN` minutes:

1. Books already in your library (a converted book with the same ASIN, or the same title and
   author) are marked **In library** - this also runs after every scan, watcher or not.
2. Up to `WATCH_MAX_PER_CYCLE` **wanted** books - never-checked first, oldest first, then the
   least recently checked - are searched on MAM's Torrent Search JSON API (audiobooks,
   English).
3. No good match (title similarity 0.90+, an author match, audio files, not already snatched):
   the book stays **wanted** with a "not found on MAM (checked ...)" note and is searched again
   later; after 3 misses in a row it becomes **not found** until you press **Retry**. A poor
   match is never forced.
4. A match: the torrent is downloaded through MAM's Download Torrent API and handed to
   qBittorrent (category `mam-audiobooks`, whose save path is your ingest folder). It is
   downloaded **without** a freeleech wedge when it is already free, VIP freeleech or a
   personal freeleech, and **with** a wedge (`&fl`) otherwise - the decision is written in the
   book's note. The book becomes **downloading**, and **in library** once ingest converts it.

The watcher only ever calls those two MAM endpoints (the ones MAM authorizes for automation),
never parses HTML and never follows a redirect. It sends one request at a time, at least 3 s
apart, and after any error it backs off (60 s, doubling to 30 minutes) and ends the cycle. A
failed download or a qBittorrent refusal marks the book **error** and is never retried on its
own - a wedge is not refunded - so you decide with **Retry**. qBittorrent is logged into before
anything is downloaded, so a qBittorrent that is down never costs a wedge.

**Dry run** applies: the watcher searches and notes what it *would* download, and downloads
nothing. Settings > **Watcher** shows its status (last and next cycle, last error, backoff),
**Run a cycle now** and **Pause watcher** (kept across restarts). MAM sessions are locked to
an IP address: create the `mam_id` session for the IP the container reaches MAM from.

```bash
curl -H "$AUTH" localhost:8080/api/watcher                            # status, never a secret
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"run"}' localhost:8080/api/watcher
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"asin":"B08G9PRS1K"}' localhost:8080/api/wishlist/retry
```

### Running MAM through a VPN

MyAnonamouse ties your session to an IP address, and many people want every request to MAM
to leave through a VPN and never from the home connection. Audiobind helps in three ways. They
stack, so use one, two or all three:

1. **Share a VPN container's network namespace** (recommended).
2. **Send MAM through an HTTP proxy** (`MAM_PROXY`).
3. **Arm the tripwire** (`MAM_HOME_WAN_IP` and/or `MAM_EXPECTED_EGRESS_IP`): a check that refuses to talk to MAM from your home IP, or from anywhere but the VPN address you name.

Audiobind also sends **no identifying `User-Agent`** to MAM: it never announces its name or
version. The request carries only your session cookie and what the endpoint needs. If you
would rather look like a particular client, set `MAM_USER_AGENT` and that text is sent as-is.

If you have never used a VPN container: it is a container that holds your VPN connection
(WireGuard or OpenVPN) and routes its own traffic through it. Other containers can then be
attached to it so *their* traffic goes through the VPN too. The examples below use Gluetun
(which supports 60+ providers), but the idea is identical for any of them.

#### 1. Share the VPN container's network namespace (recommended)

A container started with `--network container:<vpn-container>` lives *inside* the VPN
container's network stack. It has no network of its own, so the kernel itself sends everything
through the VPN: there is no second route and no fallback. If the VPN drops, Audiobind simply
has no internet, it does not quietly use your home connection. DNS also follows the VPN
container, so lookups do not leak to your ISP. (This is why a plain proxy without DNS handling
is weaker: the proxy hides the traffic, but a name lookup made outside it can still go out the
home connection.)

Because Audiobind no longer has its own network, **publish its web port on the VPN container**,
not on Audiobind: remove Audiobind's `-p` and add `-p 8686:8080` to the VPN container (host
port 8686 here, pick any).

```bash
# 1. the VPN container first (Gluetun with WireGuard; see the Gluetun wiki for your provider)
docker run -d --name gluetun --restart unless-stopped \
  --cap-add NET_ADMIN --device /dev/net/tun \
  -e VPN_SERVICE_PROVIDER=mullvad -e VPN_TYPE=wireguard \
  -e WIREGUARD_PRIVATE_KEY=... -e WIREGUARD_ADDRESSES=10.64.0.2/32 \
  -p 8686:8080 \
  qmcgaw/gluetun

# 2. Audiobind inside its network: NO -p here
docker run -d --name audiobind --restart unless-stopped \
  --network container:gluetun \
  -e MAM_ID=... -e QBIT_USER=admin -e QBIT_PASS=... \
  -e ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.10 \
  -v /path/to/audiobooks:/library:rw -v /path/to/audiobooks-fixed:/output \
  -v /path/to/appdata/audiobind:/config \
  ghcr.io/stinger303/audiobind:latest
```

Add your server's address (the one in `-p`, e.g. `192.168.1.10`) to `ALLOWED_HOSTS`, as usual.

The same pattern works with any VPN container that gives you a tunnel inside a container:
`linuxserver/wireguard`, `wg-easy`, `tailscale` (as a sidecar, with an exit node set),
`openvpn-client`, and Gluetun itself. Swap the container name in `--network container:<name>`
and publish the port on *that* container.

Things to know:

- **qBittorrent in the same namespace.** If qBittorrent also runs in the VPN container's
  network, it is reached at `127.0.0.1`, so set `QBIT_URL=http://127.0.0.1:8083` (use your own
  WebUI port). Its LAN address will time out, because the VPN container's firewall blocks it.
- **Start order matters.** The VPN container must be running before Audiobind starts, and
  Audiobind must be recreated if the VPN container is recreated (a container attached by name
  keeps pointing at the old network). In Compose use `depends_on: [gluetun]`; on Unraid put the
  VPN container above Audiobind in the autostart order (drag to reorder on the Docker tab) and
  give it a short wait.
- **Verify it** (the image has no `wget` or `curl`, so use Python):

  ```bash
  docker exec audiobind python3 -c "import urllib.request;print(urllib.request.urlopen('https://ipinfo.io/ip').read().decode())"
  ```

  It must print the VPN's exit address, **not** your home IP. If it prints your home IP, stop
  and fix the setup before you set `MAM_ID`.

#### 2. An HTTP proxy (`MAM_PROXY`)

If your VPN container offers an HTTP proxy (Gluetun: `HTTPPROXY=on`, port 8888), or you have
any HTTP CONNECT proxy, point Audiobind at it. The web UI stays on your LAN as it is today.

```bash
# on the VPN container: -e HTTPPROXY=on -p 8888:8888   (only if other machines need it)
docker run -d --name audiobind ... -p 8686:8080 \
  -e MAM_PROXY=http://gluetun:8888 \
  ghcr.io/stinger303/audiobind:latest
```

`MAM_PROXY` takes `http://host:port` or `https://host:port`; a value that is not one of those
stops Audiobind from starting, with a message naming `MAM_PROXY`. Every MAM search and download
goes through it. It is **fail-closed**: if the proxy is down or refuses, the request fails and
the watcher backs off. Audiobind never retries without the proxy and never falls back to a
direct connection.

**SOCKS5 is not supported.** Audiobind uses only the Python standard library, which has no SOCKS
client. Most VPN containers offer an HTTP proxy too, but the namespace approach above is
preferred: it covers DNS and has no "proxy forgot to be used" failure mode.

#### 3. Arm the tripwire (`MAM_HOME_WAN_IP`, `MAM_EXPECTED_EGRESS_IP`)

There are two checks, and you can use either or both:

- **"Never my home IP"** (a denylist, `MAM_HOME_WAN_IP`): refuses to run when MAM would see your home address.
- **"Only this IP"** (an allowlist, `MAM_EXPECTED_EGRESS_IP`): refuses to run unless MAM would see exactly the address you name, normally your VPN's exit IP.

The allowlist is the stronger one: the denylist only catches the one address you listed, while the allowlist also catches a VPN that reconnected on a different exit address, a proxy that changed, or any other surprise. A MAM session is tied to one IP, so pinning the address turns a silent session break into a loud refusal. The allowlist can be set as the environment variable or in **Settings > Watcher** ("Only reach MAM from one IP"); the setting takes effect on the next cycle, with no restart, and leaving it empty skips the check.

Both are optional. With neither set, Audiobind makes no lookup at all. If both are set, a home-IP match is reported with the home reason first; any other address that is not the expected one is reported as "MAM would see X, but this installation only allows Y".

To arm the denylist, set `MAM_HOME_WAN_IP` to your home connection's public IP address (what your ISP gave you;
search "what is my IP" from a device on your LAN):

```bash
-e MAM_HOME_WAN_IP=203.0.113.7
```

At the start of every watcher cycle, before anything is sent to MAM, Audiobind asks
`ipinfo.io` (and `api.ipify.org` as a backup) what public address it is connecting from, going
through `MAM_PROXY` if you set one. That is one small HTTPS request per cycle, with no cookie
and nothing about you in it.

- Same as your home IP, or (when `MAM_EXPECTED_EGRESS_IP` is set) not the expected address: the cycle is **refused**, nothing is sent to MAM, and Settings >
  Watcher says so in red.
- Different from the home IP, and equal to the expected one when that is set (the VPN's address): the cycle runs and the log says `MAM egress: <ip>`.
- Cannot be checked (both lookups fail): treated like a refusal. It **fails closed**, because
  "I could not tell" is not "it is safe".

It is the belt-and-braces check for the day a container gets recreated without the VPN, or an
update drops a setting. Settings > Watcher shows what MAM sees, when it was last checked, and
whether the guard is armed. The same values are in `GET /api/watcher` (`egress_ip`,
`egress_checked_at`, `egress_guard`, `egress_reason`, `egress_home_ip`, `egress_expected`).

### CLI

```bash
docker exec audiobind audiobind audit              # report only, writes /config/audit.csv
docker exec audiobind audiobind scan               # one pass, convert what is safe
docker exec audiobind audiobind convert --unit "Michael Lewis- Moneyball"
docker exec audiobind audiobind daemon             # the default: scan forever
docker exec audiobind audiobind health             # exit 0 while the daemon is alive
docker exec -it audiobind audiobind reset-password # forgot the password: set a new one
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LIBRARY_ROOT` | `/library` | Source library (writable so Delete can fully remove a book) |
| `OUTPUT_ROOT` | `/output` | Where finished books go |
| `STATE_DIR` | `/config` | SQLite manifest + `audit.csv` |
| `AUTO_CONVERT` | `high` | `high`, `medium` or `off` |
| `SCAN_INTERVAL_MIN` | `30` | Poll interval |
| `AUDIO_BITRATE` | `128k` | Minimum AAC bitrate. A source that is already richer keeps its own bitrate (256k mp3 stays 256k); everything is capped at 320k |
| `AUDIO_CHANNELS` | `1` | 1 = mono |
| `SAMPLE_RATE` | `44100` | Output sample rate |
| `JOBS` | `1` | Books converted in parallel |
| `NICE` | `10` | ffmpeg CPU priority |
| `MANUAL_WORKERS` | `2` | Manual actions (Retry, Convert anyway, Use this ASIN, Re-check) allowed at once; more get "another action is already running" |
| `MAX_RUNTIME_DELTA` | `0.25` | Hold when runtime differs by more than this fraction |
| `REGION` | `us` | Audible region (`us`, `ca`, `uk`, `au`, `fr`, `de`, `it`, `es`, `jp`, `in`) |
| `DRY_RUN` | `false` | Decide and log, write nothing |
| `WEB_ENABLED` | `true` | Status page |
| `WEB_PORT` | `8080` | Status page port |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names / IPs the web UI answers to (ports are ignored). **Add the LAN IP and hostname you browse to**, e.g. `192.168.1.10,tower,tower.local`; any other `Host` gets a 403. This blocks DNS-rebinding attacks from websites you visit. Keep `localhost,127.0.0.1` in the list for scripts on the server itself |
| `TRUSTED_PROXY` | `false` | `true` only behind your own reverse proxy: `X-Forwarded-Host` is then checked against `ALLOWED_HOSTS` instead of `Host` |
| `AUTH_TOKEN` | *(unset)* | **Deprecated - use the account instead (see [Signing in](#signing-in)).** A shared password for the web UI and API. It only applies while no account exists: you sign in with any username and the token as the password (session cookie, 7 days, `HttpOnly`, `SameSite=Strict`), and scripts send `Authorization: Bearer <token>` to `/api/...`. Five failed sign-ins from one IP lock it out for 10 minutes. While it is set, the first-run setup page is skipped. Once an account exists, the token no longer signs in |
| `NOTIFY_URL` | | Optional webhook for a per-scan summary |
| `MAM_ID` | *(unset)* | **The switch for the [wishlist watcher](#the-wishlist-watcher-myanonamouse).** The MyAnonamouse `mam_id` session cookie. Unset = the watcher is off: no thread, no network. A secret - never shown in the UI, the API or the log |
| `MAM_USER_AGENT` | *(unset)* | `User-Agent` sent to MAM. Unset = none is set (Audiobind never announces its name or version); a value is sent verbatim |
| `MAM_PROXY` | *(unset)* | `http://host:port` or `https://host:port` proxy for every MAM request. Unset = direct. Fail-closed, never falls back to direct; a malformed value stops startup. See [Running MAM through a VPN](#running-mam-through-a-vpn) |
| `MAM_HOME_WAN_IP` | *(unset)* | Your home public IP. When set, each watcher cycle first checks the IP MAM would see (one HTTPS request to `ipinfo.io`) and makes no MAM request if it equals this one, or cannot be checked |
| `MAM_EXPECTED_EGRESS_IP` | *(empty = off)* | Optional. The one public IP MAM may see, normally your VPN's exit address. When set, each watcher cycle first checks the IP MAM would see and makes no MAM request unless it equals this, or if it cannot be checked. A MAM session is tied to one IP, so pinning it turns a silent session break into a loud refusal. Also editable in Settings > Watcher (no restart); a value that is not an IPv4/IPv6 address stops startup |
| `WATCH_INTERVAL_MIN` | `60` | Minutes between watcher cycles (10-1440) |
| `WATCH_MAX_PER_CYCLE` | `5` | Wishlist books one watcher cycle searches at most (1-20) |
| `QBIT_URL` | `http://localhost:8083` | qBittorrent WebUI the watcher hands torrents to |
| `QBIT_USER` / `QBIT_PASS` | *(unset)* | qBittorrent WebUI login. Without them the watcher stays disabled (a warning in the log and on Settings) |
| `PUID` / `PGID` | `99` / `100` | Ownership of written files |

## Notes on correctness

* **`+use_metadata_tags` must never be added to the ffmpeg command.** It makes the MP4 muxer drop
  the attached cover art while still exiting 0. `encode.py` verifies the output has a stream with
  `attached_pic=1` before publishing, precisely because of this.
* **Audnex chapters are camelCase** (`lengthMs`, `startOffsetMs`). Reading `length_ms` yields zero
  for every chapter and silently falls back to per-file chapters.
* **Chapter timings are only trusted when they fit.** Audnex timings are scaled onto the real
  duration only if `local / catalog` is within 0.85-1.20; otherwise per-file chapters are used,
  because the catalog entry is for a different edition.
* **Audiobookshelf rewrites `metadata.json`** when it scans the new book. Losing our extra
  `duration` key is the app adopting the book, not a failed write.

## Development

```bash
python -m unittest discover -s tests -v
docker build -t audiobind:dev .
```

## License

MIT - see [LICENSE](LICENSE).