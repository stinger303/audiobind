# Audiobind: installation and first run

[← Back to the README](../README.md)

## Installation

### Unraid (Community Applications)

Audiobind is pending publication in Community Applications. Until it is listed:

1. Paste this one line into Unraid's terminal to download the template:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/audiobind.xml https://raw.githubusercontent.com/stinger303/audiobind/main/templates/audiobind.xml
   ```
2. Click **Docker** > **Add Container** and select **audiobind** from the template dropdown.
3. Fill in the paths for your library, output directory and appdata (the Library path's description explains Read/Write vs Read Only), then click **Apply**.

The template sets `PUID=99 PGID=100`, runs the container hardened (`--read-only --tmpfs /tmp
--cap-drop ALL --cap-add CHOWN --cap-add SETUID --cap-add SETGID
--security-opt no-new-privileges:true`) and publishes the web UI on host port 8686 (the
container listens on 8080; change the host port if another container already uses it). Set
**Allowed hosts** only if you want to restrict the UI to exactly the names you list; unset, it
answers on this machine and on private LAN IPs.

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
  ghcr.io/stinger303/audiobind:latest
```

First time? The [First run](#first-run) checklist below covers the mounts, ALLOWED_HOSTS and the account.

### Docker Compose

```yaml
services:
  audiobind:
    image: ghcr.io/stinger303/audiobind:latest
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

### Behind a reverse proxy

Only if you put Audiobind behind your own reverse proxy (for TLS, say), set both:

```bash
-e TRUSTED_PROXY=true \
-e TRUSTED_PROXY_IPS=172.18.0.0/16      # the proxy's address(es) as Audiobind sees them
```

`X-Forwarded-Host`, `-For` and `-Proto` are then believed, but **only from the peers listed in
`TRUSTED_PROXY_IPS`** (IPs or CIDRs); a forwarded header from any other peer is ignored. Since
0.19.0-dev5, `TRUSTED_PROXY=true` on its own trusts nobody. **If you already had
`TRUSTED_PROXY=true`, add `TRUSTED_PROXY_IPS` when you upgrade**, or your proxy's headers stop being
honoured (the plain `Host` header is checked against `ALLOWED_HOSTS` instead, and the session cookie
does not get `Secure` over https). Without a proxy, leave both unset. Audiobind is meant for your LAN: do
not expose it to the internet without a reverse proxy and TLS (see [SECURITY.md](../SECURITY.md)).

### First run

A new install needs three mounts and an account before it does anything
useful. Acquisition also needs the tracker settings, but that part is optional. In order:

1. **Mount three folders.** The container runs read-only with `--tmpfs /tmp` and writes
   only to these:

   | Mount      | What it is for                            | Without it                         |
   |------------|-------------------------------------------|------------------------------------|
   | `/library` | Your audiobooks. Mount it read-write.     | Nothing to scan. `:ro` rules out even Delete. |
   | `/output`  | Converted `.m4b`/`.mp3` files land here.  | Nowhere to write a conversion.     |
   | `/config`  | SQLite state DB, the account, `audit.csv`. | Nowhere to keep state or the account. |

2. **`ALLOWED_HOSTS` (optional).** Unset, the UI answers on this machine and on private LAN
   IPs (like `192.168.1.10`), but not on hostnames. Once you set it, only exactly the names
   you list are answered and any other `Host` gets a 403 (that is the host list, not a
   crash), which blocks DNS-rebinding attacks. Ports are ignored. To restrict it, list the
   IP and hostname you browse from, and keep `localhost,127.0.0.1` for scripts on the server:
   `-e ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.10,tower,tower.local`

3. **Start the container and create the account.** Open the web UI (on Unraid, the container's
   WebUI link, which uses host port 8686 unless you changed it; with `docker run`,
   `http://<host>:8080`). The first
   visit opens the setup page: the setup code, a username (1-64 characters) and a password (at
   least 8 characters, typed twice). The setup code is printed in the container log when
   Audiobind first starts: `docker logs audiobind | grep 'setup code'`. There is exactly one
   account; once it exists the setup page and the code are gone for good, and every page and
   API call needs you signed in. Forgot the password later? See
   [Signing in](#signing-in) (`docker exec -it audiobind audiobind reset-password`).

4. **Wait for the first scan, then review.** The first scan starts as soon as the container
   starts; the Library page shows "Scanning n / m…". After that it scans every
   `SCAN_INTERVAL_MIN` minutes. Review and held books need attention in the sidebar. You decide
   what to convert.

Smoke test: `curl -fsS http://<host>:8080/healthz` should print `ok`. It is the one public
endpoint, so it needs no sign-in.

**Optional: acquisition.** The wishlist watcher needs the `TRACKER_*` settings complete plus
qBittorrent, and stays off unless they are. Without them Audiobind is library-only (scan,
match, convert, file) and everything else works. See
[The wishlist watcher (private tracker)](acquisition.md#the-wishlist-watcher-private-tracker) and
[Bring your own downloader](acquisition.md#bring-your-own-downloader).

See the [CHANGELOG](../CHANGELOG.md) for what changed in each release.

### Signing in

On the first start nobody has an account yet, so the first visit opens a setup page. Creating
the account needs a one-time setup code, which Audiobind prints in the container log when it
first starts (`docker logs audiobind | grep 'setup code'`), so someone who can merely reach the
port cannot claim your install. The code stays the same across restarts until the account is
created, then it is used up. For an unattended install, set `AUDIOBIND_SETUP_CODE` to choose
the code yourself; it is then not generated or printed.

`/healthz` is the one public endpoint, with or without an account: it needs no sign-in, so an
uptime monitor can poll it, and it only ever answers `ok` (200) or `stale` (503; the scan loop has
not checked in for longer than about 2.5 scan intervals plus five minutes). Every other page and
API call needs the account.

* **Browser:** username and password on the sign-in page. A session lasts 7 days
  (`HttpOnly`, `SameSite=Strict` cookie). Tick **Keep me signed in for 30 days** to make that
  browser's session last 30 days instead (it is unticked by default; leave it off on a shared
  computer). Sessions are saved in the state database, so a restart or redeploy does not
  sign you out. Only a hash of each session id is stored. Changing the password or username
  still ends every other session. Five failed sign-ins from one IP lock that IP out
  for 10 minutes. A wrong username and a wrong password get the same answer.
* **Remembered username:** after a successful sign-in the browser keeps a small `ab_user`
  cookie (30 days, `HttpOnly`, `SameSite=Strict`) that only pre-fills the username box. It is
  a convenience, never a credential: a request carrying just that cookie is still signed out.
  Signing out keeps it.
* **Scripts:** send the account password as a Bearer token:
  `curl -H "Authorization: Bearer $AUDIOBIND_PASSWORD" localhost:8080/api/state`. Wrong
  passwords count towards the same lockout.
* **Spaces:** a password cannot start or end with a space (or a tab). An HTTP header cannot
  carry them, so the sign-in page and Bearer both ignore spaces at the start and end of what
  is typed or sent, and setup, Settings and `reset-password` refuse a new password that has
  them. Spaces inside the password are fine. If an older account's password starts or ends with
  a space, neither way will accept it: set a new one with `reset-password` (below).
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

## Upgrading

**To 0.19.0-dev14 or later (Audiobookshelf folder layout).** New conversions are filed as
`Author/Series/<seq> - Title/` (all authors in the folder name). Books you already converted
**stay in their old folders** after the upgrade; nothing moves them on its own.

1. Open **Settings > Library > Match Audiobookshelf layout**, click **Preview** and read the list.
2. Click **Apply** to move them. Do this **before** any **Re-match all…** or **Retry** on converted
   books, and before any **Convert anyway** (which encodes the book again into the new layout and
   leaves the old folder behind).
3. Audiobookshelf may treat a renamed folder as a new book and **lose its listening progress**.
   Check your in-progress books after it rescans.

Every move is journaled in `STATE_DIR/abs-layout-<UTC time>.json`; see
[configuration](configuration.md#match-audiobookshelf-layout-settings--library) for the details.
