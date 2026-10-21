"""The consumer settings page (docs/design/SETTINGS-AGENT.md §2, §4, card 0081): seven blocks (复现环境 since card 0204,
tested with the shared library in test_replication_renv), controls that apply on click through tunables, 当前微调 with
who changed what, 恢复默认 and 撤销, and the tables moved to /dev."""
from __future__ import annotations

import json
import re

import pytest

from research_lab import tunables
from research_lab.db.connection import get_setting, now_iso
from research_lab.pm import changes

BLOCKS = ("外观", "AI", "推送", "连接", "复现环境", "当前微调", "关于")


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _assistant_row(conn, target: str, old, new, *, batch: str = "AC-test0001", status: str = "applied",
                   at: str | None = None) -> None:
    conn.execute("INSERT INTO assistant_changes(batch_id, kind, action, target, field, old_json, new_json, status, actor, "
                 "created_at, applied_at) VALUES (?, 'setting', 'set', ?, 'value', ?, ?, ?, 'assistant', ?, ?)",
                 (batch, target, json.dumps(old), json.dumps(new), status, at or now_iso(), at or now_iso()))


def _written_at(conn, key: str, at: str) -> None:
    conn.execute("UPDATE settings SET updated_at = ? WHERE key = ?", (at, key))


EARLIER, LATER = "2026-10-01T08:00:00+00:00", "2026-10-01T08:00:05+00:00"


def test_seven_blocks_and_no_engine_controls(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert re.findall(r"<h3>([^<]+)</h3>", r.text) == list(BLOCKS)
    page = _text(r.text)
    for gone in ("运行方式", "模型", "档位", "Claude Code 每日最多运行次数", "每日上限", "文献地图目标篇数", "学年起始周",
                 "输入 · 命中缓存", "已停用", "保存设置"):
        assert gone not in page, gone
    for field in ('name="ai_mode"', 'name="ai_model"', "<select", 'name="engine_', 'name="skill_', 'type="number"'):
        assert field not in r.text, field
    assert "都是默认设置。想调什么，直接跟助手说。" in page
    assert re.search(r"每天 10 条新闻、10 篇论文 · 来源 \d+ 个", page)
    assert "在访达中显示研究仓库" in page and 'href="/dev"' in r.text


def test_status_line_names_the_engine(client, monkeypatch):
    from research_lab.ai import dispatch
    assert "排队" in _text(client.get("/settings").text)                    # tests have no CLI and no key
    monkeypatch.setattr(dispatch, "route", lambda conn: ("claude", ""))
    assert "在线 · 由本机 Claude Code 处理" in _text(client.get("/settings").text)
    monkeypatch.setattr(dispatch, "route", lambda conn: ("deepseek", ""))
    assert "在线 · 由 DeepSeek 处理" in _text(client.get("/settings").text)


def test_theme_click_applies(client, conn):
    r = client.post("/settings/theme", data={"theme": "dark"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#appearance"
    assert get_setting(conn, "ui.theme") == "dark"
    page = client.get("/settings").text
    assert 'data-theme="dark"' in page and re.search(r'value="dark" role="radio" aria-checked="true" class="on"', page)
    assert "外观：深色" in _text(page) and "默认 跟随系统" in _text(page)      # listed under 当前微调
    bad = client.post("/settings/theme", data={"theme": "purple"})
    assert "没改成" in bad.text and get_setting(conn, "ui.theme") == "dark"
    client.post("/settings/theme", data={"theme": "system"})
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'ui.theme'").fetchone() is None   # the default is no row


def test_autoimport_toggle_writes_the_setting(client, conn):
    page = client.get("/settings").text
    assert re.search(r'name="on" value="1" role="radio" aria-checked="true" class="on">开', page)
    r = client.post("/settings/autoimport", data={"on": "0"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#feeds"
    assert get_setting(conn, "feeds.autoimport_oa") is False and tunables.tunable(conn, "feeds.autoimport_oa") is False
    page = client.get("/settings").text
    assert re.search(r'name="on" value="0" role="radio" aria-checked="true" class="on">关', page)
    assert "刷新时当天能下载的论文自动入库：关" in _text(page) and "在设置页改的" in _text(page)
    client.post("/settings/autoimport", data={"on": "1"})
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'feeds.autoimport_oa'").fetchone() is None


def test_tweaks_say_who_changed_what_and_reset_restores_the_default(client, conn):
    tunables.set_value(conn, "feeds.digest.papers_n", 5)                       # changed on the page
    tunables.set_value(conn, "pm.stalled_days", 10)                            # changed by the assistant
    _written_at(conn, "pm.stalled_days", EARLIER)
    _assistant_row(conn, "pm.stalled_days", 14, 10, at=EARLIER)
    tunables.set_value(conn, "map.target_works", 40)                           # the assistant wrote 30, Leo then 40
    _assistant_row(conn, "map.target_works", 20, 30, batch="AC-test0002", at=EARLIER)
    tunables.set_value(conn, "chat.reuse_minutes", 60)                         # the assistant wrote 60, Leo wrote 60 again
    _written_at(conn, "chat.reuse_minutes", LATER)
    _assistant_row(conn, "chat.reuse_minutes", 30, 60, batch="AC-test0003", at=EARLIER)
    page = _text(client.get("/settings").text)
    assert "每天 10 条新闻、5 篇论文" in page
    assert re.search(r"每天论文篇数：5 篇 默认 10 篇 在设置页改的 · \d\d-\d\d", page)
    assert re.search(r"进行中几天没动算卡住：10 天 默认 14 天 助手改的 · 10-0[12]", page)
    assert "文献地图目标篇数：40 篇 默认 20 篇 在设置页改的" in page
    assert "多久内的消息接着上个对话：60 分钟 默认 30 分钟 在设置页改的" in page
    assert "想调别的，直接 跟助手说 ，比如「论文每天 5 篇就够了」" in page
    assert "推送来源和方向" not in page                                            # settings-table rows only

    r = client.post("/settings/reset", data={"key": "feeds.digest.papers_n"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings?note=reset#tweaks"
    assert tunables.tunable(conn, "feeds.digest.papers_n") == 10
    assert conn.execute("SELECT 1 FROM settings WHERE key = 'feeds.digest.papers_n'").fetchone() is None
    page = _text(client.get(r.headers["location"]).text)
    assert "已恢复默认。" in page and "每天论文篇数" not in page and "进行中几天没动算卡住：10 天" in page

    for key in ("ai.paused", "no.such.key", "feeds.news[BBC World].enabled"):   # never-tier, unknown, a file entry
        assert client.post("/settings/reset", data={"key": key}, follow_redirects=False).headers["location"] \
            == "/settings?note=failed#tweaks"


def test_old_unbounded_values_are_refused(client, conn):
    """The old form took any non-negative number; the page now only writes through the registry."""
    assert client.post("/settings", data={"ai_mode": "agent", "map_target_works": "100000"}).status_code == 405
    assert get_setting(conn, "ai.mode") is None and get_setting(conn, "map.target_works") is None
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('map.target_works', '100000', ?)", (now_iso(),))
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('ai.claude_daily_runs', '-5', ?)", (now_iso(),))
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('ai.model', '\"no such model!\"', ?)", (now_iso(),))
    page = _text(client.get("/settings").text)
    assert "文献地图目标篇数：存的值无效，按默认" in page                       # out of range reads as the default
    assert "Claude Code 每天最多运行次数：存的值无效，按默认" in page and "DeepSeek 模型：存的值无效，按默认" in page
    from research_lab.ai import claude_code, dispatch, pricing                     # and the runtime agrees with the page
    assert claude_code.daily_runs(conn) == claude_code.DEFAULT_DAILY_RUNS
    assert dispatch.configured_model(conn) == pricing.DEFAULT_MODEL
    from research_lab import dashboard
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('year.start_week', '\"2026-W99\"', ?)", (now_iso(),))
    assert dashboard.cadence_start(conn, [{"week": "2026-W36"}], "2026-W41") == "2026-W36"
    assert "学年起始周：存的值无效，按默认" in _text(client.get("/settings").text)
    client.post("/settings/reset", data={"key": "map.target_works"})
    assert get_setting(conn, "map.target_works") is None


@pytest.fixture()
def fake_setting_kind(monkeypatch):
    """Card 0080 registers the real 'setting' handlers; these record what pm.changes hands them."""
    calls = {"apply": [], "undo": []}

    def apply_fn(conn, rows, src):
        calls["apply"] += [r["id"] for r in rows]
        return [{"id": r["id"], "status": "applied"} for r in rows]

    def undo_fn(conn, rows, src):
        calls["undo"] += [r["id"] for r in rows]
        return [{"id": r["id"], "status": "undone"} for r in rows]

    monkeypatch.setitem(changes._HANDLERS, "setting", (apply_fn, undo_fn))
    return calls


def test_assistant_file_changes_are_listed_with_undo(client, conn, fake_setting_kind):
    batch = changes.propose(conn, [changes.change_item("feeds.news[BBC World].enabled", "value", True, False),
                                   changes.change_item("directions[sea-asean].keywords", "value", ["ASEAN"], ["ASEAN", "南海仲裁"])],
                            kind="setting", reason="test")
    assert changes.apply(conn, batch)["applied"]
    _assistant_row(conn, "pm.stalled_days", 14, 10, batch="AC-test0003")      # a settings-table change: not in this list
    r = client.get("/settings")
    page = _text(r.text)
    assert "助手改过的推送来源和方向" in page
    assert "来源「BBC World」开关：开 → 关" in page and re.search(r"方向「[^」]+」的关键词：加了 南海仲裁", page)
    assert page.count("撤销") == 1 and f'name="batch" value="{batch}"' in r.text
    assert "AC-" not in page                                                       # ids stay in hidden fields

    r = client.post("/settings/undo", data={"batch": batch}, follow_redirects=False)
    assert r.headers["location"] == "/settings?note=undone#tweaks"
    assert len(fake_setting_kind["undo"]) == 2
    assert {row["status"] for row in changes.rows(conn, batch)} == {"undone"}
    page = _text(client.get("/settings?note=undone").text)
    assert "已撤销。" in page and "BBC World" not in page
    again = client.post("/settings/undo", data={"batch": batch}, follow_redirects=False)
    assert again.headers["location"] == "/settings?note=already#tweaks"


def test_undo_refuses_other_batches(client, conn, fake_setting_kind):
    issue_batch = changes.propose(conn, [changes.comment_item("x", "hi")], reason="test")
    for batch in (issue_batch, "AC-missing"):
        r = client.post("/settings/undo", data={"batch": batch}, follow_redirects=False)
        assert r.headers["location"] == "/settings?note=failed#tweaks"
    assert fake_setting_kind["undo"] == []


def test_pause_toggle_still_works(client, conn):
    from research_lab.ai import dispatch
    r = client.post("/settings/ai-pause", data={"paused": "1"}, follow_redirects=False)
    assert r.status_code == 303 and dispatch.paused(conn)
    page = _text(client.get("/settings").text)
    assert "全部 AI 已暂停" in page and "恢复 AI" in page and "暂停全部 AI" not in page
    client.post("/settings/ai-pause", data={"paused": "0"})
    assert not dispatch.paused(conn) and "暂停全部 AI" in _text(client.get("/settings").text)


def test_dev_shows_the_tables_that_left_settings(client):
    page = _text(client.get("/dev").text)
    assert "档位和价格" in page and "输入 · 命中缓存" in page and "deepseek-flash" in page
    assert "深度" in page and "推送来源" in page and "已停用：" in page
    assert re.search(r"Claude Code 今天 0 / \d+ 次", page) and "DeepSeek 模型" in page
    assert re.search(r"技能 档位 次数", page)


def test_no_secret_is_printed(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-secret-0081")
    monkeypatch.setenv("CONTACT_EMAIL", "leo@example.org")
    for path in ("/settings", "/dev"):
        html = client.get(path).text
        assert "sk-test-secret-0081" not in html and "leo@example.org" not in html, path
    page = _text(client.get("/settings").text)
    assert "DeepSeek 密钥 已配置" in page and "联系邮箱 已配置" in page
    assert "要改：在研究仓库的 app/.env 里填写，然后重启应用。" in page


def test_ai_mode_switch_on_the_page(client, conn):
    r = client.post("/settings/ai-mode", data={"mode": "deepseek"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#ai"
    assert get_setting(conn, "ai.mode", "auto") == "deepseek"
    html = client.get("/settings").text
    assert re.search(r'name="mode" value="deepseek"[^>]*aria-checked="true"', html)
    assert re.search(r'name="mode" value="claude"[^>]*aria-checked="false"', html)
    client.post("/settings/ai-mode", data={"mode": "auto"})
    assert get_setting(conn, "ai.mode", None) is None      # the default is not stored
    assert "自动：有 DeepSeek 密钥就用 DeepSeek" in _text(client.get("/settings").text)
    r = client.post("/settings/ai-mode", data={"mode": "gpt"}, follow_redirects=False)
    assert r.headers["location"] == "/settings?note=failed#ai"
    assert get_setting(conn, "ai.mode", None) is None
