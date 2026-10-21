"""Think-tank and scholarly commentary: RSS 2.0, RSS 1.0 (RDF) and Atom parsed with the standard
library, HTML stripped from summaries, items keyed by a hash of the canonical link."""
from __future__ import annotations

import hashlib
import html
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..db.connection import now_iso
from ..intake import directions
from . import net, sources as sources_mod
from .sources import NewsFeed

log = logging.getLogger("research_lab.feeds.news")

SUMMARY_MAX = 800
WORKERS = 6
TRACKING_PARAMS = ("fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "cmpid", "sc_cid")
BOILERPLATE_RE = re.compile(r"\s*(The post .{0,300}? appeared first on .{0,120}?\.?|Continue reading.*|Read more.*)\s*$", re.I | re.S)
IMG_RE = re.compile(r"""<img[^>]+src=["'](https?://[^"']+)["']""", re.I)

# Things a site posts about itself rather than commentary: its own new issue or newsletter, events, podcasts,
# calls for papers, job ads, subscription appeals. Only shapes that ordinary headlines do not take are listed
# ("May Issue", "New Issue", "Join Us", "Now Hiring" and bare 招聘 all occur in real commentary titles);
# everything else is left to the scoring prompt's score-0 rule. Matches stay on the 推送 page under this
# category with score 0, are never sent for scoring, and never reach 首页.
NOT_NEWS = "非新闻"
_MONTH = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
_W = r"(?<![a-z])"   # word start without \b, which treats the apostrophe in "month’s" as a boundary
_END = r"\s*(?:$|[:|–—-]|out now|is out(?! of)|now available)"
NOT_NEWS_RE = re.compile("|".join([
    _W + r"(?:issue|edition|newsletter|digest|bulletin)(?![a-z])[^.?!]{0,40}?" + _W + r"(?:out now|now available|is out(?! of))(?![a-z])",
    r"^the " + _MONTH + r"(?: \d{4})? (?:issue|edition)" + _END,
    r"[’']s " + _MONTH + r"(?: \d{4})? (?:issue|edition)" + _END,
    r"^podcast\s*[:|–—-]", r"\|\s*podcast(?![a-z])", r"^episode \d+\s*[:|–—-]", r"[:|–—-]\s*episode \d+\s*$",
    r"^(?:webinar|event|upcoming event|book launch|launch event|livestream|live stream)\s*[:|–—-]",
    _W + r"save the date(?![a-z])",
    _W + r"call for (?:papers|proposals|applications|submissions|contributions|abstracts|nominations)(?![a-z])",
    _W + r"we[’']re hiring(?![a-z])", _W + r"subscribe to our(?![a-z])", _W + r"support our work(?![a-z])",
]), re.I)
# Applied only to titles that are Chinese in the original: zh_title is the assistant's translation, and
# "Now Available" rendered as 现已发布 would hide a think-tank report.
NOT_NEWS_ZH_RE = re.compile(r"现已发布|现已上线|最新一期|[一二三四五六七八九十]+月刊|本期(?:简报|通讯)|招聘启事|诚聘|征稿|^播客\s*[｜|:：]|"
                            r"讲座预告|活动预告|直播预告|报名(?:开启|通道)|欢迎订阅")
_CJK = re.compile(r"[㐀-鿿]")


def is_not_news(title: str | None) -> bool:
    """The original title only; see NOT_NEWS_ZH_RE for why the translation is not consulted."""
    t = title or ""
    return bool(NOT_NEWS_RE.search(t) or (_CJK.search(t) and NOT_NEWS_ZH_RE.search(t)))


def _section_rules() -> dict[str, NewsFeed] | None:
    """None when feeds.yml could not be read (load() then returns no sources at all)."""
    news = sources_mod.load().news
    return {f.name: f for f in news if f.keep or f.drop} if news else None


def flag_not_news(conn: sqlite3.Connection) -> int:
    """Re-judge every stored item against NOT_NEWS_RE and its source's section rules (feeds.yml keep/drop, card
    0067: sport, video, business and a country's own news in a general outlet's feed). A match gets category 非新闻,
    score 0 and leaves the scoring queue; an item marked earlier that no longer matches (a rule was narrowed) is
    put back unscored, so the next run scores it. Only this function writes 非新闻 (it is not in
    enrich.CATEGORIES), so every 非新闻 row is the rule's own. Runs after every refresh and before scoring.
    Returns how many now match."""
    ts = now_iso()
    hit, healed = [], []
    rules = _section_rules()
    for r in conn.execute("SELECT id, title, category, source, link FROM feed_news"):
        rule = (rules or {}).get(r["source"])
        match = is_not_news(r["title"]) or (rule is not None and rule.off_section(r["link"]))
        if match and r["category"] != NOT_NEWS:
            hit.append(r["id"])
        elif not match and r["category"] == NOT_NEWS and rules is not None:
            # without the rules (a broken feeds.yml) a section match cannot be told from a narrowed rule:
            # healing then would send every off-section item back to be scored
            healed.append(r["id"])
    for i in hit:
        conn.execute("UPDATE feed_news SET category = ?, score = 0, importance = 0, enriched_at = COALESCE(enriched_at, ?), "
                     "enrich_batch = CASE WHEN enriched_at IS NULL THEN NULL ELSE enrich_batch END, updated_at = ? WHERE id = ?",
                     (NOT_NEWS, ts, ts, i))
    for i in healed:
        conn.execute("UPDATE feed_news SET category = NULL, score = NULL, importance = NULL, enriched_at = NULL, enrich_batch = NULL, updated_at = ? "
                     "WHERE id = ?", (ts, i))
    return conn.execute("SELECT COUNT(*) FROM feed_news WHERE category = ?", (NOT_NEWS,)).fetchone()[0]


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def strip_html(text: str | None) -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", text or "", flags=re.I | re.S)
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return BOILERPLATE_RE.sub("", s).strip()


def canonical_link(link: str) -> str:
    """Scheme, `www.`, fragments, trailing slashes and tracking parameters never make two items."""
    parts = urlsplit((link or "").strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(query), ""))


def news_key(link: str) -> str:
    return hashlib.sha1(canonical_link(link).encode("utf-8")).hexdigest()


def parse_date(text: str | None) -> str | None:
    """RFC 822 (RSS) or ISO 8601 (Atom, dc:date) → UTC 'YYYY-MM-DDTHH:MM:SSZ'; None when unreadable."""
    t = (text or "").strip()
    if not t:
        return None
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(t)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", t)
            if m:
                return m.group(1)
            # Crisis Group writes "Friday, September 25, 2026 - 15:56" (no zone: taken as UTC)
            try:
                dt = datetime.strptime(re.sub(r"^\w+,\s*", "", t), "%B %d, %Y - %H:%M")
            except ValueError:
                return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _children(el: ET.Element) -> dict[str, list[ET.Element]]:
    out: dict[str, list[ET.Element]] = {}
    for c in el:
        out.setdefault(_local(c.tag), []).append(c)
    return out


def _text(kids: dict[str, list[ET.Element]], *names: str) -> str:
    for n in names:
        for el in kids.get(n, []):
            t = "".join(el.itertext()).strip()
            if t:
                return t
    return ""


def _link(kids: dict[str, list[ET.Element]]) -> str:
    links = kids.get("link", [])
    for el in links:   # Atom: <link rel="alternate" href="…"/>, rel defaults to alternate
        if el.get("href") and el.get("rel", "alternate") == "alternate":
            return el.get("href", "").strip()
    for el in links:
        if (el.text or "").strip():
            return el.text.strip()
    for el in links:
        if el.get("href"):
            return el.get("href", "").strip()
    guid = kids.get("guid", [])
    if guid and (guid[0].text or "").startswith("http") and guid[0].get("isPermaLink", "true") != "false":
        return guid[0].text.strip()
    return ""


def _image(kids: dict[str, list[ET.Element]], blobs: list[str]) -> str | None:
    for el in kids.get("enclosure", []):
        if "image" in (el.get("type") or "") and el.get("url"):
            return el.get("url")
    for name in ("content", "thumbnail"):
        for el in kids.get(name, []):
            if el.get("url") and (el.get("medium") in (None, "image") or "image" in (el.get("type") or "")):
                return el.get("url")
    for el in kids.get("group", []):
        img = _image(_children(el), [])
        if img:
            return img
    for b in blobs:
        m = IMG_RE.search(b or "")
        if m:
            return m.group(1)
    return None


def parse_feed(raw: bytes | str, source: str = "") -> list[dict[str, Any]]:
    """Items of an RSS 2.0 / RSS 1.0 / Atom document. Raises ValueError when it is not XML."""
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    data = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from e
    items = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        kids = _children(el)
        title = strip_html(_text(kids, "title"))
        link = _link(kids)
        if not title or not link:
            continue
        desc = _text(kids, "description", "summary")
        content = _text(kids, "encoded", "content")
        summary = strip_html(desc or content)
        if len(summary) > SUMMARY_MAX:
            summary = summary[:SUMMARY_MAX].rsplit(" ", 1)[0] + "…"
        items.append({"source": source, "title": title, "link": link,
                      "published": parse_date(_text(kids, "pubdate", "published", "date", "updated", "issued", "modified")),
                      "summary": summary or None, "image": _image(kids, [desc, content])})
    return items


def fetch(http: httpx.Client, feed: NewsFeed) -> list[dict[str, Any]]:
    """Download and parse one feed. Raises net.FetchError with a short reason."""
    if not net.public_url(feed.url):
        raise net.FetchError("bad URL")
    raw = net.get_bytes(http, feed.url, accept="application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8")
    try:
        return parse_feed(raw, feed.name)
    except ValueError as e:
        raise net.FetchError("not a feed") from e


def insert(conn: sqlite3.Connection, row: dict[str, Any], *, origin: str = "rss", first_seen: str | None = None) -> bool:
    ts = now_iso()
    cur = conn.execute(
        "INSERT OR IGNORE INTO feed_news(id, source, title, link, published, summary, image, zh_title, zh_summary, angle, "
        "category, score, direction, direction_source, origin, first_seen, status, updated_at, lane) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row["id"], row["source"], row["title"], row["link"], row.get("published"), row.get("summary"), row.get("image"),
         row.get("zh_title") or None, row.get("zh_summary") or None, row.get("angle") or None, row.get("category"),
         row.get("score"), row.get("direction"), "keyword" if row.get("direction") else None, origin, first_seen or ts,
         row.get("status") or "new", ts, row.get("lane") or "research"))
    return cur.rowcount == 1


def refresh_news(conn: sqlite3.Connection, http: httpx.Client | None = None, days: int = 4, *,
                 feeds: list[NewsFeed] | None = None) -> dict[str, Any]:
    """Fetch every feed and store items published within `days` (undated items count as new).
    Returns {'sources', 'fetched', 'new', 'old', 'per_source': {name: n}, 'failed': [{'source', 'error'}]}."""
    feeds = feeds if feeds is not None else sources_mod.load().news
    own = http is None
    http = http or net.new_client()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    dirs = directions.load()
    summary: dict[str, Any] = {"sources": len(feeds), "fetched": 0, "new": 0, "old": 0, "per_source": {}, "failed": []}

    def one(feed: NewsFeed) -> tuple[NewsFeed, list[dict] | None, str | None]:
        try:
            return feed, fetch(http, feed), None
        except net.FetchError as e:
            return feed, None, str(e)
        except Exception as e:  # noqa: BLE001 - one dead feed never breaks the run
            log.exception("feed %s failed", feed.name)
            return feed, None, type(e).__name__

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(one, feeds))
        for feed, items, err in results:
            if err is not None:
                summary["failed"].append({"source": feed.name, "error": err})
                continue
            summary["fetched"] += len(items or [])
            for it in items or []:
                if it["published"] and it["published"][:10] < cutoff:
                    summary["old"] += 1
                    continue
                it["id"] = news_key(it["link"])
                it["lane"] = feed.lane
                it["direction"] = directions.guess(it["title"], it.get("summary"), dirs=dirs)
                if insert(conn, it):
                    summary["new"] += 1
                    summary["per_source"][feed.name] = summary["per_source"].get(feed.name, 0) + 1
    finally:
        if own:
            http.close()
    summary["not_news"] = flag_not_news(conn)
    return summary
