"""The allowed-IP updater: should the tracker's allowed IP follow the egress IP, and the MAM calls.

``decide()`` is pure: no I/O, no clock, no settings. The caller passes everything in, which keeps
every branch unit-testable. The egress guard (``egress.judge``) has already decided whether a cycle
may run. This module never changes that verdict. It only answers the next question: given the IP
the tracker sees (``observed``), would changing the allowed IP to it be a sound thing to do?

``observed`` is MyAnonamouse's own answer from ``jsonIp.php`` when a MAM session is configured (the
IP MAM itself sees, read by ``read_ip()``), and ``egress.probe()``'s otherwise. The caller chooses.

The network half talks to exactly two fixed URLs (``JSON_IP_URL``, ``DYNAMIC_SEEDBOX_URL``) with
the session cookie, through a ``transport`` the caller hands in, so tests never touch the network.
It is only used when the configured tracker is MAM (``is_mam``): the session cookie is never sent
to any other host. See docs/ip-updater.md.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request

NOOP = "noop"
UPDATE = "update"
REFUSE = "refuse"

#: never two updates closer together than this (seconds)
MIN_INTERVAL = 3600.0

#: IP_UPDATER values. ``off`` is the default and changes nothing.
MODES = ("off", "notify", "auto")


def _ip(text: str) -> str:
    """The canonical spelling of an address, or "" when ``text`` is not one."""
    try:
        return str(ipaddress.ip_address(str(text or "").strip()))
    except ValueError:
        return ""


def decide(observed: str, expected: str, home_ip: str, last_updated_at: float | None, now: float,
           probe_error: str = "") -> tuple[str, str]:
    """(``noop`` | ``update`` | ``refuse``, plain-English reason).

    ``observed`` is the IP the tracker sees: MAM's ``jsonIp.php`` answer when a MAM session is
    configured, ``egress.probe()``'s otherwise (``probe_error`` is whichever one failed).
    ``expected`` is the allowed IP pinned now (TRACKER_EXPECTED_EGRESS_IP).

    Checked in this order. The first rule that applies wins:

    1. the probe failed (``probe_error`` set): refuse, because the address is unknown;
    2. ``observed`` is empty: refuse, for the same reason;
    3. ``observed`` is not an IP address: refuse;
    4. ``observed`` is the home IP: refuse. This is the egress guard's own rule: a home address
       is never written anywhere as an allowed IP;
    5. ``observed`` already equals ``expected``: noop, nothing has drifted;
    6. no ``expected`` is pinned: noop, there is no allowed IP to keep in step;
    7. the last update is under MIN_INTERVAL old (or dated in the future): refuse;
    8. otherwise: update ``expected`` to ``observed``.
    """
    if probe_error:
        return REFUSE, f"the egress IP could not be checked ({probe_error}) - nothing is changed on a guess"
    seen_raw = str(observed or "").strip()
    if not seen_raw:
        return REFUSE, "the egress IP is unknown - nothing is changed on a guess"
    seen = _ip(seen_raw)
    if not seen:
        return REFUSE, f"{seen_raw[:64]!r} is not an IP address - nothing is changed"
    home = _ip(home_ip)
    if home and seen == home:
        return REFUSE, (f"the egress IP {seen} is your home connection (TRACKER_HOME_IP) - "
                        "a home IP is never made the allowed IP")
    pinned = _ip(expected)
    if pinned and seen == pinned:
        return NOOP, f"the egress IP {seen} is already the allowed IP"
    if not pinned:
        return NOOP, "no allowed IP is pinned (TRACKER_EXPECTED_EGRESS_IP is empty) - nothing to keep in step"
    if last_updated_at:
        age = float(now) - float(last_updated_at)
        if age < 0:
            return REFUSE, "the last update is dated in the future (clock change?) - waiting rather than updating twice"
        if age < MIN_INTERVAL:
            wait = int((MIN_INTERVAL - age) // 60) + 1
            return REFUSE, f"the allowed IP was changed {int(age // 60)} min ago - at most one change an hour (wait {wait} min)"
    return UPDATE, f"the egress IP moved from {pinned} to {seen} - the allowed IP should follow it"


def mode(raw: str) -> str:
    """An IP_UPDATER value, normalised. Anything that is not a known mode is ``off``. The config
    loader rejects unknown values at startup, so this is only a safety net."""
    value = str(raw or "").strip().lower()
    return value if value in MODES else "off"


# ---------------------------------------------------------------------------- MyAnonamouse
#: the IP MAM sees on this request: ``{"ip": "203.0.113.42", ...}``. Read-only.
JSON_IP_URL = "https://t.myanonamouse.net/json/jsonIp.php"
#: sets the session's dynamic seedbox IP to the address the request comes from. GET, no body.
DYNAMIC_SEEDBOX_URL = "https://t.myanonamouse.net/json/dynamicSeedbox.php"
#: the only URLs the transport below will request
MAM_URLS = (JSON_IP_URL, DYNAMIC_SEEDBOX_URL)
MAM_DOMAIN = "myanonamouse.net"
MAM_COOKIE = "mam_id"
TIMEOUT = 20.0
MAX_ANSWER = 4096

#: UpdateError kinds
UNAVAILABLE = "unavailable"  # no answer, or a server error: MAM could not be reached
AUTH = "auth"                # the session was rejected (expired, wrong, logged out)
REFUSED = "refused"          # the session is valid but MAM will not set the IP (toggle off, too soon)
ERROR = "error"              # anything else, including a write the verify step could not confirm
TOGGLE = 'Preferences > Security > "Allow session to set dynamic seedbox IP"'


class UpdateError(Exception):
    """A MAM call failed. ``kind`` is UNAVAILABLE / AUTH / REFUSED / ERROR. The message is built
    from fixed text and MAM's own short ``msg``; it never contains the session value."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_mam(tracker_url: str) -> bool:
    """True when the configured tracker URL is MyAnonamouse (the domain or a subdomain of it).
    The MAM calls are only made then: the session cookie is never sent to another site."""
    try:
        host = (urllib.parse.urlsplit(str(tracker_url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == MAM_DOMAIN or host.endswith("." + MAM_DOMAIN)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect (usually to the login page) is answered as its status, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(proxy: str = "", user_agent: str = ""):
    """The real transport: ``transport(method, url, headers, timeout) -> (status, body)``.

    Only the two MAM_URLS, no redirects, and an explicit proxy map (TRACKER_PROXY, or none at all,
    never the environment's). One attempt per call: no retry, and never a direct fallback."""
    mapping = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(mapping), _NoRedirect())
    opener.addheaders = []

    def transport(method: str, url: str, headers: dict, timeout: float) -> tuple[int, bytes]:
        if url not in MAM_URLS:
            raise UpdateError(ERROR, "refusing a URL outside the two MAM IP endpoints")
        sent = dict(headers)
        if user_agent:
            sent["User-Agent"] = user_agent
        request = urllib.request.Request(url, headers=sent, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(MAX_ANSWER)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(MAX_ANSWER)
            except OSError:
                body = b""
            finally:
                exc.close()
            return exc.code, body

    return transport


def _clean(text, session: str) -> str:
    """MAM's ``msg``, made safe to log and show: one line, short, and never the session value."""
    out = " ".join(str(text or "").split())
    if session:
        out = out.replace(session, "[session]")
    return out[:120]


def _call(session: str, url: str, transport) -> dict:
    """One GET with the session cookie and no body. The decoded JSON object, or UpdateError."""
    if not session:
        raise UpdateError(AUTH, "no MAM session is set (TRACKER_SESSION)")
    name = url.rsplit("/", 1)[-1]
    headers = {"Cookie": f"{MAM_COOKIE}={session}", "Accept": "application/json"}
    try:
        status, body = transport("GET", url, headers, TIMEOUT)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - timeout, DNS, TLS, proxy: all "no answer"
        raise UpdateError(UNAVAILABLE, f"MAM could not be reached for {name} ({type(exc).__name__})") from None
    try:
        payload = json.loads((body or b"").decode("utf-8", "replace"))
    except ValueError:
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    msg = _clean(payload.get("msg") or payload.get("error") or "", session)
    low = msg.lower()
    said = f": {msg}" if msg else f" (HTTP {status})"
    if status == 200 and payload and payload.get("Success", True) is not False:
        return payload
    if status >= 500:
        raise UpdateError(UNAVAILABLE, f"MAM answered HTTP {status} for {name}")
    if "session type" in low or "not allowed" in low or "too recent" in low or "asn" in low or status == 429:
        raise UpdateError(REFUSED, f"MAM refused {name}{said} - check {TOGGLE}")
    if status in (301, 302, 303, 307, 308, 401, 403) or "session" in low or "login" in low:
        raise UpdateError(AUTH, f"MAM rejected the session for {name}{said} - renew TRACKER_SESSION")
    if status == 200:
        raise UpdateError(ERROR, f"MAM's {name} answer was not what it should be{said if msg else ' (no JSON)'}")
    raise UpdateError(ERROR, f"MAM answered HTTP {status} for {name}{f': {msg}' if msg else ''}")


def read_ip(session: str, transport) -> str:
    """The IP MyAnonamouse sees for this session, from ``jsonIp.php``. Read-only: nothing is
    changed on MAM. Raises UpdateError."""
    seen = _ip(_call(session, JSON_IP_URL, transport).get("ip"))
    if not seen:
        raise UpdateError(ERROR, "MAM's jsonIp.php answer carried no IP address")
    return seen


def write(session: str, observed: str, transport) -> str:
    """Set MAM's dynamic seedbox IP, then prove it. Returns the verified IP; raises UpdateError.

    1. ``dynamicSeedbox.php``: GET with the session cookie, no body. MAM registers the address the
       request comes from. Its own answer is only checked for a refusal, never trusted as success.
    2. ``jsonIp.php`` again: success only when the IP MAM now sees equals ``observed``. Anything
       else (another IP, no answer) is a failure, even after an "OK" from step 1.

    The caller has already run the egress guard and ``decide()``; this never decides on its own."""
    want = _ip(observed)
    if not want:
        raise UpdateError(ERROR, "the observed IP is not an IP address - nothing was sent to MAM")
    _call(session, DYNAMIC_SEEDBOX_URL, transport)
    try:
        seen = read_ip(session, transport)
    except UpdateError as exc:
        raise UpdateError(exc.kind, f"the update was sent but could not be verified - {exc}") from None
    if seen != want:
        raise UpdateError(ERROR, f"MAM answered the update, but jsonIp.php now shows {seen}, not {want} - "
                                 "not counted as a success")
    return seen
