"""The "Set up downloading" wizard: where the person is, what they chose, and the small pure helpers.

The pages are drawn in ``web.py`` (every page is). This module holds what has no HTML in it:

* the three internal bookmarks, kept in the same settings store (the ``meta`` table) the Settings page
  uses, under keys that are NOT ``setting_<name>`` - so they are not settings, never appear on the
  Settings page and are not in ``SETTING_CHOICES``. No schema change;
* the Host-header pre-fill for the download client's address;
* the one retry the client test makes against this container's own loopback;
* step 3's address check: the watcher's own egress probe, stored where the watcher stores it;
* step 4's Test: one search through the watcher's own tracker client, said in plain words.

The wizard writes the very same settings keys the Watcher tab writes (``qbit_url`` ...), through the same
save path. It owns no configuration of its own.
"""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

#: where the person is: 1..len(STEPS). Written when a step is shown, so a container restart in the middle
#: (to set Ingest path or the tracker proxy) resumes at the same step.
META_STEP = "wizard_dl_step"
#: how books arrive (see MODES); "" until step 1 is answered
META_MODE = "wizard_dl_mode"
#: the Library page's card has been answered ("No, just my library") or the person chose to stop at
#: step 1; the card never comes back
META_DISMISSED = "wizard_dl_dismissed"

PATH = "/setup/downloading"
API = "/api/setup/downloading"

MODE_LIBRARY, MODE_FILES, MODE_TRACKER = "library", "files", "tracker"
MODES = (MODE_LIBRARY, MODE_FILES, MODE_TRACKER)

#: (number, name). Every step the flow will have is listed, so the indicator is whole from the start.
STEPS = (
    (1, "How books arrive"),
    (2, "Download client"),
    (3, "The address the tracker sees"),
    (4, "Your tracker"),
)
#: steps that have a page. A step without one is listed, not clickable, and answers with "not built yet".
BUILT = (1, 2, 3, 4)
LAST_STEP = STEPS[-1][0]
#: the end state after the last step: not a step of its own (the indicator lists it as "Done"), but a
#: place the person can be, so it is stored and resumed like one. ``?step=done`` asks for it.
DONE = LAST_STEP + 1

#: the download client's default WebUI port, for the pre-filled address
QBIT_DEFAULT_PORT = 8080
DEFAULT_CATEGORY = "audiobooks"

#: DOWNLOAD_CLIENTS key -> the settings keys its three connection fields are saved under (None: has none)
CLIENT_KEYS = {
    "qbittorrent": {"url": "qbit_url", "user": "qbit_user", "pass": "qbit_pass"},
    "deluge": {"url": "deluge_url", "user": None, "pass": "deluge_pass"},
    "transmission": {"url": "transmission_url", "user": "transmission_user", "pass": "transmission_pass"},
    "rtorrent": {"url": "rtorrent_url", "user": "rtorrent_user", "pass": "rtorrent_pass"},
}


def read(state) -> dict:
    """{"step", "mode", "dismissed"}: the stored bookmarks, each forced into its valid range, so a hand-edited
    or half-written value can never produce a page that does not exist."""
    mode = state.get_meta(META_MODE, "")
    step = state.get_meta(META_STEP, 1)
    return {
        "mode": mode if mode in MODES else "",
        "step": step if isinstance(step, int) and not isinstance(step, bool) and 1 <= step <= DONE else 1,
        "dismissed": state.get_meta(META_DISMISSED, False) is True,
    }


def resolve_step(requested, mode: str, stored: int) -> int:
    """The step to show. ``requested`` is the ?step= text (or None: resume at ``stored``). Anything past step 1
    needs the "through a download client" answer: the person who chose "only my library" or "my own downloader"
    never reaches steps 2-4 or the end, whatever the address bar says. ``done`` is the end state."""
    try:
        if requested == "done":
            step = DONE
        else:
            step = int(requested) if requested not in (None, "") else stored
    except (TypeError, ValueError):
        step = stored
    if not 1 <= step <= DONE:
        step = 1
    return step if mode == MODE_TRACKER else 1


def remember_step(state, step: int) -> None:
    """Store the step now shown (only when it changed: a reload writes nothing)."""
    if state.get_meta(META_STEP) != step:
        state.set_meta(META_STEP, step)


def card_visible(state, tracker_configured: bool) -> bool:
    """The Library page's one-time card: not once it was answered, not while a tracker is already set up."""
    return not tracker_configured and not read(state)["dismissed"]


def safe_host(raw: str, accepted=None) -> str:
    """The hostname (or IP) from a Host header, or "" when it is anything but that.

    Only a plain DNS name or a literal IP survives: no scheme, path, query, credentials, whitespace or
    control characters - the value is typed into an address box and then saved, so ``evil.example/x?y@z``
    and ``a b`` become "". ``accepted`` (a callable taking the bare name) is the server's own Host check
    (ALLOWED_HOSTS): a host it would have refused is "" too."""
    value = (raw or "").strip().lower()
    if not value or len(value) > 255:
        return ""
    if value.startswith("["):  # [v6] or [v6]:port
        end = value.find("]")
        rest = value[end + 1:] if end > 0 else "x"
        name = value[1:end] if end > 0 else ""
        if rest and not re.fullmatch(r":\d{1,5}", rest):
            return ""
    elif value.count(":") == 1:  # name:port or v4:port
        name, _, port = value.partition(":")
        if not re.fullmatch(r"\d{1,5}", port):
            return ""
    else:  # a bare name, or a bare v6 address (several colons)
        name = value
    name = name.rstrip(".")
    try:
        ip = ipaddress.ip_address(name)
        if ip.version == 6:
            name = str(ip)
    except ValueError:
        labels = name.split(".")
        if not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            return ""
    if accepted is not None and not accepted(name):
        return ""
    return name


def default_client_url(raw_host: str, accepted=None) -> str:
    """``http://<the host the browser used>:8080``, or "" when that host is missing or not usable."""
    name = safe_host(raw_host, accepted)
    if not name:
        return ""
    return f"http://[{name}]:{QBIT_DEFAULT_PORT}" if ":" in name else f"http://{name}:{QBIT_DEFAULT_PORT}"


def loopback_url(url: str) -> str:
    """``http(s)://127.0.0.1:<same port>`` for the address that timed out, or "" when there is nothing
    different to try (it already is the loopback, or it has no usable host)."""
    try:
        parts = urlsplit(url or "")
        host, port = parts.hostname, parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not host or host in ("127.0.0.1", "localhost", "::1"):
        return ""
    return f"{parts.scheme}://127.0.0.1:{port or (443 if parts.scheme == 'https' else 80)}"


def _plain(message: str) -> str:
    """The client test's "Connected - qBittorrent 5.0.4" as the guide says it: "Signed in to qBittorrent 5.0.4"."""
    return "Signed in to " + message[len("Connected - "):] if message.startswith("Connected - ") else message


def test_client(state, extra_details=()) -> dict:
    """Step 2's Test: the Watcher tab's own client check (``watcher.probe_qbit_report``) on the saved values, said
    in plain words, with one addition for qBittorrent. If the address does not answer at all, the same login is
    tried once on ``127.0.0.1:<same port>`` (Audiobind and qBittorrent sharing one container network, where the
    server's LAN address is not the way in). The answer says which address answered; when it was the loopback one
    it is saved as the qBittorrent URL, because that is the address Audiobind can actually reach.

    ``{"ok", "message", "chain", "details", "rule", "answered", "retried", "saved_url"}``. Never a setting's
    value other than the two addresses: the messages are built from results, and a password is not among them.
    ``extra_details`` are appended to a successful answer (the folder the ingest reads, and the one-folder
    explanation)."""
    from . import watcher
    from .config import WATCHER_TEXT_CHECKS, set_effective

    set_effective(state)
    first = watcher.client_url()
    if not first.strip():
        return {"ok": False, "message": f"There is no address yet. Type the address of {watcher.client_label()}'s web "
                                        "page and test again.", "chain": "", "details": [], "rule": "",
                "answered": "", "retried": False, "saved_url": ""}
    report = watcher.probe_qbit_report()
    answer = {"ok": report["ok"], "message": _plain(report["message"]), "chain": report["chain"],
              "details": list(report["details"]), "rule": report["rule"], "answered": first if report["ok"] else "",
              "retried": False, "saved_url": ""}
    if not report["ok"] and watcher.client_kind() == "qbittorrent" and report["kind"] == "unavailable":
        alt = loopback_url(first)
        if alt:
            second = watcher.probe_qbit_report(url=alt)
            answer["retried"] = True
            if second["kind"] in ("", "auth"):  # something answered there: it is the way in from here
                try:
                    stored = WATCHER_TEXT_CHECKS["qbit_url"][1](alt)
                except ValueError:
                    stored = ""
                if stored:
                    state.set_meta("setting_qbit_url", stored)
                    set_effective(state)
                    watcher.WATCHER.settings_changed(state)
                    answer["saved_url"] = stored
                tail = (f"qBittorrent did not answer at {first}, but it did at {alt}: it shares Audiobind's network, "
                        "so that is the address Audiobind has to use. It is saved now.")
                if second["ok"]:
                    answer.update(ok=True, message=_plain(second["message"]) + ". " + tail, chain=second["chain"],
                                  details=list(second["details"]), rule=second["rule"], answered=alt)
                else:
                    answer["message"] = f"{second['message']}. {tail} Fix the user name or password and test again."
                    answer["details"] = []
            else:
                answer["message"] = (f"Nothing answered at {first}, and nothing answered at {alt} either. Check that "
                                     "qBittorrent is running, that the WebUI is switched on, and that this is the "
                                     "address you open its WebUI with.")
                answer["details"] = [f"At {first}: {report['message']}.", f"At {alt}: {second['message']}."]
    if answer["ok"]:
        answer["details"] += list(extra_details)
    return answer


# ---------------------------------------------------------------------------- step 3: the address
def address_view(state) -> dict:
    """What step 3 shows, from the last egress check on record (the watcher's ``watcher_egress``, written
    by its guard before a cycle and by ``check_address``). Reads only: showing the page sends nothing.

    ``{"ip", "at", "checked", "failed", "proxy", "expected", "verdict"}``: ``at`` is the check's time
    (None: never checked) and ``checked`` the same as text; ``failed``: the last check found no address;
    ``proxy``: requests go through the tracker proxy; ``expected``: the address the session was made for
    (the "Only reach the tracker from one IP" setting, "" = not given); ``verdict``: match | mismatch |
    home | unset | unknown."""
    from . import egress, watcher
    from .config import set_effective

    set_effective(state)
    rec = state.get_meta(watcher.META_EGRESS) or {}
    rec = rec if isinstance(rec, dict) else {}
    ip = str(rec.get("ip") or "")
    try:
        at = float(rec["at"]) if rec.get("at") is not None else None
    except (TypeError, ValueError):
        at = None
    home = watcher._setting("tracker_home_ip")
    expected = watcher._setting("tracker_expected_egress_ip")
    if not ip:
        verdict = "unknown"
    elif home and egress.judge(home, ip)[0] == "blocked":
        verdict = "home"
    elif not expected:
        verdict = "unset"
    else:
        verdict = "match" if egress.judge("", ip, expected)[0] == "ok" else "mismatch"
    return {"ip": ip, "at": at, "checked": watcher._stamp(at) if at is not None else "",
            "failed": at is not None and not ip, "proxy": bool(watcher._setting("tracker_proxy")),
            "expected": expected, "verdict": verdict}


def check_address(state) -> dict:
    """Step 3's Check again: ONE ``egress.probe`` through the tracker proxy - the very probe and proxy
    the watcher's guard uses - stored in ``watcher_egress`` in the shape the guard stores it; then
    ``address_view``. The tracker is never contacted, and there is no new network path."""
    from . import egress, watcher
    from .config import set_effective

    set_effective(state)
    ip, why = "", ""
    try:
        ip = egress.probe(watcher._setting("tracker_proxy"))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the address is unknown
        why = str(exc) or type(exc).__name__
    reason = "" if ip else f"the egress IP could not be verified ({why})"
    state.set_meta(watcher.META_EGRESS, {"ip": ip, "at": time.time(), "reason": reason})
    return address_view(state)


# ---------------------------------------------------------------------------- step 4: your tracker
#: the settings step 4 fills, in the order a person fills them, with the name the step gives each
TRACKER_FIELDS = (
    ("tracker_url", "Your tracker's address"),
    ("tracker_session", "Session cookie"),
    ("tracker_cookie_name", "Cookie name"),
    ("tracker_search_path", "Search path"),
    ("tracker_search_body", "Search request"),
    ("tracker_search_method", "Search method"),
    ("tracker_search_encoding", "Search encoding"),
    ("tracker_search_category", "Search category"),
    ("tracker_download_path", "Download path"),
)
TRACKER_LABELS = dict(TRACKER_FIELDS)


def tracker_missing() -> list[str]:
    """The step 4 boxes the watcher still needs (the watcher's own list), by the name the step gives them
    and in the order the step shows them."""
    from . import watcher
    from .config import WATCHER_REQUIRED

    needed = {attr for _var, attr in WATCHER_REQUIRED if not watcher._setting(attr)}
    return [label for key, label in TRACKER_FIELDS if key in needed]


def _tracker_failure(status: int, sent: bool, proxy: bool, wait: float) -> str:
    """A failed search in plain words, from the status code and whether anything was sent - never from the
    exception's text, which can carry a piece of the tracker's own answer."""
    if not sent:
        text = ("Nothing was sent to your tracker: the saved search settings are not usable. Check the "
                "search request.")
    elif status == 0:
        text = "Audiobind could not reach your tracker" + (
            " through your tracker proxy. Check that the proxy is running." if proxy else
            ". Check the address and this container's internet connection.")
    elif status in (401, 403):
        text = (f"Your tracker turned the request away (it answered {status}). The session cookie is wrong, "
                "has expired, or was created for a different address than the one in step 3.")
    elif status == 404:
        text = "Your tracker has nothing at that search path (it answered 404). Check the search path."
    elif status == 429:
        text = "Your tracker asked Audiobind to slow down (it answered 429)."
    elif status >= 500:
        text = f"Your tracker had a problem of its own (it answered {status}). Try again later."
    elif status == 200:
        text = ("Your tracker answered, but not with search results. Usually the session cookie has expired, "
                "was pasted wrong, or was created for a different address than the one in step 3. If the "
                "cookie is fresh, check the search path and the search request against your tracker's API "
                "page.")
    else:
        text = f"Your tracker answered with an error (it answered {status})."
    if sent and wait > 0:
        text += (f" To be polite, Audiobind now waits {int(wait) + 1} seconds before it asks your tracker "
                 "anything again.")
    return text


def _guard_refusal(state, verdict: str, observed: str) -> str:
    """Why the Test sent nothing when the egress guard said no, in plain words."""
    if verdict == "unknown":
        return ("Audiobind could not find out which address your tracker would see, and you asked it to use "
                "only one, so nothing was sent. Step 3 can check the address again.")
    view = address_view(state)
    if view["verdict"] == "home":
        return (f"Your tracker would see {observed}, your home connection, so nothing was sent. Send "
                "Audiobind through your VPN (the tracker proxy) and test again.")
    return (f"Your tracker would see {observed}, but your session is for {view['expected']}, so nothing was "
            "sent: the session would be refused. Step 3 says what to do.")


def test_tracker(state, query: str = "") -> dict:
    """Step 4's Test: ONE search on the saved settings - the configured search request, the endpoint the
    tracker allows for this - through the watcher's own client (so its spacing and backoff hold, and a
    backoff in force means nothing is sent) and behind its egress guard. ``{"ok", "sent", "message"}``.

    The message is built from the parsed results and the status code: never the cookie, the address, a
    query string or any raw text from the tracker's answer."""
    from . import tracker, watcher
    from .config import set_effective

    set_effective(state)
    missing = tracker_missing()
    if missing:
        return {"ok": False, "sent": False,
                "message": f"Fill in these first: {', '.join(missing)}. Nothing was sent to your tracker."}
    # the guard is consulted only when armed: unarmed it answers "off" without a probe, but it also clears the
    # watcher_egress record, which would wipe the address step 3 just found
    verdict, observed = "off", ""
    if watcher._setting("tracker_home_ip") or watcher._setting("tracker_expected_egress_ip"):
        verdict, _reason, observed = watcher.WATCHER._egress_guard(state)
    if verdict in ("blocked", "unknown"):
        return {"ok": False, "sent": False, "message": _guard_refusal(state, verdict, observed)}
    title = watcher.clean_test_query(query)
    client = watcher.WATCHER.client()
    before = client.requests
    try:
        found = client.search(title)
    except tracker.TrackerBackoff:
        wait = client.backoff.remaining()
        return {"ok": False, "sent": False, "message": (
            "Nothing was sent: an earlier request to your tracker failed, and Audiobind waits before it asks "
            f"again so it never hurries your tracker. Try again in {int(wait) + 1} seconds.")}
    except tracker.TrackerError as exc:
        sent = client.requests > before
        status = int(getattr(exc, "status", 0) or 0)
        return {"ok": False, "sent": sent, "message": _tracker_failure(
            status, sent, bool(watcher._setting("tracker_proxy")), client.backoff.remaining())}
    except Exception:  # noqa: BLE001 - anything else is "no usable answer"
        return {"ok": False, "sent": client.requests > before,
                "message": "Audiobind could not reach your tracker. Try again in a minute."}
    if not found:
        return {"ok": True, "sent": True, "message": (
            f"Your tracker answered the search, but found nothing for “{watcher._short(title, 80)}”. "
            "Try a title you know is there; if that finds nothing too, check the search request and "
            "category.")}
    n, first = len(found), found[0]
    who = ", ".join(a for a in first.authors[:2] if a)
    name = watcher._short(first.name) or "(no title)"
    return {"ok": True, "sent": True, "message": (
        f"Your tracker answered: {n} {'result' if n == 1 else 'results'}, first: {name}"
        + (f" by {watcher._short(who)}" if who else "") + ".")}
