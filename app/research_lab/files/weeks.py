"""ISO-week helpers. Weeks are strings like 2026-W40."""
from __future__ import annotations

from datetime import date, timedelta


def week_of(d: date | None = None) -> str:
    d = d or date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def parse_week(week: str) -> tuple[int, int]:
    y, w = week.split("-W")
    return int(y), int(w)


def week_start(week: str) -> date:
    y, w = parse_week(week)
    return date.fromisocalendar(y, w, 1)


def week_add(week: str, n: int) -> str:
    return week_of(week_start(week) + timedelta(weeks=n))


def weeks_between(start: str, end: str) -> list[str]:
    out, cur = [], start
    while True:
        out.append(cur)
        if cur == end or len(out) > 200:
            return out
        cur = week_add(cur, 1)


def weeks_since(week: str, today: date | None = None) -> int:
    return (week_start(week_of(today)) - week_start(week)).days // 7
