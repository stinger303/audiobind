# Audiobind: acquisition

[← Back to the README](../README.md)

## Bring your own downloader

Audiobind is a library fixer first. Any tool that writes **completed** audiobook files into the
ingest folder works: qBittorrent, SABnzbd, Readarr, a manual copy, a synced folder. Mount that
folder and point `INGEST_DIR` at it:

```bash
  -v /mnt/user/downloads/audiobooks:/ingest:ro \
  -e INGEST_DIR=/ingest
```

Ingest needs **both halves**: the mount and `INGEST_DIR`. With only one of them, ingest stays off, and
the ingest line in the sidebar (and one log line at start) says which half is missing. On Unraid both are
under Docker tab → Audiobind → Edit → Show more settings: "Ingest path" is the mount, "Ingest dir" is
`INGEST_DIR` (set it to `/ingest`).

The one rule: files must be complete before they land in the folder (move them in when
finished, or use the downloader's "completed" folder), or the app may see a partial book.
Wishlist acquisition (below) is optional; everything else works the same without it.

## The wishlist watcher (private tracker)

Optional, and off by default. Audiobind does not know which site it talks to: you tell it the
site's URL, the path of its JSON search endpoint, the path of its torrent download endpoint and
the JSON document its search expects, and the name and value of its session cookie. Until all six are set (and `QBIT_USER` / `QBIT_PASS`
for the qBittorrent WebUI) the watcher is off: no thread, no network, and Settings > Watcher
names what is still missing.

### The guided setup ("Set up downloading")

Instead of filling the Watcher tab field by field, you can use the guide at `/setup/downloading`.
When you're signed in, choose **View the setup guide** on Settings > Watcher to resume where you left off.
You can also open the guide from the Library page's one-time card or the **Set up downloading** button
that appears on the Watcher tab before the watcher is configured. It saves into the very same settings the
Watcher tab saves, through the same checks, and remembers where you were across a container
restart.

1. **How books arrive**: only your library, your own downloader (the ingest folder), or a
   private tracker through a download client. Only the last one goes on to steps 2-4.
2. **Download client**: which client, its address and sign-in, and the category. **Test** signs
   in and looks at the categories; it adds nothing.
3. **The address the tracker sees**: a tracker session is bound to the address it was created
   from. The step shows the address Audiobind's tracker requests leave from (through
   `TRACKER_PROXY` when set) and when it was last checked. When "Only reach the tracker from one
   IP" (`TRACKER_EXPECTED_EGRESS_IP`) is set it says whether the two match, and what happens when
   they do not: the session is refused, so create a new one from the address shown. **Check
   again** asks the same address lookup the egress guard uses, once; it never contacts the
   tracker. Showing the page sends nothing, and a failed lookup never blocks **Continue**.
4. **Your tracker**: the site's address, the session cookie (a value you create on the site's own
   preferences page and paste in - Audiobind never asks for your tracker password), the cookie's
   name, then the search settings (path, request, method, encoding, category, download path).
   **Test** saves what you changed, then sends exactly **one** search with your configured
   request, through the watcher's own client (so its spacing and backoff apply, and a backoff in
   force means nothing is sent) and behind the egress guard. It answers in plain words, such as
   "12 results, first: ..."; never the cookie, the address or the site's raw answer. The download
   parameter, freeleech parameter, User-Agent and VIP keep their values; the Watcher tab has them.

**Continue** on step 4 leads to the end of the guide: a summary of what is set up (tracker,
download client, ingest folder, the address the tracker sees, and a note if the watcher is paused
or in dry run) and what happens next, or "Almost done" with each missing piece and the step that
fixes it.

| Variable | Default | Meaning |
|---|---|---|
| `TRACKER_URL` | *(unset)* | **Required.** Base URL, e.g. `https://tracker.example`. No path, query or login; a malformed value stops startup |
| `TRACKER_SEARCH_PATH` | *(unset)* | **Required.** Path of the JSON search endpoint |
| `TRACKER_SEARCH_METHOD` | `POST` | `POST` or `GET` |
| `TRACKER_SEARCH_ENCODING` | `json` | `json` or `form`: how a `POST` sends the search body (see below) |
| `TRACKER_SEARCH_BODY` | *(unset)* | **Required, no default.** The search request as a JSON object with `{{title}}`, `{{author}}` and `{{category}}` placeholders (see below) |
| `TRACKER_SEARCH_CATEGORY` | *(unset)* | The value `{{category}}` stands for, if the site needs one |
| `TRACKER_DOWNLOAD_PATH` | *(unset)* | **Required.** Path of the torrent download endpoint |
| `TRACKER_DOWNLOAD_PARAM` | `tid` | Query parameter carrying the torrent id |
| `TRACKER_FREELEECH_PARAM` | *(unset)* | Query parameter that spends a freeleech credit, e.g. `fl`. Empty = never sent |
| `TRACKER_COOKIE_NAME` | *(unset)* | **Required.** The session cookie's name |
| `TRACKER_SESSION` | *(unset)* | **Required.** The session cookie's value. A secret - never shown in the UI, the API or the log |

`TRACKER_SEARCH_BODY` is the document sent to the search endpoint, for example
`{"q": "{{title}} {{author}}"}`. `{{title}}` and `{{author}}` are replaced with the wanted book's
title and first author, `{{category}}` with `TRACKER_SEARCH_CATEGORY`. Values are JSON-escaped, so a
quote in a title cannot break the document. **Empty-value rule:** an object member or list item whose
value is nothing but placeholders and comes out empty (a book without an author, an unset category) is
left out of the document; a string that mixes text and placeholders is trimmed. With `POST` and
`TRACKER_SEARCH_ENCODING=json` (the default) the document is sent as a JSON body. With
`TRACKER_SEARCH_ENCODING=form` it is sent as `application/x-www-form-urlencoded` fields, as is a
`GET` (as query parameters): a string is sent as-is, a list repeats its key once per item, a nested
object becomes `key[sub]`, booleans are `true` / `false`, numbers their decimal form and `null` is
dropped. Keys are not re-encoded beyond normal urlencoding, so `tor[text]` is sent as `tor%5Btext%5D`.
A form example (a fictional site):

```
TRACKER_SEARCH_ENCODING=form
TRACKER_SEARCH_BODY={"tor[text]": "{{title}}", "tor[author]": "{{author}}", "tor[searchIn]": "torrents", "tor[browse_lang][]": ["english"], "perpage": 25}
```

sends `tor%5Btext%5D=Dune&tor%5Bauthor%5D=Frank+Herbert&tor%5BsearchIn%5D=torrents&tor%5Bbrowse_lang%5D%5B%5D=english&perpage=25`
(a book without an author sends no `tor[author]` at all). Anything else the site needs - a page size, a
language filter, a sort order - goes into your template as a literal member.

The six settings that existed before this naming (session, membership, proxy, user agent and the two
IP guards) still work under their old variable names when the new name is empty (one deprecation line in the log
at startup, never the value); the new name wins when both are set. The paths, cookie name, search body and
category have no old name - set them.

**Tracker preset file.** If your tracker publishes an API preset, save it as
`/config/tracker-preset.json` (see `docs/tracker-preset.example.json`): Settings > Watcher then offers
"Fill the tracker fields from preset file", names the preset by the file's own `label`, lists the fields
it will fill and asks before it replaces anything already set. The file is only read to draw that button
and when you press it; it never changes how Audiobind runs, it cannot carry your session cookie, proxy,
IP settings or membership, and a file with any bad value saves nothing. An optional `"categories"` object
maps the tracker's own search-category values to readable names; with it, Settings > Watcher > **Search
category** becomes a dropdown (it saves the bare value, and a saved value that is not in the list stays
as its own option), and without it the field is plain text. `"settings"` may be left empty (or out) when the
file only lists categories: you get the dropdown and no fill button, and nothing is saved from it.

**Checking your setup** (Settings > Watcher). *Check IP* shows the address the tracker would see from
here, in a box you can copy, and what the egress guard would decide; it works with the guard off or armed,
and it never contacts the tracker. *Test tracker* runs one real search for the title in the box (it starts
as a common one) and reports `Found N results; first: <title> — <author>`, or says the site answered but
found nothing, which points at the search body and category. *Test qBittorrent* also lists the categories
qBittorrent has, shows where your category saves, and checks that against the folder Audiobind watches;
it only reads, and it says what it could not tell rather than guessing.

**Account budgets** (all in **Settings > Watcher**, applied from the next cycle; `0` = no limit, and for
the reserve, none). A freeleech credit is never refunded, so the watcher budgets the account:

| Setting | Variable | Default | Range | Meaning |
|---|---|---|---|---|
| Freeleech credits per cycle | `WEDGE_MAX_PER_CYCLE` | `1` | 0-10 | Most credits one cycle may spend |
| Freeleech credits per day | `WEDGE_MAX_PER_DAY` | `3` | 0-50 | Most credits spent in any 24 hours |
| Freeleech credits kept in reserve | `WEDGE_RESERVE` | `1` | 0-50 | Keep this many of today's allowance unspent, in case you want them |
| Unsatisfied snatches allowed | `WATCH_MAX_UNSATISFIED` | `0` | 0-500 | A cycle adds nothing while this many torrents the watcher added are below 100% or below a 1.0 ratio |

The numbers come from one read-only qBittorrent request per cycle plus a ledger of the credits the
watcher spent; the tracker is never asked. The site's own wedge count is not read, so the reserve is
enforced against the ledger: the watcher stops spending once the last 24 hours reach the daily cap minus
the reserve. **If qBittorrent cannot be read, no credit is spent that cycle** (books that are already
free still go through, and a torrent that is already free never touches the budget). A refused book
stays wanted with the reason in its note. A torrent qBittorrent no longer has counts as satisfied. A
dry run says what it would have spent and never writes the ledger.

With the settings in place, a background thread works through the wishlist every
`WATCH_INTERVAL_MIN` minutes (the interval and the books-per-cycle cap are editable in
**Settings > Watcher**; the env vars are the defaults, and a saved value applies from the next
cycle with no restart):

1. Books already in your library (a converted book with the same ASIN, or the same title and
   author) are removed from the wishlist - this also runs after every scan, watcher or not.
2. Up to `WATCH_MAX_PER_CYCLE` **wanted** books - never-checked first, oldest first, then the
   least recently checked - are searched on the tracker's JSON search endpoint. Before any
   search, if `WATCH_MAX_INCOMPLETE` (default 10) torrents in your qBittorrent category are still
   downloading, the cycle holds: nothing is searched or added, and one line says so. `0` turns this
   off. It never delays the **downloaded** flip or the library match.
3. No good match (title similarity 0.90+, an author match, audio files, not already snatched):
   the book stays **wanted** with a "not found on the tracker (checked ...)" note and is searched
   again later; after 3 misses in a row it becomes **not found** until you press **Retry**. A poor
   match is never forced. A result whose title matches but which is another product is
   **rejected** outright, and the note names it and why: a summary / synopsis / study guide, a box
   set, an abridgment or an adaptation (full cast, GraphicAudio) the wishlist title does not name -
   or the reverse - or another volume (`(Book 1)` for a book that is number 2 in its series).
4. A match: the torrent is downloaded through the download endpoint and handed to
   qBittorrent (category `QBIT_CATEGORY`, default `audiobooks`, whose save path is your ingest folder). It is
   downloaded **without** a freeleech credit when it is already free, freeleech for your account
   or a personal freeleech, and **with** one (`TRACKER_FREELEECH_PARAM`) otherwise - the decision
   is written in the book's note. The book becomes **downloading**, then **downloaded** once the
   torrent finishes in qBittorrent, and its row is removed once ingest has converted it and the
   book is in your library.

The watcher only ever calls the two configured endpoints, never parses HTML and never follows a
redirect. It sends one request at a time, at least 3 s apart, and after any error it backs off
(60 s, doubling to 30 minutes) and ends the cycle. A failed download or a qBittorrent refusal
marks the book **error** and is never retried on its own - a credit is not refunded - so you
decide with **Retry**. qBittorrent is logged into before anything is downloaded, so a
qBittorrent that is down never costs a credit.

**Membership-restricted torrents.** Some torrents can only be downloaded by a higher membership
tier, and the API cannot tell the watcher whether your account has it, so it is a setting:
`TRACKER_VIP` (Settings > Watcher > **VIP / Premium member**, default **No**). With No, such a torrent
is skipped and the book's note says so; with Yes it is an ordinary candidate.

The watcher has its own **Preview tracker downloads** switch (Settings > Watcher, or `WATCH_DRY_RUN`), separate from the
library preview on the main page: it searches and notes what it *would* download, and downloads
nothing. Settings > **Watcher** shows its status (last and next cycle, last error, backoff),
**Run a cycle now** and **Pause watcher** (kept across restarts); these, and **Preview tracker downloads**, appear once the tracker settings are complete. Tracker sessions are often locked to
an IP address: create the session for the IP the container reaches the tracker from.

```bash
curl -H "$AUTH" localhost:8080/api/watcher                            # status, never a secret
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"run"}' localhost:8080/api/watcher
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"asin":"B08G9PRS1K"}' localhost:8080/api/wishlist/retry
curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"asin":"B08G9PRS1K","title":"The Missing"}' localhost:8080/api/wishlist/title   # blank title clears it
```

## Running the watcher through a VPN

**What this covers:** Audiobind's own visits to the tracker (searches and `.torrent` files). The
download itself is qBittorrent's; route qBittorrent through your VPN separately.

A tracker often ties your session to an IP address, and many people want every request to it
to leave through a VPN and never from the home connection. Audiobind helps in three ways. They
stack, so use one, two or all three:

1. **Share a VPN container's network namespace** (recommended).
2. **Send tracker traffic through an HTTP proxy** (`TRACKER_PROXY`).
3. **Arm the tripwire** (`TRACKER_HOME_IP` and/or `TRACKER_EXPECTED_EGRESS_IP`): a check that refuses to talk to the tracker from your home IP, or from anywhere but the VPN address you name.

Audiobind sends a `User-Agent` of `audiobind/<version>` to the tracker by default. Set
`TRACKER_USER_AGENT` to send your own text as-is, or to the literal `none` to send no User-Agent at all.
Some trackers refuse a request with no User-Agent (HTTP 400), so only use `none` if yours accepts it.
The request otherwise carries only your session cookie and what the endpoint needs.

If you have never used a VPN container: it is a container that holds your VPN connection
(WireGuard or OpenVPN) and routes its own traffic through it. Other containers can then be
attached to it so *their* traffic goes through the VPN too. The examples below use Gluetun
(which supports 60+ providers), but the idea is identical for any of them.

### 1. Share the VPN container's network namespace (recommended)

A container started with `--network container:<vpn-container>` lives *inside* the VPN
container's network stack. It has no network of its own, so the kernel itself sends everything
through the VPN: there is no second route and no fallback. If the VPN drops, Audiobind simply
has no internet, it does not quietly use your home connection. DNS also follows the VPN
container, so lookups do not leak to your ISP. (This is why a plain proxy without DNS handling
is weaker: the proxy hides the traffic, but a name lookup made outside it can still go out the
home connection.)

Because Audiobind no longer has its own network, **publish its web port on the VPN container**,
not on Audiobind: remove Audiobind's `-p` and add `-p 8686:8080` to the VPN container (host
port 8686 here, pick any). If qBittorrent in that VPN container already uses 8080, see
**Port clash** under "Things to know" below.

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
  -e TRACKER_URL=... -e TRACKER_SESSION=... -e QBIT_USER=admin -e QBIT_PASS=... \
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
  network, it is reached at `127.0.0.1`, so set `QBIT_URL=http://127.0.0.1:<its port>` (use
  qBittorrent's own WebUI port). Its LAN address will time out, because the VPN container's
  firewall blocks it. In every other setup, `QBIT_URL` is the address you open qBittorrent's
  WebUI with, for example `http://192.168.1.10:8080`.
- **Port clash.** If another app in the VPN container (often qBittorrent) already uses 8080, add
  `WEB_PORT=8686` to Audiobind and publish `-p 8686:8686` on the VPN container. Audiobind's WebUI
  link on the Docker tab then stops working; open `http://<server IP>:8686`.
- **Start order matters.** The VPN container must be running before Audiobind starts, and
  Audiobind must be recreated if the VPN container is recreated (a container attached by name
  keeps pointing at the old network). In Compose use `depends_on: [gluetun]`; on Unraid put the
  VPN container above Audiobind in the autostart order (drag to reorder on the Docker tab) and
  give it a short wait.
- **Verify it.** Easiest: Settings > Watcher > **Check IP**. From a terminal (the image has no `wget`
  or `curl`, so use Python):

  ```bash
  docker exec audiobind python3 -c "import urllib.request;print(urllib.request.urlopen('https://ipinfo.io/ip').read().decode())"
  ```

  It must print the VPN's exit address, **not** your home IP. If it prints your home IP, stop
  and fix the setup before you set `TRACKER_SESSION`.

### 2. An HTTP proxy (`TRACKER_PROXY`)

If your VPN container offers an HTTP proxy (Gluetun: `HTTPPROXY=on`, port 8888), or you have
any HTTP CONNECT proxy, point Audiobind at it. The web UI stays on your LAN as it is today.

```bash
# on the VPN container: -e HTTPPROXY=on -p 8888:8888   (publish 8888 so Audiobind can reach it)
docker run -d --name audiobind ... -p 8686:8080 \
  -e TRACKER_PROXY=http://<server IP>:8888 \
  ghcr.io/stinger303/audiobind:latest
```

Use the server's IP address (for example `http://192.168.1.10:8888`). A container name like
`gluetun` only works when both containers are on the same custom Docker network; on Unraid's
default bridge network, use the server's IP.

`TRACKER_PROXY` takes `http://host:port` or `https://host:port`; a value that is not one of those
stops Audiobind from starting, with a message naming `TRACKER_PROXY`. On Unraid: Docker tab →
Audiobind → Edit → Show more settings → "Tracker proxy" → Apply (restarts the container). Every tracker search and download
goes through it, as do the address probe and allowed-IP updater. Environment bypass rules
(`NO_PROXY` and `no_proxy`) do not apply. It is **fail-closed**: if the proxy is down or refuses, the request fails and
the watcher backs off. Audiobind never retries without the proxy and never falls back to a
direct connection.

**SOCKS5 is not supported.** Audiobind uses only the Python standard library, which has no SOCKS
client. Most VPN containers offer an HTTP proxy too, but the namespace approach above is
preferred: it covers DNS and has no "proxy forgot to be used" failure mode.

### 3. Arm the tripwire (`TRACKER_HOME_IP`, `TRACKER_EXPECTED_EGRESS_IP`)

There are two checks, and you can use either or both:

- **"Never my home IP"** (a denylist, `TRACKER_HOME_IP`): refuses to run when the tracker would see your home address.
- **"Only this IP"** (an allowlist, `TRACKER_EXPECTED_EGRESS_IP`): refuses to run unless the tracker would see exactly the address you name, normally your VPN's exit IP.

The allowlist is the stronger one: the denylist only catches the one address you listed, while the allowlist also catches a VPN that reconnected on a different exit address, a proxy that changed, or any other surprise. A tracker session is often tied to one IP, so pinning the address turns a silent session break into a loud refusal. The allowlist can be set as the environment variable or in **Settings > Watcher** ("Only reach the tracker from one IP"); the setting takes effect on the next cycle, with no restart, and leaving it empty skips the check.

Each cycle and tracker test uses the same saved route for the address check and tracker requests.
If the routing settings change during the check, start a new cycle or test to verify the new route.

Both are optional. With neither set, Audiobind makes no lookup at all. If both are set, a home-IP match is reported with the home reason first; any other address that is not the expected one is reported as "the tracker would see X, but this installation only allows Y".

To arm the denylist, set `TRACKER_HOME_IP` to your home connection's public IP address (what your ISP gave you;
search "what is my IP" from a device on your LAN). On Unraid: Docker tab → Audiobind → Edit →
Show more settings → "Home IP (guard)" → Apply (restarts the container):

```bash
-e TRACKER_HOME_IP=203.0.113.7
```

At the start of every watcher cycle, before anything is sent to the tracker, Audiobind asks
`ipinfo.io` (and `api.ipify.org` as a backup) what public address it is connecting from, going
through `TRACKER_PROXY` if you set one. That is one small HTTPS request per cycle, with no cookie
and nothing about you in it.

- Same as your home IP, or (when `TRACKER_EXPECTED_EGRESS_IP` is set) not the expected address: the cycle is **refused**, nothing is sent to the tracker, and Settings >
  Watcher says so in red.
- Different from the home IP, and equal to the expected one when that is set (the VPN's address): the cycle runs and the log says `tracker egress: <ip>`.
- Cannot be checked (both lookups fail): treated like a refusal. It **fails closed**, because
  "I could not tell" is not "it is safe".

It is the belt-and-braces check for the day a container gets recreated without the VPN, or an
update drops a setting. Settings > Watcher shows what the tracker sees, when it was last checked, and
whether the guard is armed. The same values are in `GET /api/watcher` (`egress_ip`,
`egress_checked_at`, `egress_guard`, `egress_reason`, `egress_home_ip`, `egress_expected`).

### When the egress IP drifts (`IP_UPDATER`)

A VPN that reconnects on a new exit address trips the allowlist, and the wishlist stops until the
tracker account and `TRACKER_EXPECTED_EGRESS_IP` both name the new address. `IP_UPDATER` (default
`off`) helps you notice and catch up. Choose it on Settings > Watcher > **Allowed-IP updater**
(a value saved there wins over the container's `IP_UPDATER`). It acts **after** the guard and never changes its verdict:
a drifted cycle is still refused.

- `notify`: when the guard sees a new address, the log says so once (WARNING, `allowed-IP updater:`),
  and Settings > Watcher shows **Allowed-IP updater**, **Allowed IP** (the pin, whether it is in step,
  and why) and **Last updated** (when, old -> new).
- **Use the current IP** (only while `IP_UPDATER` is not `off`). First change the allowed IP on the
  tracker's website by hand, then press this. Audiobind checks the egress IP again and sets "Only
  reach the tracker from one IP" to that address. It refuses when the check fails, when the address
  is `TRACKER_HOME_IP`, when nothing has drifted or no IP is pinned, and when the last change was under
  an hour ago. The change is logged at INFO (`old -> new` and the reason). Scripted:
  `curl -X POST -H "$AUTH" -H 'Content-Type: application/json' -d '{"action":"use_current_ip"}' localhost:8080/api/watcher`.
  With a MyAnonamouse session, the button sets the dynamic seedbox IP on MAM, checks that MAM now
  sees it, and only then re-pins ([details](ip-updater.md)).
- `auto`: on MyAnonamouse, with "Allow session to set dynamic seedbox IP" enabled for the session,
  Audiobind sets the new IP on MAM (`dynamicSeedbox.php`). It then confirms it with `jsonIp.php`,
  re-pins, and logs `old -> new` at INFO. This happens at most once an hour, never from the home IP,
  and only with `TRACKER_HOME_IP` set. On any other tracker, or without a session, `auto` behaves like
  `notify` and says so ([details](ip-updater.md)).

With `notify` or `auto`, `GET /api/watcher` also carries `ip_updater` (`mode`, `can_write`,
`decision`, `reason`, `last_updated_at`, `last_old`, `last_new`, `last_result`). With `off`, the key is absent.

The watcher records a handoff before requesting the torrent and saves its info hash and
destination before adding it to the download client. After an interruption, the watcher
checks the saved hash in that client on startup or before its next cycle. A confirmed torrent resumes as
downloading. An uncertain handoff shows an error for owner review before using Retry,
since another tracker request could consume a second credit.
