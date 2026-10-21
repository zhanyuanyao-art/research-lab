"""The settings skill (docs/design/SETTINGS-AGENT.md §3, as changed by Leo on 10-10 in ASSISTANT-UPGRADE.md §2):
read_settings / change_settings over the tunables whitelist, every change applied at once and undoable through
assistant_changes kind 'setting', feeds.yml and directions.yml written against their sha, keys masked."""
from __future__ import annotations

import json

import httpx
import pytest

from research_lab import tunables as TU
from research_lab.agent import skills, tools
from research_lab.pm import changes as CH


def ask(conn, prompt):
    """A request on the settings skill, as the router stores it."""
    from research_lab.agent import tasks
    from research_lab.chat import service
    tid = service.create_thread(conn, "调设置")
    service.add_message(conn, tid, "user", prompt)
    ph = service.add_message(conn, tid, "assistant", "", card_kind="task")
    return tasks.create(conn, prompt, thread_id=tid, message_id=ph.id, skill="settings")


def call(conn, name, args, task_id=None):
    text, err = tools.call(conn, name, args, 4000, task_id=task_id)
    assert not err, text
    return text


def change(conn, prompt, *items):
    return call(conn, "change_settings", {"changes": list(items)}, ask(conn, prompt))


# ---------------------------------------------------------------- read_settings

def test_read_settings_lists_what_changed_and_counts_sources_without_listing_them(conn):
    text = call(conn, "read_settings", {})
    assert "all settings are at their defaults" in text and "push sources:" in text
    assert "Foreign Policy" not in text and "feeds.news[" not in text            # sources are not listed one by one
    TU.set_value(conn, "feeds.digest.papers_n", 6)
    assert "feeds.digest.papers_n · 每天论文篇数 · 现在 6 篇 · 默认 10 篇" in call(conn, "read_settings", {})


def test_read_settings_finds_by_name_key_and_source(conn):
    assert "feeds.digest.papers_n" in call(conn, "read_settings", {"query": "论文"})
    sources = call(conn, "read_settings", {"query": "Foreign Policy"})
    assert "feeds.news[Foreign Policy].enabled · 来源「Foreign Policy」开关 · 现在 开" in sources
    assert "feeds.journal[FPA].enabled" not in sources or "期刊「FPA」" in sources
    assert "directions[sea-asean].keywords" in call(conn, "read_settings", {"query": "东南亚 关键词"})
    assert "只能在设置页改" in call(conn, "read_settings", {"query": "暂停"})


def test_read_settings_explains_what_is_not_a_setting(conn):
    assert "推送不再按分数门槛挑" in call(conn, "read_settings", {"query": "推送门槛"})
    assert "app/.env" in call(conn, "read_settings", {"query": "DeepSeek key"})
    assert "每天花费上限还没接进" in call(conn, "read_settings", {"query": "花费上限"})


# ---------------------------------------------------------------- change_settings: direct, recorded, undoable

def test_a_change_applies_at_once_is_recorded_and_undoes(conn):
    text = change(conn, "论文每天 5 篇就够了", {"key": "feeds.digest.papers_n", "new": 5})
    assert TU.tunable(conn, "feeds.digest.papers_n") == 5
    assert text.startswith("已改 1 处。") and "每天论文篇数: 10 篇 → 5 篇" in text and "撤销" in text
    (row,) = conn.execute("SELECT * FROM assistant_changes WHERE kind = 'setting'").fetchall()
    assert row["status"] == "applied" and row["target"] == "feeds.digest.papers_n" and row["actor"] == "assistant"
    assert json.loads(row["detail_json"]) == {"old": 10, "new": 5}
    back = CH.undo(conn, row["batch_id"])
    assert len(back["undone"]) == 1 and TU.tunable(conn, "feeds.digest.papers_n") == 10
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'feeds.digest.papers_n'").fetchone() is None   # back to default


def test_out_of_range_never_and_off_list_are_refused_with_the_reason(conn):
    text = change(conn, "论文每天 50 篇", {"key": "feeds.digest.papers_n", "new": 50})
    assert "每天论文篇数的范围是 5–20 篇" in text and TU.tunable(conn, "feeds.digest.papers_n") == 10
    assert "只能在设置页改" in change(conn, "暂停 AI", {"key": "ai.paused", "new": True})
    assert "app/.env" in change(conn, "换 key", {"key": "ai.deepseek_api_key", "new": "x"})
    assert "每天花费上限还没接进" in change(conn, "上限提到 3 块", {"key": "ai.daily_cap_cny", "new": 3})
    assert conn.execute("SELECT COUNT(*) FROM assistant_changes").fetchone()[0] == 0


def test_what_used_to_need_a_card_now_applies_directly(conn):
    change(conn, "运行方式改成 DeepSeek", {"key": "ai.mode", "new": "DeepSeek"})
    change(conn, "对话用好一点的模型", {"key": "skill.chat.tier", "new": "标准"})
    change(conn, "Claude 每天多跑一些", {"key": "ai.claude_daily_runs", "new": 300})
    assert TU.tunable(conn, "ai.mode") == "deepseek" and TU.tunable(conn, "skill.chat.tier") == "standard"
    assert TU.tunable(conn, "ai.claude_daily_runs") == 300
    assert {r[0] for r in conn.execute("SELECT status FROM assistant_changes")} == {"applied"}


def test_a_feeds_source_changes_against_the_files_sha_and_undo_respects_a_hand_edit(conn):
    from research_lab.files import datafiles
    text = change(conn, "把 Foreign Policy 这个源关掉", {"key": "feeds.news[Foreign Policy].enabled", "new": "关"})
    assert datafiles.feed_source("news", "Foreign Policy").get("enabled") is False and "开 → 关" in text
    (batch,) = [r[0] for r in conn.execute("SELECT batch_id FROM assistant_changes")]
    CH.undo(conn, batch)
    assert datafiles.feed_source("news", "Foreign Policy").get("enabled", True) is True
    # done again, then Leo turns it back on by hand: the undo leaves his edit alone
    change(conn, "把 Foreign Policy 这个源关掉", {"key": "feeds.news[Foreign Policy].enabled", "new": False})
    batch = conn.execute("SELECT batch_id FROM assistant_changes ORDER BY id DESC LIMIT 1").fetchone()[0]
    datafiles.update_feed_source("news", "Foreign Policy", {"enabled": True}, datafiles.file_sha("feeds"))
    datafiles.update_feed_source("news", "Foreign Policy", {"weight": 70}, datafiles.file_sha("feeds"))
    out = CH.undo(conn, batch)
    assert out["kept"] == [] and out["same"]                                      # it already holds the old value
    assert datafiles.feed_source("news", "Foreign Policy")["weight"] == 70


def test_a_change_leo_made_since_is_kept_on_undo(conn):
    change(conn, "Security Dialogue 往前排", {"key": "feeds.journal[SD].weight", "new": 90})
    batch = conn.execute("SELECT batch_id FROM assistant_changes ORDER BY id DESC LIMIT 1").fetchone()[0]
    from research_lab.files import datafiles
    datafiles.update_feed_source("journal", "SD", {"weight": 95}, datafiles.file_sha("feeds"))
    out = CH.undo(conn, batch)
    (kept,) = out["kept"]
    assert kept["note"] == "这项你后来改成了 95，我没动" and datafiles.feed_source("journal", "SD")["weight"] == 95


def test_direction_keywords_take_add_and_remove(conn):
    from research_lab.files import datafiles
    change(conn, "东盟方向加个关键词：南海仲裁", {"key": "directions[sea-asean].keywords", "add": ["南海仲裁"]})
    assert datafiles.direction("sea-asean")["keywords"][-1] == "南海仲裁"
    change(conn, "去掉 hedging", {"key": "directions[sea-asean].keywords", "remove": ["hedging"]})
    assert "hedging" not in datafiles.direction("sea-asean")["keywords"]
    for (batch,) in conn.execute("SELECT DISTINCT batch_id FROM assistant_changes ORDER BY id DESC").fetchall():
        CH.undo(conn, batch)
    kw = datafiles.direction("sea-asean")["keywords"]
    assert "hedging" in kw and "南海仲裁" not in kw


def test_settings_page_undo_works_with_the_real_handler(client, conn):
    change(conn, "把 Foreign Policy 这个源关掉", {"key": "feeds.news[Foreign Policy].enabled", "new": False})
    batch = conn.execute("SELECT batch_id FROM assistant_changes").fetchone()[0]
    r = client.post("/settings/undo", data={"batch": batch}, follow_redirects=False)
    assert r.headers["location"] == "/settings?note=undone#tweaks"
    from research_lab.files import datafiles
    assert datafiles.feed_source("news", "Foreign Policy").get("enabled", True) is True


# ---------------------------------------------------------------- §3's sixteen utterances

UTTERANCES = [
    ("新闻改成七比三，世界要闻多一点", "feeds.digest.world_ratio", 0.7, 0.7),
    ("论文每天 5 篇就够了", "feeds.digest.papers_n", 5, 5),
    ("重要媒体和顶刊再靠前一点", "feeds.digest.source_boost", "强", "strong"),
    ("新闻只看最近一天的", "feeds.digest.news_window_days", 1, 1),
    ("换成深色", "ui.theme", "深色", "dark"),
    ("推送每天别超过 5 毛", "feeds.daily_cny", 0.5, 0.5),
    ("开放获取的论文不要自动下载了", "feeds.autoimport_oa", "关", False),
    ("卡住改成 10 天没动就提醒", "pm.stalled_days", 10, 10),
    ("周五的进度别发了", "pm.friday_digest", False, False),
    ("每天花费上限提到 3 块", "ai.daily_cap_cny", 3, None),                  # waits for card 0069: refused
    ("把 Foreign Policy 这个源关掉", "feeds.news[Foreign Policy].enabled", False, False),
    ("Security Dialogue 往前排", "feeds.journal[SD].weight", 90, 90),
    ("东盟方向加个关键词：南海仲裁", "directions[sea-asean].keywords", None, "南海仲裁"),
    ("对话用好一点的模型", "skill.chat.tier", "standard", "standard"),
]


@pytest.mark.parametrize("text,key,new,stored", UTTERANCES)
def test_design_utterances_route_to_settings_and_apply_directly(conn, text, key, new, stored):
    from research_lab.chat import router
    assert router.match(text) is None and skills.pick(text).name == "settings"
    item = {"key": key, "add": [stored]} if new is None else {"key": key, "new": new}
    reply = change(conn, text, item)
    if stored is None:
        assert "每天花费上限还没接进" in reply and conn.execute("SELECT COUNT(*) FROM assistant_changes").fetchone()[0] == 0
        return
    d = TU.describe(conn, key)
    assert (stored in d["value"]) if isinstance(d["value"], list) else d["value"] == stored
    assert conn.execute("SELECT status FROM assistant_changes").fetchone()[0] == "applied"


def test_what_has_been_tuned_is_a_fixed_phrase(conn):
    from research_lab.chat import router
    assert router.match("现在都调了哪些")[0] is router.cmd_tweaks
    assert router.route(conn, "现在都调了哪些")[1].content == "都是默认设置。想调什么，直接跟助手说。"
    change(conn, "论文每天 5 篇就够了", {"key": "feeds.digest.papers_n", "new": 5})
    change(conn, "把 Foreign Policy 这个源关掉", {"key": "feeds.news[Foreign Policy].enabled", "new": False})
    out = router.route(conn, "现在都调了哪些")[1].content
    assert "每天论文篇数：5 篇（默认 10 篇） · 助手" in out and "来源「Foreign Policy」开关：开 → 关" in out
    assert conn.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 2         # only the two change requests


def test_a_pasted_key_is_masked_before_it_is_stored_or_routed(conn):
    from research_lab.chat import router
    secret = "sk-" + "a1b2c3d4" * 4
    out = router.route(conn, f"把 DeepSeek 的 key 换成 {secret}", page="/settings")
    stored = conn.execute("SELECT content FROM chat_messages WHERE role = 'user'").fetchone()[0]
    assert secret not in stored and "已隐藏" in stored and secret not in out[0].content
    assert skills.pick(stored).name == "settings"
    title = conn.execute("SELECT title FROM chat_threads").fetchone()[0]
    assert secret not in title
    for token in ("ghp_" + "A" * 24 + "1", "0123456789abcdef" * 2, "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHRlc3Q0"):
        assert router.mask_secrets(f"x {token} y") == "x 已隐藏 y"
    for keep in ("https://example.com/news/2026/10/09/a-long-slug-for-the-article", "10.1080/09512748.2023.2183112",
                 "https://www.ft.com/content/" + "3f9a2b7c" * 4, "https://example.org/a?x=" + "a" * 36 + "&y=1",
                 "/Users/leoyao/Projects/research-lab/app/research_lab/web/templates/pages/feed.html"):
        assert router.mask_secrets(keep) == keep
    assert router.mask_secrets(f"https://api.example.com/?key={secret} 和 {secret}") == \
        "https://api.example.com/?key=已隐藏 和 已隐藏"


RESEARCH = [
    # the 0080 review's sentences
    "这篇论文的 key argument 是什么", "量子密钥分发对国际安全的影响", "联合国大会以 120 比 5 通过决议", "这篇论文引用了 30 篇文献",
    "调整一下回归模型的变量", "能源安全应该优先于气候目标吗", "导师提醒关注东盟方式", "美国国会提高债务上限的影响",
    "看看最近东盟的新闻", "议程设置为何在东盟失效", "开放获取期刊对学术传播的影响",
    # more of the same kind
    "议程设置理论在东盟研究里怎么用", "这篇论文用了什么模型", "这条新闻的来源是什么", "美国债务上限谈判进度", "女性议员的比例",
    "军费花费有多少", "这篇论文的主题是什么", "中美关系的关键词有哪些", "东盟方式的权重如何衡量", "这个模型的解释力怎么样",
    "最近三年的东盟峰会", "条约第五条怎么解释", "日本外交政策的优先事项", "上限理论在联盟研究中的应用",
    "提醒机制在危机管理中的作用", "这个数据源可靠吗", "安理会 13 比 2 否决", "关掉核电站的政策影响", "打开市场对东盟的影响",
    "比例代表制和东盟议会", "开放获取对南方国家学者的影响",
    # the second 0080 review: policy briefs are Leo's own writing, and 设置 / 运行方式 / 外观 are research words too
    "写一份两页的政策简报，列五条建议", "帮我把这篇改成政策简报", "帮我起草一份给导师的简报，列三条", "这份研究简报最多两页",
    "回归模型的参数设置", "实验组和对照组的比例怎么设置", "议程设置在最近的东盟峰会上有变化吗",
    "东盟地区论坛的运行方式", "联合国维和行动的运行方式是什么", "外观上看是联盟，实际是对冲", "外观设计专利在中美贸易战中的作用",
]
ISSUE_COMMANDS = ["把引言那条优先级调成高", "把第二章状态调成进行中", "把第二章设置成进行中", "把第二章往前排"]


@pytest.mark.parametrize("text", RESEARCH)
def test_research_sentences_do_not_route_to_settings(repo, text):
    assert skills.pick(text).name != "settings"


@pytest.mark.parametrize("text", ISSUE_COMMANDS)
def test_issue_commands_with_setting_words_stay_with_the_issue_tools(repo, text):
    picked = skills.pick(text).name
    assert picked != "settings" and {"change_issues", "create_issues"} <= set(skills.get(picked).tools)


@pytest.mark.parametrize("text", ["换成深色会不会更费电", "论文每天 5 篇是不是太少了", "推送能不能少一点"])
def test_a_question_changes_no_setting(conn, text):
    reply = change(conn, text, {"key": "ui.theme", "new": "dark"})
    assert reply.startswith("not changed: the user's message is a question") and TU.tunable(conn, "ui.theme") == "system"
    assert conn.execute("SELECT COUNT(*) FROM assistant_changes").fetchone()[0] == 0


def test_the_deepseek_model_must_be_a_known_one(conn):
    from research_lab.ai.pricing import KNOWN_MODELS
    reply = change(conn, "DeepSeek 模型换成 gpt-nonexistent-9", {"key": "ai.model", "new": "gpt-nonexistent-9"})
    assert "DeepSeek 模型只能是" in reply and TU.tunable(conn, "ai.model") == "deepseek-flash"
    other = next(m for m in KNOWN_MODELS if m != "deepseek-flash")
    change(conn, f"DeepSeek 模型换成 {other}", {"key": "ai.model", "new": other})
    assert TU.tunable(conn, "ai.model") == other


def test_runtime_reads_engine_and_tier_settings_through_the_registry(conn):
    from research_lab.db.connection import set_setting
    set_setting(conn, "engine.claude.standard", "gpt-unknown")                 # the page calls this invalid
    assert skills.engine_model(conn, "claude", "standard") == "sonnet"
    set_setting(conn, "engine.claude.standard", "opus")
    assert skills.engine_model(conn, "claude", "standard") == "opus"
    set_setting(conn, "skill.translate.tier", "huge")
    assert skills.tier_of(conn, skills.get("translate")) == "standard"


def test_settings_skill_budget_and_tools():
    sk = skills.get("settings")
    assert (sk.tier, sk.max_tool_calls, sk.max_output_tokens) == ("light", 8, 800)
    assert set(sk.tools) == {"read_settings", "change_settings", "ask_user", "find_journal", "subscribe_journal", "unsubscribe_source", "add_news_source", "list_subscriptions"}   # card 0211
    assert tools.TOOLS["change_settings"].needs_task


def test_undo_phrase_reverts_a_settings_change(conn):
    from research_lab.chat import router
    task = ask(conn, "换成深色")
    tid = conn.execute("SELECT thread_id FROM agent_tasks WHERE id = ?", (task,)).fetchone()[0]
    call(conn, "change_settings", {"changes": [{"key": "ui.theme", "new": "dark"}]}, task)
    reply = router.route(conn, "撤销刚才的修改", thread_id=tid)[1].content
    assert reply.startswith("已撤销 1 处。") and TU.tunable(conn, "ui.theme") == "system"


# ---------------------------------------------------------------- DeepSeek, end to end

def test_deepseek_settings_request_applies_directly(conn, monkeypatch):
    from research_lab.agent import tasks
    from research_lab.ai import claude_code, dispatch, lease
    from research_lab.chat import router
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(claude_code.WORKER, "hosted", True)
    monkeypatch.setattr(lease, "acquire", lambda conn: True)
    monkeypatch.setattr(tasks.RUNNER, "submit", tasks.run)
    seen: list[dict] = []
    steps = iter([
        {"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_settings", "arguments": json.dumps({"query": "论文"})}}]},
        {"tool_calls": [{"id": "c2", "type": "function", "function": {"name": "change_settings", "arguments": json.dumps(
            {"changes": [{"key": "feeds.digest.papers_n", "new": 5}]})}}]},
        {"content": "已把每天论文篇数从 10 篇改成 5 篇，想改回去说「撤销」。"},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        msg = {"content": "", **next(steps)}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}})
    monkeypatch.setattr(dispatch, "_TRANSPORT", httpx.MockTransport(handler))
    router.route(conn, "论文每天 5 篇就够了", page="/feed")
    t = conn.execute("SELECT * FROM agent_tasks").fetchone()
    assert t["skill"] == "settings" and t["status"] == "done" and t["model"] == "deepseek-flash"
    assert {f["function"]["name"] for f in seen[0]["tools"]} == set(skills.get("settings").tools)
    assert TU.tunable(conn, "feeds.digest.papers_n") == 5
    assert seen[2]["messages"][-1]["content"].startswith("已改 1 处。")


@pytest.mark.parametrize("text", ["换成深色", "切到浅色模式", "外观换成深色", "助手的运行方式改成 DeepSeek", "每日简报的世界要闻多一点",
                                  "推送论文每天少推几篇，5 篇"])
def test_app_phrasings_still_route_to_settings(repo, text):
    assert skills.pick(text).name == "settings"


def test_fewer_with_an_explicit_number_is_an_instruction(conn):
    text = "推送论文每天少推几篇，5 篇"
    assert not skills.asks(text) and skills.asks("推送论文每天少推几篇？") and skills.asks("能少推几篇吗")
    change(conn, text, {"key": "feeds.digest.papers_n", "new": 5})
    assert TU.tunable(conn, "feeds.digest.papers_n") == 5
