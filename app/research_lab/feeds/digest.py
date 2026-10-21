"""今日精选 (cards 0070, 0083): one issue a day of 10 news items and 10 papers, picked by plain rules. No item is
scored by the AI any more (Leo, 10-08: 「也不用打分搞得太复杂了，简单一点逻辑选取不可以吗」); the AI only writes
Chinese for what is picked (feeds.enrich) and the day's brief.

An issue is the setting `feeds.picks:<Beijing date>` = {"news": [ids], "papers": [ids], "seen": [ids]}. It is filled
when a page asks for it and appended to, so what Leo has seen stays; nothing picked within repeat_days comes back. The
daily 8 o'clock refresh and 刷新今天 recompute it (recompute(), card 0104): the old picks move to "seen" and the new
issue prefers items not shown yet today.

- 世界要闻 (world_n of news_n): the events of today's brief in its order, each shown by one report: the outlet with
  the highest feeds.yml weight, the newest on a tie. Without a brief, or once its events run out: world items in the
  window by outlet weight, then the newest, one per outlet. Until the brief is written these picks are provisional:
  shown but not stored, so the brief can still take the slots when it lands (it is queued by the refresh that
  opening the app starts, a minute after the page has asked for its picks). They are stored once it can no longer
  come today (brief.may_still_come, judged by the brief's job).
- 研究相关 (the rest): research-lane items in the window, a direction keyword hit first (feed_news.direction, set by
  directions.guess when the item is stored), then outlet weight, then the newest; at most two per outlet.
- 论文 (papers_n): papers in the window by journal weight (85+: 2, 70–84: 1), plus 1 for a radar hit and 1 for a
  direction keyword hit; ties go to the newest. At most three per journal (a journal publishes in batches), unless
  that would leave slots empty.
Announcements (category 非新闻, news.is_not_news) never enter. A lane with no candidates left lends its slots to the
other. source_boost scales the weight terms: 不加分 ranks by keyword hits and recency alone.

A world item picked for a brief event without a Chinese title takes the event's title, what and why as its
zh_title, zh_summary and angle, so the lane reads in Chinese at once and feeds.enrich has nothing to translate; an
empty region takes the event's, so the item stays under the region filter.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator

from ..db.connection import get_setting, set_setting
from ..tunables import default, tunable
from . import brief, sources, store

KEY = "feeds.picks:"
NEWS_N = default("feeds.digest.news_n")       # defaults; Knobs carries the live values
PAPERS_N = default("feeds.digest.papers_n")
TOP, GOOD = 85, 70                             # journal weights that earn a bonus
PAPER_BONUS = {TOP: 2.0, GOOD: 1.0}            # at source_boost 普通
BOOST = {"none": 0.0, "normal": 1.0, "strong": 2.0}   # 不加分 · 普通 · 强
NEWS_WEIGHT = 60                               # sources.NewsFeed's default, for an outlet no longer in feeds.yml
WORLD_PER_OUTLET, RESEARCH_PER_OUTLET = 1, 2
PAPERS_PER_JOURNAL = 3                         # Leo's sample on 10-08: one journal's batch took 8 of 10

# candidates for one slot, best first; the per-outlet cap (None for a brief event); the brief event, if any
Group = tuple[list[sqlite3.Row], int | None, dict | None]


@dataclass(frozen=True)
class Knobs:
    news_n: int
    papers_n: int
    world_n: int          # 世界要闻 slots; 6 of 10 by default (Leo, 10-08: 6:4)
    news_days: int        # news older than this is yesterday's issue
    papers_days: int      # journals publish in weekly bursts
    repeat_days: int      # how far back "picked on an earlier day" looks
    boost: float


def knobs(conn: sqlite3.Connection) -> Knobs:
    news_n = tunable(conn, "feeds.digest.news_n")
    # half up, not banker's rounding: 5 × 0.5 gives 3 world items, like 7 × 0.5 gives 4
    world_n = int(news_n * tunable(conn, "feeds.digest.world_ratio") + 0.5)
    return Knobs(news_n, tunable(conn, "feeds.digest.papers_n"), world_n, tunable(conn, "feeds.digest.news_window_days"),
                 tunable(conn, "feeds.digest.papers_window_days"), tunable(conn, "feeds.digest.repeat_days"),
                 BOOST[tunable(conn, "feeds.digest.source_boost")])


def _issue(conn: sqlite3.Connection, day: str) -> dict[str, list[str]]:
    v = get_setting(conn, KEY + day, None) or {}
    return {"news": list(v.get("news") or []), "papers": list(v.get("papers") or [])}


def _seen(conn: sqlite3.Connection, day: str) -> list[str]:
    """Picks shown earlier that day and replaced by a recompute."""
    return list((get_setting(conn, KEY + day, None) or {}).get("seen") or [])


def days(conn: sqlite3.Connection) -> list[str]:
    """Days that have an issue with anything in it, oldest first."""
    out = []
    for (key,) in conn.execute("SELECT key FROM settings WHERE key LIKE ? ORDER BY key", (KEY + "%",)):
        d = key[len(KEY):]
        issue = _issue(conn, d)
        if issue["news"] or issue["papers"]:
            out.append(d)
    return out


def _weekend(day: str) -> tuple[str, str] | None:
    """Saturday and Sunday when `day` is a Monday. Monday's issue covers the weekend too (Leo, 10-19): the weekend
    issues are built by the daily refresh whether or not he opens them, and on a quiet weekend they used up every
    new paper, leaving Monday with none."""
    d = date.fromisoformat(day)
    if d.weekday() != 0:
        return None
    return (d - timedelta(days=2)).isoformat(), (d - timedelta(days=1)).isoformat()


def _picked_before(conn: sqlite3.Connection, day: str, repeat_days: int) -> set[str]:
    """Picks of the earlier days in the repeat window, never offered again. On a Monday the weekend's picks are
    offered again, except the ones Leo saved or took into the library."""
    since = (date.fromisoformat(day) - timedelta(days=repeat_days)).isoformat()
    weekend = _weekend(day) or ()
    seen: set[str] = set()
    for (key,) in conn.execute("SELECT key FROM settings WHERE key LIKE ? AND key >= ? AND key < ?",
                               (KEY + "%", KEY + since, KEY + day)):
        issue = _issue(conn, key[len(KEY):])
        if key[len(KEY):] in weekend:
            seen.update(_acted_on(conn, "feed_news", issue["news"]))
            seen.update(_acted_on(conn, "feed_papers", issue["papers"]))
            continue
        seen.update(issue["news"])
        seen.update(issue["papers"])
        seen.update(_seen(conn, key[len(KEY):]))
    return seen


# ---------------------------------------------------------------- candidates, best first

# `at` orders by recency; first_seen breaks ties between items a feed dates by day only
_NEWS_COLS = "f.id, f.source, f.title, f.lane, f.direction, COALESCE(f.published, f.first_seen) AS at, f.first_seen AS seen"


def _is_news(r: sqlite3.Row) -> bool:
    from .news import is_not_news   # the rule for titles the stored category does not mark yet
    return not is_not_news(r["title"])


def _window(conn: sqlite3.Connection, lane: str, since: str) -> list[sqlite3.Row]:
    rows = conn.execute(f"SELECT {_NEWS_COLS} FROM feed_news f WHERE f.lane = ? AND f.status != 'dismissed' "
                        "AND COALESCE(f.category, '') != '非新闻' AND substr(f.first_seen, 1, 10) >= ?", (lane, since))
    return [r for r in rows if _is_news(r)]


def _world(conn: sqlite3.Connection, events: list[dict] | None, since: str, weights: dict[str, int],
           k: Knobs) -> Iterator[Group]:
    """One group per brief event (its world-lane reports, best first, no outlet cap), then the window one item at
    a time."""
    def best(r: sqlite3.Row) -> tuple:
        return (weights.get(r["source"], NEWS_WEIGHT) if k.boost else 0, r["at"] or "", r["seen"] or "")
    for ev in events or []:
        ids = [s["id"] for s in ev.get("sources") or [] if s.get("id")]
        if not ids:
            continue
        rows = conn.execute(f"SELECT {_NEWS_COLS} FROM feed_news f WHERE f.id IN ({','.join('?' * len(ids))}) "
                            "AND f.lane = 'world' AND f.status != 'dismissed' AND COALESCE(f.category, '') != '非新闻'",
                            ids).fetchall()
        reports = sorted((r for r in rows if _is_news(r)), key=best, reverse=True)
        if reports:
            yield reports, None, ev
    for r in sorted(_window(conn, "world", since), key=best, reverse=True):
        yield [r], WORLD_PER_OUTLET, None


def _research(conn: sqlite3.Connection, since: str, weights: dict[str, int], k: Knobs) -> Iterator[Group]:
    def rank(r: sqlite3.Row) -> tuple:
        return (r["direction"] is not None, weights.get(r["source"], NEWS_WEIGHT) if k.boost else 0, r["at"] or "",
                r["seen"] or "")
    for r in sorted(_window(conn, "research", since), key=rank, reverse=True):
        yield [r], RESEARCH_PER_OUTLET, None


def _paper_bonus(weight: int, boost: float) -> float:
    return boost * (PAPER_BONUS[TOP] if weight >= TOP else PAPER_BONUS[GOOD] if weight >= GOOD else 0.0)


def _papers(conn: sqlite3.Connection, since: str, skip: set[str], n: int, weights: dict[str, int], k: Knobs,
            per_journal: dict[str, int]) -> list[str]:
    """The best n, at most PAPERS_PER_JOURNAL per journal counting today's earlier picks (`per_journal`); a journal
    over its cap fills what is left only when the others run out."""
    if n <= 0:
        return []
    rows = conn.execute("SELECT f.id, f.journal, f.radar, f.direction, COALESCE(f.published, f.first_seen) AS at, "
                        "f.first_seen AS seen "
                        "FROM feed_papers f WHERE f.status != 'dismissed' AND substr(f.first_seen, 1, 10) >= ? "
                        "AND f.kind = 'article'",   # book reviews have their own list (card 0154), never a pick
                        (since,)).fetchall()

    def rank(r: sqlite3.Row) -> tuple:
        bonus = _paper_bonus(weights.get(r["journal"], sources.DEFAULT_WEIGHT), k.boost)
        return (bonus + (1 if r["radar"] else 0) + (1 if r["direction"] else 0), r["at"] or "", r["seen"] or "")
    out, over = [], []
    for r in sorted((r for r in rows if r["id"] not in skip), key=rank, reverse=True):
        if len(out) >= n:
            break
        if per_journal.get(r["journal"], 0) < PAPERS_PER_JOURNAL:
            per_journal[r["journal"]] = per_journal.get(r["journal"], 0) + 1
            out.append(r["id"])
        else:
            over.append(r["id"])
    return out + over[: n - len(out)]


class _Lane:
    """Takes a lane's candidates in order, skipping what is already picked and outlets at their cap."""

    def __init__(self, groups: Iterable[Group], taken: set[str], per_outlet: dict[str, int]):
        self._groups = iter(groups)
        self._taken = taken          # shared by both lanes and the earlier days: an item is picked once
        self._per_outlet = per_outlet   # this lane's picks per outlet today, the stored ones included
        self.events: dict[str, dict] = {}   # picked id → the brief event it reports

    def take(self, n: int) -> list[str]:
        out: list[str] = []
        while len(out) < n and (group := next(self._groups, None)) is not None:
            rows, cap, event = group
            for r in rows:   # a brief event yields its best report not taken yet; it fills one slot at most
                if r["id"] in self._taken or (cap is not None and self._per_outlet.get(r["source"], 0) >= cap):
                    continue
                self._taken.add(r["id"])
                self._per_outlet[r["source"]] = self._per_outlet.get(r["source"], 0) + 1
                out.append(r["id"])
                if event is not None:
                    self.events[r["id"]] = event
                break
        return out


# ---------------------------------------------------------------- the issue

def fill(conn: sqlite3.Connection, day: str | None = None, *, now: datetime | None = None) -> dict[str, list[str]]:
    """The issue for `day` (default today): the stored picks, topped up and saved. For today it also carries the
    provisional 世界要闻 picks while the brief may still come, in "news" and in "provisional", never saved. Only
    today's issue is topped up: an earlier day stays as Leo saw it. Read and write share one transaction, so two
    pages opening at once (two tabs, or the browser server beside the Mac app) cannot overwrite each other."""
    now = now or brief.now_bj()
    today = now.date().isoformat()
    day = day or today
    issue = _issue(conn, day)
    k = knobs(conn) if day == today else None
    if k is None or (len(issue["news"]) >= k.news_n and len(issue["papers"]) >= k.papers_n):
        return {**issue, "provisional": []}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        out = _top_up(conn, day, k, now)
        if own:
            conn.execute("COMMIT")
    except Exception:
        if own:
            conn.execute("ROLLBACK")
        raise
    return out


def recompute(conn: sqlite3.Connection, day: str | None = None, *, now: datetime | None = None) -> dict[str, list[str]]:
    """Pick today's issue again after a refresh. Picks Leo has acted on stay where they are (saved, or a paper taken
    into the library); the others become "seen", and their slots go first to items not shown yet today, then (when
    those run out) to the best of the rest, so a quiet day still fills them."""
    now = now or brief.now_bj()
    day = day or now.date().isoformat()
    if day != now.date().isoformat():
        return fill(conn, day, now=now)
    k = knobs(conn)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        old = _issue(conn, day)
        kept = {"news": _acted_on(conn, "feed_news", old["news"]), "papers": _acted_on(conn, "feed_papers", old["papers"])}
        gone = [i for s in ("news", "papers") for i in old[s] if i not in kept[s]]
        seen = list(dict.fromkeys(_seen(conn, day) + gone))
        set_setting(conn, KEY + day, {**kept, "seen": seen})
        _top_up(conn, day, k, now, avoid=set(seen))
        out = _top_up(conn, day, k, now)
        if own:
            conn.execute("COMMIT")
    except Exception:
        if own:
            conn.execute("ROLLBACK")
        raise
    return out


def _acted_on(conn: sqlite3.Connection, table: str, ids: list[str]) -> list[str]:
    """The picks Leo saved (or, for papers, took into the library), in their order."""
    if not ids:
        return []
    extra = " OR paper_id IS NOT NULL" if table == "feed_papers" else ""
    hit = {r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE id IN ({','.join('?' * len(ids))}) "
                                      f"AND (status = 'saved'{extra})", ids)}
    return [i for i in ids if i in hit]


def _top_up(conn: sqlite3.Connection, day: str, k: Knobs, now: datetime, avoid: set[str] = frozenset()) -> dict[str, list[str]]:
    issue = _issue(conn, day)
    srcs = sources.load()
    outlets = {f.name: f.weight for f in srcs.news}
    journals = {j.abbr: j.weight for j in srcs.journals}
    skip = _picked_before(conn, day, k.repeat_days) | set(issue["news"]) | set(issue["papers"]) | set(avoid)
    weekend = _weekend(day)
    news_since = (date.fromisoformat(day) - timedelta(days=k.news_days)).isoformat()
    if weekend:
        news_since = min(news_since, weekend[0])
    per: dict[str, dict[str, int]] = {"world": {}, "research": {}}
    for row in conn.execute(f"SELECT lane, source FROM feed_news WHERE id IN ({','.join('?' * len(issue['news']))})",
                            issue["news"]):
        lane = per["world" if row["lane"] == "world" else "research"]
        lane[row["source"]] = lane.get(row["source"], 0) + 1
    world_have = sum(per["world"].values())
    research_have = len(issue["news"]) - world_have
    today_brief = brief.get(conn, day)
    settled = today_brief is not None or not brief.may_still_come(conn, day, now)
    taken = set(skip)
    world = _Lane(_world(conn, (today_brief or {}).get("events"), news_since, outlets, k), taken, per["world"])
    research = _Lane(_research(conn, news_since, outlets, k), taken, per["research"])
    # Every top-up is capped by the free slots: a lane that lent slots earlier in the day must not overfill later.
    free = k.news_n - len(issue["news"])
    w = world.take(min(free, max(0, k.world_n - world_have)))
    free -= len(w)
    r = research.take(min(free, max(0, k.news_n - k.world_n - research_have)))
    free -= len(r)
    more = world.take(free)        # a lane short of candidates lends its slots to the other
    w += more
    r += research.take(free - len(more))
    papers_since = (date.fromisoformat(day) - timedelta(days=k.papers_days)).isoformat()
    if weekend:
        papers_since = min(papers_since, weekend[0])
    per_journal: dict[str, int] = {}
    for (journal,) in conn.execute(f"SELECT journal FROM feed_papers WHERE id IN ({','.join('?' * len(issue['papers']))})",
                                   issue["papers"]):
        per_journal[journal] = per_journal.get(journal, 0) + 1
    papers = _papers(conn, papers_since, skip, k.papers_n - len(issue["papers"]), journals, k, per_journal)
    for item_id, ev in world.events.items():
        _chinese_from_brief(conn, item_id, ev)
    stored = r + w if settled else r
    if stored or papers:
        issue = {"news": issue["news"] + stored, "papers": issue["papers"] + papers}
        seen = _seen(conn, day)
        set_setting(conn, KEY + day, {**issue, "seen": seen} if seen else issue)
    provisional = [] if settled else w
    return {"news": issue["news"] + provisional, "papers": issue["papers"], "provisional": provisional}


def _chinese_from_brief(conn: sqlite3.Connection, item_id: str, ev: dict) -> None:
    """The brief event's Chinese for a report that has no Chinese title (a field already filled is kept), and its
    region where the report has none."""
    conn.execute("UPDATE feed_news SET zh_title = ?, "
                 "zh_summary = CASE WHEN COALESCE(zh_summary, '') = '' THEN ? ELSE zh_summary END, "
                 "angle = CASE WHEN COALESCE(angle, '') = '' THEN ? ELSE angle END "
                 "WHERE id = ? AND COALESCE(zh_title, '') = ''",
                 (ev.get("title") or None, ev.get("what") or None, ev.get("why") or None, item_id))
    if ev.get("region"):
        conn.execute("UPDATE feed_news SET region = ? WHERE id = ? AND COALESCE(region, '') = ''", (ev["region"], item_id))


def items(conn: sqlite3.Connection, day: str | None = None) -> dict[str, Any]:
    """The issue as feed items for the pages: news split into 世界要闻 and 研究相关, in pick order."""
    day = day or brief.today_bj().isoformat()
    issue = fill(conn, day)
    # a dismissed item keeps its slot (the issue does not reshuffle) but is not shown again
    news = [it for i in issue["news"] if (it := store.get(conn, "news", i)) and it["status"] != "dismissed"]
    papers = [it for i in issue["papers"] if (it := store.get(conn, "papers", i)) and it["status"] != "dismissed"]
    all_days = days(conn)
    earlier = [d for d in all_days if d < day]
    later = [d for d in all_days if d > day]
    return {"date": day, "is_today": day == brief.today_bj().isoformat(),
            "world": [it for it in news if it.get("lane") == "world"],
            "research": [it for it in news if it.get("lane") != "world"],
            "news": news, "papers": papers,
            "prev": earlier[-1] if earlier else None, "next": later[0] if later else None}
