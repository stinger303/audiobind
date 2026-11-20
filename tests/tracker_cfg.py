"""Shared test setup: a configured (fictional) tracker. Nothing here names a real site."""

from unittest import mock

from audiobind import tracker
from audiobind.config import CONFIG

BASE = "https://tracker.example"
SEARCH_URL = BASE + "/search.json"
DOWNLOAD_URL = BASE + "/download.php"
COOKIE = "session_cookie"

BODY = '{"q": "{{title}} {{author}}", "category": "{{category}}"}'

SETTINGS = {
    "tracker_url": BASE,
    "tracker_search_path": "/search.json",
    "tracker_download_path": "/download.php",
    "tracker_cookie_name": COOKIE,
    "tracker_freeleech_param": "fl",
    "tracker_search_category": "13",
    "tracker_search_body": BODY,
}


#: a fictional qBittorrent address: an empty QBIT_URL means "watcher off, not set yet", so a fixture
#: that wants the watcher on must have one (tests of the empty value patch it back to "")
QBIT_URL = "http://qbit.example:8080"


def tracker_cfg(session: str = "x") -> mock._patch:
    """Patch CONFIG so the watcher is on (``session`` "" = the one missing setting, i.e. off)."""
    return mock.patch.multiple(CONFIG, tracker_session=session, qbit_url=QBIT_URL, **SETTINGS)


def endpoints(**overrides) -> tracker.Endpoints:
    args = dict(search_method="POST", download_param="tid", freeleech_param="fl",
                search_category="13", search_body=BODY)
    args.update(overrides)
    return tracker.Endpoints.build(BASE, "/search.json", "/download.php", **args)


def make_client(session: str = "cookie-value", **kwargs) -> tracker.TrackerClient:
    eps = kwargs.pop("endpoints", None) or endpoints()
    return tracker.TrackerClient(session, eps, kwargs.pop("cookie_name", COOKIE), **kwargs)
