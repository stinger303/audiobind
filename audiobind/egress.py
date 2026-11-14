"""The egress guard: which public IP does MAM see, and is it the home connection?

``probe`` asks a plain-text "what is my IP" service (through MAM_PROXY when one is set, so it
sees what MAM sees); ``judge`` compares the answer with MAM_HOME_WAN_IP. The watcher runs both
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


def judge(configured_home_ip: str, observed: str) -> tuple[str, str]:
    """(``off`` | ``ok`` | ``blocked`` | ``unknown``, reason)."""
    home = (configured_home_ip or "").strip()
    if not home:
        return "off", "MAM_HOME_WAN_IP is not set - the egress guard is not armed"
    seen = (observed or "").strip()
    if not seen:
        return "unknown", "the egress IP could not be verified"
    try:
        same = ipaddress.ip_address(seen) == ipaddress.ip_address(home)
    except ValueError:
        same = seen == home
    if same:
        return "blocked", f"MAM would see {seen}, which is your home connection (MAM_HOME_WAN_IP)"
    return "ok", f"MAM sees {seen}, not your home connection"
