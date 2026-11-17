"""qBittorrent WebUI client - moved to ``audiobind.clients.qbittorrent``; this name stays.

Everything the app and its tests have always imported from here (``QbitClient``, ``QbitError``,
``info_hash``, ``multipart``) is the same object as in the new module, so ``qbit.QbitError`` still
catches exactly what it caught before.
"""

from __future__ import annotations

from .clients.qbittorrent import (  # noqa: F401 - the public names, re-exported unchanged
    MAX_ANSWER,
    TIMEOUT,
    QbitClient,
    QbitError,
    _answer_hash,
    _skip,
    info_hash,
    multipart,
)
