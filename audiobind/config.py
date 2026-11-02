"""Runtime configuration, entirely from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "")
    return default if raw == "" else raw.lower() in TRUTHY


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    library_root: Path = Path("/library")
    output_root: Path = Path("/output")
    state_dir: Path = Path("/config")
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

    ignore_globs: list[str] = field(default_factory=lambda: ["@eaDir", "#recycle", "System Volume Information"])

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            library_root=Path(_env("LIBRARY_ROOT", "/library")),
            output_root=Path(_env("OUTPUT_ROOT", "/output")),
            state_dir=Path(_env("STATE_DIR", "/config")),
            audio_bitrate=_env("AUDIO_BITRATE", "128k"),
            audio_channels=_env_int("AUDIO_CHANNELS", 1),
            sample_rate=_env_int("SAMPLE_RATE", 44100),
            jobs=max(1, _env_int("JOBS", 1)),
            auto_convert=_env("AUTO_CONVERT", "high").lower(),
            max_runtime_delta=float(_env("MAX_RUNTIME_DELTA", "0.25") or 0.25),
            scan_interval_min=max(1, _env_int("SCAN_INTERVAL_MIN", 30)),
            dry_run=_env_bool("DRY_RUN", False),
            web_enabled=_env_bool("WEB_ENABLED", True),
            web_port=_env_int("WEB_PORT", 8080),
            notify_url=_env("NOTIFY_URL", ""),
            region=_env("REGION", "us"),
            nice=_env_int("NICE", 10),
        )

    def ensure_dirs(self) -> None:
        for path in (self.state_dir, self.work_dir, self.output_root):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except PermissionError as exc:
                raise RuntimeError(
                    f"cannot write to {path} - check that PUID/PGID own this path "
                    f"({exc})") from exc

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
