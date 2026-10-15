"""Fetch a public web page or file for extraction, refusing internal network addresses."""

import ipaddress
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 25
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (compatible; GrantTracker/1.0; +opportunity import)"


class FetchError(Exception):
    pass


@dataclass
class Fetched:
    url: str
    content: bytes
    content_type: str


def _check_host(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("Only http(s) links can be imported.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise FetchError(f"Couldn't find the site {parsed.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError("That address points to a private or internal network and can't be imported.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, max_bytes=MAX_BYTES, timeout=TIMEOUT):
    """GET a URL, following redirects manually so every hop is checked."""
    for _ in range(MAX_REDIRECTS + 1):
        _check_host(url)
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/pdf,text/plain;q=0.9,*/*;q=0.5",
        })
        try:
            response = _opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                continue
            raise FetchError(f"The site returned an error ({exc.code}).")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError(f"Couldn't reach the site ({getattr(exc, 'reason', exc)}).")
        with response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError("That file is too large to import.")
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return Fetched(url=response.geturl() or url, content=data, content_type=content_type)
    raise FetchError("Too many redirects.")


def post_json(url, payload, timeout=TIMEOUT):
    """POST JSON to a public API (used for Grants.gov)."""
    import json

    _check_host(url)
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(MAX_BYTES).decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise FetchError(f"The API returned an error ({exc.code}).")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise FetchError(f"API request failed ({getattr(exc, 'reason', exc)}).")
