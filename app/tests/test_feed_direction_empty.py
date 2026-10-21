"""Card 0199 (Leo 10-19: 文献推送点进去是 0): when the remembered research direction filters every paper of today's
edition away, the page says so and how many all directions have, instead of 「今天还没有论文」."""
from __future__ import annotations

from research_lab.intake import directions
from research_lab.web.context import DIRECTION_COOKIE

from .test_feed_digest import _paper


def test_a_direction_that_hides_every_pick_says_so(client, conn):
    _paper(conn, "p1")                                       # no direction
    conn.commit()
    slug = str(directions.load()[0]["slug"])
    name = str(directions.load()[0].get("name") or slug)
    client.cookies.set(DIRECTION_COOKIE, slug)
    page = client.get("/feed?tab=papers").text
    assert f"{name} 方向今天没有论文" in page and "全部方向有 1 篇" in page and "/feed?tab=papers&amp;direction=" in page
    assert "今天还没有论文" not in page
    client.cookies.delete(DIRECTION_COOKIE)
    page = client.get("/feed?tab=papers&direction=").text
    assert "Paper p1" in page


def test_an_empty_edition_without_a_direction_still_says_nothing_yet(client):
    page = client.get("/feed?tab=papers&direction=").text
    assert "今天还没有论文" in page and "方向今天没有" not in page
