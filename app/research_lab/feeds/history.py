"""One-time import of the old newsletter's pushes (a local clone of ~/Projects/IR&MORE) into the streams.

Papers come from `digests/*.md` (weekly digest, daily brief, monthly watch / hunt reports, radar items
included), `state/papers_recent.jsonl` and `brief_bundle.json`, merged by DOI; news from
`digests/news/news-*.md`. Original dates and the existing Chinese titles are kept: first_seen is the date
of the report that first carried the item. Running it again adds nothing (rows are keyed by DOI / link).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ..db.connection import now_iso, set_setting
from ..intake import directions
from ..intake.extract import cjk_share
from . import news as news_mod, papers as papers_mod, sources as sources_mod

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SECTION_RE = re.compile(r"^## +(\S+?) +｜ +(.+?) +｜ +(.+?)\s*$")
ITEM_RE = re.compile(r"^- +(🎯\s*)?\*\*(.+?)\*\*\s*$")
META_RE = re.compile(r"^\s+(.*?) +｜ +(\d{4}-\d{2}-\d{2}) +｜ +(\S+)\s*$")
ORIG_RE = re.compile(r"^\s+原题[:：]\s*(.+?)\s*$")
HITS_RE = re.compile(r"^\s+命中[:：]\s*(.+?)\s*$")
ABSTRACT_RE = re.compile(r"^\s+摘要[:：]\s*(.*?)\s*$")
OA_PDF_RE = re.compile(r"\[OA PDF\]\((\S+?)\)")
DOI_RE = re.compile(r"10\.\d{4,9}/\S+")
FILE_POOL = {"digest": "core", "daily": "daily", "watch": "watch", "hunt": "hunt"}
SETTING = "feeds.history_import"


def _is_zh(text: str | None) -> bool:
    return bool(text) and cjk_share(text) >= 0.3


def _access(verdict: str | None) -> int | None:
    v = verdict or ""
    if "闭源" in v:
        return 0
    if v.startswith("✅") or v.startswith("⛔"):
        return 1
    return None


def parse_digest_md(text: str, path: str = "") -> list[dict[str, Any]]:
    """Items of one digest / daily / watch / hunt report (radar items included)."""
    m = DATE_RE.search(Path(path).name) or DATE_RE.search(text.splitlines()[0] if text else "")
    reported = m.group(1) if m else None
    kind = re.match(r"([a-z]+)", Path(path).name)
    pool = FILE_POOL.get(kind.group(1) if kind else "", "core")
    out: list[dict[str, Any]] = []
    journal = name = column = ""
    cur: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            s = SECTION_RE.match(line)
            journal, name, column = (s.group(1), s.group(2), s.group(3)) if s else ("", "", "")
            cur = None
            continue
        if not journal:
            continue
        if (i := ITEM_RE.match(line)):
            bold = i.group(2).replace("🎯", "").strip()
            cur = {"journal": journal, "journal_name": name, "journal_column": column, "pool": pool, "bold": bold,
                   "radar": bool(i.group(1)) or "🎯" in i.group(2), "radar_terms": [], "reported": reported,
                   "title": None, "zh_title": None, "authors": [], "published": None, "doi": None, "abstract": None,
                   "zh_abstract": None, "is_oa": None, "oa_pdf_url": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if (o := ORIG_RE.match(line)):
            cur["title"] = o.group(1)
        elif not cur["doi"] and (mm := META_RE.match(line)):
            authors = mm.group(1).strip()
            if "缺失" not in authors:
                cur["authors"] = [a.strip() for a in re.sub(r"\s*等$", "", authors).split(",") if a.strip()]
            cur["published"] = mm.group(2)
            d = DOI_RE.search(mm.group(3))
            cur["doi"] = d.group(0).rstrip(".,;)").lower() if d else None
        elif (h := HITS_RE.match(line)):
            cur["radar_terms"] = [t.strip() for t in re.split(r"[、,，]", h.group(1)) if t.strip()]
            cur["radar"] = True
        elif (a := ABSTRACT_RE.match(line)):
            body = a.group(1)
            cur["zh_abstract" if _is_zh(body) else "abstract"] = body or None
        elif stripped.startswith(("✅", "⚠️", "⛔")):
            cur["is_oa"] = _access(stripped)
        if (p := OA_PDF_RE.search(line)):
            cur["oa_pdf_url"] = p.group(1)
    for it in out:
        bold = it.pop("bold")
        if it["title"]:
            it["zh_title"] = bold if _is_zh(bold) else None
        else:
            it["title"] = bold
            it["zh_title"] = bold if _is_zh(bold) else None
    return out


def _merge(into: dict[str, Any], new: dict[str, Any]) -> None:
    for k, v in new.items():
        if v in (None, "", []):
            continue
        cur = into.get(k)
        if k == "reported":
            into[k] = min(cur, v) if cur else v
        elif k in ("abstract", "zh_abstract"):
            if not cur or len(str(v)) > len(str(cur)):
                into[k] = v
        elif k == "radar":
            into[k] = bool(cur) or bool(v)
        elif k == "radar_terms":
            into[k] = sorted(set(cur or []) | set(v))
        elif k == "title":
            # The English original beats a Chinese stand-in used when a report had no 原题 line.
            if not cur or (_is_zh(cur) and not _is_zh(v)):
                into[k] = v
        elif cur in (None, "", []):
            into[k] = v


def collect_papers(repo: Path) -> list[dict[str, Any]]:
    by_doi: dict[str, dict[str, Any]] = {}
    folder = repo / "digests"
    files = sorted((f for f in folder.glob("*.md") if DATE_RE.search(f.name)), key=lambda f: DATE_RE.search(f.name).group(1)) \
        if folder.is_dir() else []
    for f in files:
        for it in parse_digest_md(f.read_text(encoding="utf-8"), str(f)):
            if it["doi"]:
                _merge(by_doi.setdefault(it["doi"], {}), it)
    recent = repo / "state" / "papers_recent.jsonl"
    if recent.exists():
        for line in recent.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            doi = (r.get("doi") or "").lower().strip()
            if not doi:
                continue
            abstract = r.get("abstract") or None
            _merge(by_doi.setdefault(doi, {}), {
                "doi": doi, "title": r.get("title"), "zh_title": r.get("zh_title") or None, "journal": r.get("journal"),
                "pool": r.get("pool"), "reported": r.get("reported"), "radar": bool(r.get("radar")),
                "radar_terms": list(r.get("radar") or []), "is_oa": _access(r.get("verdict")),
                ("zh_abstract" if _is_zh(abstract) else "abstract"): abstract})
    bundle = repo / "brief_bundle.json"
    if bundle.exists():
        try:
            data = json.loads(bundle.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        for e in data.get("entries") or [] if isinstance(data, dict) else []:
            doi = (e.get("doi") or "").lower().strip()
            if not doi:
                continue
            access = e.get("access") or {}
            _merge(by_doi.setdefault(doi, {}), {
                "doi": doi, "title": e.get("title"), "zh_title": e.get("zh_title") or None, "journal": e.get("journal_abbr"),
                "journal_name": e.get("journal_name"), "journal_column": e.get("column"),
                "authors": [a.strip() for a in str(e.get("authors") or "").split(",") if a.strip() and "缺失" not in a],
                "published": e.get("date") or None, "abstract": e.get("abstract") or None,
                "zh_abstract": e.get("zh_abstract") or None, "radar": bool(e.get("radar")),
                "radar_terms": e["radar"] if isinstance(e.get("radar"), list) else [], "reported": data.get("today"),
                "is_oa": 1 if access.get("is_oa") else _access(access.get("verdict")), "oa_pdf_url": access.get("pdf_url") or None})
    return list(by_doi.values())


def import_papers(conn: sqlite3.Connection, repo: Path) -> dict[str, int]:
    srcs = sources_mod.load()
    dirs = directions.load()
    counts = {"found": 0, "new": 0, "filled": 0, "skipped": 0}
    for it in collect_papers(repo):
        counts["found"] += 1
        if not it.get("title") or papers_mod.is_non_paper(it["title"]):
            counts["skipped"] += 1
            continue
        j = srcs.journal(it.get("journal"))
        if j:
            it["journal_name"] = it.get("journal_name") or j.name
            it["journal_column"] = it.get("journal_column") or j.column
            it["pool"] = j.pool
        if not it.get("radar_terms") and srcs.radar_terms:
            it["radar_terms"] = sources_mod.radar_hits(f"{it['title']} {it.get('abstract') or ''}", srcs.radar_terms)
        if it.get("oa_pdf_url") and not papers_mod.net.looks_public(it["oa_pdf_url"]):
            it["oa_pdf_url"] = None
        it["id"] = papers_mod.paper_key(it["doi"])
        it["direction"] = directions.guess(it["title"], it.get("abstract"), it.get("zh_title"), it.get("zh_abstract"), dirs=dirs)
        first_seen = f"{it['reported']}T00:00:00+00:00" if it.get("reported") else now_iso()
        if papers_mod.insert(conn, it, origin="history", first_seen=first_seen):
            counts["new"] += 1
        elif _fill_paper(conn, it, first_seen):
            counts["filled"] += 1
    return counts


def _fill_paper(conn: sqlite3.Connection, it: dict[str, Any], first_seen: str) -> bool:
    """An item a live refresh found first keeps its row; the old digest only adds what the row lacks
    (Chinese title and abstract, radar mark, open-access link) and its earlier date."""
    cur = conn.execute(
        "UPDATE feed_papers SET zh_title = COALESCE(NULLIF(zh_title, ''), ?), zh_abstract = COALESCE(NULLIF(zh_abstract, ''), ?), "
        "abstract = COALESCE(NULLIF(abstract, ''), ?), radar = MAX(radar, ?), "
        "radar_json = CASE WHEN COALESCE(radar_json, '[]') = '[]' THEN ? ELSE radar_json END, "
        "oa_pdf_url = COALESCE(oa_pdf_url, ?), is_oa = COALESCE(is_oa, ?), first_seen = MIN(first_seen, ?), updated_at = ? "
        "WHERE id = ? AND (COALESCE(zh_title, '') = '' AND ? IS NOT NULL OR COALESCE(zh_abstract, '') = '' AND ? IS NOT NULL "
        "OR first_seen > ? OR (radar = 0 AND ? = 1) OR (oa_pdf_url IS NULL AND ? IS NOT NULL))",
        (it.get("zh_title"), it.get("zh_abstract"), it.get("abstract"), 1 if it.get("radar") else 0,
         json.dumps(it.get("radar_terms") or [], ensure_ascii=False), it.get("oa_pdf_url"), it.get("is_oa"), first_seen, now_iso(),
         it["id"], it.get("zh_title"), it.get("zh_abstract"), first_seen, 1 if it.get("radar") else 0, it.get("oa_pdf_url")))
    return cur.rowcount == 1


def import_news(conn: sqlite3.Connection, repo: Path) -> dict[str, int]:
    from .. import digest
    folder = repo / "digests" / "news"
    files = sorted((f for f in folder.glob("news-*.md") if DATE_RE.search(f.name)), key=lambda f: f.name) if folder.is_dir() else []
    dirs = directions.load()
    counts = {"found": 0, "new": 0, "filled": 0}
    for f in files:
        d = digest.parse_news(f.read_text(encoding="utf-8"), str(f))
        reported = d.date or DATE_RE.search(f.name).group(1)
        for it in d.items:
            if not it.url:
                continue
            counts["found"] += 1
            zh = it.zh_title if it.zh_title and it.zh_title != it.title else None
            row = {"id": news_mod.news_key(it.url), "source": it.source or "未知来源", "title": it.title, "link": it.url,
                   "published": it.date or reported, "zh_title": zh, "zh_summary": it.summary or None, "angle": it.angle,
                   "direction": directions.guess(it.title, zh, it.summary, dirs=dirs)}
            first_seen = f"{reported}T00:00:00+00:00"
            if news_mod.insert(conn, row, origin="history", first_seen=first_seen):
                counts["new"] += 1
            elif conn.execute(
                    "UPDATE feed_news SET zh_title = COALESCE(NULLIF(zh_title, ''), ?), zh_summary = COALESCE(NULLIF(zh_summary, ''), ?), "
                    "angle = COALESCE(NULLIF(angle, ''), ?), first_seen = MIN(first_seen, ?), updated_at = ? WHERE id = ? AND "
                    "(COALESCE(zh_title, '') = '' AND ? IS NOT NULL OR first_seen > ?)",
                    (zh, row["zh_summary"], row["angle"], first_seen, now_iso(), row["id"], zh, first_seen)).rowcount:
                counts["filled"] += 1
    return counts


def import_all(conn: sqlite3.Connection, repo: Path) -> dict[str, Any]:
    from .scheduler import link_library
    repo = Path(repo).expanduser()
    result = {"repo": str(repo), "papers": import_papers(conn, repo), "news": import_news(conn, repo)}
    result["linked"] = link_library(conn)
    set_setting(conn, SETTING, {**result, "at": now_iso()})
    return result
