"""Reads and status changes for the two streams. Items are addressed by their content key (DOI hash or
link hash), never by position."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any

from ..db.connection import get_setting, now_iso
from ..tunables import default, tunable

STREAMS = {"papers": "feed_papers", "news": "feed_news"}
STATUSES = ("new", "saved", "dismissed")
KINDS = {"article": "Articles", "review": "Book reviews"}   # 推送 · 论文's switch (card 0154)
VIEWS = {"unread": "未读", "all": "全部", "dismissed": "已忽略"}
LAST_KEY = "feeds.last_refresh"
HOME_LIMIT = default("feeds.home_limit")   # per stream on 首页 by default; 查看更多 opens the full 推送 page
PAGE = 20


def table(stream: str) -> str:
    if stream not in STREAMS:
        raise LookupError(stream)
    return STREAMS[stream]


def time_label(ts: str | None) -> str | None:
    """'10月1日 08:30' in local time for an ISO timestamp."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ts[:16]
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def short_source(value: str | None) -> str:
    return re.split(r"\s+·\s+", value or "", maxsplit=1)[0]


def author_names(authors: list[str]) -> str:
    # Drop scraped affiliations without changing the stored bibliographic record.
    affiliation = re.compile(r"\b(?:University|School|Centre|Center|Institute|Professor|Fellow|Department|Studies|Director)\b", re.I)
    names = []
    for author in authors:
        for part in re.split(r"[,;\n]", author):
            name = part.strip().rstrip(".")
            if name and len(name.split()) <= 5 and not affiliation.search(name):
                names.append(name)
    return "、".join(names[:3]) + (" 等" if len(names) > 3 else "")


def _item(stream: str, r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    zh = (d.get("zh_title") or "").strip()
    d["stream"] = stream
    d["display_title"] = zh or d["title"]
    d["orig_title"] = d["title"] if zh and zh != d["title"] else None
    d["date"] = (d.get("published") or d.get("first_seen") or "")[:10]
    if stream == "papers":
        d["authors"] = json.loads(d.get("authors_json") or "[]")
        d["author_names"] = author_names(d["authors"])
        d["radar_terms"] = json.loads(d.get("radar_json") or "[]")
        d["url"] = f"https://doi.org/{d['doi']}" if d.get("doi") else None
        d["origin_label"] = d.get("journal_name") or d.get("journal")
        d["zh_text"] = d.get("zh_summary") or d.get("zh_abstract")
        d["text"] = d.get("abstract")
        d["imported"] = bool(d.get("paper_id") and d.get("paper_exists"))
        if d.get("kind") == "review":   # card 0154: the reviewed book leads; the raw title is the fallback
            d["review"] = True
            d["display_title"] = d.get("book_title") or d["title"]
            d["reviewer_names"] = d["author_names"]
    else:
        d["url"] = d["link"]
        d["origin_label"] = short_source(d["source"])
        d["zh_text"] = d.get("zh_summary")
        d["text"] = d.get("summary")
        d["imported"] = False
    return d


def _select(stream: str) -> str:
    if stream == "papers":
        return ("SELECT f.*, p.id AS paper_exists FROM feed_papers f LEFT JOIN papers p ON p.id = f.paper_id")
    return "SELECT f.* FROM feed_news f"


def get(conn: sqlite3.Connection, stream: str, item_id: str) -> dict[str, Any] | None:
    table(stream)
    row = conn.execute(_select(stream) + " WHERE f.id = ?", (item_id,)).fetchone()
    return _item(stream, row) if row else None


def set_status(conn: sqlite3.Connection, stream: str, item_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(status)
    cur = conn.execute(f"UPDATE {table(stream)} SET status = ?, status_at = ?, updated_at = ? WHERE id = ?",
                       (status, now_iso(), now_iso(), item_id))
    if cur.rowcount == 0:
        raise LookupError(item_id)
    return get(conn, stream, item_id)


def toggle_saved(conn: sqlite3.Connection, stream: str, item_id: str) -> dict[str, Any]:
    item = get(conn, stream, item_id)
    if item is None:
        raise LookupError(item_id)
    return set_status(conn, stream, item_id, "new" if item["status"] == "saved" else "saved")


def mark_imported(conn: sqlite3.Connection, item_id: str, paper_id: str) -> None:
    conn.execute("UPDATE feed_papers SET paper_id = ?, status = CASE WHEN status = 'dismissed' THEN status ELSE 'saved' END, "
                 "status_at = ?, updated_at = ? WHERE id = ?", (paper_id, now_iso(), now_iso(), item_id))


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_items(conn: sqlite3.Connection, stream: str, *, view: str = "unread", direction: str | None = None,
               source: str | None = None, q: str | None = None, lane: str | None = None, region: str | None = None,
               kind: str | None = "article", limit: int = PAGE, offset: int = 0) -> tuple[list[dict], int]:
    """(items, total) newest first. view: unread (new) | all (new + saved) | dismissed | saved. For papers, `kind` is
    article (the default) or review (book reviews, card 0154); None lists both."""
    table(stream)
    where, args = [], []
    where.append({"unread": "f.status = 'new'", "all": "f.status != 'dismissed'", "dismissed": "f.status = 'dismissed'",
                  "saved": "f.status = 'saved'"}.get(view, "f.status = 'new'"))
    if stream == "papers" and kind in KINDS:
        where.append("f.kind = ?"); args.append(kind)
    if direction:
        if direction == "_none":
            where.append("f.direction IS NULL")
        else:
            where.append("f.direction = ?"); args.append(direction)
    if source:
        where.append("f.journal = ?" if stream == "papers" else "f.source = ?"); args.append(source)
    if stream == "news" and lane in ("world", "research"):   # 世界要闻 · 研究相关 (card 0061)
        where.append("f.lane = ?"); args.append(lane)
    if stream == "news" and region:
        where.append("f.region = ?"); args.append(region)
    if q and q.strip():
        cols = ("f.title", "f.zh_title", "f.abstract", "f.zh_abstract", "f.zh_summary", "f.journal_name", "f.authors_json") if stream == "papers" \
            else ("f.title", "f.zh_title", "f.summary", "f.zh_summary", "f.source", "f.angle")
        for word in q.split()[:6]:
            where.append("(" + " OR ".join(f"{c} LIKE ? ESCAPE '\\'" for c in cols) + ")")
            args += [_like(word)] * len(cols)
    clause = " WHERE " + " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM {STREAMS[stream]} f{clause}", args).fetchone()[0]
    order = " ORDER BY COALESCE(f.published, f.first_seen) DESC, f.first_seen DESC, f.id"
    if view == "saved":
        order = " ORDER BY f.status_at DESC, f.id"
    rows = conn.execute(_select(stream) + clause + order + " LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
    return [_item(stream, r) for r in rows], total


def home(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, Any]:
    """首页's 推送 block: today's 今日精选 (feeds/digest.py), `limit` per lane (default: the tunable feeds.home_limit)."""
    from . import digest
    limit = limit or tunable(conn, "feeds.home_limit")
    issue = digest.items(conn)
    lanes = dict(conn.execute("SELECT lane, COUNT(*) FROM feed_news WHERE status != 'dismissed' GROUP BY lane").fetchall())
    return {"papers": issue["papers"][:limit], "news": issue["research"][:limit], "world": issue["world"][:limit],
            "lane_totals": {"world": lanes.get("world", 0), "research": lanes.get("research", 0)}}


def headlines(conn: sqlite3.Connection, n: int = 3) -> dict[str, Any]:
    """The front-page slides (one lead + two seconds) from 今日精选: 世界要闻 in the brief's order first, then
    研究相关, and the first papers; plus how many items arrived in the last 24 hours (kept for the 推送 page; the
    carousel header no longer shows it)."""
    from . import digest
    issue = digest.items(conn)
    fresh = {s: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE status = 'new' AND "
                             "replace(substr(first_seen, 1, 19), 'T', ' ') >= datetime('now', '-1 day')").fetchone()[0]
             for s, t in STREAMS.items()}
    return {"news": (issue["world"] + issue["research"])[:n], "papers": issue["papers"][:n], "fresh": fresh}


def counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Per stream: items by status and total. papers counts articles; book reviews are counted on their own."""
    out = {}
    for stream, sql in (("papers", "SELECT status, COUNT(*) FROM feed_papers WHERE kind = 'article' GROUP BY status"),
                        ("reviews", "SELECT status, COUNT(*) FROM feed_papers WHERE kind = 'review' GROUP BY status"),
                        ("news", "SELECT status, COUNT(*) FROM feed_news GROUP BY status")):
        c = {s: 0 for s in STATUSES}
        for r in conn.execute(sql):
            c[r[0]] = r[1]
        c["total"] = sum(c[s] for s in STATUSES)
        out[stream] = c
    return out


def source_options(conn: sqlite3.Connection, stream: str) -> list[dict[str, Any]]:
    """Journals / sources that have items, for the stream filter."""
    if stream == "papers":
        rows = conn.execute("SELECT journal AS key, MAX(journal_name) AS label, COUNT(*) AS n FROM feed_papers "
                            "WHERE journal IS NOT NULL GROUP BY journal ORDER BY label").fetchall()
    else:
        rows = conn.execute("SELECT source AS key, source AS label, COUNT(*) AS n FROM feed_news GROUP BY source ORDER BY source").fetchall()
    return [dict(r) for r in rows]


def last_refresh(conn: sqlite3.Connection) -> dict[str, Any] | None:
    return get_setting(conn, LAST_KEY, None)


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the 推送 header shows: when the last refresh ran, what it found, whether one is running."""
    from . import scheduler
    last = last_refresh(conn) or {}
    papers, news = last.get("papers") or {}, last.get("news") or {}
    failed = [f["source"] for f in (papers.get("failed") or []) + (news.get("failed") or [])]
    jobs = [j for j in (last.get("ai_jobs") or []) if isinstance(j, str)]
    enriching = conn.execute(f"SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued', 'claimed') AND id IN ({','.join('?' * len(jobs))})",
                             jobs).fetchone()[0] if jobs else 0
    return {"running": scheduler.running(), "last_at": last.get("at"), "label": time_label(last.get("at")),
            "papers_new": papers.get("new"), "news_new": news.get("new"), "failed": failed, "error": last.get("error"),
            "enriching": enriching}
