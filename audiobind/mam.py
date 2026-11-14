"""MyAnonamouse (MAM): the two endpoints the wishlist watcher is allowed to automate.

MAM authorizes automation ONLY on its listed endpoints. This module talks to exactly two:

* Torrent Search JSON - ``POST /tor/js/loadSearchJSONbasic.php``
* Download Torrent    - ``GET  /tor/download.php?tid=<id>`` (``&fl`` spends a freeleech wedge)

Nothing else on the site is ever requested, no HTML is ever parsed, and redirects are never
followed (a redirect would be a request to an endpoint nobody authorized). A banned account is
the catastrophic outcome, so every choice here leans conservative.
"""

from __future__ import annotations

#: the site; the two endpoint URLs below are the only URLs this module builds
SITE = "https://www.myanonamouse.net"
SEARCH_URL = SITE + "/tor/js/loadSearchJSONbasic.php"
DOWNLOAD_URL = SITE + "/tor/download.php"

_FALSE_WORDS = {"", "0", "false", "no", "off", "null", "none"}


def flag(value) -> bool:
    """A MAM yes/no field read defensively: JSON true/false, 1/0, "1"/"0", "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_WORDS
    return False


def has_datetime(value) -> bool:
    """``personal_freeleech`` is a datetime when you hold a personal freeleech, else null.
    An empty string, a zero date or a 0/false also mean "none"."""
    if value is None or isinstance(value, bool):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip()
    if text.lower() in _FALSE_WORDS:
        return False
    return not text.startswith("0000-00-00")


def wedge_decision(entry: dict) -> tuple[bool, str]:
    """(spend a wedge?, why) for one search result.

    The torrent already costs nothing when it is site-wide freeleech (``free``), VIP
    freeleech (``fl_vip``) or you hold a personal freeleech on it (``personal_freeleech``):
    download WITHOUT ``&fl`` - MAM "will even spend on VIP torrents, no refunds". Anything
    else is downloaded WITH ``&fl``. Exactly one answer per torrent; there is no retry with
    the other one.
    """
    if flag(entry.get("free")):
        return False, "already freeleech (free) - no wedge spent"
    if flag(entry.get("fl_vip")):
        return False, "already freeleech (VIP) - no wedge spent"
    if has_datetime(entry.get("personal_freeleech")):
        return False, "personal freeleech already held - no wedge spent"
    return True, "not freeleech - downloaded with a freeleech wedge (&fl)"


def download_url(tid, use_wedge: bool) -> str:
    """The Download Torrent URL for one torrent id: ``&fl`` only when ``use_wedge``."""
    tid = str(tid).strip()
    if not tid.isdigit():
        raise ValueError("a MAM torrent id is a number")
    return f"{DOWNLOAD_URL}?tid={tid}" + ("&fl" if use_wedge else "")
