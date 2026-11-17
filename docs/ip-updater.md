# The allowed-IP updater: research notes

[← Back to configuration](configuration.md)

Parity feature 3 asks: when the egress IP drifts away from `TRACKER_EXPECTED_EGRESS_IP`, can Audiobind
update the tracker account's allowed (seedbox) IP itself?

## What the tracker API offers - the evidence

| Source | What it says |
|---|---|
| `audiobind/tracker.py` | The client talks to **exactly two** owner-configured endpoints, search and download. `authorized_url()` refuses every other URL, and `urllib_transport()` refuses it again. There is no account, profile or IP endpoint. |
| `audiobind/config.py` | The `TRACKER_*` settings name the site URL, the search path and the download path. No setting names an IP or account endpoint. |
| `docs/acquisition.md` | Says tracker sessions "are often locked to an IP address: create the session for the IP the container reaches the tracker from". That is a manual step on the tracker's website, and the doc describes no API for it. |
| The tracker's own API docs | **Not checked.** The docs are behind the site's login, and this sandbox was not allowed to fetch web pages. |
| The `audiobind-ops` skill ("Tracker automation rules") | **Not readable** in this sandbox. |

**Answer: no endpoint we can verify.** Nothing in the repository or its docs shows an API call that sets
the allowed IP, and I could not read the tracker's own docs to confirm that one exists. The brief says
not to invent an endpoint, and `tracker.py` promises that only two endpoints are ever automated. So
this build has **no write call**: Audiobind never changes anything on the tracker.

Some trackers do offer a "dynamic seedbox IP" call that uses the session cookie. If yours documents
one, adding it is a separate change. It needs the endpoint's exact URL, method, answer format and
rate limit from the tracker's own docs. It would also need a third entry in `authorized_url()` and a
change to the "two endpoints" promise, so it needs the owner's sign-off.

## What was built instead (the manual half)

* `audiobind/ipupdate.py` `decide()`: a pure function that says whether an update would be
  appropriate (`noop` / `update` / `refuse`, with the reason).
* `IP_UPDATER=off|notify|auto` (default `off`). `notify` logs the drift and shows it on Settings >
  Watcher with the IP to enter on the tracker. With no write endpoint, `auto` behaves exactly like
  `notify` and says so in the log and on the page.
* **Use the current IP** (visible only when `IP_UPDATER` is not `off`). Press it after you have
  changed the allowed IP on the tracker's website. It checks the egress IP again, asks `decide()`,
  then sets Audiobind's own allowlist pin (the "Only reach the tracker from one IP" setting) to the
  observed address and records when. It sends nothing to the tracker.
