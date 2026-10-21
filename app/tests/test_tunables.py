"""Card 0076 (SETTINGS-AGENT §3, §5): the tunables registry, tunable() reads that survive broken rows, the push
constants honouring stored values, and the feeds.yml writer that keeps comments and refuses a stale read."""
from __future__ import annotations

import difflib
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from research_lab.db.connection import now_iso, set_setting

# SETTINGS-AGENT §5 plus the keys other cards read by name (0075: pm.*; 0080: chat.reuse_minutes)
SECTION5 = {
    "feeds.digest.news_n": 10, "feeds.digest.papers_n": 10, "feeds.digest.world_ratio": 0.6,
    "feeds.digest.news_window_days": 2, "feeds.digest.papers_window_days": 10, "feeds.digest.repeat_days": 30,
    "feeds.digest.source_boost": "normal", "feeds.home_limit": 3, "feeds.daily_cny": 0.8,
    "feeds.refresh_hours": 24, "feeds.news_fresh_minutes": 30, "feeds.brief_hour": 7, "feeds.autoimport_oa": True,
    "pm.stalled_days": 14, "pm.friday_digest": True, "pm.rule.overdue": True,
    "pm.rule.stalled": True, "chat.reuse_minutes": 30,
}


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _on_day(days_back: int) -> str:
    """A first_seen on a given local calendar day: the windows compare dates, so hours would be flaky near midnight."""
    return (date.today() - timedelta(days=days_back)).isoformat() + "T12:00:00+00:00"


def _news(conn, nid, *, lane="world", first_seen=None, source=None):
    """Each item from its own outlet unless told otherwise: 今日精选 takes one world item per outlet (card 0083)."""
    ts = first_seen or _ago(hours=1)
    conn.execute("INSERT INTO feed_news(id, source, title, link, published, first_seen, lane) VALUES (?,?,?,?,?,?,?)",
                 (nid, source or f"Outlet {nid}", f"Story {nid}", f"https://example.org/{nid}", ts, ts, lane))


def _paper(conn, pid, *, journal="TPR", first_seen=None, radar=0, direction=None):
    ts = first_seen or _ago(days=1)
    conn.execute("INSERT INTO feed_papers(id, doi, journal, title, published, first_seen, radar, direction) "
                 "VALUES (?,?,?,?,?,?,?,?)", (pid, f"10.1/{pid}", journal, f"Paper {pid}", ts[:10], ts, radar, direction))


def _picks(issue):
    return {"news": issue["news"], "papers": issue["papers"]}


# ---------------------------------------------------------------- registry

def test_every_entry_is_complete_and_its_default_validates():
    from research_lab import tunables as T
    seen = set()
    for t in T.entries():
        assert t.key not in seen and t.name and t.type in (T.INT, T.FLOAT, T.BOOL, T.CHOICE, T.STR, T.LIST)
        assert t.store in (T.SETTINGS, T.FEEDS, T.DIRECTIONS) and t.tier in (T.DIRECT, T.CONFIRM, T.NEVER)
        assert not (t.spend and t.tier != T.DIRECT)          # the direction rule only refines a direct entry
        assert t.store == T.SETTINGS or (t.tier == T.CONFIRM and t.default is None)   # §3: files always confirm
        seen.add(t.key)
        if t.family or t.store != T.SETTINGS or t.tier == T.NEVER:
            continue
        assert T.validate(t.key, t.default) == t.default, t.key
        if t.type in (T.INT, T.FLOAT):
            assert t.lo is not None and t.hi is not None and t.unit or t.key == "feeds.digest.world_ratio"


def test_the_section5_keys_and_their_defaults(repo):
    from research_lab import tunables as T
    assert {k: T.default(k) for k in SECTION5} == SECTION5
    assert "ai.daily_cap_cny" not in {t.key for t in T.entries()}   # 0069 is on hold
    reuse = T.entry("chat.reuse_minutes")
    assert (reuse.lo, reuse.hi) == (5, 240)
    assert dict(T.entry("feeds.digest.source_boost").choices) == {"none": "不加分", "normal": "普通", "strong": "强"}
    assert [T.entry(k).tier for k in ("feeds.daily_cny", "ai.mode", "feeds.news[BBC World].enabled",
                                      "directions[sea-asean].keywords", "ai.paused", "related.weights")] == \
        [T.DIRECT, T.CONFIRM, T.CONFIRM, T.CONFIRM, T.NEVER, T.NEVER]
    for gone in ("ai.daily_cap_usd", "feeds.home_min_score"):   # card 0083: nothing is scored, so no bar
        with pytest.raises(KeyError):
            T.entry(gone)


def test_registry_defaults_agree_with_the_code_that_reads_them():
    from research_lab import tunables as T
    from research_lab.agent import skills
    from research_lab.ai import claude_code, pricing
    from research_lab.web.routes import settings
    assert T.default("ai.claude_daily_runs") == claude_code.DEFAULT_DAILY_RUNS
    assert T.default("ai.model") == pricing.DEFAULT_MODEL
    assert dict(T.SKILL_TIERS) == skills.TIER_LABEL and tuple(dict(T.SKILL_TIERS)) == skills.TIERS
    assert tuple(dict(T.entry("ui.theme").choices)) == settings.THEMES
    assert tuple(dict(T.entry("ai.mode").choices)) == settings.MODES
    assert T.WEEK_RE == settings.WEEK_RE.pattern.strip("^$") and T.MODEL_RE == settings.MODEL_RE.pattern.strip("^$")
    assert T.default("engine.deepseek.deep") == skills.ENGINE_TIERS["deepseek"]["deep"]
    chat = skills.get("chat")
    assert T.default("skill.chat.task_tier") == (chat.task_tier or chat.tier)
    with pytest.raises(KeyError):
        T.entry("skill.no-such-skill.tier")


def test_validate_takes_labels_and_refuses_what_the_entry_does_not_take():
    from research_lab import tunables as T
    assert T.validate("ui.theme", "深色") == "dark" and T.validate("feeds.autoimport_oa", "关") is False
    assert T.validate("feeds.digest.papers_n", "5") == 5 and T.validate("feeds.digest.world_ratio", 0.7) == 0.7
    assert T.validate("feeds.digest.source_boost", "强") == "strong" and T.validate("year.start_week", "") is None
    assert T.validate("feeds.gp_terms", ["hierarch", " war"]) == ["hierarch", " war"]   # the space is part of the root
    assert T.validate("feeds.radar_terms", "hierarch， care") == ["hierarch", "care"]
    for key, bad in (("feeds.digest.world_ratio", 0.65), ("feeds.digest.papers_n", 4), ("feeds.digest.papers_n", 5.5),
                     ("feeds.digest.source_boost", "超强"), ("feeds.autoimport_oa", "maybe"), ("feeds.home_limit", True),
                     ("year.start_week", "2026-W60"), ("feeds.news[BBC World].keep", ["world"])):
        with pytest.raises(T.TunableError):
            T.validate(key, bad)
    assert T.label("feeds.daily_cny", 0.5) == "0.5 元" and T.label("feeds.digest.source_boost", "none") == "不加分"


# ---------------------------------------------------------------- settings table

def test_tunable_falls_back_to_the_default_on_a_hand_broken_value(conn):
    from research_lab.tunables import tunable
    assert tunable(conn, "feeds.digest.papers_n") == 10
    for bad in (3, 99, "lots", None, [5], True):
        set_setting(conn, "feeds.digest.papers_n", bad)
        assert tunable(conn, "feeds.digest.papers_n") == 10, bad
    conn.execute("UPDATE settings SET value = 'not json' WHERE key = 'feeds.digest.papers_n'")
    assert tunable(conn, "feeds.digest.papers_n") == 10
    set_setting(conn, "feeds.digest.papers_n", 5)
    assert tunable(conn, "feeds.digest.papers_n") == 5
    set_setting(conn, "skill.chat.task_tier", "enormous")
    assert tunable(conn, "skill.chat.task_tier") == "light"
    with pytest.raises(LookupError):
        tunable(conn, "feeds.radar_terms")   # lives in feeds.yml


def test_set_value_reset_changed_and_describe(conn):
    from research_lab import tunables as T
    assert T.changed(conn) == []
    assert T.set_value(conn, "feeds.digest.source_boost", "强") == "strong"
    T.set_value(conn, "skill.chat.task_tier", "deep")
    set_setting(conn, "ui.theme", "system")              # the settings page stores defaults too: not a 微调
    set_setting(conn, "feeds.brief_hour", 99)            # broken: listed so 恢复默认 can clear it
    rows = {d["key"]: d for d in T.changed(conn)}
    assert set(rows) == {"feeds.digest.source_boost", "skill.chat.task_tier", "feeds.brief_hour"}
    boost = rows["feeds.digest.source_boost"]
    assert boost["value_label"] == "强" and boost["default_label"] == "普通"
    assert rows["feeds.brief_hour"]["broken"] and rows["feeds.brief_hour"]["value"] == 7
    assert boost["updated_at"] and boost["tier_label"] == "直接改"
    T.set_value(conn, "feeds.digest.source_boost", "普通")   # back to the default: the row goes
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'feeds.digest.source_boost'").fetchone() is None
    assert T.reset(conn, "feeds.brief_hour") is True and T.reset(conn, "feeds.brief_hour") is False
    assert [d["key"] for d in T.changed(conn)] == ["skill.chat.task_tier"]
    with pytest.raises(T.TunableError):
        T.set_value(conn, "ai.paused", True)             # never: only the settings page pauses
    with pytest.raises(T.TunableError):
        T.set_value(conn, "feeds.digest.news_n", 50)
    assert T.describe(conn, "feeds.digest.news_n")["range_label"] == "5–20 条"


def test_the_tier_follows_the_direction_of_spend():
    from research_lab import tunables as T
    assert T.tier_for("feeds.daily_cny", 0.5, 0.8) == T.DIRECT and T.tier_for("feeds.daily_cny", 1.0, 0.8) == T.CONFIRM
    assert T.tier_for("feeds.autoimport_oa", False, True) == T.DIRECT and T.tier_for("feeds.autoimport_oa", "开", False) == T.CONFIRM
    assert T.tier_for("skill.chat.task_tier", "deep", "light") == T.CONFIRM and T.tier_for("skill.chat.task_tier", "light", "deep") == T.DIRECT
    assert T.tier_for("feeds.digest.world_ratio", 0.7) == T.DIRECT and T.tier_for("ai.mode", "claude") == T.CONFIRM
    small = [("feeds.digest.papers_n", 5, 10), ("feeds.home_limit", 2, 3), ("ui.theme", "dark", "system")]
    assert T.batch_tier(small) == T.DIRECT
    assert T.batch_tier(small + [("feeds.brief_hour", 8, 7)]) == T.CONFIRM          # more than 3 at once
    assert T.batch_tier(small[:1] + [("ai.paused", True, False)]) == T.NEVER


# ---------------------------------------------------------------- the push follows stored values

def test_papers_n_and_world_ratio_shape_the_digest(conn):
    from research_lab.feeds import digest
    for i in range(10):
        _news(conn, f"w{i}")
        _news(conn, f"r{i}", lane="research")
    for i in range(8):
        _paper(conn, f"p{i}")
    set_setting(conn, "feeds.digest.papers_n", 5)
    set_setting(conn, "feeds.digest.world_ratio", 0.7)
    issue = digest.fill(conn)
    assert len(issue["papers"]) == 5
    assert sum(i.startswith("w") for i in issue["news"]) == 7 and sum(i.startswith("r") for i in issue["news"]) == 3


def test_with_nothing_stored_the_digest_is_six_to_four_and_ten_papers(conn):
    from research_lab.feeds import digest
    for i in range(10):
        _news(conn, f"w{i}")
        _news(conn, f"r{i}", lane="research")
    for i in range(12):
        _paper(conn, f"p{i}")
    issue = digest.fill(conn)
    assert len(issue["papers"]) == 10 and [i[0] for i in issue["news"]].count("w") == 6
    assert digest.knobs(conn) == digest.Knobs(10, 10, 6, 2, 10, 30, 1.0)


def test_news_n_windows_and_repeat_days(conn, monkeypatch):
    from research_lab.feeds import digest
    monkeypatch.setattr(digest, "_weekend", lambda day: None)   # Monday's reach over the weekend: test_feed_digest
    _news(conn, "w_today")
    _news(conn, "w_2d", first_seen=_on_day(2))
    _paper(conn, "p_5d", first_seen=_on_day(5))
    old_day = (date.today() - timedelta(days=10)).isoformat()
    set_setting(conn, digest.KEY + old_day, {"news": ["w_today"], "papers": []})   # picked 10 days ago
    assert _picks(digest.fill(conn)) == {"news": ["w_2d"], "papers": ["p_5d"]}   # defaults: 2-day news, 10-day papers, 30-day repeat
    conn.execute("DELETE FROM settings WHERE key = ?", (digest.KEY + date.today().isoformat(),))
    for key, v in (("news_window_days", 1), ("papers_window_days", 3), ("repeat_days", 7), ("news_n", 5)):
        set_setting(conn, f"feeds.digest.{key}", v)
    assert _picks(digest.fill(conn)) == {"news": ["w_today"], "papers": []}
    assert digest.knobs(conn).news_n == 5 and digest.knobs(conn).world_n == 3   # 5 × 0.6 = 3


def test_source_boost_scales_the_journal_bonus(conn, monkeypatch):
    from research_lab.feeds import digest, sources
    monkeypatch.setattr(sources, "load", lambda paths=None: sources.Sources(journals=[
        sources.Journal("TOPJ", "Top", ("1",), weight=100), sources.Journal("LOWJ", "Low", ("2",), weight=40)]))
    _paper(conn, "top", journal="TOPJ", first_seen=_ago(days=2))
    _paper(conn, "low_hits", journal="LOWJ", radar=1, direction="sea-asean")
    _paper(conn, "low", journal="LOWJ")
    assert digest.fill(conn)["papers"] == ["low_hits", "top", "low"]     # 普通: 2 = 2, the newer first

    def again(boost):
        conn.execute("DELETE FROM settings WHERE key LIKE 'feeds.picks:%'")
        set_setting(conn, "feeds.digest.source_boost", boost)
        return digest.fill(conn)["papers"]
    assert again("none") == ["low_hits", "low", "top"]
    assert again("strong") == ["top", "low_hits", "low"]


def test_home_limit(conn):
    from research_lab.feeds import store
    for i in range(4):
        _news(conn, f"r{i}", lane="research")
    assert len(store.home(conn)["news"]) == 3
    set_setting(conn, "feeds.home_limit", 1)
    assert len(store.home(conn)["news"]) == 1 and len(store.home(conn, limit=2)["news"]) == 2


def test_daily_cny_refresh_brief_and_autoimport_follow_their_settings(conn):
    from research_lab.feeds import brief, enrich, importer, scheduler, store
    conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) "
                 "VALUES ('J-1', 'feeds.enrich', 'done', '{}', '{}', ?)", (_ago(minutes=5),))
    conn.execute("INSERT INTO ai_runs(job_id, provider, model, started_at, cost_usd) VALUES ('J-1', 'deepseek', 'x', ?, ?)",
                 (_ago(minutes=5), 0.6 / enrich.USD_CNY))
    assert not enrich.over_budget(conn)
    set_setting(conn, "feeds.daily_cny", 0.5)
    assert enrich.over_budget(conn)

    set_setting(conn, store.LAST_KEY, {"at": now_iso()})
    assert scheduler.seconds_until_due(conn) > 24 * 3600 - 60
    set_setting(conn, "feeds.refresh_hours", 6)
    assert 6 * 3600 - 60 < scheduler.seconds_until_due(conn) <= 6 * 3600
    set_setting(conn, "feeds.news_fresh_minutes", 90)
    assert scheduler.news_fresh(conn) == 90 * 60

    conn.execute("UPDATE ai_runs SET cost_usd = 0")
    for i in range(brief.MIN_ITEMS):
        _news(conn, f"b{i}")
    nine = datetime.now().replace(hour=9, minute=0)
    set_setting(conn, "feeds.brief_hour", 10)
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=nine) is None
    conn.execute("DELETE FROM settings WHERE key = 'feeds.brief_hour'")
    assert brief.maybe_enqueue(conn, dispatch_jobs=False, now=nine)

    assert importer.autoimport_enabled(conn) is True
    set_setting(conn, importer.AUTO_KEY, False)
    assert importer.autoimport_enabled(conn) is False
    set_setting(conn, importer.AUTO_KEY, "garbled")
    assert importer.autoimport_enabled(conn) is True


# ---------------------------------------------------------------- feeds.yml writer (a temp copy: the repo fixture)

def _feeds(repo):
    return repo / "_data" / "feeds.yml"


def _diff(before: str, after: str) -> list[str]:
    return [line for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0, lineterm="")
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def test_one_source_field_changes_one_line_and_keeps_the_comments(repo):
    from research_lab.feeds import sources
    from research_lab.files import datafiles
    path = _feeds(repo)
    before = path.read_text(encoding="utf-8")
    sha = datafiles.file_sha("feeds")
    sha = datafiles.update_feed_source("news", "Foreign Policy", {"enabled": False}, sha)
    after = path.read_text(encoding="utf-8")
    assert _diff(before, after) == [
        '-  - {name: "Foreign Policy", url: "https://foreignpolicy.com/feed/", weight: 88}',
        '+  - {name: "Foreign Policy", url: "https://foreignpolicy.com/feed/", enabled: false, weight: 88}']
    assert [ln for ln in before.splitlines() if ln.lstrip().startswith("#")] == \
        [ln for ln in after.splitlines() if ln.lstrip().startswith("#")]
    src = sources.load()
    assert "Foreign Policy" not in {f.name for f in src.news} and "Foreign Policy" in {n for n, _ in src.disabled_news}

    sha = datafiles.update_feed_source("journal", "sd", {"weight": 90}, sha)     # by abbr, any case
    assert sources.load().journal("SD").weight == 90
    sha = datafiles.update_feed_source("news", "East Asia Forum", {"enabled": True}, sha)
    eaf = datafiles.feed_source("news", "East Asia Forum")
    assert "enabled" not in eaf and eaf["note"]                                  # absent means on; the note stays
    sha = datafiles.update_feed_source("news", "BBC World", {"keep": ["/news/world/"], "weight": 91}, sha)
    assert sources.load().news[[f.name for f in sources.load().news].index("BBC World")].keep == ("/news/world/",)
    sha = datafiles.update_feed_source("news", "BBC World", {"keep": []}, sha)
    assert "keep" not in datafiles.feed_source("news", "BBC World")
    assert len(_diff(before, path.read_text(encoding="utf-8"))) == 8               # four sources, one line each
    with pytest.raises(KeyError):
        datafiles.update_feed_source("news", "No Such Feed", {"enabled": False}, sha)
    with pytest.raises(datafiles.DataFileError):
        datafiles.update_feed_source("journal", "SD", {"weight": 140}, sha)
    with pytest.raises(datafiles.DataFileError):
        datafiles.update_feed_source("journal", "SD", {"keep": ["/x/"]}, sha)   # journals have no sections


def test_a_hand_edit_since_the_read_stops_the_write(repo):
    from research_lab.files import datafiles
    from research_lab.files.yamlio import StaleFileError
    path = _feeds(repo)
    sha = datafiles.file_sha("feeds")
    edited = path.read_text(encoding="utf-8") + "# Leo was here\n"
    path.write_text(edited, encoding="utf-8")
    with pytest.raises(StaleFileError):
        datafiles.update_feed_source("news", "Foreign Policy", {"enabled": False}, sha)
    with pytest.raises(StaleFileError):
        datafiles.set_feed_terms("radar_terms", ["gender"], sha)
    with pytest.raises(ValueError):
        datafiles.update_feed_source("news", "Foreign Policy", {"enabled": False}, None)
    assert path.read_text(encoding="utf-8") == edited


def test_word_lists_keep_their_quoting(repo):
    from research_lab.feeds import sources
    from research_lab.files import datafiles
    path = _feeds(repo)
    before = path.read_text(encoding="utf-8")
    terms = sources.load().radar_terms
    datafiles.set_feed_terms("radar_terms", terms + ["World Order"], datafiles.file_sha("feeds"))
    (old, new), = zip(*[iter(_diff(before, path.read_text(encoding="utf-8")))] * 2)
    assert new == old.replace('"care "]', '"care ", "world order"]').replace("-radar", "+radar")
    assert sources.load().radar_terms[-1] == "world order"


def test_set_file_value_reads_and_writes_through_the_registry(repo):
    from research_lab import tunables as T
    from research_lab.files import datafiles
    from research_lab.files.yamlio import StaleFileError
    d = T.describe(None, "feeds.journal[SD].weight")
    assert d["value"] == 80 and d["sha"] and d["tier"] == T.CONFIRM
    sha = T.set_file_value("feeds.journal[SD].weight", "90", d["sha"])
    assert T.describe(None, "feeds.journal[SD].weight")["value"] == 90
    with pytest.raises(StaleFileError):
        T.set_file_value("feeds.journal[SD].weight", 70, d["sha"])           # the sha the caller read is gone
    with pytest.raises(T.TunableError):
        T.set_file_value("feeds.journal[SD].weight", 101, sha)
    assert T.describe(None, "feeds.news[Foreign Policy].enabled")["value"] is True
    kw = T.describe(None, "directions[sea-asean].keywords")
    T.set_file_value("directions[sea-asean].keywords", kw["value"] + ["南海仲裁"], kw["sha"])
    assert datafiles.direction("sea-asean")["keywords"][-1] == "南海仲裁"
    with pytest.raises(KeyError):
        T.describe(None, "feeds.news[No Such Feed].enabled")
    with pytest.raises(LookupError):
        T.set_file_value("feeds.digest.news_n", 5, sha)
    assert json.loads(json.dumps(T.describe(None, "feeds.radar_terms")))["value"]   # tool output stays JSON-safe


def test_review_fixes_overflow_spend_tier_and_term_quoting(conn, tmp_path, monkeypatch):
    """Card 0076 review: a 309-digit stored number falls back instead of raising; a spend change with no old
    value confirms unless the current one is read; new terms that YAML would turn into non-strings are quoted."""
    from research_lab import tunables
    from research_lab.db.connection import set_setting
    set_setting(conn, "feeds.daily_cny", 10 ** 320)
    assert tunables.tunable(conn, "feeds.daily_cny") == tunables.entry("feeds.daily_cny").default
    assert tunables.tier_for("feeds.daily_cny", 0.7) == "confirm"
    set_setting(conn, "feeds.daily_cny", 0.3)
    assert tunables.tier_for("feeds.daily_cny", 0.7, conn=conn) == "confirm"
    assert tunables.tier_for("feeds.daily_cny", 0.2, conn=conn) == "direct"


def test_new_terms_that_yaml_would_retype_stay_strings(repo):
    from research_lab.feeds import sources
    from research_lab.files import datafiles
    _feeds(repo)
    terms = sources.load().radar_terms
    datafiles.set_feed_terms("radar_terms", terms + ["null", "true", "1e3"], datafiles.file_sha("feeds"))
    assert sources.load().radar_terms[-3:] == ["null", "true", "1e3"]
