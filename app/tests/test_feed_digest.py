"""今日精选 (cards 0070, 0083): 10 news items and 10 papers a day picked by plain rules, no AI score. 世界要闻 come from
the day's brief (one report per event, the weightiest outlet), by outlet weight before it; 研究相关 by direction
keyword, outlet weight and recency, two per outlet; papers by journal weight, radar and direction. Stable once
shown, never repeating an earlier day's picks; the AI only writes Chinese for the picks, within the push budget."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from research_lab.db.connection import set_setting

MORNING = datetime.now().replace(hour=8, minute=0)       # after the brief hour
ONE_PM = datetime.now().replace(hour=13, minute=0)       # Leo's first visit on two of three days in his data
TODAY = date.today().isoformat()


@pytest.fixture(autouse=True)
def _today():
    """The day as each test starts, not as the module was imported: a run that goes past midnight would otherwise
    look for today's issue under yesterday's keys."""
    global MORNING, ONE_PM, TODAY
    now = datetime.now()
    MORNING, ONE_PM, TODAY = now.replace(hour=8, minute=0), now.replace(hour=13, minute=0), now.date().isoformat()


def _last_weekday() -> str:
    """The latest earlier day whose picks today never offers again: yesterday, or Friday on a Monday (a Monday
    reaches back over the weekend)."""
    return (date.today() - timedelta(days=3 if date.today().weekday() == 0 else 1)).isoformat()


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _news(conn, nid, *, lane="world", source=None, hours=1, title=None, direction=None, category=None, zh=None):
    """Each item from its own outlet unless told otherwise."""
    ts = _ago(hours=hours)
    conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane, direction, category, zh_title) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (nid, source or f"Outlet {nid}", title or f"Story {nid}",
                                                  f"https://example.org/{nid}", ts, ts, lane, direction, category, zh))


def _paper(conn, pid, *, journal="TPR", radar=0, direction=None, days=1, zh=None):
    ts = _ago(days=days)
    conn.execute("INSERT INTO feed_papers(id, doi, journal, title, published, first_seen, radar, direction, zh_title) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (pid, f"10.1/{pid}", journal, f"Paper {pid}", ts[:10], ts, radar, direction, zh))


def _sources(monkeypatch, outlets=(), journals=()):
    from research_lab.feeds import sources
    monkeypatch.setattr(sources, "load", lambda paths=None: sources.Sources(
        news=[sources.NewsFeed(name, "u", "world", weight=w) for name, w in outlets],
        journals=[sources.Journal(abbr, abbr, (abbr,), weight=w) for abbr, w in journals]))


def _brief(conn, *events):
    """Today's brief, its events citing the given feed_news ids."""
    set_setting(conn, "news.brief:" + TODAY, {"date": TODAY, "events": [
        {"title": f"事件 {n}", "what": "x", "why": None, "region": "全球", "direction": None,
         "sources": [{"id": i, "source": "?", "url": "u", "title": "t"} for i in ids]} for n, ids in enumerate(events)]})


def _stored(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", ("feeds.picks:" + TODAY,)).fetchone()
    return json.loads(row[0]) if row else {"news": [], "papers": []}


def _world(conn, ids) -> list[str]:
    lanes = dict(conn.execute("SELECT id, lane FROM feed_news").fetchall())
    return [i for i in ids if lanes.get(i) == "world"]


def _research(conn, n=4):
    for i in range(n):   # an hour apart, newest first: a shared timestamp would order them by the clock's second
        _news(conn, f"r{i}", lane="research", hours=i + 1)


def _brief_job(conn, status, minutes_ago, n=0):
    conn.execute("INSERT INTO ai_jobs(id, kind, target_id, status, request_json, schema_json, created_at) "
                 "VALUES (?, 'news.brief', ?, ?, '{}', '{}', ?)", (f"J-brief-{status}-{n}", TODAY, status, _ago(minutes=minutes_ago)))


def _budget_spent(conn):
    """A push run that spent today's budget, stamped at local midnight: the budget counts from local midnight, so
    "5 minutes ago" would be yesterday in a day's first minutes."""
    from research_lab.ai.cost import local_day_start_utc
    from research_lab.feeds import enrich
    at = local_day_start_utc()
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) "
                 "VALUES ('J-1', 'feeds.enrich', 'done', '{}', '{}', ?)", (at,))
    conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at, cost_usd) VALUES ('J-1', 'deepseek', 'deepseek-flash', ?, ?)",
                 (at, (enrich.FEEDS_DAILY_CNY + 0.01) / enrich.USD_CNY))


def _no_brief(conn):
    """Today's brief failed for good, so the fallback 世界要闻 are stored at once."""
    from research_lab.feeds import brief
    for n in range(brief.MAX_TRIES):
        _brief_job(conn, "failed", 5, n)


# ---------------------------------------------------------------- 世界要闻

def test_world_news_follows_the_brief_one_report_per_event(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch, outlets=[("The Economist", 95), ("BBC World", 90), ("Al Jazeera", 78), ("NPR", 70)])
    _news(conn, "e0_bbc", source="BBC World")
    _news(conn, "e0_eco", source="The Economist", hours=3)                       # the weightiest outlet wins
    _news(conn, "e0_aj", source="Al Jazeera")
    _news(conn, "e1_old", source="BBC World", hours=5)
    _news(conn, "e1_new", source="BBC World", hours=2)                           # same outlet: the newest
    _news(conn, "e2_seen", source="NPR")                                         # picked on an earlier day: the event is skipped
    for n in range(3, 9):
        _news(conn, f"e{n}", source="NPR")                                       # no outlet cap across events
    _news(conn, "r_lane", lane="research")                                       # cited by the brief, not a world report
    set_setting(conn, "feeds.picks:" + _last_weekday(), {"news": ["e2_seen"], "papers": []})
    _research(conn)
    _brief(conn, ["e0_bbc", "e0_eco", "e0_aj"], ["e1_old", "e1_new"], ["e2_seen"], ["r_lane", "e3"],
           *[[f"e{n}"] for n in range(4, 9)])
    issue = digest.fill(conn, now=MORNING)
    assert _world(conn, issue["news"]) == ["e0_eco", "e1_new", "e3", "e4", "e5", "e6"]
    assert issue["provisional"] == [] and _stored(conn)["news"] == issue["news"]  # the brief settles the slots


def test_before_the_brief_world_picks_are_provisional_and_the_brief_takes_the_slots(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch, outlets=[("The Economist", 95), ("BBC World", 90), ("Al Jazeera", 78), ("DW", 72), ("NPR", 70),
                                   ("France 24", 69), ("UN News", 68)])
    _news(conn, "eco1", source="The Economist", hours=1)
    _news(conn, "eco2", source="The Economist", hours=2)                         # one per outlet before the brief
    for nid, src in (("bbc1", "BBC World"), ("aj1", "Al Jazeera"), ("dw1", "DW"), ("npr1", "NPR"), ("f24", "France 24"),
                     ("un1", "UN News")):
        _news(conn, nid, source=src, hours=4)
    for i in range(2):                                                           # enough world news for a brief
        _news(conn, f"minor{i}", hours=10)
    _research(conn)
    early = digest.fill(conn, now=MORNING)
    assert early["provisional"] == ["eco1", "bbc1", "aj1", "dw1", "npr1", "f24"]  # outlet weight, then the newest
    assert _world(conn, _stored(conn)["news"]) == [] and len(_stored(conn)["news"]) == 4   # shown, not stored
    _brief(conn, ["npr1", "dw1"], ["un1"])
    issue = digest.fill(conn, now=MORNING)
    assert _world(conn, issue["news"]) == ["dw1", "un1", "eco1", "bbc1", "aj1", "npr1"]   # the brief, then by weight
    assert issue["provisional"] == [] and _world(conn, _stored(conn)["news"]) == _world(conn, issue["news"])


def test_an_afternoon_first_visit_still_waits_for_the_brief(conn, monkeypatch):
    """0083 review: Leo often first opens the app after noon, and the page asks for its picks before the refresh
    that queues the brief has run; the wait follows the brief's job, not the clock."""
    from research_lab.feeds import brief, digest
    _sources(monkeypatch)
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}", hours=i + 1)
    _research(conn)
    first = digest.fill(conn, now=ONE_PM)
    assert len(first["provisional"]) == 6 and _world(conn, _stored(conn)["news"]) == []   # no job yet: wait
    job = brief.maybe_enqueue(conn, dispatch_jobs=False, now=ONE_PM)              # the refresh a minute later
    conn.execute("UPDATE ai_jobs SET created_at = ? WHERE id = ?", ((ONE_PM + timedelta(minutes=1)).astimezone(timezone.utc)
                                                                     .isoformat(timespec="seconds"), job))
    assert job and len(digest.fill(conn, now=ONE_PM + timedelta(minutes=2))["provisional"]) == 6   # waiting while it runs
    conn.execute("UPDATE ai_jobs SET status = 'done' WHERE id = ?", (job,))
    _brief(conn, ["w9"], ["w8"])
    issue = digest.fill(conn, now=ONE_PM)
    assert _world(conn, issue["news"])[:2] == ["w9", "w8"] and issue["provisional"] == []
    assert _world(conn, _stored(conn)["news"]) == _world(conn, issue["news"])


def test_the_fallback_is_kept_once_the_brief_cannot_come(conn, monkeypatch):
    from research_lab.feeds import brief, digest
    _sources(monkeypatch)
    for i in range(6):                                                           # too little world news for a brief
        _news(conn, f"w{i}", hours=i + 1)
    _research(conn)
    issue = digest.fill(conn, now=ONE_PM)
    assert issue["provisional"] == [] and _world(conn, _stored(conn)["news"]) == [f"w{i}" for i in range(6)]
    _news(conn, "late")
    _brief(conn, ["late"])
    assert digest.fill(conn, now=ONE_PM)["news"] == issue["news"]                 # full: what Leo saw stays
    conn.execute("DELETE FROM settings WHERE key LIKE 'news.brief:%'")
    for i in range(6, 12):
        _news(conn, f"w{i}")
    assert brief.may_still_come(conn, TODAY, ONE_PM)                             # enough news, none tried yet

    def waiting(*jobs) -> bool:
        conn.execute("DELETE FROM ai_jobs")
        for n, (status, minutes_ago) in enumerate(jobs):
            _brief_job(conn, status, minutes_ago, n)
        return brief.may_still_come(conn, TODAY, datetime.now())
    assert waiting(("queued", 30)) and waiting(("failed", 20))                    # running, or a retry to come
    assert not waiting(("queued", 150))                                          # 2 hours after the first try
    assert not waiting(("done", 5))                                              # done, every event dropped
    assert not waiting(*[("failed", 5)] * brief.MAX_TRIES)
    assert not brief.may_still_come(conn, (date.today() - timedelta(days=1)).isoformat(), datetime.now())
    conn.execute("DELETE FROM ai_jobs")
    _budget_spent(conn)
    assert not brief.may_still_come(conn, TODAY, datetime.now())                 # over the push budget: none will be queued


# ---------------------------------------------------------------- 研究相关 and papers

def test_research_ranks_keyword_hits_then_outlet_weight_then_recency_two_per_outlet(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch, outlets=[("Lowy", 82), ("The Diplomat", 75), ("War on the Rocks", 88)])
    _news(conn, "kw_lowy", lane="research", source="Lowy", direction="sea-asean", hours=6)
    for n, h in ((1, 5), (2, 4), (3, 3)):
        _news(conn, f"kw_dip{n}", lane="research", source="The Diplomat", direction="sea-asean", hours=h)
    _news(conn, "plain_wotr", lane="research", source="War on the Rocks", hours=1)
    assert digest.fill(conn)["news"] == ["kw_lowy", "kw_dip3", "kw_dip2", "plain_wotr"]


def test_papers_rank_by_journal_weight_radar_and_direction(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch, journals=[("EJIR", 100), ("SD", 80), ("TPR", 54)])
    _paper(conn, "ejir", journal="EJIR", days=3)                                 # 2 for the journal
    _paper(conn, "sd_radar", journal="SD", radar=1, days=2)                       # 1 + 1
    _paper(conn, "tpr_both", radar=1, direction="sea-asean", days=1)              # 1 + 1
    _paper(conn, "sd", journal="SD", days=1)                                      # 1
    _paper(conn, "tpr", days=0)                                                   # 0, though the newest
    _paper(conn, "gone", journal="EJIR", radar=1, days=12)                        # outside the 10-day window
    assert digest.fill(conn)["papers"] == ["tpr_both", "sd_radar", "ejir", "sd", "tpr"]


def test_papers_take_at_most_three_per_journal_unless_slots_would_stay_empty(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch, journals=[("RIS", 88)])
    for i in range(8):                                                           # one journal's batch, all strong
        _paper(conn, f"ris{i}", journal="RIS", radar=1, direction="sea-asean", days=1 + i * 0.1)
    for i in range(4):
        _paper(conn, f"o{i}", journal=f"J{i}", days=1 + i * 0.1)                # a few others, one journal each
    assert digest.fill(conn)["papers"] == ["ris0", "ris1", "ris2", "o0", "o1", "o2", "o3",
                                           "ris3", "ris4", "ris5"]               # too few others: the cap gives way
    conn.execute("DELETE FROM settings WHERE key LIKE 'feeds.picks:%'")
    for i in range(4, 10):
        _paper(conn, f"o{i}", journal=f"J{i}", days=1 + i * 0.1)
    set_setting(conn, "feeds.picks:" + TODAY, {"news": [], "papers": ["ris0", "ris1"]})   # picked earlier today
    assert digest.fill(conn)["papers"] == ["ris0", "ris1", "ris2"] + [f"o{i}" for i in range(7)]


def test_a_brief_pick_without_chinese_takes_the_events_chinese(conn, monkeypatch):
    from research_lab.feeds import digest, enrich
    _sources(monkeypatch)
    _news(conn, "bare")
    _news(conn, "own", zh="自己的中文标题")
    conn.execute("UPDATE feed_news SET region = '欧洲' WHERE id = 'own'")
    set_setting(conn, "news.brief:" + TODAY, {"date": TODAY, "events": [
        {"title": "苏丹停火谈判恢复", "what": "双方重回谈判桌。", "why": "影响红海航运。", "region": "非洲",
         "sources": [{"id": "bare"}]},
        {"title": "另一件事", "what": "别的事。", "why": None, "region": "中东", "sources": [{"id": "own"}]}]})
    _research(conn)
    assert _world(conn, digest.fill(conn, now=MORNING)["news"]) == ["bare", "own"]
    rows = {r["id"]: r for r in conn.execute("SELECT id, zh_title, zh_summary, angle, region FROM feed_news")}
    assert (rows["bare"]["zh_title"], rows["bare"]["zh_summary"], rows["bare"]["angle"], rows["bare"]["region"]) == \
        ("苏丹停火谈判恢复", "双方重回谈判桌。", "影响红海航运。", "非洲")                # the region filter finds it
    assert (rows["own"]["zh_title"], rows["own"]["zh_summary"], rows["own"]["region"]) == ("自己的中文标题", None, "欧洲")
    enrich.enqueue_pending(conn, dispatch_jobs=False)
    sent = {r[0] for r in conn.execute("SELECT id FROM feed_news WHERE enrich_batch IS NOT NULL")}
    assert "bare" not in sent and "own" not in sent and sent == {f"r{i}" for i in range(4)}


def test_announcements_never_enter(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch)
    _news(conn, "ann", lane="research", title="The Navigator’s September Issue — Out Now")   # news.is_not_news
    _news(conn, "flagged", lane="research", category="非新闻")
    _news(conn, "w_ann", category="非新闻")
    _news(conn, "ok", lane="research")
    assert digest.fill(conn)["news"] == ["ok"]


# ---------------------------------------------------------------- the issue

def test_a_lane_with_no_candidates_lends_its_slots(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch)
    _no_brief(conn)
    for i in range(12):
        _news(conn, f"w{i}", hours=i + 1)
    assert digest.fill(conn)["news"] == [f"w{i}" for i in range(10)] == _stored(conn)["news"]
    conn.execute("DELETE FROM settings WHERE key LIKE 'feeds.picks:%'")
    conn.execute("DELETE FROM feed_news")
    for i in range(12):
        _news(conn, f"r{i}", lane="research", source=f"Outlet {i // 2}", hours=i + 1)   # two per outlet
    _news(conn, "r_third", lane="research", source="Outlet 0", hours=20)
    assert digest.fill(conn)["news"] == [f"r{i}" for i in range(10)]           # an outlet's third waits


def test_top_ups_never_pass_ten(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch)
    _no_brief(conn)
    _research(conn, 2)
    first = digest.fill(conn)["news"]
    assert first == ["r0", "r1"]
    for i in range(12):
        _news(conn, f"w{i}")
    for i in range(2, 8):
        _news(conn, f"r{i}", lane="research")
    second = digest.fill(conn)["news"]
    assert second[:2] == first and len(second) == 10 and len(_world(conn, second)) == 6
    _news(conn, "late", lane="research", direction="sea-asean")
    assert digest.fill(conn)["news"] == second == _stored(conn)["news"]         # full stays full


def test_the_issue_is_stable_and_earlier_picks_do_not_return(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch)
    _news(conn, "r0", lane="research")
    first = digest.fill(conn)["news"]
    _news(conn, "r_kw", lane="research", direction="sea-asean")                  # stronger, arrives later
    again = digest.fill(conn)["news"]
    assert again[: len(first)] == first and again[-1] == "r_kw"                  # appended, not reshuffled
    yesterday = _last_weekday()
    conn.execute("DELETE FROM settings WHERE key = ?", (digest.KEY + TODAY,))
    set_setting(conn, digest.KEY + yesterday, {"news": ["r0"], "papers": []})
    today = digest.fill(conn)["news"]
    assert "r0" not in today and "r_kw" in today
    assert digest.fill(conn, yesterday) == {"news": ["r0"], "papers": [], "provisional": []}   # a past issue stays
    view = digest.items(conn, yesterday)
    assert view["next"] == TODAY and [it["id"] for it in view["research"]] == ["r0"]


def test_a_dismissed_pick_leaves_the_page_but_keeps_its_slot(conn, monkeypatch):
    from research_lab.feeds import digest
    _sources(monkeypatch)
    _news(conn, "r0", lane="research", hours=1)
    _news(conn, "r1", lane="research", hours=2)
    digest.fill(conn)
    conn.execute("UPDATE feed_news SET status = 'dismissed' WHERE id = 'r0'")
    view = digest.items(conn)
    assert [it["id"] for it in view["news"]] == ["r1"] and digest.fill(conn)["news"] == ["r0", "r1"]


# ---------------------------------------------------------------- the AI writes Chinese for the picks only

def test_only_the_picks_without_chinese_go_to_the_ai(conn, monkeypatch):
    from research_lab.ai import queue
    from research_lab.feeds import digest, enrich
    _sources(monkeypatch)
    for i in range(20):
        _news(conn, f"w{i}", hours=i + 1)
    for i in range(8):
        _news(conn, f"r{i}", lane="research", hours=i + 1)
    _news(conn, "r_zh", lane="research", direction="sea-asean", zh="已有中文标题")   # picked first, already in Chinese
    for i in range(12):
        _paper(conn, f"p{i}", days=1 + i * 0.1, zh="已有" if i == 0 else None)
    jobs = enrich.enqueue_pending(conn, dispatch_jobs=False)
    picks = digest.fill(conn)
    assert len(jobs) == 2 and "r_zh" in picks["news"] and "p0" in picks["papers"]
    batched = {r[0] for t in ("feed_news", "feed_papers") for r in conn.execute(f"SELECT id FROM {t} WHERE enrich_batch IS NOT NULL")}
    assert batched == (set(picks["news"]) | set(picks["papers"])) - {"r_zh", "p0"}
    assert len(json.loads(queue.get(conn, jobs[0])["request_json"])["params"]["ids"]) == 9
    assert enrich.enqueue_pending(conn, dispatch_jobs=False) == []              # already in open batches


def test_the_push_stops_once_its_daily_budget_is_spent(conn):
    from research_lab.feeds import brief, enrich
    _news(conn, "r0", lane="research")
    _budget_spent(conn)
    assert enrich.over_budget(conn) and enrich.enqueue_pending(conn, dispatch_jobs=False) == []
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"w{i}")
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=datetime.now().replace(hour=9)) is None
    conn.execute("UPDATE ai_runs SET cost_usd = 0")
    assert enrich.enqueue_pending(conn, dispatch_jobs=False)


# ---------------------------------------------------------------- pages

def test_the_feed_page_and_home_follow_the_digest(client, conn):
    _news(conn, "w0", title="World story w0")
    _news(conn, "r0", lane="research", title="Research story r0")
    _news(conn, "r_old", lane="research", title="Research story r_old", hours=24 * 5)   # outside the news window
    conn.commit()
    page = client.get("/feed?tab=news").text
    block = page.split('id="digest"')[1].split("</section>")[0]
    assert "世界要闻" in block and "研究相关" in block and "World story w0" in block and "Research story r_old" not in block
    assert "Research story r0" in block and "助手估计" not in block                 # no score badges any more
    home = client.get("/").text
    assert "World story w0" in home and "Research story r0" in home and "Research story r_old" not in home
    assert "r0" in _stored(conn)["news"]


def test_today_button_wins_over_leftover_filters(client, conn):
    _news(conn, "w0")
    conn.commit()
    page = client.get("/feed?tab=news&pick=today&view=all&lane=world&q=x").text
    assert 'id="digest"' in page and 'aria-label="今日精选"' in page and 'id="feed-filters"' not in page
    assert 'id="digest"' not in client.get("/feed?tab=news&lane=world").text      # a filtered link still browses


def test_after_a_long_break_the_digest_waits_for_the_refresh_that_brings_todays_news(conn, monkeypatch):
    """0083 re-review: with the app closed a day, items_for is empty until the refresh runs; storing the fallback
    then would shut the brief out for the day."""
    from datetime import datetime, timedelta, timezone
    from research_lab.db.connection import set_setting
    from research_lab.feeds import brief, scheduler, store
    monkeypatch.setattr(scheduler, "running", lambda: False)
    now = datetime.now()
    day = now.date().isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    set_setting(conn, store.LAST_KEY, {"at": stale, "full_at": stale})
    assert brief.may_still_come(conn, day, now)                                  # news not fetched yet: wait
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    set_setting(conn, store.LAST_KEY, {"at": fresh, "full_at": fresh})
    assert not brief.may_still_come(conn, day, now)                              # fetched and still too little


def _paper_on(conn, pid, day: date):
    ts = f"{day.isoformat()}T04:00:00+00:00"
    conn.execute("INSERT INTO feed_papers(id, doi, journal, title, published, first_seen) VALUES (?,?,?,?,?,?)",
                 (pid, f"10.1/{pid}", "TPR", f"Paper {pid}", day.isoformat(), ts))


def test_monday_offers_the_weekend_picks_again_unless_leo_kept_them(conn, monkeypatch):
    """Leo 10-19: the weekend issues are built whether or not he opens them; on a quiet weekend they took every new
    paper and Monday showed none."""
    from research_lab.feeds import digest
    _sources(monkeypatch)
    mon = date.today() - timedelta(days=date.today().weekday())
    fri, sat, sun = (mon - timedelta(days=n) for n in (3, 2, 1))
    for pid, day in (("fri1", fri), ("sat1", sat), ("sat2", sat), ("sun1", sun)):
        _paper_on(conn, pid, day)
    set_setting(conn, digest.KEY + fri.isoformat(), {"news": [], "papers": ["fri1"]})
    set_setting(conn, digest.KEY + sat.isoformat(), {"news": [], "papers": ["sat1", "sat2"]})
    set_setting(conn, digest.KEY + sun.isoformat(), {"news": [], "papers": ["sun1"], "seen": ["sat1"]})
    conn.execute("UPDATE feed_papers SET status = 'saved' WHERE id = 'sat2'")
    monday = digest.fill(conn, now=datetime.combine(mon, datetime.min.time()).replace(hour=9))["papers"]
    assert sorted(monday) == ["sat1", "sun1"]            # Friday's stays shown; the saved one is not offered twice
    tuesday = digest.fill(conn, now=datetime.combine(mon + timedelta(days=1), datetime.min.time()).replace(hour=9))
    assert tuesday["papers"] == []                       # a Tuesday never reaches back past Monday's issue


def test_monday_news_reaches_back_to_saturday_however_short_the_window(conn, monkeypatch):
    from research_lab.feeds import digest
    from research_lab.tunables import set_value
    _sources(monkeypatch)
    set_value(conn, "feeds.digest.news_window_days", 1)
    mon = date.today() - timedelta(days=date.today().weekday())
    ts = f"{(mon - timedelta(days=2)).isoformat()}T04:00:00+00:00"
    conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane) VALUES (?,?,?,?,?,?,?)",
                 ("sat_r", "Outlet", "Story", "https://example.org/s", ts, ts, "research"))
    issue = digest.fill(conn, now=datetime.combine(mon, datetime.min.time()).replace(hour=9))
    assert "sat_r" in issue["news"]
