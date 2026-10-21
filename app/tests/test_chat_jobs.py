"""/chat/jobs (card 0042, P2-15): local time instead of sliced UTC strings, the displayed time is the sort key,
and a failure reads as one Chinese sentence with the raw error kept in the tooltip."""
from __future__ import annotations

import os
import re
import time

import pytest

from research_lab.web.routes.chat import failure_reason

RAW = ("Command '['/Users/leoyao/.local/bin/claude', '-p', '--output-format', 'stream-json', '--system-prompt', "
       "'Return JSON']' timed out after 600 seconds")


@pytest.fixture()
def shanghai(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _job(conn, job_id: str, status: str, created: str, completed: str | None, error: str | None = None) -> None:
    conn.execute("INSERT INTO ai_jobs(id, kind, status, target_type, target_id, request_json, schema_json, error, created_at, completed_at) "
                 "VALUES (?, 'news.arrange', ?, NULL, NULL, '{}', '{}', ?, ?, ?)", (job_id, status, error, created, completed))
    conn.commit()


def _times(html: str) -> list[str]:
    table = html.split('<table class="data">')[1].split("</table>")[0]
    return re.findall(r'<time datetime="([^"]+)">([^<]+)</time>', table)


def test_jobs_show_local_time_sorted_by_the_shown_field(client, conn, shanghai):
    # created order differs from completed order: the old page sorted by created_at but showed completed_at.
    _job(conn, "J-1", "done", "2026-10-01T02:00:00+00:00", "2026-10-01T12:17:54+00:00")
    _job(conn, "J-2", "done", "2026-10-01T03:00:00+00:00", "2026-10-01T04:07:00+00:00")
    _job(conn, "J-3", "queued", "2026-10-01T05:30:00+00:00", None)
    shown = _times(client.get("/chat/jobs").text)
    assert [t for _, t in shown] == ["10-01 20:17", "10-01 13:30", "10-01 12:07"]
    assert [d for d, _ in shown] == sorted((d for d, _ in shown), reverse=True)


def test_failure_is_one_chinese_line_with_raw_text_in_title(client, conn, shanghai):
    _job(conn, "J-9", "failed", "2026-10-01T01:00:00+00:00", "2026-10-01T01:10:00+00:00", RAW)
    page = client.get("/chat/jobs").text
    assert "运行超时" in page
    reason = page.split('class="muted small job-reason"')[1].split("</span>")[0]
    assert "title=\"Command" in reason and "运行超时" in reason.split(">", 1)[1]
    visible = re.sub(r"<[^>]+>", " ", page)
    assert "/Users/leoyao" not in visible and "UTC" not in visible


def test_failure_reason_mapping():
    assert failure_reason(RAW).startswith("运行超时")
    assert failure_reason("Command '['/x/claude', '-p']' returned non-zero exit status 1.") == "助手进程意外退出"
    assert "上限" in failure_reason("daily cap $1.00 reached (spent $1.0100); raise ai.daily_cap_usd or pass --force")
    assert failure_reason("1 validation error for Result") == "助手返回的内容格式不对，没能保存"
    assert failure_reason(None) == "运行出错，没有结果"


def test_dev_page_does_not_say_utc(client, conn, shanghai):
    from research_lab.db.connection import set_setting
    set_setting(conn, "engine.claude.rate_limits", {"five_hour": 0.2, "seven_day": 0.4, "status": "allowed",
                                                    "at": "2026-10-01T04:07:00+00:00"})
    page = re.sub(r"<[^>]+>", " ", client.get("/dev").text)   # usage and prices left 设置 (card 0081)
    assert "UTC" not in page and "12:07 更新" in page
    assert "09:00–12:00" in page and "14:00–18:00" in page


def test_dev_and_data_pages_use_local_time():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "research_lab" / "web" / "templates"
    for rel in ("pages/dev.html", "pages/data_run.html"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "[5:16]" not in text and "[11:16]" not in text and "[:16]" not in text, rel
