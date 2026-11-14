"""The egress guard: which public IP does MAM see, and is it the home connection?

``probe`` asks a plain-text "what is my IP" service (through MAM_PROXY when one is set, so it
sees what MAM sees); ``judge`` compares the answer with MAM_HOME_WAN_IP (never this IP) and MAM_EXPECTED_EGRESS_IP (only this IP). The watcher runs both
at the start of every cycle and makes no MAM request unless the verdict is ``ok`` (or ``off``).
This is the only request in the app that is not a MAM endpoint, and it carries no cookie.
"""

from __future__ import annotations

import ipaddress
import urllib.request

PROBE_URLS = ("https://ipinfo.io/ip", "https://api.ipify.org")
MAX_ANSWER = 256


class EgressError(Exception):
    """The egress IP could not be determined."""


def _fetch(url: str, proxy: str, timeout: float) -> str:
    """One GET, plain text. The single network call of the guard; tests replace it."""
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"Accept": "text/plain"})
    with opener.open(request, timeout=timeout) as response:
        return response.read(MAX_ANSWER).decode("ascii", "replace")


def _valid(text: str) -> str:
    return str(ipaddress.ip_address(text.strip()))


def probe(proxy: str, timeout: float = 10.0) -> str:
    """The public egress IP: ipinfo.io first, api.ipify.org once if that fails.
    Raises EgressError when neither gives a valid IPv4/IPv6 address."""
    problems = []
    for url in PROBE_URLS:
        try:
            return _valid(_fetch(url, proxy, timeout))
        except Exception as exc:  # noqa: BLE001 - timeout, DNS, TLS, proxy, junk: all "no answer"
            problems.append(f"{url.split('/')[2]}: {type(exc).__name__}")
    raise EgressError("; ".join(problems))


def _same(a: str, b: str) -> bool:
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return a == b


def judge(configured_home_ip: str, observed: str, expected_ip: str = "") -> tuple[str, str]:
    """(``off`` | ``ok`` | ``blocked`` | ``unknown``, reason).

    Order: the home IP (denylist) is checked first, then the expected IP (allowlist). With
    neither set the guard is ``off``; with one armed and no observed address it is ``unknown``
    (fail closed)."""
    home = (configured_home_ip or "").strip()
    expected = (expected_ip or "").strip()
    if not home and not expected:
        return "off", "neither MAM_HOME_WAN_IP nor MAM_EXPECTED_EGRESS_IP is set - the egress guard is not armed"
    seen = (observed or "").strip()
    if not seen:
        return "unknown", "the egress IP could not be verified"
    if home and _same(seen, home):
        return "blocked", f"MAM would see {seen}, which is your home connection (MAM_HOME_WAN_IP)"
    if expected:
        if not _same(seen, expected):
            return "blocked", f"MAM would see {seen}, but this installation only allows {expected}"
        return "ok", f"MAM sees {seen}, the allowed address"
    return "ok", f"MAM sees {seen}, not your home connection"
