"""Research directions (_data/directions.yml): lookup, validation and the deterministic keyword guess
the radar uses (no AI). Papers, excerpts and news items carry a direction slug."""
from __future__ import annotations

import re
from typing import Any

# Chinese anchors for the Chinese-language news digest; applied only for slugs that exist in
# directions.yml, so renaming a direction there never produces a dangling slug here.
ZH_KEYWORDS: dict[str, list[str]] = {
    "alliance-us": ["同盟", "联盟", "美韩", "美日", "延伸威慑", "责任分担", "盟友", "美国外交"],
    "sea-asean": ["东盟", "东南亚", "东亚峰会", "印太", "对冲", "区域主义", "马来西亚", "印尼", "越南", "菲律宾", "新加坡", "泰国"],
    "strategy-signaling": ["威慑", "信号", "可信度", "胁迫", "危机", "核计划", "核武", "战略稳定"],
    "periphery-nts": ["边境", "海上安全", "气候", "移民", "疫情", "灾害", "能源安全", "粮食安全", "南海", "非传统安全"],
}


def load() -> list[dict[str, Any]]:
    from ..files import datafiles
    try:
        rows = datafiles.read("directions") or []
    except Exception:  # noqa: BLE001 - a broken yml must not break papers pages
        return []
    return [dict(r) for r in rows if isinstance(r, dict) and r.get("slug")]


def slugs() -> list[str]:
    return [str(d["slug"]) for d in load()]


def by_slug() -> dict[str, dict[str, Any]]:
    return {str(d["slug"]): d for d in load()}


def name(slug: str | None) -> str | None:
    if not slug:
        return None
    d = by_slug().get(slug)
    return str(d.get("name") or slug) if d else slug


def clean(slug: Any) -> str | None:
    """A known slug, else None (empty string and unknown values both clear)."""
    s = str(slug or "").strip()
    return s if s in slugs() else None


def _norm(text: str) -> str:
    return re.sub(r"[‐-―\-]", "-", (text or "").lower())


def _hits(keyword: str, text: str) -> int:
    kw = _norm(keyword).strip()
    if not kw:
        return 0
    if re.search(r"[㐀-鿿]", kw):
        return text.count(kw)
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text))


def guess(*texts: str | None, dirs: list[dict[str, Any]] | None = None) -> str | None:
    """Slug with the most keyword hits across the texts; ties go to the earlier direction in the file.
    None when nothing matches."""
    text = _norm(" ".join(t for t in texts if t))
    best, best_n = None, 0
    for d in dirs if dirs is not None else load():
        slug = str(d["slug"])
        words = [str(k) for k in (d.get("keywords") or [])] + ZH_KEYWORDS.get(slug, [])
        n = sum(_hits(k, text) for k in words)
        if n > best_n:
            best, best_n = slug, n
    return best
