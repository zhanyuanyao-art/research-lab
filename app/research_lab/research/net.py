"""Bounded public-source retrieval. Redirects and DNS answers are checked on every hop.

Pin the validated IP on the socket while keeping the original Host header and TLS hostname.
No cookies, model credentials, proxy environment, or local-network requests are forwarded.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from ..feeds import net as feed_net

MAX_BYTES = 8 * 1024 * 1024


class FetchError(ValueError):
    pass


def validate_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii')
        if any(c in url for c in ('\r', '\n', '\x00')) or host.endswith(('.local', '.localhost')) or host == 'localhost':
            raise ValueError()
    except (ValueError, UnicodeError):
        raise FetchError('Only public HTTP(S) sources on standard ports may be read.') from None
    return p, host


def public_addresses(host, port):
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(x).is_global for x in addresses):
        raise FetchError('This source resolves to a local or non-public address.')
    return addresses


def fetch(url):
    started = time.monotonic()
    for _ in range(feed_net.MAX_REDIRECTS + 1):
        p, host = validate_url(url)
        port = p.port or (443 if p.scheme == 'https' else 80)
        try:
            addresses = public_addresses(host, port)
        except OSError as exc:
            raise FetchError(f"Source DNS lookup failed ({type(exc).__name__}).") from None
        if not feed_net.public_url(url, addresses=addresses):
            raise FetchError("Source URL or resolved address is not public.")
        ip = addresses[0]
        conn = http.client.HTTPConnection(host, port, timeout=12)
        try:
            sock = socket.create_connection((ip, port), timeout=12)
            conn.sock = sock
            if p.scheme == 'https':
                conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            target = urlunsplit(('', '', p.path or '/', p.query, ''))
            conn.request('GET', target, headers={'User-Agent': 'Research-Lab/0.3 research source reader',
                                               'Accept': 'text/html,application/pdf,text/plain', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise FetchError('Redirect has no destination.')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError(f'Source returned HTTP {response.status}; no full text was read.')
            length = response.getheader('Content-Length')
            if length and int(length) > MAX_BYTES:
                raise FetchError('Source exceeds the 8 MB reading limit.')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise FetchError('Compressed response unsupported; no full text was read.')
            chunks, size = [], 0
            while chunk := response.read(65536):
                size += len(chunk)
                if size > MAX_BYTES or time.monotonic() - started > 45:
                    raise FetchError('Source exceeded the size or time limit.')
                chunks.append(chunk)
            return b''.join(chunks), response.getheader('Content-Type', ''), url
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, FetchError):
                raise
            raise FetchError(f'Source retrieval failed ({type(exc).__name__}); no full text was read.') from None
        finally:
            conn.close()
    raise FetchError(f'Source exceeded {feed_net.MAX_REDIRECTS} redirects.')
