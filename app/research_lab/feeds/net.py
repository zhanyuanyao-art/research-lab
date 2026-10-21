"""Polite HTTP for the feed engine: one client, per-host spacing shared with the intake's metadata
lookups, the contact address as Crossref `mailto`, short timeouts, and errors returned as data."""
from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import APP_VERSION, env
from ..intake import metadata

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_REDIRECTS = 5
# Some publishers and feed hosts answer 403 to anything that does not look like a browser.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Safari/537.36 research-lab/" + APP_VERSION)
EXTRA_INTERVAL = {"api.unpaywall.org": 0.3}


class FetchError(Exception):
    """A source could not be read; the message is a short reason for the refresh summary."""


def contact() -> str | None:
    return env("CONTACT_EMAIL")


class UnsafeURL(httpx.RequestError):
    """Rejected before the request reaches the transport."""


def _check_request(request: httpx.Request) -> None:
    if not public_url(str(request.url)):
        raise UnsafeURL("External request blocked: URL or resolved address is not public.", request=request)


def guard_client(client: httpx.Client) -> httpx.Client:
    # Request hooks run again before every redirect, including on injected clients.
    if _check_request not in client.event_hooks["request"]:
        client.event_hooks["request"].append(_check_request)
    client.max_redirects = min(client.max_redirects, MAX_REDIRECTS)
    return client


def new_client(*, timeout: httpx.Timeout | float = TIMEOUT,
               transport: httpx.BaseTransport | None = None) -> httpx.Client:
    # trust_env stays on: Leo's Mac may reach BBC or the NYT only through the system proxy.
    return guard_client(httpx.Client(timeout=timeout, transport=transport, follow_redirects=True,
                                    max_redirects=MAX_REDIRECTS))


def error_reason(error: httpx.HTTPError) -> str:
    if isinstance(error, UnsafeURL):
        return str(error)
    if isinstance(error, httpx.TooManyRedirects):
        return f"External request exceeded {MAX_REDIRECTS} redirects."
    return type(error).__name__


def throttle(host: str) -> None:
    gap = metadata.MIN_INTERVAL.get(host, EXTRA_INTERVAL.get(host, 0.0))
    if not gap:
        return
    with metadata._throttle_lock:  # noqa: SLF001 - shared with the intake so both stay inside the polite rate
        wait = metadata._last_call.get(host, 0.0) + gap - time.monotonic()  # noqa: SLF001
        if wait > 0:
            time.sleep(wait)
        metadata._last_call[host] = time.monotonic()  # noqa: SLF001


def api_headers() -> dict[str, str]:
    mail = contact()
    return {"User-Agent": f"research-lab/{APP_VERSION}" + (f" (mailto:{mail})" if mail else ""), "Accept": "application/json"}


def get_json(http: httpx.Client, url: str, params: dict[str, Any] | None = None) -> Any:
    """GET a JSON API. Raises FetchError with a short reason; never caches (feed queries change daily)."""
    host = httpx.URL(url).host
    throttle(host)
    try:
        resp = guard_client(http).get(url, params=params or {}, headers=api_headers())
    except httpx.HTTPError as e:
        raise FetchError(error_reason(e)) from e
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as e:
        raise FetchError("invalid JSON") from e


def get_bytes(http: httpx.Client, url: str, *, accept: str = "*/*") -> bytes:
    try:
        resp = guard_client(http).get(url, headers={"User-Agent": BROWSER_UA, "Accept": accept})
    except httpx.HTTPError as e:
        raise FetchError(error_reason(e)) from e
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    return resp.content


def resolve_addresses(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def _host_port(url: str | None) -> tuple[str, int] | None:
    """(host, port) of an http(s) URL that names no local host, else None."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                or any(ord(c) < 32 or ord(c) == 127 for c in url)):
            return None
        host = parts.hostname.rstrip(".").lower().encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        return None
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return None
    return host, port


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def looks_public(url: str | None) -> bool:
    """The URL's own form: http(s), no local name, no private literal address. No DNS, so it suits links being
    stored (a lookup hiccup must not drop an open-access link for good); every request still passes public_url,
    which resolves."""
    hp = _host_port(url)
    if hp is None:
        return False
    try:
        return _public_ip(ipaddress.ip_address(hp[0]))
    except ValueError:
        return True


def public_url(url: str | None, *, addresses: list[str] | None = None) -> bool:
    """For a request about to be made: reject the URL if any DNS answer is non-public, not only the first."""
    hp = _host_port(url)
    if hp is None:
        return False
    host, port = hp
    try:
        try:
            ips = [ipaddress.ip_address(host)]
        except ValueError:
            # A pinned transport can reuse the answers it will actually connect to.
            ips = [ipaddress.ip_address(ip) for ip in
                   (addresses if addresses is not None else resolve_addresses(host, port))]
        return bool(ips) and all(_public_ip(ip) for ip in ips)
    except (OSError, ValueError):
        return False
