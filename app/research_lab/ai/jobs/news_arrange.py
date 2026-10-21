"""news.arrange: cluster one news digest's items by theme and tie them to projects (SPEC-v2).

target_id is the news digest date (YYYY-MM-DD). Guardrail: only the items given — clusters and
project links that cite unknown item ids, projects or directions are dropped at apply time. The
result is stored in the settings row `news.arrangement:<date>`; there is no table of its own.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...db.connection import now_iso, set_setting
from ..provider import SYSTEM_RULES, ChatRequest, JobSpec
from ..registry import register


def _ids(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()]


class Cluster(BaseModel):
    model_config = ConfigDict(extra="ignore")
    theme: str
    item_ids: list[str] = Field(default_factory=list)
    why_it_matters: str | None = None
    direction: str | None = None

    @field_validator("item_ids", mode="before")
    @classmethod
    def _items(cls, v: Any) -> list[str]:
        return _ids(v)

    @field_validator("direction", mode="before")
    @classmethod
    def _dir(cls, v: Any) -> str | None:
        return str(v or "").strip() or None


class ProjectLink(BaseModel):
    model_config = ConfigDict(extra="ignore")
    item_id: str
    project_slug: str
    note: str | None = None

    @field_validator("item_id", "project_slug", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return str(v).strip()


class NewsArrangement(BaseModel):
    model_config = ConfigDict(extra="ignore")
    clusters: list[Cluster] = Field(default_factory=list)
    project_links: list[ProjectLink] = Field(default_factory=list)


EXAMPLE = {
    "clusters": [{"theme": "朝鲜半岛核态势与延伸威慑", "item_ids": ["n5"],
                  "why_it_matters": "朝鲜核能力的新评估直接影响美韩同盟的再保证需求。", "direction": "alliance-us"}],
    "project_links": [{"item_id": "n5", "project_slug": "us-rok-reassurance", "note": "可作为 2026 年再保证压力的背景材料。"}],
}


def _system() -> str:
    zh = "Write theme, why_it_matters and note in Simplified Chinese; ids and slugs stay exactly as given."
    return SYSTEM_RULES.replace("Write English.", zh) if "Write English." in SYSTEM_RULES else f"{SYSTEM_RULES} {zh}"


def _projects() -> list[dict[str, Any]]:
    from ...files import datafiles
    try:
        return [dict(p) for p in (datafiles.read("projects") or []) if isinstance(p, dict) and p.get("slug")]
    except Exception:  # noqa: BLE001
        return []


class NewsArrange(JobSpec):
    kind = "news.arrange"
    title = "Arrange a news digest by theme and project"
    Result = NewsArrangement
    target_type = "news"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        from ... import digest
        from ...intake import directions
        news = digest.news_for(conn, target_id)
        if news is None or not news.items:
            raise LookupError(f"no news digest for {target_id}")
        projects = _projects()
        dirs = directions.load()
        item_lines = []
        for it in news.items:
            item_lines.append(f"- {it.id} · {it.source} · {it.date} · {it.title} / {it.zh_title}\n  摘要: {it.summary}"
                              + (f"\n  切入点: {it.angle}" if it.angle else ""))
        project_lines = [f"- {p['slug']}: {p.get('name', '')} — question: {p.get('question', '(not set)')}"
                         + (f" (direction {p['direction']})" if p.get("direction") else "") for p in projects]
        dir_lines = [f"- {d['slug']}: {d.get('name', '')} / {d.get('name_en', '')}" for d in dirs]
        user = "\n".join([
            f"Task: arrange the {len(news.items)} commentary items of the news digest of {news.date} for Leo.",
            "",
            "Research directions:", *(dir_lines or ["(none)"]), "",
            "Projects:", *(project_lines or ["(none)"]), "",
            "Rules:",
            "- Use only the items listed below and refer to them by id (n1, n2, …). Do not add outside events or facts.",
            "- clusters: 2–6 themes; each item appears in at most one cluster; items that fit nowhere may be left out.",
            "- direction: one direction slug from the list for each cluster, or null.",
            "- why_it_matters: one or two sentences on why the theme matters for Leo's research directions.",
            "- project_links: only where an item plainly bears on a listed project's question; project_slug from the list.",
            "- Write theme, why_it_matters and note in Simplified Chinese.",
            "",
            "Example of the JSON shape (content is illustrative):",
            json.dumps(EXAMPLE, ensure_ascii=False),
            "",
            "# ITEMS",
            *item_lines,
        ])
        req = ChatRequest(messages=[{"role": "system", "content": _system()}, {"role": "user", "content": user}],
                          max_tokens=2500, json_mode=True,
                          params={"schema_name": "NewsArrangement", "date": news.date,
                                  "item_ids": [i.id for i in news.items], "project_slugs": [p["slug"] for p in projects],
                                  "direction_slugs": [d["slug"] for d in dirs]})
        return req, [news.path]

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        assert isinstance(result, NewsArrangement)
        row = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?",
                           (run_id,)).fetchone()
        params = (json.loads(row[0]).get("params") or {}) if row else {}
        items = set(params.get("item_ids") or [])
        projects = set(params.get("project_slugs") or [])
        dirs = set(params.get("direction_slugs") or [])
        if not items:
            from ... import digest
            news = digest.news_for(conn, target_id)
            items = {i.id for i in news.items} if news else set()
        used: set[str] = set()
        clusters = []
        for c in result.clusters:
            ids = [i for i in dict.fromkeys(c.item_ids) if i in items and i not in used]
            if not ids or not c.theme.strip():
                continue
            used.update(ids)
            clusters.append(Cluster(theme=c.theme.strip(), item_ids=ids, why_it_matters=c.why_it_matters,
                                    direction=c.direction if c.direction in dirs else None))
        links = [l for l in result.project_links if l.item_id in items and l.project_slug in projects]
        result.clusters, result.project_links = clusters, links
        set_setting(conn, f"news.arrangement:{target_id}", {**result.model_dump(), "run_id": run_id, "at": now_iso()})

    def example(self) -> dict[str, Any]:
        return EXAMPLE


register(NewsArrange())
