"""Upstream fetching: target parsing, redirect following, host whitelist."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests

from .version import __version__

REDIRECT_STATUS = {301, 302, 303, 307, 308}

# The server token is a GitHub credential. Only send it to GitHub's own API and
# download hosts, and only over TLS: release assets redirect to a signed CDN URL
# that does not need it, and EXTRA_HOSTS must never receive it.
TOKEN_HOSTS = frozenset({"github.com", "api.github.com", "codeload.github.com"})


def should_send_token(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in TOKEN_HOSTS


class BadTarget(ValueError):
    """The requested path is not a usable upstream URL."""


class Forbidden(ValueError):
    """A host is outside the whitelist."""


def parse_target(path: str) -> str:
    """Accept '/https://host/path' and '/host/path'."""
    value = path.lstrip("/")
    if not value:
        raise BadTarget("empty target")
    if value.startswith(("http://", "https://")):
        if ".." in value:
            raise BadTarget("path traversal")
        return value
    if "/" in value:
        host = value.split("/", 1)[0]
        if "." in host and ".." not in host:
            return "https://" + value
    raise BadTarget("expected /https://host/path")


class Upstream:
    def __init__(
        self,
        allowed_hosts: frozenset[str],
        token: str = "",
        timeout: tuple[float, float] = (10.0, 60.0),
        max_redirects: int = 10,
    ):
        self.allowed_hosts = allowed_hosts
        self.token = token
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.session = requests.Session()
        self.session.trust_env = True  # honour HTTP(S)_PROXY / NO_PROXY

    def open(
        self,
        url: str,
        method: str = "GET",
        range_header: str | None = None,
    ) -> tuple[requests.Response, str]:
        """Fetch url, following redirects only into whitelisted hosts."""
        current = url
        for _ in range(self.max_redirects + 1):
            host = (urlsplit(current).hostname or "").lower()
            if host not in self.allowed_hosts:
                raise Forbidden(f"host not allowed: {host}")

            headers = {"User-Agent": f"ghproxy/{__version__}", "Accept": "*/*"}
            if range_header:
                headers["Range"] = range_header
            if self.token and should_send_token(current):
                headers["Authorization"] = f"Bearer {self.token}"

            response = self.session.request(
                method,
                current,
                headers=headers,
                stream=True,
                allow_redirects=False,
                timeout=self.timeout,
            )
            if response.status_code in REDIRECT_STATUS:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise Forbidden("redirect without Location")
                current = urljoin(current, location)
                continue
            return response, current
        raise Forbidden("too many redirects")
