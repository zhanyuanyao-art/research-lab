"""Push sources as data: journals, news feeds and the keyword lists, read from _data/feeds.yml.

The file is hand-editable (ruamel round trip like the other _data files); it is re-read before every
refresh, so nothing here is cached across calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Paths

POOLS = ("core", "watch", "hunt", "expansion")
DEFAULT_WEIGHT = 40


@dataclass(frozen=True)
class Journal:
    abbr: str
    name: str
    issns: tuple[str, ...]
    column: str = ""
    pool: str = "core"
    weight: int = DEFAULT_WEIGHT
    gp_filter: bool = False


LANES = ("world", "research")   # 世界要闻 · 研究相关 (docs/design/NEWS-v2.md)


@dataclass(frozen=True)
class NewsFeed:
    name: str
    url: str
    lane: str = "research"
    # Section rules on the item's link path (card 0067): a general outlet's feed mixes world politics with sport,
    # video, business and its own country's news. keep: only these path prefixes count; drop: these never do.
    keep: tuple[str, ...] = ()
    drop: tuple[str, ...] = ()
    weight: int = 60          # the outlet's standing, on the journals' 0-100 scale (今日精选 ranks with it)

    def off_section(self, link: str | None) -> bool:
        from urllib.parse import urlparse
        path = urlparse(link or "").path or "/"
        if self.keep and not any(path.startswith(k) for k in self.keep):
            return True
        return any(path.startswith(d) for d in self.drop)


@dataclass
class Sources:
    journals: list[Journal] = field(default_factory=list)
    news: list[NewsFeed] = field(default_factory=list)
    gp_terms: list[str] = field(default_factory=list)
    radar_terms: list[str] = field(default_factory=list)
    disabled_news: list[tuple[str, str]] = field(default_factory=list)   # (name, why), shown in 设置

    def journal(self, abbr: str | None) -> Journal | None:
        return next((j for j in self.journals if j.abbr == abbr), None)


def _enabled(row: Any) -> bool:
    return isinstance(row, dict) and row.get("enabled", True) is not False


def _weight(v: Any, default: int) -> int:
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return default


def parse(data: Any) -> Sources:
    data = data if isinstance(data, dict) else {}
    journals = []
    for j in data.get("journals") or []:
        if not _enabled(j) or not j.get("abbr") or not j.get("issn"):
            continue
        issns = j["issn"] if isinstance(j["issn"], (list, tuple)) else [j["issn"]]
        journals.append(Journal(str(j["abbr"]).strip(), str(j.get("name") or j["abbr"]).strip(),
                                tuple(str(i).strip() for i in issns if str(i).strip()), str(j.get("column") or ""),
                                str(j.get("pool") or "core"), int(j.get("weight") or DEFAULT_WEIGHT), bool(j.get("gp_filter"))))
    # a bare string (`drop: /video/`) is one path, not a sequence of characters
    paths = lambda v: tuple(str(p).strip() for p in ([v] if isinstance(v, str) else v or []) if str(p).strip().startswith("/"))  # noqa: E731
    news = [NewsFeed(str(n["name"]).strip(), str(n["url"]).strip(), n.get("lane") if n.get("lane") in LANES else "research",
                     paths(n.get("keep")), paths(n.get("drop")), _weight(n.get("weight"), 60))
            for n in data.get("news") or [] if _enabled(n) and n.get("name") and n.get("url")]
    off = [(str(n["name"]).strip(), str(n.get("note") or "").strip())
           for n in data.get("news") or [] if isinstance(n, dict) and not _enabled(n) and n.get("name")]
    terms = lambda key: [str(t).lower() for t in data.get(key) or [] if str(t).strip()]  # noqa: E731
    return Sources(journals, news, terms("gp_terms"), terms("radar_terms"), off)


def load(paths: Paths | None = None) -> Sources:
    from ..files import datafiles
    try:
        from .presets import PreferenceError, expand
        data = datafiles.read("feeds", paths)
        try:
            data = expand(data)
        except PreferenceError:
            pass  # Invalid preset metadata must not disable manual subscriptions.
        return parse(data)
    except Exception:  # noqa: BLE001 - a broken yml must not break the home page
        return Sources()


def gp_relevant(text: str, terms: list[str]) -> bool:
    blob = f" {(text or '').lower()} "
    return any(t in blob for t in terms)


def radar_hits(text: str, terms: list[str]) -> list[str]:
    """Terms are word-start prefixes ("feminis" matches feminism and feminist), so "race" never fires on
    "embrace" or "trace"."""
    blob = (text or "").lower()
    return sorted({t.strip() for t in terms if t.strip() and re.search(r"(?<![a-z])" + re.escape(t.lstrip()), blob)})
