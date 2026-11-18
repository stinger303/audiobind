# The allowed-IP updater

[← Back to configuration](configuration.md)

Parity feature 3: when the egress IP drifts away from `TRACKER_EXPECTED_EGRESS_IP`, Audiobind can
update the tracker account's allowed (dynamic seedbox) IP itself. This works on **MyAnonamouse
only**, and only when `IP_UPDATER=auto` is set or you press **Use the current IP**.

## Prerequisite on MyAnonamouse

The MAM session in `TRACKER_SESSION` (the `mam_id` cookie value) must have
**Preferences → Security → "Allow session to set dynamic seedbox IP"** turned on. Without it, MAM
refuses the update, and Audiobind reports `refused` with that hint.

## The two endpoints

Both are plain GETs that carry the session as the `mam_id` cookie. Neither has a request body.

| call | what Audiobind uses it for |
|---|---|
| `https://t.myanonamouse.net/json/jsonIp.php` | Reads the IP **MAM itself sees** (`{"ip": "…"}`). Read-only. |
| `https://t.myanonamouse.net/json/dynamicSeedbox.php` | Sets the session's dynamic seedbox IP to the address the request comes from. |

Sources: Mousehole's documented integration and MouseSearch's implementation, which both use this
pair. They are only called when `TRACKER_URL` is on `myanonamouse.net` (the domain or a subdomain),
so the session cookie never goes to another site. Requests go through `TRACKER_PROXY` with an
explicit proxy map, redirects are never followed, and the transport refuses every other URL. This
is separate from the tracker client: `tracker.py` still automates only search and download.

## What `observed` is

`ipupdate.decide()` compares an **observed** IP with the pinned one. When a MAM session is
configured, observed is MAM's own `jsonIp.php` answer. Without one, it is the egress probe's
answer (ipinfo.io / ipify). The probe still runs first, as the egress guard. MAM is asked only once
the probe-based decision already says "update": the probe answered, the address is not the home IP,
an IP is pinned, and the hourly limit allows it.

## What each mode does

* `off` (default): nothing new. No extra request, no log line, no page rows.
* `notify`: logs a drift once per new address and shows it on Settings > Watcher. It is
  **read-only**. With a MAM session it reads `jsonIp.php` while a drift lasts, but it **never** calls
  `dynamicSeedbox.php`.
* `auto` with a MAM session: when the guard has blocked a cycle because of a drift, Audiobind
  1. runs `decide()` on MAM's answer;
  2. checks the guard again: `TRACKER_HOME_IP` must be set, and neither the probe's answer nor MAM's
     may be that address;
  3. calls `dynamicSeedbox.php`;
  4. reads `jsonIp.php` again, and counts it as a success **only** if MAM now sees the observed IP;
  5. re-pins `TRACKER_EXPECTED_EGRESS_IP` to that IP, logs `allowed IP old -> new` at INFO, and
     records the time for the hourly limit.

  The cycle that saw the drift is still refused. The next one runs against the new pin.
* `auto` without a MAM session (or on another tracker) behaves like `notify` and says so.

### What `auto` will not do

* Write when the egress probe failed, when the probe or MAM sees `TRACKER_HOME_IP`, or when
  `TRACKER_HOME_IP` is not set at all.
* Write more than once an hour. A failed attempt is not retried for an hour either.
* Trust `dynamicSeedbox.php`'s own "OK". If `jsonIp.php` disagrees afterwards, or does not answer,
  the attempt is a failure and the pin stays where it was.
* Change the egress guard's verdict, or contact the search or download endpoints.

### Failures

Failures are typed, logged at WARNING and shown in the **Last MAM result** row:

* `unavailable`: no answer, or a server error.
* `auth`: the session was rejected (expired or invalid, or a redirect to the login page).
* `refused`: the session may not set the IP (the Security toggle is off, or the last change was too
  recent).
* `error`: anything else, including the verify step disagreeing.

Messages are built from fixed text plus MAM's short `msg`. The session value is never included.

## Use the current IP

The button appears while `IP_UPDATER` is not `off`. With a MAM session, it runs the same write,
verify and re-pin steps as `auto`, even in `notify` mode, because pressing it is your explicit
go-ahead. Without a session, change the allowed IP on the tracker's website first. The button then
only re-pins Audiobind's own allowlist and sends nothing to the tracker.

## Not verified live

MAM's exact answer texts (`Invalid session`, `Incorrect session type`, `Last change too recent`) map
to `auth` / `refused`. That mapping comes from how the reference implementations read them, and it
is tested against a mocked transport only. A refusal MAM words differently falls through to `error`,
which still never counts as success.
