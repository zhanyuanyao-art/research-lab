"""Legacy journal radar (the /digest page): reads the weekly digests produced by the old
ir-core-pool-digest project (a local clone by default, GitHub API as fallback) and lets one item become
a paper record. The home page's 推送 now comes from the local feed engine in research_lab/feeds, which
also imported this repo's history once; `save_item` is shared with its 一键导入.

Two feeds come from the clone: `digests/digest*.md` (new papers in the core journals) and
`digests/news/news-YYYY-MM-DD.md` (curated think-tank commentary, written by news_pool.py; the
richer source — state/news_queue.json only holds the rolling leftovers). Every item gets a
deterministic direction guess from the directions.yml keywords (no AI)."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .config import get_paths
from .db.connection import get_setting, now_iso

DEFAULT_REPO = Path.home() / "Projects" / "IR&MORE"
GITHUB_REPO = "leoyao0701/ir-core-pool-digest"
STALL_DAYS = 8   # the digest is weekly; anything older than a week plus a day has missed a run

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
SECTION_RE = re.compile(r"^## +(\S+?) +｜ +(.+?) +｜ +(.+?)\s*$")
ITEM_RE = re.compile(r"^- +\*\*(.+?)\*\*\s*$")
META_RE = re.compile(r"^\s+(.*?) +｜ +(\d{4}-\d{2}-\d{2}) +｜ +(\S+)\s*$")
ABSTRACT_RE = re.compile(r"^\s+摘要[:：]\s*(.*)$")
TABLE_RE = re.compile(r"^\| *([A-Za-z]+) *\| *(\d+) *\| *(\d+) *\|$")
WINDOW_RE = re.compile(r"共 *(\d+) *篇.*?命中 *(\d+) *篇")


from .intake.bibliography import Item, save_item, saved_paper_for


@dataclass
class Digest:
    date: str
    path: str
    total: int = 0
    hits: int = 0
    journals: list[dict] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {**asdict(self), "items": [asdict(i) for i in self.items]}


def repo_path(conn: sqlite3.Connection | None = None) -> Path:
    p = get_setting(conn, "digest.repo_path", None) if conn is not None else None
    return Path(p).expanduser() if p else DEFAULT_REPO


def _digest_files(folder: Path) -> list[Path]:
    files = [f for f in folder.glob("digest*.md") if DATE_RE.search(f.name)]
    return sorted(files, key=lambda f: DATE_RE.search(f.name).group(1))


def parse(text: str, path: str = "") -> Digest:
    m = DATE_RE.search(text.splitlines()[0] if text else "") or DATE_RE.search(path)
    d = Digest(date=m.group(1) if m else "", path=path)
    journal = name = tag = ""
    current: Item | None = None
    for line in text.splitlines():
        if (w := WINDOW_RE.search(line)):
            d.total, d.hits = int(w.group(1)), int(w.group(2))
            continue
        if (t := TABLE_RE.match(line)):
            d.journals.append({"code": t.group(1), "new": int(t.group(2)), "hits": int(t.group(3))})
            continue
        if (s := SECTION_RE.match(line)):
            journal, name, tag = s.group(1), s.group(2), s.group(3)
            current = None
            continue
        if (i := ITEM_RE.match(line)):
            title = i.group(1).strip()
            hit = "🎯" in title
            current = Item(index=len(d.items), title=title.replace("🎯", "").strip(), authors="", date="", url="",
                           doi=None, journal=journal, journal_name=name, tag=tag, hit=hit)
            d.items.append(current)
            continue
        if current is not None and (mm := META_RE.match(line)) and not current.url:
            current.authors = mm.group(1).strip("（）() ") if "缺失" not in mm.group(1) else ""
            current.date, current.url = mm.group(2), mm.group(3)
            dm = re.search(r"10\.\d{4,9}/\S+", current.url)
            current.doi = dm.group(0).rstrip(".,;)") if dm else None
            continue
        if current is not None and (a := ABSTRACT_RE.match(line)):
            current.abstract = a.group(1).strip()
    return d


def annotate_directions(items: list) -> None:
    """Keyword-based direction chip for digest papers and news items (title + abstract/summary)."""
    from .intake import directions
    dirs = directions.load()
    names = {str(d["slug"]): str(d.get("name") or d["slug"]) for d in dirs}
    for it in items:
        texts = (it.title, getattr(it, "abstract", None), getattr(it, "zh_title", None), getattr(it, "summary", None))
        it.direction = directions.guess(*texts, dirs=dirs)
        it.direction_name = names.get(it.direction) if it.direction else None


def _fetch_from_github(cache: Path) -> Path | None:
    """Fallback when the local clone is missing: latest digest via the authenticated gh CLI."""
    try:
        out = subprocess.run(["gh", "api", f"repos/{GITHUB_REPO}/contents/digests", "--jq", ".[].name"],
                             capture_output=True, text=True, timeout=20)
        names = [n for n in out.stdout.split() if n.startswith("digest") and n.endswith(".md")]
        if not names:
            return None
        latest = sorted(names, key=lambda n: DATE_RE.search(n).group(1) if DATE_RE.search(n) else "")[-1]
        raw = subprocess.run(["gh", "api", f"repos/{GITHUB_REPO}/contents/digests/{latest}", "--jq", ".content"],
                             capture_output=True, text=True, timeout=20).stdout
        import base64
        text = base64.b64decode(raw).decode("utf-8", errors="replace")
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / latest
        target.write_text(text, encoding="utf-8")
        return target
    except Exception:  # noqa: BLE001 — the panel degrades to "no digest found"
        return None


def latest(conn: sqlite3.Connection | None = None) -> Digest | None:
    folder = repo_path(conn) / "digests"
    files = _digest_files(folder) if folder.is_dir() else []
    cache = get_paths().cache / "digests"
    if not files:
        cached = _digest_files(cache) if cache.is_dir() else []
        fetched = _fetch_from_github(cache)
        files = _digest_files(cache) if (fetched or cached) else []
    if not files:
        return None
    path = files[-1]
    d = parse(path.read_text(encoding="utf-8"), str(path))
    annotate_directions(d.items)
    return d


def status(d: Digest | None, today: date | None = None) -> dict:
    today = today or date.today()
    if d is None or not d.date:
        return {"state": "missing", "days": None, "note": "没有找到旧简报。新的推送由本机抓取，见首页推送。"}
    days = (today - date.fromisoformat(d.date)).days
    if days > STALL_DAYS:
        return {"state": "stalled", "days": days, "note": "旧简报已停止更新。新的推送由本机抓取，见首页推送。"}
    return {"state": "fresh", "days": days, "note": "按时更新（每周一）。"}


# ---------------------------------------------------------------- news (时评精选)

NEWS_TITLE_RE = re.compile(r"^## +(\d+)\. +(.+?)\s*$")
NEWS_META_RE = re.compile(r"^(.+?) +｜ +(\d{4}-\d{2}-\d{2}) +｜ +\[[^\]]*\]\((\S+?)\)\s*$")
NEWS_ORIG_RE = re.compile(r"^原题[:：]\s*(.+)$")
NEWS_ANGLE_RE = re.compile(r"^>\s*✍️\s*(.+)$")
NEWS_PAPER_RE = re.compile(r"^>\s*📎\s*[^\[]*\[《?(.+?)》?\]\((\S+?)\)\s*(?:[（(]([^）)]*)[）)])?\s*[—-]*\s*(.*)$")
NEWS_WINDOW_RE = re.compile(r"^近 *\d+ *天 *｜.*$")


@dataclass
class NewsItem:
    id: str                      # n1, n2 … within one news file; news.arrange refers to these
    index: int
    title: str                   # original (usually English) title
    zh_title: str
    source: str = ""
    date: str = ""
    url: str = ""
    summary: str = ""            # the digest's one-paragraph Chinese summary
    angle: str | None = None     # ✍️ writing angle
    paper_title: str | None = None
    paper_url: str | None = None
    paper_note: str | None = None
    direction: str | None = None
    direction_name: str | None = None


@dataclass
class NewsDigest:
    date: str
    path: str
    window: str = ""
    items: list[NewsItem] = field(default_factory=list)

    def by_id(self) -> dict[str, NewsItem]:
        return {i.id: i for i in self.items}


def parse_news(text: str, path: str = "") -> NewsDigest:
    lines = text.splitlines()
    m = DATE_RE.search(lines[0] if lines else "") or DATE_RE.search(path)
    d = NewsDigest(date=m.group(1) if m else "", path=path)
    cur: NewsItem | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not d.window and NEWS_WINDOW_RE.match(line):
            d.window = line
            continue
        if (t := NEWS_TITLE_RE.match(line)):
            cur = NewsItem(id=f"n{len(d.items) + 1}", index=len(d.items), title=t.group(2).strip(), zh_title=t.group(2).strip())
            d.items.append(cur)
            continue
        if cur is None or line.startswith("#"):
            continue
        if not cur.url and (mm := NEWS_META_RE.match(line)):
            cur.source, cur.date, cur.url = mm.group(1).strip(), mm.group(2), mm.group(3)
        elif (o := NEWS_ORIG_RE.match(line)):
            cur.title = o.group(1).strip()
        elif (a := NEWS_ANGLE_RE.match(line)):
            cur.angle = a.group(1).strip()
        elif (pp := NEWS_PAPER_RE.match(line)):
            cur.paper_title, cur.paper_url = pp.group(1).strip(), pp.group(2)
            cur.paper_note = (pp.group(4) or "").strip(" —-") or None
        elif not line.startswith(">") and not cur.summary:
            cur.summary = line
    return d


def news_files(conn: sqlite3.Connection | None = None) -> list[Path]:
    folder = repo_path(conn) / "digests" / "news"
    if not folder.is_dir():
        return []
    files = [f for f in folder.glob("news-*.md") if DATE_RE.search(f.name)]
    return sorted(files, key=lambda f: DATE_RE.search(f.name).group(1))


def news_dates(conn: sqlite3.Connection | None = None) -> list[str]:
    return [DATE_RE.search(f.name).group(1) for f in news_files(conn)]


def news_for(conn: sqlite3.Connection | None, date_: str | None = None) -> NewsDigest | None:
    """The news digest of `date_` (YYYY-MM-DD), or the latest one."""
    files = news_files(conn)
    if date_:
        files = [f for f in files if DATE_RE.search(f.name).group(1) == date_]
    if not files:
        return None
    path = files[-1]
    d = parse_news(path.read_text(encoding="utf-8"), str(path))
    d.date = d.date or DATE_RE.search(path.name).group(1)
    annotate_directions(d.items)
    return d


ARRANGEMENT_KEY = "news.arrangement:{date}"


def arrangement(conn: sqlite3.Connection, date_: str) -> dict | None:
    return get_setting(conn, ARRANGEMENT_KEY.format(date=date_), None)
