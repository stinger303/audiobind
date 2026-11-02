"""Shared run controls, driven by the status page.

The daemon asks this object whether it may start another book, and the web handlers flip it.
Nothing here touches the filesystem - persistence lives in State.set_meta("paused").
"""
from __future__ import annotations

import threading


class Control:
    def __init__(self) -> None:
        self._scan = threading.Event()
        self._paused = threading.Event()

    # ---------------------------------------------------------------- pause
    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()
        self.request_scan()          # resuming should not wait for the next interval

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
        return {"paused": self.paused, "scan_pending": self.scan_requested()}


CONTROL = Control()
