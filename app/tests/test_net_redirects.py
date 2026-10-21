"""All public download paths reject unsafe redirect destinations before sending."""
from __future__ import annotations

import io
import ipaddress
import socket

import httpx
import pytest

from research_lab.feeds import importer, net
from research_lab.intake.metadata import MetadataClient
from research_lab.research import net as research_net

PATHS = ("client", "json", "bytes", "pdf", "metadata", "research")


@pytest.fixture(autouse=True)
def dns(monkeypatch, public_dns):
    answers = {}

    def resolve(host, port, **kwargs):
        value = answers.get(host)
        if isinstance(value, Exception):
            raise value
        if value is None:
            try:
                value = [str(ipaddress.ip_address(host))]
            except ValueError:
                value = ["93.184.216.34"]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, "", (ip, port)) for ip in value]

    monkeypatch.setattr(net, "resolve_addresses", public_dns)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return answers


def read_source(path, handler, monkeypatch, conn):
    url = "http://source.example/start"
    transport = httpx.MockTransport(handler)
    if path == "research":
        class Response:
            def __init__(self, response):
                self.status = response.status_code
                self.getheader = response.headers.get
                self.read = io.BytesIO(response.content).read

        class Connection:
            def __init__(self, host, port, **kwargs):
                self.host = host

            def request(self, method, target, headers):
                self.request_ = httpx.Request(method, f"http://{self.host}{target}", headers=headers)

            def getresponse(self):
                return Response(transport.handle_request(self.request_))

            def close(self):
                pass

        monkeypatch.setattr(research_net.http.client, "HTTPConnection", Connection)
        monkeypatch.setattr(research_net.socket, "create_connection", lambda *a, **k: object())
        return research_net.fetch(url)
    with (net.new_client(transport=transport) if path == "client" else
          httpx.Client(transport=transport, follow_redirects=True)) as http:
        if path == "client":
            return http.get(url)
        if path == "json":
            return net.get_json(http, url)
        if path == "bytes":
            return net.get_bytes(http, url)
        if path == "pdf":
            return importer.download_pdf(url, http)
        with MetadataClient(conn, http, refresh=True) as metadata:
            result = metadata.get_json(url)
            if metadata.errors:
                assert result == (None, None)
                raise net.FetchError("; ".join(metadata.errors))
            return result


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("destination", [
    "http://127.0.0.1/secret", "http://10.0.0.1/secret", "http://localhost/secret",
    "http://localhost./secret", "http://[::1]/secret", "http://private.example/secret",
])
def test_unsafe_redirect_never_reaches_transport(path, destination, dns, monkeypatch, conn):
    dns["private.example"] = ["192.168.1.8"]
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": destination})

    with pytest.raises((net.FetchError, net.UnsafeURL, research_net.FetchError), match="public"):
        read_source(path, handler, monkeypatch, conn)
    assert calls == ["http://source.example/start"]
    from research_lab.config import get_paths
    assert not list((get_paths().cache / "incoming").glob("feed-*.pdf"))


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("answers", [["10.0.0.7"], ["93.184.216.34", "127.0.0.1"], ["fd00::1"], []])
def test_dns_answers_must_all_be_public(path, answers, dns, monkeypatch, conn):
    dns["source.example"] = answers
    calls = []
    with pytest.raises((net.FetchError, net.UnsafeURL, research_net.FetchError), match="public"):
        read_source(path, lambda req: calls.append(req) or httpx.Response(200), monkeypatch, conn)
    assert calls == []


@pytest.mark.parametrize("path", PATHS)
def test_dns_failure_is_a_clear_fetch_failure(path, dns, monkeypatch, conn):
    dns["source.example"] = socket.gaierror("lookup failed")
    with pytest.raises((net.FetchError, net.UnsafeURL, research_net.FetchError), match="public|DNS"):
        read_source(path, lambda req: pytest.fail("must not send"), monkeypatch, conn)


@pytest.mark.parametrize("path", PATHS)
def test_five_public_redirects_succeed(path, monkeypatch, conn, sample_pdf):
    calls = []
    body = sample_pdf.read_bytes() if path == "pdf" else b'{"ok":true}'

    def handler(request):
        calls.append(str(request.url))
        if len(calls) <= 5:
            target = "http://other.example/hop" if len(calls) == 1 else f"/hop{len(calls)}"
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=body, headers={"Content-Type": "application/pdf"})

    result = read_source(path, handler, monkeypatch, conn)
    assert len(calls) == 6 and calls[-1] == "http://other.example/hop5"
    if path == "pdf":
        assert result.read_bytes() == body
    elif path == "research":
        assert result == (body, "application/pdf", calls[-1])
    elif path == "json":
        assert result == {"ok": True}
    elif path == "metadata":
        assert result == (200, {"ok": True})
    elif path == "client":
        assert result.content == body and len(result.history) == 5
    else:
        assert result == body


@pytest.mark.parametrize("path", PATHS)
def test_sixth_redirect_is_rejected(path, monkeypatch, conn):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/again"})

    with pytest.raises((net.FetchError, httpx.TooManyRedirects, research_net.FetchError), match="redirects"):
        read_source(path, handler, monkeypatch, conn)
    assert len(calls) == 6


def test_guard_is_idempotent_and_preserves_hooks():
    seen = []
    with httpx.Client(event_hooks={"request": [lambda req: seen.append(str(req.url))]},
                      transport=httpx.MockTransport(lambda req: httpx.Response(200))) as http:
        net.guard_client(http)
        net.guard_client(http)
        http.get("http://source.example/")
        assert len(http.event_hooks["request"]) == 2 and len(seen) == 1
        assert http.max_redirects == 5


def test_autoimport_treats_blocked_redirect_as_missing_pdf(conn, monkeypatch):
    from research_lab.feeds import papers, store
    item_id = papers.paper_key("10.1234/blocked")
    papers.insert(conn, {"id": item_id, "doi": "10.1234/blocked", "title": "Redirect fixture",
                        "journal": "TEST", "journal_name": "Test", "pool": "hunt", "authors": [],
                        "published": "2026-10-09", "oa_pdf_url": "http://source.example/a.pdf", "is_oa": 1})
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        result = importer.import_item(conn, item_id, http=http, pdf_only=True)
    assert not result.paper_id and not result.with_pdf and result.note == importer.PDF_FAILED
    assert not store.get(conn, "papers", item_id)["imported"]
    assert conn.execute("SELECT count(*) FROM papers").fetchone()[0] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("url,ok", [
    ("https://oa.example.org/a.pdf", True), ("http://93.184.216.34/a.pdf", True),
    ("http://127.0.0.1/a.pdf", False), ("http://10.0.0.7/a.pdf", False), ("http://[fd00::1]/a.pdf", False),
    ("http://localhost/a.pdf", False), ("http://printer.local/a.pdf", False), ("ftp://oa.example.org/a.pdf", False),
    ("https://user:pw@oa.example.org/a.pdf", False), (None, False)])
def test_looks_public_reads_the_url_alone(url, ok, dns):
    dns["oa.example.org"] = socket.gaierror("lookup failed")
    assert net.looks_public(url) is ok


def test_a_dns_outage_keeps_the_stored_link_but_requests_still_resolve(dns, monkeypatch, conn):
    """Storing a link reads only its form; fetching it resolves, so a host that points inward is refused."""
    from research_lab.feeds import papers, store
    pdf = "https://oa.example.org/a.pdf"
    dns["oa.example.org"] = socket.gaierror("lookup failed")
    monkeypatch.setattr(net, "contact", lambda: "lab@example.org")
    unpaywall = httpx.MockTransport(lambda request: httpx.Response(
        200, json={"is_oa": True, "best_oa_location": {"url_for_pdf": pdf}}))
    with httpx.Client(transport=unpaywall) as http:
        assert papers.unpaywall(http, "10.1234/dns")["pdf_url"] == pdf
    assert not net.public_url(pdf)

    item_id = papers.paper_key("10.1234/dns")
    papers.insert(conn, {"id": item_id, "doi": "10.1234/dns", "title": "DNS fixture", "journal": "TEST",
                        "journal_name": "Test", "pool": "hunt", "authors": [], "published": "2026-10-09",
                        "oa_pdf_url": pdf, "is_oa": 1})
    dns["oa.example.org"] = ["10.0.0.7"]
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))) as http:
        result = importer.import_item(conn, item_id, http=http, pdf_only=True)
    assert not result.paper_id and result.note == importer.PDF_FAILED
    assert calls == []
    assert store.get(conn, "papers", item_id)["oa_pdf_url"] == pdf


def test_a_blocked_news_source_only_lands_in_failed(dns, conn):
    from research_lab.feeds import news
    from research_lab.feeds.sources import NewsFeed
    dns["inward.example"] = ["10.0.0.7"]
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, text='<?xml version="1.0"?><rss version="2.0"><channel><title>x</title><item>'
                                        '<title>Talks resume in Jakarta</title><link>https://open.example/world/talks</link>'
                                        '</item></channel></rss>', headers={"content-type": "application/rss+xml"})

    feeds = [NewsFeed("Open", "https://open.example/rss"), NewsFeed("Inward", "https://inward.example/rss")]
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        summary = news.refresh_news(conn, http, days=2, feeds=feeds)
    assert [f["source"] for f in summary["failed"]] == ["Inward"]
    assert summary["new"] == 1 and hosts == ["open.example"]
