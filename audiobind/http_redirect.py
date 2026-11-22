"""Origin checks for HTTP clients that accept redirects."""

import logging
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler

log = logging.getLogger("audiobind")
DEFAULT_PORT = {"http": 80, "https": 443}


class CrossOriginRedirect(HTTPError):
    """The redirect would leave the request's origin."""


def origin(url: str) -> tuple:
    parts = urlsplit(url)
    port = parts.port if parts.port is not None else DEFAULT_PORT.get(parts.scheme)
    return parts.scheme, parts.hostname, port


class SameOriginRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.check_origin(req, fp, code, headers, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location", headers.get("uri"))
        if location is not None:
            self.check_origin(req, fp, code, headers, location)
        return super().http_error_302(req, fp, code, msg, headers)

    @staticmethod
    def check_origin(req, fp, code, headers, location):
        try:
            allowed = origin(req.full_url) == origin(urljoin(req.full_url, location))
        except ValueError:
            allowed = False
        if not allowed:
            reason = "blocked redirect outside the request origin (scheme, host or port changed)"
            log.warning("%s (HTTP %d)", reason, code)
            raise CrossOriginRedirect(req.full_url, code, reason, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
