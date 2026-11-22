"""HTTP opener policy shared by tracker traffic and its egress checks."""

import base64
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ExplicitProxy(urllib.request.ProxyHandler):
    def proxy_bypass(self, host):
        return False

    def proxy_open(self, req, proxy, type):
        # ProxyHandler.proxy_open calls a module-level bypass function. Apply its
        # HTTP(S) routing locally so unrelated urllib users retain their policy.
        if req.has_proxy() or self.proxy_bypass(req.host):
            return None
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.username and parsed.password:
            username = urllib.parse.unquote(parsed.username)
            password = urllib.parse.unquote(parsed.password)
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            req.add_header("Proxy-authorization", f"Basic {credentials}")
        req.set_proxy(urllib.parse.unquote(parsed.netloc.rsplit("@", 1)[-1]), parsed.scheme)
        if type == "http" and parsed.scheme == "https":
            # Dispatch the rewritten request to the HTTPS handler. If it fails,
            # propagate the failure to the caller.
            return self.parent.open(req, timeout=req.timeout)
        return None


def build_opener(proxy: str):
    mapping = {"http": proxy, "https": proxy} if proxy else {}
    opener = urllib.request.build_opener(_ExplicitProxy(mapping), _NoRedirect())
    opener.addheaders = []
    return opener
