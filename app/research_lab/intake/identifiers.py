"""Adding papers by identifier (card 0130, docs/design/LIBRARY-UI.md §3.3): a DOI, an arXiv id or an article URL
that carries a DOI, one per line. With an open-access PDF the paper goes through the normal PDF intake; without
one it becomes a bibliography record that waits under 待补原文 for its PDF.

download_pdf is shared with 推送's 一键导入 (feeds/importer.py), which used to own it.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import httpx

from ..config import get_paths
from . import extract

log = logging.getLogger("research_lab.intake.identifiers")

PDF_TIMEOUT = httpx.Timeout(45.0, connect=10.0)
CHUNK = 1 << 16
MAX_LINES = 50                     # one sheet's paste; more is almost certainly not a list of DOIs
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
# new-style arXiv ids (2007 on): 2310.01234, optionally with a version and the arxiv.org/abs|pdf/ prefix
ARXIV_RE = re.compile(r"^(?:arxiv:\s*|https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/)?(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?$", re.I)


@dataclass
class Ident:
    raw: str
    kind: str                      # doi · arxiv · unknown
    value: str = ""                # the DOI (lowercase) or the arXiv id

    @property
    def doi(self) -> str | None:
        if self.kind == "doi":
            return self.value
        if self.kind == "arxiv":
            return f"10.48550/arxiv.{self.value}"
        return None


@dataclass
class Added:
    """What one identifier became, for the import sheet's queue row."""
    state: str                     # ok (in the library with its PDF) · biblio (no PDF: 待补原文) · exists · dup · error
    note: str
    paper_id: str | None = None
    title: str | None = None
    pending: str | None = None     # a held PDF that looks like a paper already in the library (state dup)
    duplicate_of: str | None = None
    notes: list[str] = field(default_factory=list)


def parse_one(line: str) -> Ident:
    raw = line.strip()
    text = unquote(raw).strip().rstrip(".,;)")
    m = ARXIV_RE.match(text)
    if m:
        return Ident(raw, "arxiv", m.group(1))
    m = DOI_RE.search(text)
    if m:
        doi = m.group(1).rstrip(".,;)")
        doi = re.sub(r"(\.pdf|/full|/abstract|/epdf)$", "", doi, flags=re.I)
        return Ident(raw, "doi", doi.lower())
    return Ident(raw, "unknown")


def parse(text: str) -> list[Ident]:
    """One identifier per non-empty line, duplicates dropped, at most MAX_LINES."""
    seen: set[str] = set()
    out: list[Ident] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        ident = parse_one(line)
        key = ident.doi or ident.raw
        if key in seen:
            continue
        seen.add(key)
        out.append(ident)
    return out[:MAX_LINES]


def download_pdf(url: str, http: httpx.Client | None = None) -> Path:
    """Stream a PDF into the incoming folder. Raises net.FetchError or extract.RejectedFile."""
    from ..feeds import net
    if not net.public_url(url):
        raise net.FetchError("PDF URL or resolved address is not public.")
    incoming = get_paths().cache / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / f"feed-{uuid.uuid4().hex}.pdf"
    client = net.guard_client(http) if http is not None else net.new_client(timeout=PDF_TIMEOUT)
    try:
        with client.stream("GET", url, headers={"User-Agent": net.BROWSER_UA, "Accept": "application/pdf,*/*;q=0.8"}) as resp:
            if resp.status_code != 200:
                raise net.FetchError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "").lower()
            if "html" in ctype:
                raise extract.RejectedFile("不是 PDF 文件")
            size = 0
            with open(dest, "wb") as out:
                for chunk in resp.iter_bytes(CHUNK):
                    size += len(chunk)
                    if size > extract.MAX_BYTES:
                        raise extract.RejectedFile(f"文件超过 {extract.MAX_BYTES // (1024 * 1024)} MB")
                    out.write(chunk)
        extract.check_pdf(dest)
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise net.FetchError(net.error_reason(e)) from e
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    finally:
        if http is None:
            client.close()
    return dest


def oa_pdf_url(ident: Ident, http: httpx.Client | None = None) -> str | None:
    """arXiv serves every paper; anything else asks Unpaywall once (a no-op without a contact address)."""
    if ident.kind == "arxiv":
        return f"https://arxiv.org/pdf/{ident.value}"
    from ..feeds import net, papers as papers_mod
    client = http or net.new_client()
    try:
        found = papers_mod.unpaywall(client, ident.doi or "")
    finally:
        if http is None:
            client.close()
    return found["pdf_url"] if found and found.get("pdf_url") else None


def metadata_only(conn: sqlite3.Connection, doi: str, *, project: str | None, direction: str | None,
                  http: httpx.Client | None, title: str = "", authors: str = "", date: str = "", journal: str = "",
                  journal_name: str = "", tag: str = "", abstract: str | None = None, url: str = "",
                  hit: bool = False) -> str:
    """A bibliography record from the DOI's metadata (the radar's save path), filed under `project` only when
    one is given."""
    from . import bibliography
    item = bibliography.Item(index=-1, title=title or doi, authors=authors, date=date, url=url or f"https://doi.org/{doi}",
                       doi=doi, journal=journal, journal_name=journal_name, tag=tag, abstract=abstract, hit=hit,
                       direction=direction)
    return bibliography.save_item(conn, item, project, http=http, direction=direction)


def add(conn: sqlite3.Connection, ident: Ident, *, project: str | None = None, direction: str | None = None,
        http: httpx.Client | None = None) -> Added:
    """Bring one identifier into the library. Never raises for a bad line: the sheet shows the note."""
    from ..feeds import net
    from . import metadata, pipeline
    if ident.kind == "unknown":
        return Added("error", "认不出编号：现在支持 DOI、arXiv 编号和带 DOI 的文章网址")
    doi = ident.doi or ""
    row = conn.execute("SELECT id, title FROM papers WHERE lower(doi) = ?", (doi,)).fetchone()
    if row:
        has_file = conn.execute("SELECT 1 FROM paper_files WHERE paper_id = ?", (row["id"],)).fetchone() is not None
        return Added("exists", "已经在文献库里" + ("" if has_file else "，还没有原文"), row["id"], row["title"])
    pdf_note = "没有开放获取的 PDF，只建了书目，放在「待补原文」"
    url = oa_pdf_url(ident, http)
    if url:
        tmp: Path | None = None
        try:
            tmp = download_pdf(url, http)
            name = f"{doi.replace('/', '_')}.pdf"
            r = pipeline.ingest(conn, tmp, doi=doi, project=project, direction=direction, http=http, original_name=name)
            if r.pending:
                info = pipeline.pending_info(r.pending) or {}
                return Added("dup", "下载到的 PDF 可能和库里的一篇是同一篇", title=info.get("title"), pending=r.pending,
                             duplicate_of=r.duplicate_of)
            paper_id = r.paper_id or r.duplicate_of
            title = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()
            if not r.created:   # the very same file is already in the library
                return Added("exists", "已经在文献库里", paper_id, title[0] if title else None)
            return Added("ok", "已入库 · 找到开放获取 PDF，轻读概要排队中", paper_id, title[0] if title else None,
                         notes=list(r.notes or []))
        except (net.FetchError, extract.RejectedFile) as e:
            log.info("OA PDF for %s not usable: %s", doi, e)
            pdf_note = "开放获取的 PDF 下载不了，只建了书目，放在「待补原文」"
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    try:
        res = metadata.fetch_for_doi(conn, doi, http=http)
    except Exception:  # noqa: BLE001 - offline: say so rather than create an empty record
        res = None
    if not res or not res.chosen:
        return Added("error", "没找到这个编号的引用信息，检查一下是否写对")
    paper_id = metadata_only(conn, doi, project=project, direction=direction, http=http, title=res.chosen.title or doi)
    title = conn.execute("SELECT title FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return Added("biblio", pdf_note, paper_id, title[0] if title else None)
