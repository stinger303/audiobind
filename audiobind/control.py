"""Shared run controls, driven by the status page.

The daemon asks this object whether it may start another book, whether writes are allowed, and
the web handlers flip those switches at runtime. Nothing here touches the filesystem -
persistence lives in State.set_meta() ("paused", "dry_run"), so a restart keeps your choices.
"""
from __future__ import annotations

import threading

from .config import CONFIG


class Control:
    def __init__(self) -> None:
        self._scan = threading.Event()
        self._paused = threading.Event()
        #: None = use the DRY_RUN env default; True/False = the user chose in the web UI
        self._dry_run: bool | None = None

    # ---------------------------------------------------------------- pause
    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self.request_scan()          # resuming should not wait for the next interval

    # ---------------------------------------------------------------- dry run
    @property
    def dry_run(self) -> bool:
        """Effective dry-run state: the UI override wins over the DRY_RUN environment."""
        return CONFIG.dry_run if self._dry_run is None else self._dry_run

    def set_dry_run(self, value: bool) -> None:
        self._dry_run = bool(value)

    @property
    def dry_run_source(self) -> str:
        return "default" if self._dry_run is None else "set in UI"

    # ---------------------------------------------------------------- scan
    def request_scan(self) -> None:
        self._scan.set()

    def scan_requested(self) -> bool:
        return self._scan.is_set()

    def clear_scan(self) -> None:
        self._scan.clear()

    def wait(self, timeout: float) -> bool:
        """Sleep until the timeout or until someone clicks 'Scan now'."""
        return self._scan.wait(timeout)

    # ---------------------------------------------------------------- helpers
    def describe(self) -> dict:
        return {
            "paused": self.paused,
            "scan_pending": self.scan_requested(),
            "dry_run": self.dry_run,
            "dry_run_source": self.dry_run_source,
        }


CONTROL = Control()
