"""Card 0138: Leo turned down the empty-week nags (10-12), so the home page, the week page and the chat
replies never say how many weeks went by without output."""
from __future__ import annotations

from datetime import date

from research_lab import dashboard, weekly
from research_lab.chat import router

from .test_pm_home import render_home, visible

LATE = date(2026, 11, 2)   # weeks after any test artifact, where the old banners fired


def test_home_and_chat_status_do_not_nag_about_empty_weeks(conn, repo):
    d = dashboard.build(conn, today=LATE)
    assert not {"alert", "streak", "nudge"} & d.keys()
    assert "周没有产出" not in visible(render_home(conn, LATE))
    assert "周没有产出" not in router.cmd_status(conn, "", {}).content


def test_week_page_has_no_empty_week_banner(client):
    page = visible(client.get("/week").text)
    assert "没有 artifact" not in page and "还没有写出 Claim" not in page
    assert not hasattr(weekly, "alerts")
