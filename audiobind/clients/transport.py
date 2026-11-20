"""The one HTTP request helper the new adapters share (qBittorrent keeps its own, unchanged).

Every network failure becomes ``ClientError(kind=UNAVAILABLE)`` with the client's name and
address in the message - never the password - so no adapter can leak an ``OSError`` or an
``http.client`` exception into the watcher.
"""

from __future__ import annotations

import base64
import http.client
import urllib.error
import urllib.parse
import urllib.request

from .base import ERROR, UNAVAILABLE, ClientError

TIMEOUT = 20.0
#: a whole-category listing can be large on a big client; anything past this is cut and fails to parse
MAX_ANSWER = 8 * 1024 * 1024


def http_url(url: str, setting: str) -> tuple[str, str]:
    """(url without a trailing /, its origin) or ClientError naming the setting."""
    url = str(url or "").strip().rstrip("/")
    if not url:
        raise ClientError(f"{setting} is not set", kind=ERROR)
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username is not None:
        raise ClientError(f"{setting} is not an http(s) URL (with no login in it): {url!r}", kind=ERROR)
    return url, f"{parts.scheme}://{parts.netloc}"


def basic_auth(user: str, password: str) -> dict:
    """An Authorization header for HTTP basic auth, or {} when neither is set."""
    if not user and not password:
        return {}
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": "Basic " + token}


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1  # unparseable: matches nothing real
    return scheme, (parts.hostname or "").lower(), port or {"http": 80, "https": 443}.get(scheme)


class GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """SEC-04: urllib copies every header but Content-Type/-Length onto a redirect, to any
    host. A request carrying Authorization follows a redirect only within its own origin
    (scheme, host and port - so never https -> http); otherwise the 3xx is the answer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.has_header("Authorization") and _origin(newurl) != _origin(req.full_url):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_opener(*handlers):
    """build_opener() with GuardedRedirects in place of the default redirect handler."""
    return urllib.request.build_opener(GuardedRedirects, *handlers)


def http_post(opener, url: str, body: bytes, headers: dict, name: str) -> tuple[int, dict, bytes]:
    """POST ``body``: (status, lower-cased response headers, answer). An HTTP error status is an
    answer, not an exception; not reaching the server at all is ClientError(UNAVAILABLE)."""
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_ANSWER)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read(MAX_ANSWER)
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        where = urllib.parse.urlsplit(url).netloc
        raise ClientError(f"{name} at {where} could not be reached ({type(exc).__name__})",
                          kind=UNAVAILABLE) from None
