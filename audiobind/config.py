"""Runtime configuration, entirely from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _proxy_value(raw: str) -> str:
    """MAM_PROXY, checked at load: ``http://host:port`` or ``https://host:port`` (or empty =
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
            "MAM_PROXY is not valid: use http://host:port or https://host:port "
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


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    return default if raw == "" else raw.lower() in TRUTHY


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
    audio_channels: int = 1
    sample_rate: int = 44100
    jobs: int = 1

    #: which confidence tiers convert without asking ("high", "medium" or "off")
    auto_convert: str = "high"
    #: reject a match when the file runtime differs from the catalog by more than this
    max_runtime_delta: float = 0.25

    scan_interval_min: int = 30
    dry_run: bool = False

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
    #: manual Retry / Convert anyway / Use this ASIN / Re-check actions allowed at once
    manual_workers: int = 2
    #: deprecated shared secret for the web UI and API: only signs in while no account exists
    #: in the state DB (and skips first-run /setup); the account takes precedence once created
    auth_token: str = ""

    #: Phase 3 wishlist watcher (MAM auto-acquire). Empty MAM_ID = the watcher is off: no
    #: thread, no network. The session cookie and the qBittorrent password are secrets - they
    #: are kept out of repr() so a log line or traceback can never print them.
    mam_id: str = field(default="", repr=False)
    watch_interval_min: int = 60
    watch_max_per_cycle: int = 5
    qbit_url: str = "http://localhost:8083"
    qbit_user: str = ""
    qbit_pass: str = field(default="", repr=False)
    #: how MAM requests leave the container. ``mam_user_agent`` "" = no User-Agent override is
    #: sent; ``mam_proxy`` "" = direct (it may hold credentials, so never in repr());
    #: ``mam_home_wan_ip`` set = the egress guard is armed and refuses to run from that IP.
    mam_user_agent: str = ""
    mam_proxy: str = field(default="", repr=False)
    mam_home_wan_ip: str = ""
    #: optional allowlist: set = MAM must see exactly this address, or the watcher refuses to
    #: contact MAM. "" = this check is off. Also editable in Settings > Watcher.
    mam_expected_egress_ip: str = ""
    #: MyAnonamouse VIP membership (not detectable from the API): False = VIP-only torrents are
    #: skipped. Also editable in Settings > Watcher.
    mam_vip: bool = False

    ignore_globs: list[str] = field(
        default_factory=lambda: ["@eaDir", "#recycle", "System Volume Information"]
    )

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            library_root=Path(_env("LIBRARY_ROOT", "/library")),
            output_root=Path(_env("OUTPUT_ROOT", "/output")),
            state_dir=Path(_env("STATE_DIR", "/config")),
            # unset = the default file under STATE_DIR; set but empty = file log off
            log_file=_env("LOG_FILE", str(Path(_env("STATE_DIR", "/config")) / "audiobind.log")),
            audio_bitrate=_env("AUDIO_BITRATE", "128k"),
            audio_channels=_env_int("AUDIO_CHANNELS", 1),
            sample_rate=_env_int("SAMPLE_RATE", 44100),
            jobs=max(1, _env_int("JOBS", 1)),
            auto_convert=_env("AUTO_CONVERT", "high").lower(),
            max_runtime_delta=_env_float("MAX_RUNTIME_DELTA", 0.25),
            scan_interval_min=max(1, _env_int("SCAN_INTERVAL_MIN", 30)),
            dry_run=_env_bool("DRY_RUN", False),
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
            auth_token=_env("AUTH_TOKEN", ""),
            manual_workers=max(1, _env_int("MANUAL_WORKERS", 2)),
            mam_id=_env("MAM_ID", ""),
            watch_interval_min=_env_int("WATCH_INTERVAL_MIN", 60),
            watch_max_per_cycle=_env_int("WATCH_MAX_PER_CYCLE", 5),
            qbit_url=_env("QBIT_URL", "http://localhost:8083").rstrip("/") or "http://localhost:8083",
            qbit_user=_env("QBIT_USER", ""),
            qbit_pass=os.environ.get("QBIT_PASS", ""),  # a password keeps its spaces
            mam_user_agent=_env("MAM_USER_AGENT", ""),
            mam_proxy=_proxy_value(_env("MAM_PROXY", "")),
            mam_home_wan_ip=_ip_value("MAM_HOME_WAN_IP", _env("MAM_HOME_WAN_IP", "")),
            mam_expected_egress_ip=_ip_value("MAM_EXPECTED_EGRESS_IP", _env("MAM_EXPECTED_EGRESS_IP", "")),
            mam_vip=_env_bool("MAM_VIP", False),
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

SETTING_KEYS = ("auto_convert", "audio_bitrate", "runtime_delta", "scan_interval", "nice", "region", "expected_egress", "vip")

SETTING_MAP: dict[str, str] = {
    "auto_convert": "auto_convert",
    "audio_bitrate": "audio_bitrate",
    "runtime_delta": "max_runtime_delta",
    "scan_interval": "scan_interval_min",
    "nice": "nice",
    "region": "region",
    "expected_egress": "mam_expected_egress_ip",
    "vip": "mam_vip",
}

SETTING_TYPES: dict[str, type] = {
    "auto_convert": str,
    "audio_bitrate": str,
    "runtime_delta": int,  # percentage (5-50), divided by 100 at use
    "scan_interval": int,
    "nice": int,
    "region": str,
    "expected_egress": str,  # free text, validated by valid_ip_or_empty (not a choice, not a number)
    "vip": str,  # "YES" / "NO"; effective_val turns it into a bool
}

#: the values the settings page offers; an environment value outside these is added to the
#: choices (and accepted on save) so the page can always show and keep what is really running
SETTING_CHOICES: dict[str, tuple[str, ...]] = {
    "auto_convert": ("HIGH", "MEDIUM", "OFF"),
    "audio_bitrate": ("64k", "128k", "192k", "256k"),
    "region": ("us", "uk", "ca", "au", "de"),
    "vip": ("NO", "YES"),
}

#: (min, max) for the numeric settings, widened to include the environment value
SETTING_BOUNDS: dict[str, tuple[int, int]] = {
    "runtime_delta": (5, 50),
    "scan_interval": (1, 1440),
    "nice": (0, 19),
}


def setting_env_defaults() -> dict[str, object]:
    """What each setting is when nothing is saved in the UI - read live from CONFIG.

    Shaped like the settings page fields: the tier upper-case to match the select's
    options, the runtime tolerance as a whole percentage.
    """
    return {
        "auto_convert": str(CONFIG.auto_convert or "high").upper(),
        "audio_bitrate": str(CONFIG.audio_bitrate).lower(),
        "runtime_delta": int(round(CONFIG.max_runtime_delta * 100)),
        "scan_interval": int(CONFIG.scan_interval_min),
        "nice": int(CONFIG.nice),
        "region": str(CONFIG.region).lower(),
        "expected_egress": str(CONFIG.mam_expected_egress_ip or ""),
        "vip": "YES" if CONFIG.mam_vip else "NO",
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


def setting_env(key: str):
    """Return the env default for a setting key (live from CONFIG, never a hardcoded copy)."""
    return setting_env_defaults().get(key)


def _config_to_setting(name: str) -> str | None:
    for sk, cn in SETTING_MAP.items():
        if cn == name:
            return sk
    return None


#: politeness bounds for the wishlist watcher. A value outside them is pulled back in: MAM
#: tolerates automation only on its listed endpoints and a banned account is unrecoverable,
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


def watch_interval_min() -> int:
    """Minutes between watcher cycles (WATCH_INTERVAL_MIN through the effective getter)."""
    return _clamp(effective_val("watch_interval_min"), WATCH_INTERVAL_BOUNDS, 60)


def watch_max_per_cycle() -> int:
    """Wishlist rows one watcher cycle may search at most (WATCH_MAX_PER_CYCLE)."""
    return _clamp(effective_val("watch_max_per_cycle"), WATCH_MAX_PER_CYCLE_BOUNDS, 5)


def effective_auto_tiers() -> set[str]:
    """Return the auto-convert tiers using the effective override or env default."""
    tier = str(effective_val("auto_convert") or "high").lower()
    mapping = {"high": {"HIGH"}, "medium": {"HIGH", "MEDIUM"}, "off": set()}
    return mapping.get(tier, {"HIGH"})
