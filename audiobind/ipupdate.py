"""The allowed-IP updater's decision: should the tracker's allowed IP follow the egress IP?

Pure: no I/O, no clock, no settings. The caller passes everything in, which keeps every branch
unit-testable. The egress guard (``egress.judge``) has already decided whether a cycle may run.
This module never changes that verdict. It only answers the next question: given what the guard
saw, would changing the allowed IP to the observed address be a sound thing to do?

No tracker endpoint for that write could be verified (see docs/ip-updater.md), so today the
``update`` outcome drives a notice and the manual **Use the current IP** action. Nothing is sent
to the tracker.
"""

from __future__ import annotations

import ipaddress

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
