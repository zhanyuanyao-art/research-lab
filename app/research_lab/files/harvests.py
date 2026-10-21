"""_harvests/*.md: monthly, quarterly and annual harvests created from templates/harvest.md.

A harvest's name is its period: `2026-10` (monthly), `2026-Q4` (quarterly), `2026` (annual).
A period covers the ISO weeks whose Thursday falls inside it, so every week belongs to exactly
one month and one quarter (October 2026 = 2026-W40 … 2026-W44).
"""
from __future__ import annotations

import re
import sqlite3
from calendar import month_name
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..config import Paths, get_paths
from . import artifacts
from .artifacts import Artifact
from .weeks import week_of
from .yamlio import atomic_write

KINDS = ("monthly", "quarterly", "annual")
NAME_RE = re.compile(r"^(\d{4})(?:-(0[1-9]|1[0-2])|-Q([1-4]))?$")
QUARTERLY_PROMPT = (
    "## What am I actually researching?\n\n"
    "<!-- One page. Answer it without looking at the project list first. -->\n\n...\n"
)


@dataclass
class Harvest:
    doc: Artifact

    @property
    def path(self) -> Path: return self.doc.path
    @property
    def name(self) -> str: return self.doc.path.stem
    @property
    def sha(self) -> str: return self.doc.sha
    @property
    def kind(self) -> str: return str(self.doc.get("kind", ""))
    @property
    def period(self) -> str: return str(self.doc.get("period", ""))
    @property
    def title(self) -> str: return str(self.doc.get("title", self.name))
    @property
    def from_week(self) -> str: return str(self.doc.get("from", ""))
    @property
    def to_week(self) -> str: return str(self.doc.get("to", ""))

    def text(self) -> str: return self.doc.text()


def kind_of(period: str) -> str:
    m = NAME_RE.match(period or "")
    if not m:
        raise ValueError(f"not a harvest period: {period!r} (use 2026-10, 2026-Q4 or 2026)")
    return "monthly" if m.group(2) else "quarterly" if m.group(3) else "annual"


def _months(period: str) -> tuple[int, int, int]:
    """(year, first month, last month) of a period."""
    m = NAME_RE.match(period)
    if not m:
        raise ValueError(period)
    year = int(m.group(1))
    if m.group(2):
        mon = int(m.group(2))
        return year, mon, mon
    if m.group(3):
        q = int(m.group(3))
        return year, 3 * q - 2, 3 * q
    return year, 1, 12



def period_range(period: str) -> tuple[str, str]:
    """First and last ISO week (inclusive) of a period."""
    year, m1, m2 = _months(period)
    first_day = date(year, m1, 1)
    last_day = (date(year + (m2 == 12), (m2 % 12) + 1, 1) - timedelta(days=1))
    # first Thursday on or after the first day, last Thursday on or before the last day
    first_thu = first_day + timedelta(days=(3 - first_day.weekday()) % 7)
    last_thu = last_day - timedelta(days=(last_day.weekday() - 3) % 7)
    return week_of(first_thu), week_of(last_thu)


def period_title(period: str) -> str:
    year, m1, m2 = _months(period)
    kind = kind_of(period)
    if kind == "monthly":
        return f"Harvest: {month_name[m1]} {year}"
    if kind == "quarterly":
        return f"Research map: {year} Q{(m1 + 2) // 3}"
    return f"Portfolio: {year}"


def period_for(kind: str, d: date | None = None) -> str:
    """The period of `kind` that contains the ISO week of `d` (by its Thursday)."""
    d = d or date.today()
    thu = d + timedelta(days=3 - d.weekday())
    if kind == "monthly":
        return f"{thu.year}-{thu.month:02d}"
    if kind == "quarterly":
        return f"{thu.year}-Q{(thu.month + 2) // 3}"
    if kind == "annual":
        return str(thu.year)
    raise ValueError(kind)


def previous_period(period: str) -> str:
    year, m1, _ = _months(period)
    kind = kind_of(period)
    step = {"monthly": 1, "quarterly": 3, "annual": 12}[kind]
    m, y = m1 - step, year
    if m < 1:
        m += 12
        y -= 1
    return period_for(kind, date(y, m, 15)) if kind != "annual" else str(year - 1)


def path_for(period: str, paths: Paths | None = None) -> Path:
    kind_of(period)
    return (paths or get_paths()).harvests / f"{period}.md"


def load(path: Path) -> Harvest:
    return Harvest(artifacts.parse_text(path.read_text(encoding="utf-8"), path))


def get(period: str, paths: Paths | None = None) -> Harvest | None:
    p = path_for(period, paths)
    return load(p) if p.exists() else None


def list_all(paths: Paths | None = None) -> list[Harvest]:
    p = paths or get_paths()
    out = []
    for f in sorted(p.harvests.glob("*.md")):
        try:
            out.append(load(f))
        except Exception:  # noqa: BLE001 - a hand-broken file must not hide the others
            continue
    return sorted(out, key=lambda h: (h.from_week, h.name), reverse=True)


def create(kind: str, period: str, paths: Paths | None = None, title: str | None = None) -> Path:
    p = paths or get_paths()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind_of(period) != kind:
        raise ValueError(f"{period} is not a {kind} period")
    path = path_for(period, p)
    if path.exists():
        raise FileExistsError(path)
    doc = artifacts.parse_text((p.templates / "harvest.md").read_text(encoding="utf-8"), path)
    start, end = period_range(period)
    doc.front["title"] = title or period_title(period)
    doc.front["kind"] = kind
    doc.front["period"] = period
    doc.front["from"] = start
    doc.front["to"] = end
    if kind in ("quarterly", "annual") and "What am I actually researching" not in doc.body:
        doc.body = doc.body.rstrip("\n") + "\n\n" + QUARTERLY_PROMPT
    atomic_write(path, doc.text())
    return path


def save_text(period: str, text: str, expected_sha: str, paths: Paths | None = None) -> str:
    """Write a user-edited harvest file; StaleFileError if it changed since `expected_sha`."""
    path = path_for(period, paths)
    doc = artifacts.parse_text(text, path)
    return atomic_write(path, doc.text(), expected_sha)


def covered_artifacts(conn: sqlite3.Connection, from_week: str, to_week: str,
                      counting_only: bool = False) -> list[sqlite3.Row]:
    """Artifacts from the derived index whose week lies in [from_week, to_week]."""
    sql = "SELECT * FROM artifacts WHERE week >= ? AND week <= ?"
    if counting_only:
        sql += " AND counts = 1"
    return conn.execute(sql + " ORDER BY week, ref", (from_week, to_week)).fetchall()


def last_monthly(paths: Paths | None = None) -> Harvest | None:
    monthly = [h for h in list_all(paths) if h.kind == "monthly" and h.to_week]
    return max(monthly, key=lambda h: h.to_week) if monthly else None


def due(conn: sqlite3.Connection, paths: Paths | None = None, threshold: int = 4) -> dict:
    """A monthly harvest is due once `threshold` counting artifacts exist after the last one."""
    last = last_monthly(paths)
    if last:
        n = conn.execute("SELECT COUNT(*) FROM artifacts WHERE counts = 1 AND week > ?", (last.to_week,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM artifacts WHERE counts = 1").fetchone()[0]
    return {"due": n >= threshold, "count": n, "threshold": threshold, "since": last.name if last else None}
