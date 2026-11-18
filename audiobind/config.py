"""Runtime configuration, entirely from environment variables."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


#: Deprecated names of settings that were renamed in 0.19.0-dev7. An old variable that is set
#: while its new name is empty is still honoured (one startup log line each, never the value);
#: the new name always wins when both are set.
DEPRECATED_ENV = {
    "MAM_ID": "TRACKER_SESSION",
    "MAM_VIP": "TRACKER_VIP",
    "MAM_PROXY": "TRACKER_PROXY",
    "MAM_USER_AGENT": "TRACKER_USER_AGENT",
    "MAM_HOME_WAN_IP": "TRACKER_HOME_IP",
    "MAM_EXPECTED_EGRESS_IP": "TRACKER_EXPECTED_EGRESS_IP",
}
_NEW_TO_OLD = {new: old for old, new in DEPRECATED_ENV.items()}


def _resolve(new: str, honoured: list, ignored: list) -> tuple[str, str]:
    """(value, the variable it came from) for ``new``: the new name when it is set, else its
    deprecated alias. The alias use is recorded in ``honoured`` / ``ignored`` (names only)."""
    value = _env(new, "")
    old = _NEW_TO_OLD.get(new, "")
    old_value = _env(old, "") if old else ""
    if value:
        if old_value:
            ignored.append((old, new))
        return value, new
    if old_value:
        honoured.append((old, new))
        return old_value, old
    return "", new


def _proxy_value(raw: str, name: str = "TRACKER_PROXY") -> str:
    """TRACKER_PROXY, checked at load: ``http://host:port`` or ``https://host:port`` (or empty =
    direct). Anything else stops the start with a message naming the variable - a typo must
    never quietly turn into a direct connection."""
    from urllib.parse import urlsplit

    raw = raw.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a bad port
        ok = (parts.scheme in ("http", "https") and bool(parts.hostname)
              and parts.path in ("", "/") and not parts.query and not parts.fragment)
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeError(
            f"{name} is not valid: use http://host:port or https://host:port "
            "(SOCKS is not supported); leave it empty for no proxy"
        )
    return raw.rstrip("/")


def _ip_value(name: str, raw: str) -> str:
    """An IP address variable, checked at load ("" = unset)."""
    import ipaddress

    raw = raw.strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        raise RuntimeError(f"{name} is not valid: {raw!r} is not an IPv4 or IPv6 address") from None


def default_bool(raw: str, default: bool) -> bool:
    return default if raw == "" else raw.strip().lower() in TRUTHY


def _env_bool(name: str, default: bool) -> bool:
    return default_bool(_env(name, ""), default)


def _tracker_url(raw: str) -> str:
    """TRACKER_URL, checked at load: ``http(s)://host[:port]`` with no path, query or login
    ("" = unset). A typo stops the start with a message naming the variable."""
    from urllib.parse import urlsplit

    raw = raw.strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a bad port
        ok = (parts.scheme in ("http", "https") and bool(parts.hostname) and parts.username is None
              and parts.path in ("", "/") and not parts.query and not parts.fragment)
    except ValueError:
        ok = False
    if not ok:
        raise RuntimeError("TRACKER_URL is not valid: use https://host (or http://host:port), "
                           "with no path, query or login - the paths are separate settings")
    return raw.rstrip("/")


_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")
_COOKIE_VALUE_RE = re.compile(r"^[\x21-\x7e]+$")


def _tracker_path(name: str, raw: str) -> str:
    """A URL path setting: starts with ``/``, no query, no fragment, no spaces ("" = unset)."""
    raw = raw.strip()
    if raw and (not _PATH_RE.match(raw) or raw.startswith("//")):
        raise RuntimeError(f"{name} is not valid: use a path that starts with / (no query string), e.g. /search")
    return raw


def _tracker_name(name: str, raw: str, allow_value: bool = False) -> str:
    """A parameter / cookie name setting ("" = unset); ``allow_value`` also accepts ``name=value``."""
    raw = raw.strip()
    head, sep, tail = raw.partition("=") if allow_value else (raw, "", "")
    if raw and (not _NAME_RE.match(head) or (sep and not re.match(r"^[A-Za-z0-9_.\-]*$", tail))):
        raise RuntimeError(f"{name} is not valid: use letters, digits and _ . - [ ] only")
    return raw


def _tracker_method(raw: str) -> str:
    raw = raw.strip().upper() or "POST"
    if raw not in ("POST", "GET"):
        raise RuntimeError("TRACKER_SEARCH_METHOD is not valid: use POST or GET")
    return raw


def _tracker_encoding(raw: str) -> str:
    raw = raw.strip().lower() or "json"
    if raw not in ("json", "form"):
        raise RuntimeError("TRACKER_SEARCH_ENCODING is not valid: use json or form")
    return raw


def _tracker_session(raw: str) -> str:
    """The session cookie's value: printable characters without ``;`` (a header cannot carry
    more). The value is never echoed in the error."""
    raw = raw.strip()
    if raw and (not _COOKIE_VALUE_RE.match(raw) or ";" in raw):
        raise RuntimeError("TRACKER_SESSION is not valid: it must be a single cookie value "
                           "(no spaces, semicolons or control characters)")
    return raw


def _tracker_body(raw: str) -> str:
    """TRACKER_SEARCH_BODY, checked at load: a JSON object (placeholders live inside its strings)."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise RuntimeError("TRACKER_SEARCH_BODY is not valid: it must be a JSON object, "
                           'e.g. {"q": "{{title}} {{author}}"}') from None
    if not isinstance(parsed, dict):
        raise RuntimeError("TRACKER_SEARCH_BODY is not valid: it must be a JSON object")
    return raw


#: DOWNLOAD_CLIENT's values -> the program's name as people know it
DOWNLOAD_CLIENTS = {"qbittorrent": "qBittorrent", "deluge": "Deluge", "transmission": "Transmission",
                    "rtorrent": "rTorrent"}


def _download_client(raw: str) -> str:
    """DOWNLOAD_CLIENT, checked at load ("" = qbittorrent). A typo stops the start: it must never
    quietly send torrents to a different client than the one meant."""
    raw = raw.strip().lower() or "qbittorrent"
    if raw not in DOWNLOAD_CLIENTS:
        raise RuntimeError(f"DOWNLOAD_CLIENT is not valid: use one of {', '.join(DOWNLOAD_CLIENTS)}")
    return raw


IP_UPDATER_MODES = ("off", "notify", "auto")


def _ip_updater(raw: str) -> str:
    """IP_UPDATER, checked at load ("" = off). A typo stops the start, as DOWNLOAD_CLIENT does."""
    raw = raw.strip().lower() or "off"
    if raw not in IP_UPDATER_MODES:
        raise RuntimeError(f"IP_UPDATER is not valid: use one of {', '.join(IP_UPDATER_MODES)}")
    return raw


HARDCOVER_EVIDENCE_MODES = ("off", "on")
_FALSY = {"", "0", "false", "no", "off"}


def _hardcover_evidence(raw: str) -> str:
    """HARDCOVER_EVIDENCE, checked at load ("" = off). A typo stops the start, as IP_UPDATER does."""
    raw = raw.strip().lower()
    if raw in TRUTHY:
        return "on"
    if raw in _FALSY:
        return "off"
    raise RuntimeError("HARDCOVER_EVIDENCE is not valid: use on or off")


def _hardcover_token(raw: str) -> str:
    """HARDCOVER_TOKEN: one line, no spaces inside, a "Bearer " prefix dropped ("" = unset = off).
    Never echoes the value."""
    text = raw.strip()
    if text.lower() == "bearer":
        return ""
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if len(text) > 4000 or any(ord(ch) <= 32 or ord(ch) == 127 for ch in text):
        raise RuntimeError("HARDCOVER_TOKEN is not valid: paste the token from hardcover.app/account/api")
    return text


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str, default: str) -> list[str]:
    """A comma-separated variable as a list of non-empty, lower-cased items."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        raw = default
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    library_root: Path = Path("/library")
    output_root: Path = Path("/output")
    state_dir: Path = Path("/config")
    #: rotating log file; "" = no file log (LOG_FILE unset = <state_dir>/audiobind.log)
    log_file: str = "/config/audiobind.log"
    work_dir: Path = Path("/work") if Path("/work").is_dir() else Path("/config/work")

    audio_bitrate: str = "128k"
    audio_channels: int = 2
    sample_rate: int = 44100
    jobs: int = 1

    #: which confidence tiers convert without asking ("high", "medium" or "off")
    auto_convert: str = "high"
    #: what each book is published as: "M4B", "MP3" or "M4B + MP3"
    output_format: str = "M4B"
    #: reject a match when the file runtime differs from the catalog by more than this. 15% not 25%:
    #: a US/UK edition pair commonly differs by 16-21%, and at 25% the only guard against writing
    #: the wrong edition passes silently (see docs/match-testing.md, brief-0220-case-hard-way.md)
    max_runtime_delta: float = 0.15

    scan_interval_min: int = 30
    dry_run: bool = False
    #: acquisition watcher only: search and decide, never download (independent of dry_run)
    watch_dry_run: bool = False

    web_enabled: bool = True
    web_port: int = 8080
    notify_url: str = ""
    region: str = "us"
    nice: int = 10
    #: host-side paths shown in the UI when the container paths are meaningless to the user
    host_library: str = ""
    host_output: str = ""
    #: read-only folder of finished torrent downloads (still seeding). Each direct child folder
    #: is a candidate book. Empty = the ingest feature is off. A plain string, never a Path:
    #: Path("") is ".", which would quietly turn "off" into "the current directory"
    ingest_dir: str = ""

    #: Host names (or IPs) the web UI answers to; any other Host header gets a 403. This is
    #: the DNS-rebinding defence: a hostile page can point its own domain at this server,
    #: but the browser still sends that domain as Host. A port in an entry is ignored.
    allowed_hosts: list[str] = field(default_factory=lambda: ["localhost", "127.0.0.1"])
    #: True when ALLOWED_HOSTS was set explicitly (the default only reaches the UI locally)
    allowed_hosts_set: bool = False
    #: honour X-Forwarded-Host (only behind a reverse proxy you control)
    trusted_proxy: bool = False
    #: the peer addresses (IPs or CIDRs) whose X-Forwarded-* headers are believed. TRUSTED_PROXY
    #: alone is not enough: with this empty no peer is trusted
    trusted_proxy_ips: list[str] = field(default_factory=list)
    #: concurrent web connections served at once; more get a 503 and are closed
    max_connections: int = 64
    #: manual Retry / Convert anyway / Use this ASIN / Re-check actions allowed at once
    manual_workers: int = 2
    #: deprecated shared secret for the web UI and API: only signs in while no account exists
    #: in the state DB (and skips first-run /setup); the account takes precedence once created
    auth_token: str = ""

    #: Phase 3 wishlist watcher (acquisition from a private tracker). Every tracker setting is
    #: optional and empty by default; the watcher is off until the URL, both paths, the cookie's
    #: name and the session are set. The session cookie and the qBittorrent password are secrets -
    #: they are kept out of repr() so a log line or traceback can never print them.
    tracker_url: str = ""
    tracker_search_path: str = ""
    tracker_search_method: str = "POST"
    #: the search request's JSON document with {{title}} / {{author}} / {{category}} placeholders.
    #: No default: the watcher is off until it is set. ``tracker_search_category`` is the value
    #: {{category}} stands for (empty = every member that is only {{category}} is left out).
    tracker_search_body: str = ""
    #: how a POST sends that document: ``json`` (a JSON body) or ``form`` (urlencoded key=value pairs)
    tracker_search_encoding: str = "json"
    tracker_search_category: str = ""
    tracker_download_path: str = ""
    tracker_download_param: str = "tid"
    #: the query parameter that spends a freeleech credit; empty = the client never sends one
    tracker_freeleech_param: str = ""
    tracker_cookie_name: str = ""
    tracker_session: str = field(default="", repr=False)
    watch_interval_min: int = 60
    watch_max_per_cycle: int = 5
    #: no torrent is added in a cycle while this many in ``qbit_category`` are still downloading (0 = off)
    watch_max_incomplete: int = 10
    #: account-protection budgets (0 = no limit; reserve 0 = none). Set by the operator, never inferred
    wedge_reserve: int = 1
    wedge_per_cycle: int = 1
    wedge_per_day: int = 3
    watch_max_unsatisfied: int = 0
    qbit_url: str = "http://localhost:8083"
    #: the qBittorrent category torrents are added to (its save path is the ingest folder)
    qbit_category: str = "audiobooks"
    qbit_user: str = ""
    qbit_pass: str = field(default="", repr=False)
    #: which download client the watcher hands torrents to (DOWNLOAD_CLIENT). The default keeps every
    #: existing install on qBittorrent; the category above (QBIT_CATEGORY) is used by every client -
    #: as Deluge's label, Transmission's label or rTorrent's custom1. The other clients' settings are
    #: empty by default and read only when their client is the one selected.
    download_client: str = "qbittorrent"
    deluge_url: str = ""
    deluge_pass: str = field(default="", repr=False)
    transmission_url: str = ""
    transmission_user: str = ""
    transmission_pass: str = field(default="", repr=False)
    rtorrent_url: str = ""
    rtorrent_user: str = ""
    rtorrent_pass: str = field(default="", repr=False)
    #: how tracker requests leave the container. ``tracker_user_agent`` "" = ``audiobind/<version>``,
    #: "none" = no User-Agent at all; ``tracker_proxy`` "" = direct (it may hold credentials, so never in repr());
    #: ``tracker_home_ip`` set = the egress guard is armed and refuses to run from that IP.
    tracker_user_agent: str = ""
    tracker_proxy: str = field(default="", repr=False)
    tracker_home_ip: str = ""
    #: optional allowlist: set = the tracker must see exactly this address, or the watcher refuses
    #: to contact it. "" = this check is off. Also editable in Settings > Watcher.
    tracker_expected_egress_ip: str = ""
    #: whether the account holds the tracker's top membership tier (not detectable from the
    #: API): False = torrents restricted to it are skipped. Also editable in Settings > Watcher.
    tracker_vip: bool = False
    #: the allowed-IP updater (IP_UPDATER): "off" (default, exactly the behaviour before it existed),
    #: "notify" (log and show egress drift) or "auto" (no write endpoint is known, so it acts as notify)
    ip_updater: str = "off"
    #: Hardcover as an evidence-only second opinion (detail panel line, never the grade):
    #: both the token and HARDCOVER_EVIDENCE=on are needed; either missing = no request at all
    hardcover_token: str = field(default="", repr=False)
    hardcover_evidence: str = "off"
    #: deprecated variables that were honoured / ignored at load, as (old, new) names only -
    #: the daemon logs one line each at startup
    deprecated_used: list = field(default_factory=list, repr=False)
    deprecated_ignored: list = field(default_factory=list, repr=False)

    ignore_globs: list[str] = field(
        default_factory=lambda: ["@eaDir", "#recycle", "System Volume Information"]
    )

    @classmethod
    def from_env(cls) -> Config:
        honoured: list = []
        ignored: list = []
        session, _ = _resolve("TRACKER_SESSION", honoured, ignored)
        agent, _ = _resolve("TRACKER_USER_AGENT", honoured, ignored)
        proxy, proxy_name = _resolve("TRACKER_PROXY", honoured, ignored)
        home, home_name = _resolve("TRACKER_HOME_IP", honoured, ignored)
        expected, expected_name = _resolve("TRACKER_EXPECTED_EGRESS_IP", honoured, ignored)
        vip_raw, _ = _resolve("TRACKER_VIP", honoured, ignored)
        return cls(
            library_root=Path(_env("LIBRARY_ROOT", "/library")),
            output_root=Path(_env("OUTPUT_ROOT", "/output")),
            state_dir=Path(_env("STATE_DIR", "/config")),
            # unset = the default file under STATE_DIR; set but empty = file log off
            log_file=_env("LOG_FILE", str(Path(_env("STATE_DIR", "/config")) / "audiobind.log")),
            audio_bitrate=_env("AUDIO_BITRATE", "128k"),
            audio_channels=_env_int("AUDIO_CHANNELS", 2),
            sample_rate=_env_int("SAMPLE_RATE", 44100),
            jobs=max(1, _env_int("JOBS", 1)),
            auto_convert=_env("AUTO_CONVERT", "high").lower(),
            output_format=_env("OUTPUT_FORMAT", "M4B").strip().upper() or "M4B",
            max_runtime_delta=_env_float("MAX_RUNTIME_DELTA", 0.15),
            scan_interval_min=max(1, _env_int("SCAN_INTERVAL_MIN", 30)),
            dry_run=_env_bool("DRY_RUN", False),
            watch_dry_run=_env_bool("WATCH_DRY_RUN", False),
            web_enabled=_env_bool("WEB_ENABLED", True),
            web_port=_env_int("WEB_PORT", 8080),
            notify_url=_env("NOTIFY_URL", ""),
            region=_env("REGION", "us"),
            nice=_env_int("NICE", 10),
            host_library=_env("HOST_LIBRARY", ""),
            host_output=_env("HOST_OUTPUT", ""),
            ingest_dir=_env("INGEST_DIR", ""),
            allowed_hosts=_env_list("ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
            allowed_hosts_set=bool(_env("ALLOWED_HOSTS", "")),
            trusted_proxy=_env_bool("TRUSTED_PROXY", False),
            trusted_proxy_ips=_env_list("TRUSTED_PROXY_IPS", ""),
            auth_token=_env("AUTH_TOKEN", ""),
            manual_workers=max(1, _env_int("MANUAL_WORKERS", 2)),
            max_connections=max(1, _env_int("MAX_CONNECTIONS", 64)),
            tracker_url=_tracker_url(_env("TRACKER_URL", "")),
            tracker_search_path=_tracker_path("TRACKER_SEARCH_PATH", _env("TRACKER_SEARCH_PATH", "")),
            tracker_search_method=_tracker_method(_env("TRACKER_SEARCH_METHOD", "")),
            tracker_search_body=_tracker_body(_env("TRACKER_SEARCH_BODY", "")),
            tracker_search_encoding=_tracker_encoding(_env("TRACKER_SEARCH_ENCODING", "")),
            tracker_search_category=_env("TRACKER_SEARCH_CATEGORY", ""),
            tracker_download_path=_tracker_path("TRACKER_DOWNLOAD_PATH", _env("TRACKER_DOWNLOAD_PATH", "")),
            tracker_download_param=_tracker_name(
                "TRACKER_DOWNLOAD_PARAM", _env("TRACKER_DOWNLOAD_PARAM", "")) or "tid",
            tracker_freeleech_param=_tracker_name(
                "TRACKER_FREELEECH_PARAM", _env("TRACKER_FREELEECH_PARAM", ""), allow_value=True),
            tracker_cookie_name=_tracker_name("TRACKER_COOKIE_NAME", _env("TRACKER_COOKIE_NAME", "")),
            tracker_session=_tracker_session(session),
            watch_interval_min=_env_int("WATCH_INTERVAL_MIN", 60),
            watch_max_per_cycle=_env_int("WATCH_MAX_PER_CYCLE", 5),
            watch_max_incomplete=max(0, _env_int("WATCH_MAX_INCOMPLETE", 10)),
            wedge_reserve=_env_int("WEDGE_RESERVE", 1),
            wedge_per_cycle=_env_int("WEDGE_MAX_PER_CYCLE", 1),
            wedge_per_day=_env_int("WEDGE_MAX_PER_DAY", 3),
            watch_max_unsatisfied=_env_int("WATCH_MAX_UNSATISFIED", 0),
            qbit_url=_env("QBIT_URL", "http://localhost:8083").rstrip("/") or "http://localhost:8083",
            qbit_category=_env("QBIT_CATEGORY", "audiobooks") or "audiobooks",
            qbit_user=_env("QBIT_USER", ""),
            qbit_pass=os.environ.get("QBIT_PASS", ""),  # a password keeps its spaces
            download_client=_download_client(_env("DOWNLOAD_CLIENT", "")),
            deluge_url=_env("DELUGE_URL", ""),
            deluge_pass=os.environ.get("DELUGE_PASS", ""),
            transmission_url=_env("TRANSMISSION_URL", ""),
            transmission_user=_env("TRANSMISSION_USER", ""),
            transmission_pass=os.environ.get("TRANSMISSION_PASS", ""),
            rtorrent_url=_env("RTORRENT_URL", ""),
            rtorrent_user=_env("RTORRENT_USER", ""),
            rtorrent_pass=os.environ.get("RTORRENT_PASS", ""),
            tracker_user_agent=agent,
            tracker_proxy=_proxy_value(proxy, proxy_name),
            tracker_home_ip=_ip_value(home_name, home),
            tracker_expected_egress_ip=_ip_value(expected_name, expected),
            tracker_vip=default_bool(vip_raw, False),
            ip_updater=_ip_updater(_env("IP_UPDATER", "")),
            hardcover_token=_hardcover_token(os.environ.get("HARDCOVER_TOKEN", "")),
            hardcover_evidence=_hardcover_evidence(_env("HARDCOVER_EVIDENCE", "")),
            deprecated_used=honoured,
            deprecated_ignored=ignored,
        )

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.work_dir, self.output_root):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise RuntimeError(
                    f"cannot write to {path} - check that PUID/PGID own this path ({exc})"
                ) from exc

    def preflight(self) -> None:
        """Fail early and loudly instead of halfway through a conversion."""
        self.ensure_dirs()
        probe = self.output_root / ".audiobind-write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise RuntimeError(f"output root {self.output_root} is not writable: {exc}") from exc
        if not self.library_root.exists():
            raise RuntimeError(f"library root {self.library_root} does not exist")
        if self.library_root.resolve() == self.output_root.resolve():
            raise RuntimeError("LIBRARY_ROOT and OUTPUT_ROOT must be different directories")

    @property
    def auto_tiers(self) -> set[str]:
        mapping = {"high": {"HIGH"}, "medium": {"HIGH", "MEDIUM"}, "off": set()}
        return mapping.get(self.auto_convert, {"HIGH"})


CONFIG = Config.from_env()

#: Settings whose DB overrides take precedence over environment defaults.
#: Keys match the ``setting_*`` meta rows in State; values are typed like the
#: corresponding Config attribute (str, int, float).  Updated at the start of
#: each scan and whenever the settings page saves.
_EFFECTIVE: dict[str, object] = {}

#: the settings "Reset to defaults" on the Settings page clears - every key that existed before the
#: watcher's connection settings became editable. The connection keys below are deliberately not in
#: it: resetting the conversion settings must never wipe a tracker session or a qBittorrent password
#: typed into the page. Each of those has its own "Reset to container value".
GENERAL_SETTING_KEYS = ("auto_convert", "audio_bitrate", "runtime_delta", "scan_interval", "nice", "region",
                        "expected_egress", "vip", "watch_interval", "watch_per_cycle", "output_format",
                        "wedge_reserve", "wedge_per_cycle", "wedge_per_day", "watch_max_unsatisfied")

#: 0.21.0-dev5: the watcher's connection settings, editable on the Settings page (Watcher section).
#: TRACKER_PROXY and TRACKER_HOME_IP stay container-only.
WATCHER_SETTING_KEYS = ("tracker_url", "tracker_session", "tracker_cookie_name", "tracker_search_path",
                        "tracker_search_body", "tracker_search_method", "tracker_search_encoding",
                        "tracker_search_category", "tracker_download_path", "tracker_download_param",
                        "tracker_freeleech_param", "tracker_user_agent",
                        "qbit_url", "qbit_user", "qbit_pass", "qbit_category", "watch_max_incomplete")

#: the download-client choice and the non-qBittorrent clients' connection settings (Settings > Watcher >
#: Download client). Like the watcher's connection keys they are never wiped by "Reset to defaults".
#: The qBittorrent ones stay in WATCHER_SETTING_KEYS, where they have always been.
CLIENT_SETTING_KEYS = ("download_client", "deluge_url", "deluge_pass", "transmission_url", "transmission_user",
                       "transmission_pass", "rtorrent_url", "rtorrent_user", "rtorrent_pass")

#: the allowed-IP updater's mode (Settings > Watcher). A page value wins over IP_UPDATER; like the
#: watcher's keys it is never wiped by "Reset to defaults".
IPUPDATE_SETTING_KEYS = ("ip_updater",)

#: Hardcover evidence (Settings > Library). A page value wins over the env; never wiped by "Reset to
#: defaults" (the token is a secret typed into the page, like the tracker session).
HARDCOVER_SETTING_KEYS = ("hardcover_evidence", "hardcover_token")

SETTING_KEYS = (GENERAL_SETTING_KEYS + WATCHER_SETTING_KEYS + CLIENT_SETTING_KEYS + IPUPDATE_SETTING_KEYS
                + HARDCOVER_SETTING_KEYS)

#: settings whose value is never sent to a browser: not in a page, an attribute, a JSON answer or a log
SECRET_SETTING_KEYS = frozenset({"tracker_session", "qbit_pass", "deluge_pass", "transmission_pass", "rtorrent_pass",
                                 "hardcover_token"})

SETTING_MAP: dict[str, str] = {
    "auto_convert": "auto_convert",
    "audio_bitrate": "audio_bitrate",
    "runtime_delta": "max_runtime_delta",
    "scan_interval": "scan_interval_min",
    "nice": "nice",
    "region": "region",
    "expected_egress": "tracker_expected_egress_ip",
    "vip": "tracker_vip",
    "watch_interval": "watch_interval_min",
    "watch_per_cycle": "watch_max_per_cycle",
    "wedge_reserve": "wedge_reserve",
    "wedge_per_cycle": "wedge_per_cycle",
    "wedge_per_day": "wedge_per_day",
    "watch_max_unsatisfied": "watch_max_unsatisfied",
    "output_format": "output_format",
    # the watcher's connection settings: the key is the Config attribute's own name
    **{key: key for key in WATCHER_SETTING_KEYS},
    **{key: key for key in CLIENT_SETTING_KEYS},
    "ip_updater": "ip_updater",
    "hardcover_evidence": "hardcover_evidence",
    "hardcover_token": "hardcover_token",
}

SETTING_TYPES: dict[str, type] = {
    "auto_convert": str,
    "output_format": str,
    "audio_bitrate": str,
    "runtime_delta": int,  # percentage (5-50), divided by 100 at use
    "scan_interval": int,
    "nice": int,
    "region": str,
    "expected_egress": str,  # free text, validated by valid_ip_or_empty (not a choice, not a number)
    "vip": str,  # "YES" / "NO"; effective_val turns it into a bool
    "watch_interval": int,  # minutes, clamped to WATCH_INTERVAL_BOUNDS at use
    "watch_per_cycle": int,  # books, clamped to WATCH_MAX_PER_CYCLE_BOUNDS at use
    "wedge_reserve": int,  # wedges kept unspent, clamped to WEDGE_BOUNDS at use
    "wedge_per_cycle": int,  # 0 = no limit
    "wedge_per_day": int,  # 0 = no limit
    "watch_max_unsatisfied": int,  # 0 = no limit
    # the watcher's connection settings: text (validated on save), except the one number
    **{key: str for key in WATCHER_SETTING_KEYS if key != "watch_max_incomplete"},
    "watch_max_incomplete": int,  # torrents still downloading at which a cycle adds none; 0 = off
    **{key: str for key in CLIENT_SETTING_KEYS},
    "ip_updater": str,  # one of IP_UPDATER_MODES
    "hardcover_evidence": str,  # one of HARDCOVER_EVIDENCE_MODES
    "hardcover_token": str,  # secret
}

#: the values the settings page offers; an environment value outside these is added to the
#: choices (and accepted on save) so the page can always show and keep what is really running
SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "auto_convert": ("HIGH", "MEDIUM", "OFF"),
    "output_format": ("M4B", "MP3", "M4B + MP3"),
    "audio_bitrate": ("64k", "128k", "192k", "256k"),
    "region": ("us", "uk", "ca", "au", "de"),
    "vip": ("NO", "YES"),
    "tracker_search_method": ("POST", "GET"),
    "tracker_search_encoding": ("json", "form"),
    "download_client": tuple(DOWNLOAD_CLIENTS),
    "ip_updater": IP_UPDATER_MODES,
    "hardcover_evidence": HARDCOVER_EVIDENCE_MODES,
}

#: (min, max) for the numeric settings, widened to include the environment value
SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "runtime_delta": (5, 50),
    "scan_interval": (1, 1440),
    "nice": (0, 19),
    "watch_interval": (10, 1440),  # == WATCH_INTERVAL_BOUNDS
    "watch_per_cycle": (1, 20),  # == WATCH_MAX_PER_CYCLE_BOUNDS
    "wedge_reserve": (0, 50),
    "wedge_per_cycle": (0, 10),
    "wedge_per_day": (0, 50),
    "watch_max_unsatisfied": (0, 500),
    "watch_max_incomplete": (0, 500),
}

#: settings whose bounds are politeness limits: never widened to fit an env value
FIXED_BOUNDS = frozenset({"watch_interval", "watch_per_cycle", "wedge_reserve", "wedge_per_cycle", "wedge_per_day",
                          "watch_max_unsatisfied"})


def setting_env_defaults() -> dict[str, object]:
    """What each setting is when nothing is saved in the UI - read live from CONFIG.

    Shaped like the settings page fields: the tier upper-case to match the select's
    options, the runtime tolerance as a whole percentage.
    """
    return {
        "auto_convert": str(CONFIG.auto_convert or "high").upper(),
        "output_format": str(CONFIG.output_format or "M4B").upper(),
        "audio_bitrate": str(CONFIG.audio_bitrate).lower(),
        "runtime_delta": int(round(CONFIG.max_runtime_delta * 100)),
        "scan_interval": int(CONFIG.scan_interval_min),
        "nice": int(CONFIG.nice),
        "region": str(CONFIG.region).lower(),
        "expected_egress": str(CONFIG.tracker_expected_egress_ip or ""),
        "vip": "YES" if CONFIG.tracker_vip else "NO",
        "watch_interval": watch_interval_min_env(),
        "watch_per_cycle": watch_max_per_cycle_env(),
        "wedge_reserve": _budget_env("wedge_reserve"),
        "wedge_per_cycle": _budget_env("wedge_per_cycle"),
        "wedge_per_day": _budget_env("wedge_per_day"),
        "watch_max_unsatisfied": _budget_env("watch_max_unsatisfied"),
        # the watcher's connection settings, as the container set them (secrets included: this is
        # the server's own view - web.public_settings() strips them before anything is sent)
        **{key: str(getattr(CONFIG, key) or "") for key in WATCHER_SETTING_KEYS if key != "watch_max_incomplete"},
        "watch_max_incomplete": max(0, int(CONFIG.watch_max_incomplete or 0)),
    }


def _plain_text(label: str, raw: str, limit: int = 300, keep_spaces: bool = False) -> str:
    """A free-text setting: one line, printable, not too long. Never echoes the value."""
    text = raw if keep_spaces else raw.strip()
    if len(text) > limit or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"{label}: use one line of plain text (at most {limit} characters)")
    return text


def _qbit_url(raw: str) -> str:
    """QBIT_URL: http(s)://host[:port][/path], no login in it ("" = the default address)."""
    from urllib.parse import urlsplit

    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a bad port
        ok = parts.scheme in ("http", "https") and bool(parts.hostname) and parts.username is None \
            and not parts.query and not parts.fragment
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("qBittorrent URL: use http://host:port (or https://), with no login in it")
    return raw


def _client_url(label: str, schemes: tuple[str, ...], example: str):
    """A download client's address: one of ``schemes``, a host, no login, query or fragment ("" = unset)."""
    from urllib.parse import urlsplit

    def check(raw: str) -> str:
        raw = raw.strip().rstrip("/")
        if not raw:
            return ""
        try:
            parts = urlsplit(raw)
            port = parts.port
            ok = parts.scheme in schemes and bool(parts.hostname) and parts.username is None \
                and not parts.query and not parts.fragment and (parts.scheme != "scgi" or bool(port))
        except ValueError:
            ok = False
        if not ok:
            raise ValueError(f"{label}: use {example}, with no login in it")
        return raw

    return check


def _from_startup_check(label: str, check, raw: str) -> str:
    """Run one of the start-up validators (which raise RuntimeError naming the variable) and turn
    its message into a page message naming the field instead."""
    try:
        return check(raw)
    except RuntimeError as exc:
        message = str(exc)
        _, sep, why = message.partition(" is not valid: ")
        raise ValueError(f"{label}: {why if sep else message}") from None


#: key -> (label, validator) for the watcher's text settings. Each validator takes the typed text
#: and returns what is stored, or raises ValueError with a plain message (never the value itself).
WATCHER_TEXT_CHECKS = {
    "tracker_url": ("Tracker URL", lambda raw: _from_startup_check("Tracker URL", _tracker_url, raw)),
    "tracker_session": ("Session cookie value",
                        lambda raw: _from_startup_check("Session cookie value", _tracker_session, raw)),
    "tracker_cookie_name": ("Session cookie name", lambda raw: _from_startup_check(
        "Session cookie name", lambda v: _tracker_name("TRACKER_COOKIE_NAME", v), raw)),
    "tracker_search_path": ("Search path", lambda raw: _from_startup_check(
        "Search path", lambda v: _tracker_path("TRACKER_SEARCH_PATH", v), raw)),
    "tracker_search_body": ("Search request body", lambda raw: _from_startup_check("Search request body", _tracker_body, raw)),
    "tracker_search_category": ("Search category", lambda raw: _plain_text("Search category", raw, 200)),
    "tracker_download_path": ("Download path", lambda raw: _from_startup_check(
        "Download path", lambda v: _tracker_path("TRACKER_DOWNLOAD_PATH", v), raw)),
    "tracker_download_param": ("Download parameter", lambda raw: _from_startup_check(
        "Download parameter", lambda v: _tracker_name("TRACKER_DOWNLOAD_PARAM", v), raw) or "tid"),
    "tracker_freeleech_param": ("Freeleech parameter", lambda raw: _from_startup_check(
        "Freeleech parameter", lambda v: _tracker_name("TRACKER_FREELEECH_PARAM", v, allow_value=True), raw)),
    "tracker_user_agent": ("User-Agent", lambda raw: _plain_text("User-Agent", raw, 300)),
    "hardcover_token": ("Hardcover token", lambda raw: _from_startup_check("Hardcover token", _hardcover_token, raw)),
    "qbit_url": ("qBittorrent URL", _qbit_url),
    "qbit_user": ("qBittorrent user", lambda raw: _plain_text("qBittorrent user", raw, 200)),
    "qbit_pass": ("qBittorrent password", lambda raw: _plain_text("qBittorrent password", raw, 300, keep_spaces=True)),
    "qbit_category": ("qBittorrent category", lambda raw: _plain_text("qBittorrent category", raw, 200)),
    # the other download clients (download_client itself is a select, checked against its choices)
    "deluge_url": ("Deluge URL", _client_url("Deluge URL", ("http", "https"), "http://deluge:8112")),
    "deluge_pass": ("Deluge password", lambda raw: _plain_text("Deluge password", raw, 300, keep_spaces=True)),
    "transmission_url": ("Transmission URL", _client_url(
        "Transmission URL", ("http", "https"), "http://transmission:9091/transmission/rpc")),
    "transmission_user": ("Transmission user", lambda raw: _plain_text("Transmission user", raw, 200)),
    "transmission_pass": ("Transmission password",
                          lambda raw: _plain_text("Transmission password", raw, 300, keep_spaces=True)),
    "rtorrent_url": ("rTorrent URL", _client_url(
        "rTorrent URL", ("scgi", "http", "https"), "scgi://rtorrent:5000 or http://rutorrent/RPC2")),
    "rtorrent_user": ("rTorrent user", lambda raw: _plain_text("rTorrent user", raw, 200)),
    "rtorrent_pass": ("rTorrent password", lambda raw: _plain_text("rTorrent password", raw, 300, keep_spaces=True)),
}


def valid_ip_or_empty(text: str) -> str:
    """The canonical form of an IPv4/IPv6 address, or "" for blank. Raises ValueError otherwise."""
    import ipaddress

    text = (text or "").strip()
    return str(ipaddress.ip_address(text)) if text else ""


def setting_choices(key: str) -> list[str]:
    """The select options for a setting, including the environment value if it is unusual."""
    options = list(SETTING_CHOICES.get(key, ()))
    env = str(setting_env(key))
    if options and env and env.upper() not in {o.upper() for o in options}:
        options.append(env)
    return options


def setting_bounds(key: str) -> tuple[int, int]:
    """(min, max) for a numeric setting, widened so the environment value always fits."""
    lo, hi = SETTING_BOUNDS[key]
    if key in FIXED_BOUNDS:
        return lo, hi
    try:
        env = int(setting_env(key))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return lo, hi
    return min(lo, env), max(hi, env)


def set_effective(state) -> None:
    """Refresh all effective overrides from the State DB."""
    for key in SETTING_KEYS:
        db = state.get_meta(f"setting_{key}")
        if db is not None:
            _EFFECTIVE[key] = db
        else:
            _EFFECTIVE.pop(key, None)


def effective_val(name: str):
    """Return the DB override for *name* (the Config attribute), else the env default."""
    setting_key = _config_to_setting(name)
    if setting_key and setting_key in _EFFECTIVE:
        raw = _EFFECTIVE[setting_key]
        if setting_key == "runtime_delta":
            return raw / 100.0  # percentage -> float tolerance
        if setting_key == "vip":
            return str(raw).strip().upper() == "YES"
        return raw
    return getattr(CONFIG, name)


def is_custom(name: str) -> bool:
    """True when *name* (the Config attribute) has an active DB override."""
    setting_key = _config_to_setting(name)
    return setting_key is not None and setting_key in _EFFECTIVE


def setting_effective(key: str):
    """Return the effective value for a setting key (e.g. ``runtime_delta``)."""
    if key in _EFFECTIVE:
        return _EFFECTIVE[key]
    return setting_env(key)


def client_setting_env_defaults() -> dict[str, object]:
    """The download-client settings as the container set them (secrets included - the server's own
    view; web.public_settings() strips them). Kept apart from setting_env_defaults(), whose keys are
    the settings that existed before multi-client support."""
    return {key: str(getattr(CONFIG, key) or "") for key in CLIENT_SETTING_KEYS}


def setting_env(key: str):
    """Return the env default for a setting key (live from CONFIG, never a hardcoded copy)."""
    defaults = setting_env_defaults()
    if key in defaults:
        return defaults[key]
    if key == "ip_updater":
        return str(CONFIG.ip_updater or "off")
    if key == "hardcover_evidence":
        return str(CONFIG.hardcover_evidence or "off")
    if key == "hardcover_token":
        return str(CONFIG.hardcover_token or "")
    return client_setting_env_defaults().get(key)


def _config_to_setting(name: str) -> str | None:
    for sk, cn in SETTING_MAP.items():
        if cn == name:
            return sk
    return None


#: politeness bounds for the wishlist watcher. A value outside them is pulled back in: a private
#: tracker tolerates automation only on its listed endpoints and a banned account is unrecoverable,
#: so no setting can make the watcher search more often or more per cycle than this.
WATCH_INTERVAL_BOUNDS = (10, 1440)
WATCH_MAX_PER_CYCLE_BOUNDS = (1, 20)


def _clamp(value, bounds: tuple[int, int], default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    lo, hi = bounds
    return max(lo, min(hi, number))


def watch_interval_min_env() -> int:
    """The env default, as the clamp would use it (so the page never shows an unusable value)."""
    return _clamp(CONFIG.watch_interval_min, WATCH_INTERVAL_BOUNDS, 60)


def watch_max_per_cycle_env() -> int:
    return _clamp(CONFIG.watch_max_per_cycle, WATCH_MAX_PER_CYCLE_BOUNDS, 5)


def watch_interval_min() -> int:
    """Minutes between watcher cycles (WATCH_INTERVAL_MIN through the effective getter)."""
    return _clamp(effective_val("watch_interval_min"), WATCH_INTERVAL_BOUNDS, 60)


def watch_max_per_cycle() -> int:
    """Wishlist rows one watcher cycle may search at most (WATCH_MAX_PER_CYCLE)."""
    return _clamp(effective_val("watch_max_per_cycle"), WATCH_MAX_PER_CYCLE_BOUNDS, 5)


#: account-protection budgets: setting key -> (default, bounds). 0 = no limit (reserve: none)
BUDGET_DEFAULTS = {"wedge_reserve": 1, "wedge_per_cycle": 1, "wedge_per_day": 3, "watch_max_unsatisfied": 0}


def _budget_env(key: str) -> int:
    return _clamp(getattr(CONFIG, SETTING_MAP[key]), SETTING_BOUNDS[key], BUDGET_DEFAULTS[key])


def _budget(key: str) -> int:
    return _clamp(effective_val(SETTING_MAP[key]), SETTING_BOUNDS[key], BUDGET_DEFAULTS[key])


def wedge_reserve() -> int:
    """Freeleech credits to keep unspent (WEDGE_RESERVE; 0 = no reserve)."""
    return _budget("wedge_reserve")


def wedge_per_cycle() -> int:
    """Most credits one cycle may spend (WEDGE_MAX_PER_CYCLE; 0 = no limit)."""
    return _budget("wedge_per_cycle")


def wedge_per_day() -> int:
    """Most credits spent in any 24 hours (WEDGE_MAX_PER_DAY; 0 = no limit)."""
    return _budget("wedge_per_day")


def watch_max_unsatisfied() -> int:
    """Unsatisfied snatches at which a cycle adds nothing (WATCH_MAX_UNSATISFIED; 0 = no limit)."""
    return _budget("watch_max_unsatisfied")


def watch_max_incomplete() -> int:
    """Torrents of the qBittorrent category still downloading at which a cycle adds none (0 = off)."""
    try:
        return max(0, int(effective_val("watch_max_incomplete")))
    except (TypeError, ValueError):
        return 10


def effective_auto_tiers() -> set[str]:
    """Return the auto-convert tiers using the effective override or env default."""
    tier = str(effective_val("auto_convert") or "high").lower()
    mapping = {"high": {"HIGH"}, "medium": {"HIGH", "MEDIUM"}, "off": set()}
    return mapping.get(tier, {"HIGH"})


def deprecated_env_lines(config: Config | None = None) -> list[str]:
    """One startup log line per deprecated variable that was in use (names only, never a value)."""
    config = config or CONFIG
    lines = [f"{old} is deprecated, use {new}" for old, new in config.deprecated_used]
    lines += [f"{old} is deprecated and ignored: {new} is set" for old, new in config.deprecated_ignored]
    return lines


#: the settings that must all be present before the watcher can run: (variable, Config attribute)
WATCHER_REQUIRED = (
    ("TRACKER_URL", "tracker_url"),
    ("TRACKER_SEARCH_PATH", "tracker_search_path"),
    ("TRACKER_SEARCH_BODY", "tracker_search_body"),
    ("TRACKER_DOWNLOAD_PATH", "tracker_download_path"),
    ("TRACKER_COOKIE_NAME", "tracker_cookie_name"),
    ("TRACKER_SESSION", "tracker_session"),
)
