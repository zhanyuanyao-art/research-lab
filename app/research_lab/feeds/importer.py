"""一键导入: a feed paper becomes a library paper.

With an open-access PDF the file is downloaded (size and type checked like an upload) and goes through
the normal PDF intake, so pages, the Chinese summary and translation work. Without one the paper is
created from its DOI metadata (the radar's save path) and can get its PDF later. The feed item keeps
the paper id either way.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_paths
from ..intake import extract, identifiers, pipeline
from ..intake.identifiers import PDF_TIMEOUT, download_pdf  # noqa: F401 - shared with the import sheet (card 0130)
from . import net, papers as papers_mod, store

log = logging.getLogger("research_lab.feeds.importer")

NO_PDF = "没有开放获取的 PDF，可稍后上传"
PDF_FAILED = "开放获取的 PDF 暂时下载不了，可稍后上传"


@dataclass
class ImportResult:
    paper_id: str
    with_pdf: bool
    created: bool
    note: str | None = None


def _pdf_url(conn: sqlite3.Connection, item: dict, http: httpx.Client | None) -> str | None:
    """The stored OA link, else one Unpaywall lookup now (history items were never looked up)."""
    if item.get("oa_pdf_url") and net.looks_public(item["oa_pdf_url"]):
        return item["oa_pdf_url"]
    if not item.get("doi") or item.get("is_oa") == 0:
        return None
    client = http or net.new_client()
    try:
        found = papers_mod.unpaywall(client, item["doi"])
    finally:
        if http is None:
            client.close()
    if not found:
        return None
    conn.execute("UPDATE feed_papers SET is_oa = ?, oa_pdf_url = ?, oa_license = COALESCE(?, oa_license) WHERE id = ?",
                 (1 if found["is_oa"] else 0, found["pdf_url"], found["license"], item["id"]))
    return found["pdf_url"]


def _has_file(conn: sqlite3.Connection, paper_id: str) -> bool:
    return conn.execute("SELECT 1 FROM paper_files WHERE paper_id = ?", (paper_id,)).fetchone() is not None


def _attach_primary(conn: sqlite3.Connection, paper_id: str, path: Path, *, ai: bool = True) -> None:
    """A metadata-only library paper gets the downloaded PDF as its file, then a summary job (unless `ai` is off)."""
    sha = extract.sha256_file(path)
    ex = extract.extract(path)
    conn.execute("BEGIN")
    try:
        pipeline._insert_file(conn, paper_id, path, sha, ex, path.name, True)  # noqa: SLF001 - same intake step as an upload
        if ex.language:
            conn.execute("UPDATE papers SET language = ? WHERE id = ?", (ex.language, paper_id))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if ai:
        pipeline.enqueue_summary(conn, paper_id)


def _metadata_only(conn: sqlite3.Connection, item: dict, http: httpx.Client | None) -> str:
    """A bibliography record for a feed paper without a usable PDF. It joins no 课题: importing no longer files
    papers under the current project (card 0130)."""
    return identifiers.metadata_only(conn, item.get("doi") or "", project=None, direction=item.get("direction"), http=http,
                                     title=item["title"], authors=", ".join(item.get("authors") or []),
                                     date=item.get("published") or "", url=item.get("url") or "",
                                     journal=item.get("journal") or "", journal_name=item.get("journal_name") or "",
                                     tag=item.get("journal_column") or "", abstract=item.get("abstract"),
                                     hit=bool(item.get("radar")))


def _set_direction(conn: sqlite3.Connection, paper_id: str, slug: str | None) -> None:
    if slug:
        conn.execute("UPDATE papers SET direction = ? WHERE id = ? AND direction IS NULL", (slug, paper_id))


def import_item(conn: sqlite3.Connection, item_id: str, *, http: httpx.Client | None = None,
                pdf_only: bool = False, ai: bool = True) -> ImportResult:
    """Raises LookupError for an unknown item. `http` serves metadata lookups and the PDF download (tests
    pass a client with a MockTransport). `pdf_only` (自动入库) creates nothing when no PDF could be had:
    paper_id is then empty and the item stays as it was. `ai` off stores the PDF and its pages but queues no
    summary or translation: the reader asks for the summary when the paper is first opened (card 0155)."""
    item = store.get(conn, "papers", item_id)
    if item is None:
        raise LookupError(item_id)
    if item["imported"]:
        return ImportResult(item["paper_id"], _has_file(conn, item["paper_id"]), False)
    existing = None
    if item.get("doi"):
        row = conn.execute("SELECT id FROM papers WHERE doi = ?", (item["doi"],)).fetchone()
        existing = row["id"] if row else None
    if existing and _has_file(conn, existing):
        store.mark_imported(conn, item_id, existing)
        return ImportResult(existing, True, False)

    url = _pdf_url(conn, item, http)
    note = None if url else NO_PDF
    tmp: Path | None = None
    if url:
        try:
            tmp = download_pdf(url, http)
        except (net.FetchError, extract.RejectedFile) as e:
            log.info("OA PDF for %s not usable: %s", item.get("doi"), e)
            note = PDF_FAILED
    try:
        if tmp is not None and existing:
            _attach_primary(conn, existing, tmp, ai=ai)
            _set_direction(conn, existing, item.get("direction"))
            store.mark_imported(conn, item_id, existing)
            return ImportResult(existing, True, False)
        if tmp is not None:
            name = f"{(item.get('doi') or item_id).replace('/', '_')}.pdf"
            r = pipeline.ingest(conn, tmp, doi=item.get("doi"), http=http, original_name=name, ai=ai)
            if r.pending and pdf_only:
                # a similar title is already in the library: whether it is the same paper is Leo's call, and
                # nobody is there to ask on the automatic path, so nothing is attached and the item stays as is
                pipeline._drop_pending(r.pending)  # noqa: SLF001 - the held copy of a file nobody will be asked about
                log.info("auto-import of %s skipped: a similar title is already in the library", item.get("doi"))
                return ImportResult("", False, False, "文献库里有标题相近的文献，没有自动入库")
            if r.pending:   # a similar title is already in the library: keep this file with that paper
                pipeline.attach_pending(conn, r.pending, r.duplicate_of)
            paper_id = r.paper_id or r.duplicate_of
            if paper_id:
                _set_direction(conn, paper_id, item.get("direction"))
                store.mark_imported(conn, item_id, paper_id)
                return ImportResult(paper_id, True, r.created)
            note = PDF_FAILED
    except extract.RejectedFile as e:
        log.info("OA PDF for %s rejected: %s", item.get("doi"), e)
        note = PDF_FAILED
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    if pdf_only:
        return ImportResult("", False, False, note)
    paper_id = existing or _metadata_only(conn, item, http)
    _set_direction(conn, paper_id, item.get("direction"))
    store.mark_imported(conn, item_id, paper_id)
    return ImportResult(paper_id, False, existing is None, note)


# ---------------------------------------------------------------- 自动入库 (cards 0073, 0156)
# Leo, 10-08: 「是否可以直接获取（if open source）载入论文」; 10-13: 「能下载的论文其实都可以直接下载了」 and 「跟着刷新走」. Right
# after Leo's own refresh (the carousel's 刷新今天, the 推送 page's 刷新), the articles that arrived that day with a PDF to
# download go into the library: today's picks first, then the newest, at most AUTO_CAP a run. Nothing runs on a page
# view, on the background timer or on the 8 o'clock refresh, and nothing older is swept up: the backlog stays for
# 「导入文献库」. Book reviews are left to Leo. No AI on the way in (「token 太多了，我点了你再读写概要」): the reader queues
# the summary when the paper is first opened (routes/papers.paper_page).
AUTO_KEY = "feeds.autoimport_oa"          # on by default; the assistant can turn it off (SETTINGS-AGENT.md)
TRIED_KEY = "feeds.autoimport_tried:"     # + Beijing date: ids already attempted that day, so a dead link is tried once
AUTO_CAP = 15
DOWNLOADABLE = ("kind = 'article' AND status != 'dismissed' AND paper_id IS NULL AND is_oa = 1 "
                "AND COALESCE(oa_pdf_url, '') != ''")
_auto_lock = threading.Lock()
_auto_thread: threading.Thread | None = None


def autoimport_enabled(conn: sqlite3.Connection) -> bool:
    from ..tunables import tunable
    return tunable(conn, AUTO_KEY)


def _day_start_utc(now) -> str:
    """Midnight of `now`'s Beijing day, as the UTC ISO text feed_papers.first_seen is written in (now_iso)."""
    from datetime import datetime, time as dtime, timezone
    from . import brief
    return datetime.combine(now.date(), dtime.min, tzinfo=brief.TZ).astimezone(timezone.utc).isoformat()


def autoimport_pending(conn: sqlite3.Connection, limit: int = AUTO_CAP, *, now=None) -> list[str]:
    """The downloadable articles that arrived today (Beijing day), not tried today: today's picks first in their
    order, then the newest of the rest; at most `limit`."""
    from ..db.connection import get_setting
    from . import brief, digest
    if not autoimport_enabled(conn):
        return []
    now = now or brief.now_bj()
    tried = set(get_setting(conn, TRIED_KEY + now.date().isoformat(), None) or [])
    fresh = [r["id"] for r in conn.execute(f"SELECT id FROM feed_papers WHERE {DOWNLOADABLE} AND first_seen >= ? "
                                           "ORDER BY first_seen DESC, id", (_day_start_utc(now),)) if r["id"] not in tried]
    if not fresh:
        return []
    picked = [i for i in digest.fill(conn)["papers"] if i in set(fresh)]
    return (picked + [i for i in fresh if i not in picked])[:limit]


def _sweep_incoming(max_age: float = 3600) -> None:
    """Downloads cut off by an app quit leave feed-*.pdf behind in cache/incoming."""
    import time
    for f in (get_paths().cache / "incoming").glob("feed-*.pdf"):
        try:
            if time.time() - f.stat().st_mtime > max_age:
                f.unlink()
        except OSError:
            pass


def autoimport(conn: sqlite3.Connection, *, http: httpx.Client | None = None) -> list[ImportResult]:
    """Try each pending paper once today; commit after each so a slow download never holds the others back."""
    from ..db.connection import get_setting, set_setting
    from . import brief
    _sweep_incoming()
    out = []
    key = TRIED_KEY + brief.today_bj().isoformat()
    for item_id in autoimport_pending(conn):
        set_setting(conn, key, sorted(set(get_setting(conn, key, None) or []) | {item_id}))
        conn.commit()
        try:
            out.append(import_item(conn, item_id, http=http, pdf_only=True, ai=False))
        except Exception:  # noqa: BLE001 - one bad paper never stops the rest
            log.exception("auto-import of %s failed", item_id)
        conn.commit()
    return out


def _autoimport_run(http: httpx.Client | None = None) -> None:
    from ..db.connection import connect
    try:
        conn = connect()
        try:
            autoimport(conn, http=http)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - background work never takes the app down
        log.exception("auto-import run failed")
    finally:
        _auto_lock.release()


def autoimport_after_refresh(http: httpx.Client | None = None) -> bool:
    """Called by feeds.scheduler once Leo's own refresh has finished: import today's downloadable articles in a
    thread of their own, so the refresh button is free again while the PDFs come down. Returns whether a run
    started (one already running, or nothing waiting, starts none)."""
    global _auto_thread
    from ..db.connection import connect
    conn = connect()
    try:
        if not autoimport_pending(conn):
            return False
    finally:
        conn.close()
    if not _auto_lock.acquire(blocking=False):
        return False
    try:
        _auto_thread = threading.Thread(target=_autoimport_run, args=(http,), daemon=True, name="feeds-autoimport")
        _auto_thread.start()
    except RuntimeError:
        _auto_lock.release()
        return False
    return True


def wait_autoimport(timeout: float = 60.0) -> None:
    """Block until the last auto-import run finishes (tests)."""
    if _auto_thread is not None:
        _auto_thread.join(timeout)

