"""Papers: list and upload, detail with metadata/summary/map fields, translation, the text reader and page images.

UI text is Simplified Chinese (SPEC-v2); ids, citekeys, commands and paper text are shown as stored."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response, JSONResponse
from starlette.datastructures import UploadFile

from ...ai import queue
from ...config import get_paths
from ...db.connection import now_iso
from ...intake import directions, extract, pipeline
from ...files import datafiles
from ... import paper_collections as collections
from ...excerpts import citation
from ..context import chosen_direction, get_conn, render
from .search import fts_query, has_cjk

router = APIRouter()

MAX_FILES = 10
CHUNK = 1 << 20
# 阅读状态 (docs/design/PAPERS.md §A): three states on top of the stored read_depth values.
READ_STATUS_WHERE = {
    "all": "1=1",
    "unread": "(p.read_depth IS NULL OR p.read_depth = 'abstract')",
    "reading": "p.read_depth = 'skimmed'",
    "done": "p.read_depth IN ('read', 'reread')",
}
READ_STATUS_LABELS = {"all": "全部", "unread": "未读", "reading": "在读", "done": "读完"}
# Old `filter=` links (the 首页 action "n 篇文献还没有中文概要" points at filter=unsummarized) keep working;
# the page no longer offers them as controls.
LEGACY_FILTERS = {"unsummarized": "p.ai_summary_json IS NULL", "unverified": "p.metadata_status != 'verified'",
                  "unread": READ_STATUS_WHERE["unread"]}


# what each state writes into read_depth; 读完 keeps 重读 when it was already that
STATE_DEPTH = {"unread": None, "reading": "skimmed", "done": "read"}
STATES = (("unread", "未读"), ("reading", "在读"), ("done", "读完"))


def read_status(read_depth: str | None) -> str:
    return "done" if read_depth in ("read", "reread") else "reading" if read_depth == "skimmed" else "unread"


SUMMARY_ROWS = [
    ("research_question", "研究问题"), ("argument", "核心论点"), ("mechanism", "机制"),
    ("period", "时段"), ("method", "方法"), ("level_of_analysis", "分析层次"),
    ("concept_usage", "概念用法"), ("cannot_explain_guess", "解释不了什么"),
    ("relevance_to_project", "与项目的关系"),
]
USER_FIELD_LABELS = [
    ("claim_text", "论点（一句话，用你自己的话）"), ("mechanism", "机制"), ("cannot_explain", "解释不了什么"),
    ("concept_usage", "概念用法"), ("method", "方法"), ("period", "时段"), ("level_of_analysis", "分析层次"),
]
FIELD_NAMES = {"camp": "阵营", "claim_text": "论点", "mechanism": "机制", "cannot_explain": "解释不了什么",
               "concept_usage": "概念用法", "method": "方法", "period": "时段", "level_of_analysis": "分析层次",
               "direction": "方向"}
READ_DEPTH_NAMES = {"abstract": "只读摘要", "skimmed": "略读", "read": "已读", "reread": "重读"}
RELATION_NAMES = {"supports": "支持", "rivals": "竞争解释", "scope-condition": "范围条件", "irrelevant": "无关"}
STATUS_NAMES = {"verified": "已核验", "unverified": "未核验", "candidate": "待确认"}
KIND_NAMES = {"article": "期刊论文", "book": "专著", "chapter": "章节", "report": "报告", "other": "其他"}
ADOPT_FROM = {src: col for col, (src, _key) in pipeline.ADOPT_MAP.items()}
SUBNAV = (("文献库", "/papers"), ("全部划线", "/excerpts"))


def subnav(active: str) -> dict[str, Any]:
    """Sub-tabs of the 论文 section; rendered by partials/_subnav.html when that partial exists."""
    items = [{"label": label, "href": href, "active": href == active} for label, href in SUBNAV]
    return {"subnav_items": items, "subnav_partial": (get_paths().web_templates / "partials" / "_subnav.html").exists()}


def label_context() -> dict[str, Any]:
    return {"lang_names": pipeline.LANG_NAMES, "status_names": STATUS_NAMES, "kind_names": KIND_NAMES,
            "read_depth_names": READ_DEPTH_NAMES, "relation_names": RELATION_NAMES, "field_names": FIELD_NAMES,
            "direction_names": {str(d["slug"]): str(d.get("name") or d["slug"]) for d in directions.load()}}


def _http(request: Request):
    """Tests put an httpx.Client with a MockTransport on app.state.intake_http."""
    return getattr(request.app.state, "intake_http", None)


def _go(request: Request, url: str) -> Response:
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-current-url"):
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _paper_or_404(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row:
    p = pipeline.get_paper(conn, paper_id)
    if p is None:
        raise HTTPException(404, f"没有论文 {paper_id}")
    return p


def _paper_dict(p: sqlite3.Row) -> dict[str, Any]:
    d = dict(p)
    d["authors"] = json.loads(p["authors_json"] or "[]")
    return d


def _projects_of(conn: sqlite3.Connection, paper_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ? ORDER BY project_slug", (paper_id,))]


def _camps_for(conn: sqlite3.Connection, slugs: list[str]) -> list[sqlite3.Row]:
    if not slugs:
        return []
    return conn.execute(f"SELECT id, label, project_slug FROM camps WHERE project_slug IN ({','.join('?' * len(slugs))}) "
                        "ORDER BY project_slug, sort, label", slugs).fetchall()


def source_url(p: sqlite3.Row) -> str | None:
    """Where the paper can be fetched: its DOI, else OpenAlex's landing page. Links to it open in the default browser
    (target=_blank; the Mac window hands those to the system, OPEN_EXTERNAL_LINKS_IN_BROWSER)."""
    if p["doi"]:
        return "https://doi.org/" + quote(p["doi"].strip(), safe="/:;()._-")
    try:
        oa = json.loads(p["openalex_json"] or "{}")
        url = ((oa.get("primary_location") or {}).get("landing_page_url") or "") if isinstance(oa, dict) else ""
    except (TypeError, ValueError, AttributeError):
        url = ""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else None


def card_context(conn: sqlite3.Connection, paper_id: str, **extra: Any) -> dict[str, Any]:
    _paper_or_404(conn, paper_id)
    pipeline.ensure_language(conn, paper_id)
    p = _paper_or_404(conn, paper_id)
    ctx = {"paper": _paper_dict(p), "citation": citation(p), "source_url": source_url(p),
           "file": pipeline.primary_file(conn, paper_id), "lookup": pipeline.lookup_state(p),
           "kinds": pipeline.KINDS, "files": conn.execute("SELECT * FROM paper_files WHERE paper_id = ? ORDER BY is_primary DESC, id",
                                                          (paper_id,)).fetchall(),
           "is_cjk": extract.is_cjk(p["language"]), **label_context()}
    ctx.update(extra)
    return ctx


def translation_context(conn: sqlite3.Connection, paper_id: str, **extra: Any) -> dict[str, Any]:
    p = _paper_or_404(conn, paper_id)
    lang = pipeline.ensure_language(conn, paper_id)
    targets = [t for t in pipeline.TRANSLATE_TARGETS if t != lang]
    default = pipeline.default_translate_target(lang)
    targets.sort(key=lambda t: t != default)
    statuses = {t: pipeline.translation_status(conn, paper_id, t) for t in targets}
    mode = queue.mode(conn)
    polling = any(s["chunks_claimed"] or s["chunks_open"] for s in statuses.values())
    fast = any(s["chunks_claimed"] or (s["chunks_open"] and mode in ("deepseek", "claude")) for s in statuses.values())
    ctx = {"paper": _paper_dict(p), "file": pipeline.primary_file(conn, paper_id), "trans_targets": targets,
           "trans_status": statuses, "trans_polling": polling, "trans_poll_every": "3s" if fast else "8s",
           "lang_names": pipeline.LANG_NAMES}
    ctx.update(extra)
    return ctx


def summary_context(conn: sqlite3.Connection, paper_id: str, **extra: Any) -> dict[str, Any]:
    p = _paper_or_404(conn, paper_id)
    slugs = _projects_of(conn, paper_id)
    job = pipeline.latest_summary_job(conn, paper_id)
    mode = queue.mode(conn)
    # Queued jobs are polled too: in agent mode a Claude Code session finishes them out of band.
    polling = bool(job and job["status"] in ("queued", "claimed"))
    fast = bool(job and (job["status"] == "claimed" or mode in ("deepseek", "claude")))
    ctx = {"paper": _paper_dict(p), "summary": pipeline.summary_of(p), "job": job, "polling": polling,
           "poll_every": "3s" if fast else "8s", "has_pdf": pipeline.primary_file(conn, paper_id) is not None,
           "paper_projects": slugs, "camps": _camps_for(conn, slugs), "summary_rows": SUMMARY_ROWS,
           "user_fields": USER_FIELD_LABELS, "adopt_from": ADOPT_FROM, "read_depths": pipeline.READ_DEPTHS,
           "relations": pipeline.RELATIONS, "ai_mode": mode, **label_context()}
    ctx.update(extra)
    return ctx


# ---------------------------------------------------------------- list and upload

TRANS_JOBS_OPEN = ("(SELECT COUNT(*) FROM ai_jobs j WHERE j.kind = 'paper.translate' AND j.status IN ('queued','claimed') "
                   "AND substr(j.target_id, 1, length(p.id) + 1) = p.id || ':')")


def _trans_state(r: sqlite3.Row) -> tuple[str, str]:
    """(css state, label) for the library's 译文 column."""
    if r["trans_open"]:
        return "running", f"进行中 {r['n_trans']}/{r['n_text_pages']}"
    if r["n_trans"] and r["n_trans"] >= r["n_text_pages"]:
        return "done", "已完成"
    if r["n_trans"]:
        return "partial", f"部分 {r['n_trans']}/{r['n_text_pages']}"
    return "none", "—"


def _summary_state(r: sqlite3.Row) -> tuple[str, str]:
    if r["ai_summary_json"]:
        return "done", "已摘要"
    return {"queued": ("running", "排队中"), "claimed": ("running", "生成中"),
            "failed": ("failed", "失败")}.get(r["job_status"] or "", ("none", "—"))


# The library lists only what can be read here (Leo, 10-02: a bibliography entry without the original stays off
# the list); entries without a file wait behind the 待补原文 link.
HAS_FILE = "EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id = p.id)"
# 分栏 by publication type; a chapter files under 书籍, anything else shows only under 全部.
KIND_TABS = {"article": ("论文", ("article",)), "book": ("书籍", ("book", "chapter")), "report": ("报告", ("report",))}


def archive_rail(conn: sqlite3.Connection) -> dict[str, Any]:
    """The 档案栏: 全部 · 未归档 · every 方向 with its 课题, each with counts of the papers that have their
    original (and unread counts for the top rows), in the order of directions.yml and projects.yml."""
    from ...files import datafiles
    by_dir = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT direction, COUNT(*), SUM(read_depth IS NULL OR read_depth = 'abstract') FROM papers p WHERE {HAS_FILE} GROUP BY direction")}
    by_project = {r[0]: r[1] for r in conn.execute(
        f"SELECT pp.project_slug, COUNT(*) FROM paper_projects pp JOIN papers p ON p.id = pp.paper_id WHERE {HAS_FILE} GROUP BY pp.project_slug")}
    projects = [dict(x) for x in (datafiles.read("projects") or [])]
    rail = []
    for d in directions.load():
        slug = str(d["slug"])
        total, unread = by_dir.get(slug, (0, 0))
        rail.append({"slug": slug, "name": str(d.get("name") or slug), "total": total, "unread": unread or 0,
                     "projects": [{"slug": str(pr["slug"]), "name": str(pr.get("name") or pr["slug"]), "total": by_project.get(str(pr["slug"]), 0)}
                                  for pr in projects if str(pr.get("direction") or "") == slug]})
    total = sum(t for t, _u in by_dir.values())
    unread = sum((u or 0) for _t, u in by_dir.values())
    none_total, none_unread = conn.execute(f"SELECT COUNT(*), SUM(read_depth IS NULL OR read_depth='abstract') FROM papers p WHERE {HAS_FILE} AND NOT EXISTS (SELECT 1 FROM paper_collection_items pci WHERE pci.paper_id=p.id)").fetchone()
    return {"total": total, "unread": unread, "unsorted": none_total, "unsorted_unread": none_unread or 0, "unclassified":sum(t for slug,(t,u) in by_dir.items() if slug not in {d["slug"] for d in rail}), "directions": rail,
            "project_names": {str(pr["slug"]): str(pr.get("name") or pr["slug"]) for pr in projects}}


# The list's sort orders (the 显示 menu); 导入时间 first, like 研究档案's newest-first lists.
SORTS = {"created": ("导入时间", "p.created_at DESC, p.id"),
         "opened": ("最近打开", "p.last_opened_at IS NULL, p.last_opened_at DESC, p.created_at DESC, p.id"),
         "year": ("年份", "p.year IS NULL, p.year DESC, p.created_at DESC, p.id"),
         "title": ("标题", "p.title COLLATE NOCASE, p.id")}
# light = 轻读 (card 0122, only title, abstract and the first page); a summary written before 0122 has no mode and is full
SUMMARY_MODE_SQL = ("CASE WHEN p.ai_summary_json IS NULL THEN NULL WHEN json_valid(p.ai_summary_json) "
                    "AND json_extract(p.ai_summary_json, '$.mode') = 'light' THEN 'light' ELSE 'full' END")
SUMMARY_MODE_NAMES = {"full": "中文概要：完整", "light": "中文概要：轻读，只读了标题、摘要和首页"}


def _surname(name: str) -> str:
    """What the list's 作者 column shows of one author: the family name; Chinese names and organisations whole."""
    name = re.sub(r"\s*\((?:editors?|eds?\.?|ed\.)\)", "", name.strip(), flags=re.I)
    if " and " in name or "/" in name:          # an organisation, e.g. "E3/EU+3 and Iran"
        return name
    if "," in name:
        last = name.split(",")[0].strip()
        return last.title() if last.isupper() else last
    if has_cjk(name):
        return name
    parts = name.split()
    return parts[-1] if parts else name


def byline(authors: list[str], year: Any) -> str:
    """作者 · 年份 for a list row (LIBRARY-UI §四): one family name, two joined by 、, three or more as 「X 等」."""
    names = [n for n in (_surname(str(a)) for a in authors or []) if n]
    who = ("" if not names else names[0] if len(names) == 1 else f"{names[0]}、{names[1]}" if len(names) == 2
           else f"{names[0]} 等")
    return " · ".join(b for b in (who, str(year) if year else "") if b) or "—"


def _day(ts: str | None) -> str:
    from ..context import date_zh, local_time
    return date_zh(local_time(ts, "%Y-%m-%d")) if ts else ""


def _line(r: sqlite3.Row, project_names: dict[str, str]) -> dict[str, Any]:
    """One paper as partials/_paper_line.html shows it."""
    slugs = [x for x in (r["project_slugs"] or "").split(",") if x]
    d = _paper_dict(r)
    return d | {"status": read_status(r["read_depth"]), "n_files": r["n_files"], "summary_mode": r["summary_mode"],
                "project_names": [project_names.get(x, x) for x in slugs], "byline": byline(d["authors"], r["year"]),
                "created_day": _day(r["created_at"]), "opened_day": _day(r["last_opened_at"])}


def _list_where(*, q: str, direction: str, project: str, status: str, filter: str, missing: bool, collection: str,
                folder_browser: bool, known: list[str]) -> tuple[list[str], list[Any]]:
    """The library list's WHERE clauses (all but 类型), shared by the list and the reader's 上一篇 / 下一篇."""
    where, args = [READ_STATUS_WHERE[status]], []
    if folder_browser:
        where.append("NOT EXISTS (SELECT 1 FROM paper_collection_items pci WHERE pci.paper_id=p.id)")
    if collection:
        where.append('p.id IN (SELECT paper_id FROM paper_collection_items WHERE collection_id=?)')
        args.append(collection)
    if filter in LEGACY_FILTERS:
        where.append(LEGACY_FILTERS[filter])
    match = fts_query(q)
    if q.strip():
        like = f"%{q.strip()}%"
        clauses = ["p.id LIKE ?", "lower(p.doi) = lower(?)"]
        cargs: list[Any] = [like, q.strip()]
        if match:
            clauses.insert(0, "p.rowid IN (SELECT rowid FROM papers_fts WHERE papers_fts MATCH ?)")
            cargs.insert(0, match)
        if has_cjk(q):
            # unicode61 keeps a run of CJK characters as one token, so Chinese titles need a substring test
            clauses += ["p.title LIKE ?", "p.abstract LIKE ?"]
            cargs += [like, like]
        where.append("(" + " OR ".join(clauses) + ")")
        args += cargs
    if direction == "none":
        where.append("NOT EXISTS (SELECT 1 FROM paper_collection_items pci WHERE pci.paper_id=p.id)")
    elif direction == "_none":
        if known:
            where.append("COALESCE(p.direction, '') NOT IN (" + ','.join('?' for _ in known) + ")")
            args += known
    elif direction:
        where.append("p.direction = ?")
        args.append(direction)
    if project:
        where.append("p.id IN (SELECT paper_id FROM paper_projects WHERE project_slug = ?)")
        args.append(project)
    where.append(f"NOT {HAS_FILE}" if missing else HAS_FILE)
    return where, args


def source_ids(conn: sqlite3.Connection, from_q: str) -> list[str] | None:
    """The ids of the list a paper was opened from, in the order the list shows them (groups too), or None when
    `from_q` (that list's query string) names nothing that exists. The reader's 上一篇 / 下一篇 and 3 / 10."""
    from urllib.parse import parse_qsl
    q = dict(parse_qsl(from_q or "", keep_blank_values=True))
    get = lambda k: (q.get(k) or "").strip()  # noqa: E731
    status = get("status") if get("status") in READ_STATUS_WHERE else "all"
    sort = get("sort") if get("sort") in SORTS else "created"
    direction, project, collection, view, kind = get("direction"), get("project"), get("collection"), get("view"), get("kind")
    missing = get("src") == "missing"
    folder_browser = view == "folders" and not (get("q") or direction or project or status != "all" or get("filter") or kind
                                                  or missing or collection)
    if collection and not collections.get(conn, collection):
        return None
    known = [str(d["slug"]) for d in directions.load()]
    where, args = _list_where(q=get("q"), direction=direction, project=project, status=status, filter=get("filter"),
                              missing=missing, collection=collection, folder_browser=folder_browser, known=known)
    if kind in KIND_TABS:
        kinds = KIND_TABS[kind][1]
        where.append(f"p.kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    rows = conn.execute(f"SELECT p.id, p.direction FROM papers p WHERE {' AND '.join(where)} ORDER BY {SORTS[sort][1]} LIMIT 500",
                        args).fetchall()
    if not (direction or project or collection or missing or folder_browser) and get("group") != "none":
        rank = {slug: i for i, slug in enumerate(known)}
        rows = sorted(rows, key=lambda r: rank.get(r["direction"], len(known)))   # stable: the sort stays inside a group
    return [r["id"] for r in rows]


@router.get("/papers")
def papers_list(request: Request, q: str = "", direction: str = "", project: str = "", status: str = "all",
                filter: str = "", kind: str = "", src: str = "", collection: str = "", view: str = "", group: str = "",
                sort: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    """The library (card 0128, docs/design/LIBRARY-UI.md §3.1): 全部 is one list grouped by 方向; a 方向, 课题,
    集合 or filter shows the same rows ungrouped. Old links keep working: ?direction= (also none 未归档 and
    _none 未分类), ?project=, ?collection=, ?view=all (the same as 全部) and ?view=folders (now the 集合 page, card 0132).
    The direction bar's pick, remembered across pages, filters 全部 and the lists, not the folders
    (context.chosen_direction)."""
    bar = not (view == "folders" or collection)
    if bar:
        direction = chosen_direction(request, scoped=bool(project))
    status = status if status in READ_STATUS_WHERE else "all"
    sort = sort if sort in SORTS else "created"
    folder_browser = view in ("folders", "collections") and not (q or direction or project or status != "all" or filter
                                                                 or kind or src or collection)
    if folder_browser:
        return collections_page(request, conn)
    known = [str(d["slug"]) for d in directions.load()]
    missing = src == "missing"
    # 全部: no 方向, 课题, folder or 待补原文 narrows the list; filters (status, type, search) still apply
    everything = not (direction or project or collection or missing or folder_browser)
    grouped = everything and group != "none"
    selected_collection = collections.get(conn, collection) if collection else None
    if collection and not selected_collection:
        raise HTTPException(404, '没有这个集合。')
    where, args = _list_where(q=q, direction=direction, project=project, status=status, filter=filter, missing=missing,
                              collection=collection, folder_browser=folder_browser, known=known)
    by_kind = {r[0]: r[1] for r in conn.execute(f"SELECT p.kind, COUNT(*) FROM papers p WHERE {' AND '.join(where)} GROUP BY p.kind", args)}
    kind_tabs = [{"key": "", "label": "全部", "count": sum(by_kind.values())}] + [
        {"key": k, "label": label, "count": sum(by_kind.get(x, 0) for x in kinds)} for k, (label, kinds) in KIND_TABS.items()]
    missing_count = conn.execute(f"SELECT COUNT(*) FROM papers p WHERE NOT {HAS_FILE}").fetchone()[0]
    no_summary = conn.execute(f"SELECT COUNT(*) FROM papers p WHERE {HAS_FILE} AND p.ai_summary_json IS NULL").fetchone()[0]
    kind = kind if kind in KIND_TABS else ""
    if kind:
        kinds = KIND_TABS[kind][1]
        where.append(f"p.kind IN ({','.join('?' * len(kinds))})")
        args += list(kinds)
    rows = conn.execute(
        "SELECT p.id, p.title, p.authors_json, p.year, p.venue, p.kind, p.doi, p.language, p.direction, p.read_depth, "
        f"p.created_at, p.last_opened_at, {SUMMARY_MODE_SQL} AS summary_mode, "
        "(SELECT COUNT(*) FROM paper_files f WHERE f.paper_id = p.id) AS n_files, "
        "(SELECT GROUP_CONCAT(pp.project_slug) FROM paper_projects pp WHERE pp.paper_id = p.id) AS project_slugs "
        f"FROM papers p WHERE {' AND '.join(where)} ORDER BY {SORTS[sort][1]} LIMIT 500", args).fetchall()
    rail = archive_rail(conn)
    papers = [_line(r, rail["project_names"]) for r in rows]
    groups = []
    if grouped:
        names = {d["slug"]: d["name"] for d in rail["directions"]}
        for slug in known + [""]:
            items = [x for x in papers if (x["direction"] if x["direction"] in names else "") == slug]
            if items:
                groups.append({"slug": slug, "name": names.get(slug, "未分类"), "items": items,
                               "unread": sum(1 for x in items if x["status"] == "unread")})
    qp = request.query_params
    pending = [dict(info, sha12=s) for s in qp.getlist("pending") if (info := pipeline.pending_info(s))]
    base_q = {"q": q, "direction": direction, "project": project, "status": status if status != "all" else "", "kind": kind,
              "src": "missing" if missing else "",
              "collection": collection, "view": view, "group": group if group == "none" else "", "sort": sort if sort != "created" else ""}
    in_direction = direction in known
    # the list's own query, so the reader's 上一篇 / 下一篇 walk this list (source_ids); empty means 全部
    from_q = urlencode(sorted((k, v) for k, v in base_q.items() if v))
    heading = {"unsummarized": "还没有中文概要的文献", "unverified": "引用信息待核验的文献"}.get(filter, "文献")
    if direction == "none":
        heading = "未归档"
    elif project:
        heading = rail["project_names"].get(project, project)
    if missing:
        heading = "待补原文" if heading == "文献" else f"{heading} · 待补原文"
    if selected_collection:
        heading = selected_collection['name'] + (' · 待补原文' if missing else '')
    if direction == "_none":
        heading = "未分类"
    folder_version = collections.order_version(conn)
    folder_rows = collections.listing(conn)
    return render(request, conn, "pages/papers.html", title="文献", papers=papers, groups=groups, grouped=grouped, from_q=from_q,
                  everything=everything, q=q, direction=direction, in_direction=in_direction,
                  direction_name=next((d["name"] for d in rail["directions"] if d["slug"] == direction), ""), project=project,
                  status=status, base_q=base_q, rail=rail, heading=heading, status_labels=READ_STATUS_LABELS,
                  kind=kind, kind_tabs=kind_tabs, missing=missing, missing_count=missing_count, no_summary=no_summary,
                  sort=sort, sorts={k: v[0] for k, v in SORTS.items()}, summary_mode_names=SUMMARY_MODE_NAMES,
                  collections=folder_rows, collection=collection, collection_order=folder_version, folder_browser=folder_browser, selected_collection=selected_collection,
                  added=qp.getlist("added"), dups=qp.getlist("dup"), rejected=qp.getlist("rejected"), pending=pending,
                  total=rail["total"], **({"dir_filter": direction, "dir_counts": {d["slug"]: d["total"] for d in rail["directions"]},
                                           "dir_total": rail["total"]} if bar else {}),
                  **label_context(), **subnav("/papers"))


@router.get("/papers/{paper_id}/peek")
def paper_peek(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The list's preview column (LIBRARY-UI §3.1, from 1600px): what the paper asks and argues, its 课题, Read and
    Open PDF. A fragment for papers.js; it never marks the paper opened."""
    p = _paper_or_404(conn, paper_id)
    f = pipeline.primary_file(conn, paper_id)
    summary = pipeline.summary_of(p)
    names = {str(x["slug"]): str(x.get("name") or x["slug"]) for x in datafiles.read("projects") or []}
    return render(request, conn, "partials/_paper_peek.html", paper=_paper_dict(p), summary=summary,
                  summary_mode=("light" if summary.get("mode") == "light" else "full") if summary else None,
                  pages=f["page_count"] if f else None, has_pdf=f is not None,
                  projects=[(s, names.get(s, s)) for s in _projects_of(conn, paper_id)], kind_names=KIND_NAMES,
                  peek_rows=SUMMARY_ROWS[:2])


COLLECTION_NOTES = {'deleted': '已删除集合，里面的文献还在。',
                    'stale': '集合在别处改过，这次没有删除；下面是刷新后的列表，可以再删一次。',
                    'missing': '这个集合已经不在了。'}


def collections_page(request: Request, conn: sqlite3.Connection):
    """集合 (card 0132, LIBRARY-UI §3.4): one row per collection in the list's columns: name, the directions its
    papers come from, papers and unread, last change, ⋯; New collection last. The order version is read before the
    listing, so an order changed in between is refused rather than overwritten."""
    version = collections.order_version(conn)
    rows = collections.listing(conn)
    for r in rows:
        day = str(r["updated"] or "")[:10]
        r["when"] = f"{int(day[5:7])}月{int(day[8:10])}日" if len(day) == 10 else ""
    return render(request, conn, "pages/collections.html", title="集合", collections=rows, collection_order=version,
                  note=COLLECTION_NOTES.get(request.query_params.get("folder", ""), ""), **label_context(), **subnav("/papers"))


@router.post('/paper-collections')
async def collection_create(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    try:
        ref = collections.create(conn, form.get('name'))
    except collections.CollectionError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    url = '/papers?collection=' + ref
    return JSONResponse({'url': url}) if request.headers.get('X-Library-Form') else RedirectResponse(url, status_code=303)


@router.post('/paper-collections/order')
async def collection_order(request: Request, conn=Depends(get_conn)):
    form = await request.form()
    try:
        collections.reorder(conn, form.getlist('ids'), form.get('version'))
    except collections.CollectionError as e:
        return JSONResponse({'error': str(e)}, status_code=409)
    return JSONResponse({'ok': True})


@router.post('/paper-collections/{ref}/rename')
async def collection_rename(ref: str, request: Request, conn=Depends(get_conn)):
    form = await request.form()
    try:
        collections.rename(conn, ref, form.get('name'), form.get('version'))
    except collections.CollectionError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    url = '/papers?collection=' + ref
    return JSONResponse({'url': url}) if request.headers.get('X-Library-Form') else RedirectResponse(url, status_code=303)


@router.post('/paper-collections/{ref}/delete')
async def collection_delete(ref: str, request: Request, conn=Depends(get_conn)):
    """The collection's ⋯ menu posts a plain form: every outcome lands back on the 集合 page with a notice."""
    form = await request.form()
    library = request.headers.get('X-Library-Form')
    try:
        collections.delete(conn, ref, form.get('version'))
    except LookupError:
        if library:
            return JSONResponse({'error': '这个集合已经不在了，请刷新。'}, status_code=404)
        return RedirectResponse('/papers?view=folders&folder=missing', status_code=303)
    except collections.CollectionError as e:
        if library:
            return JSONResponse({'error': str(e)}, status_code=409)
        return RedirectResponse('/papers?view=folders&folder=stale', status_code=303)
    url = '/papers?view=folders'
    return JSONResponse({'url': url}) if library else RedirectResponse(url + '&folder=deleted', status_code=303)


@router.post("/papers/{paper_id}/archive")
async def paper_archive(paper_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """File a paper from the 档案栏: `direction` (slug, or empty to clear) and / or `project` (slug; a paper
    without a direction takes the 课题's). Dropping a row on a rail node posts here."""
    from ...files import datafiles
    if pipeline.get_paper(conn, paper_id) is None:
        raise HTTPException(404, "没有这篇文献")
    form = await request.form()
    if form.get('move') == '1':
        try:
            collections.move(conn, paper_id, str(form.get('collection') or '') or None)
        except collections.CollectionError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        return JSONResponse({'ok': True})
    if form.get('collection'):
        try:
            collections.assign(conn, str(form['collection']), paper_id, remove=form.get('remove') == '1')
        except collections.CollectionError as e:
            return JSONResponse({'error': str(e)}, status_code=400)
        return JSONResponse({'url': '/papers?collection=' + str(form['collection'])}) if request.headers.get('X-Library-Form') else Response(status_code=204)
    project = str(form.get("project") or "").strip()
    if project:
        entry = datafiles.project(project)
        if not entry:
            raise HTTPException(400, "没有这个课题")
        conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (paper_id, project))
        if not pipeline.get_paper(conn, paper_id)["direction"] and entry.get("direction"):
            pipeline.set_direction(conn, paper_id, str(entry["direction"]))
    if "direction" in form:
        try:
            pipeline.set_direction(conn, paper_id, str(form.get("direction") or ""))
            if not form.get('direction'):
                conn.execute('DELETE FROM paper_collection_items WHERE paper_id=?', (paper_id,))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if request.headers.get("hx-request") == "true" and request.headers.get("hx-target"):
        return Response(status_code=204)
    from .tasks import safe_next
    return _go(request, safe_next(str(form.get("next") or "")) or "/papers")


@router.post("/papers/{paper_id}/status")
async def paper_status(paper_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """未读 · 在读 · 读完 from the reader's toolbar (or the archive). Only Leo sets 读完."""
    p = _paper_or_404(conn, paper_id)
    form = await request.form()
    state = str(form.get("state") or "")
    if state not in STATE_DEPTH:
        raise HTTPException(400, "阅读状态只能是未读、在读或读完")
    depth = STATE_DEPTH[state]
    if state == "done" and p["read_depth"] == "reread":
        depth = "reread"
    pipeline.update_user_fields(conn, paper_id, {"read_depth": depth})
    from .tasks import safe_next
    return _go(request, safe_next(str(form.get("next") or "")) or f"/papers/{paper_id}")


def _neighbours(conn: sqlite3.Connection, p: sqlite3.Row, from_q: str | None) -> dict[str, Any]:
    """上一篇 / 下一篇 and "3 / 10" in the list the paper was opened from (`from`, that list's query; card 0129);
    opened from elsewhere, its 方向's list (未分类 without one). The links carry `from` on, so [ and ] keep walking
    the same list."""
    ids = source_ids(conn, from_q) if from_q is not None else None
    if ids is None or p["id"] not in ids:
        from_q = None
        ids = source_ids(conn, urlencode({"direction": p["direction"] or "_none"})) or []
    if p["id"] not in ids:
        return {"prev_paper": None, "next_paper": None, "position": None, "from_q": None}
    i = ids.index(p["id"])
    titles = dict(conn.execute(f"SELECT id, title FROM papers WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()) if ids else {}
    pick = lambda j: {"id": ids[j], "title": titles.get(ids[j], "")} if 0 <= j < len(ids) else None  # noqa: E731
    return {"prev_paper": pick(i - 1), "next_paper": pick(i + 1), "position": (i + 1, len(ids)), "from_q": from_q}


async def _save_upload(upload: UploadFile) -> Path:
    incoming = get_paths().cache / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    dest = incoming / f"upload-{uuid.uuid4().hex}.pdf"
    size = 0
    with open(dest, "wb") as out:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > extract.MAX_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise extract.RejectedFile(f"文件超过 {extract.MAX_BYTES // (1024 * 1024)} MB")
            out.write(chunk)
    return dest


def import_target(page: str | None) -> dict[str, str | None]:
    """Where an import came from decides where it is filed (docs/design/PAPERS.md §B, card 0035). `page`
    is the path (and query) of the page the PDF was dropped on: a 课题 page files it there; a 方向 or 课题
    shelf on /papers files it on that shelf; a bibliography-only paper's own page means "this is its
    PDF". Anything else falls back, in ingest, to the current project's direction."""
    from urllib.parse import parse_qs, urlparse
    u = urlparse(page or "")
    path, q = u.path or "", parse_qs(u.query)
    out: dict[str, str | None] = {"project": None, "direction": None, "paper": None}
    parts = [x for x in path.split("/") if x]
    if len(parts) >= 2 and parts[0] == "projects" and parts[1] not in ("new",):
        out["project"] = parts[1]
    elif parts == ["papers"]:
        d = (q.get("direction") or [""])[0]
        out["direction"] = d if d and d not in ("none", "_none") else None
        out["project"] = (q.get("project") or [None])[0]
        out["collection"] = (q.get("collection") or [None])[0]
    elif len(parts) >= 2 and parts[0] == "papers" and parts[1] not in ("pending",):
        out["paper"] = parts[1]
    return out


@router.post("/papers")
async def papers_upload(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from ...files import datafiles
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_FILES * extract.MAX_BYTES + CHUNK:
        raise HTTPException(413, "upload too large")
    async with request.form(max_files=MAX_FILES, max_fields=20) as form:
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        target = import_target(str(form.get("page") or "") or request.headers.get("referer"))
        project = str(form.get("project") or "") or target["project"] or None
        if project and not datafiles.project(project):
            project = None
        direction = str(form.get("direction") or "") or target["direction"] or None
        folder = target.get("collection")
        if folder and not collections.get(conn, folder):
            raise HTTPException(400, "文件夹已不存在，请刷新后再导入。")
        if not uploads:
            return _go(request, "/papers?" + urlencode({"rejected": "未选择文件"}))
        # Dropped on a bibliography-only record's page: the PDF is that record's (补 PDF).
        holder = pipeline.get_paper(conn, target["paper"]) if target["paper"] and len(uploads) == 1 else None
        if holder is not None and pipeline.primary_file(conn, holder["id"]) is None:
            tmp = None
            try:
                tmp = await _save_upload(uploads[0])
                await run_in_threadpool(pipeline.attach_file, conn, tmp, holder["id"],
                                        original_name=Path(uploads[0].filename or "upload.pdf").name)
            except (extract.RejectedFile, ValueError) as e:
                return _go(request, f"/papers/{holder['id']}?" + urlencode({"pdf_error": str(e)}))
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            return _go(request, f"/papers/{holder['id']}?note=pdf-added")
        added, dups, rejected, pending = [], [], [], []
        http = _http(request)
        for up in uploads:
            name = Path(up.filename or "upload.pdf").name
            tmp: Path | None = None
            try:
                tmp = await _save_upload(up)
                r = await run_in_threadpool(pipeline.ingest, conn, tmp, project=project, direction=direction, http=http,
                                            original_name=name)
            except extract.RejectedFile as e:
                rejected.append(f"{name}: {e}")
                continue
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            if folder and (r.created or (r.duplicate_of and not r.pending)):
                collections.assign(conn, folder, r.paper_id if r.created else r.duplicate_of)
            if r.created:
                added.append(r.paper_id)
            elif r.pending:
                pending.append(r.pending)
            else:
                dups.append(r.duplicate_of)
    if len(uploads) == 1 and (added or dups):
        return _go(request, f"/papers/{added[0]}" if added else f"/papers/{dups[0]}?note=duplicate")
    return _go(request, "/papers?" + urlencode({"added": added, "dup": dups, "rejected": rejected, "pending": pending, **({"collection": folder} if folder else {})}, doseq=True))


@router.post("/papers/{paper_id}/files")
async def paper_add_file(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """补 PDF from the record's own page: the chosen PDF becomes its file (the primary one when it had
    none) and a summary is queued. The global drop on that page arrives at POST /papers instead."""
    _paper_or_404(conn, paper_id)
    async with request.form(max_files=1, max_fields=5) as form:
        up = next((f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename), None)
        if up is None:
            return _go(request, f"/papers/{paper_id}?" + urlencode({"pdf_error": "未选择文件"}))
        tmp = None
        try:
            tmp = await _save_upload(up)
            await run_in_threadpool(pipeline.attach_file, conn, tmp, paper_id, original_name=Path(up.filename).name)
        except (extract.RejectedFile, ValueError) as e:
            return _go(request, f"/papers/{paper_id}?" + urlencode({"pdf_error": str(e)}))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return _go(request, f"/papers/{paper_id}?note=pdf-added")


@router.post("/papers/{paper_id}/files/replace")
async def paper_replace_file(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """换原文 (card 0175): the chosen PDF takes the place of the one attached by mistake."""
    _paper_or_404(conn, paper_id)
    async with request.form(max_files=1, max_fields=5) as form:
        up = next((f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename), None)
        if up is None:
            return _go(request, f"/papers/{paper_id}?" + urlencode({"file_error": "未选择文件"}))
        tmp = None
        try:
            tmp = await _save_upload(up)
            await run_in_threadpool(pipeline.replace_primary_file, conn, tmp, paper_id, original_name=Path(up.filename).name)
        except (extract.RejectedFile, ValueError) as e:
            return _go(request, f"/papers/{paper_id}?" + urlencode({"file_error": str(e)}))
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
    return _go(request, f"/papers/{paper_id}?note=pdf-replaced")


@router.post("/papers/{paper_id}/files/{file_id}/remove")
async def paper_remove_file(request: Request, paper_id: str, file_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    """移除 a file (card 0175); the page says why when the paper's highlights hold it in place."""
    _paper_or_404(conn, paper_id)
    try:
        await run_in_threadpool(pipeline.remove_file, conn, paper_id, file_id)
    except LookupError:
        raise HTTPException(404, "没有这个文件") from None
    except ValueError as e:
        return _go(request, f"/papers/{paper_id}?" + urlencode({"file_error": str(e)}))
    return _go(request, f"/papers/{paper_id}?note=pdf-removed")


@router.post("/papers/summarize")
async def papers_summarize(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Queue Chinese summaries for every paper with a PDF and none yet, in one 课题 (`project`) or all."""
    from ...files import datafiles
    form = await request.form()
    project = str(form.get("project") or "") or None
    if project and not datafiles.project(project):
        raise HTTPException(400, "没有这个课题")
    counts = pipeline.summarize_missing(conn, project=project)
    from .tasks import safe_next
    nxt = safe_next(str(form.get("next") or "")) or "/papers"
    sep = "&" if "?" in nxt else "?"
    return _go(request, nxt + sep + urlencode({"summarized": counts["queued"], "summary_waiting": counts["waiting"],
                                                "summary_no_file": counts["no_file"]}))


@router.post("/papers/pending/{sha12}/attach")
async def pending_attach(request: Request, sha12: str, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    paper_id = str(form.get("paper_id") or "")
    folder = str(form.get("collection") or "")
    if folder and not collections.get(conn, folder):
        raise HTTPException(400, "文件夹已不存在，请刷新后重试。")
    try:
        await run_in_threadpool(pipeline.attach_pending, conn, sha12, paper_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    if folder:
        collections.assign(conn, folder, paper_id)
    return _go(request, f"/papers/{paper_id}?note=attached")


@router.post("/papers/pending/{sha12}/import")
async def pending_import(request: Request, sha12: str, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    folder = str(form.get("collection") or "")
    if folder and not collections.get(conn, folder):
        raise HTTPException(400, "文件夹已不存在，请刷新后重试。")
    try:
        r = await run_in_threadpool(pipeline.import_pending, conn, sha12, http=_http(request))
    except LookupError as e:
        raise HTTPException(404, str(e))
    if folder:
        collections.assign(conn, folder, r.paper_id)
    return _go(request, f"/papers/{r.paper_id}")


# ---------------------------------------------------------------- detail

@router.get("/papers/{paper_id}/card")
def paper_card(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id))


@router.post("/papers/{paper_id}/direction")
async def paper_direction(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    form = await request.form()
    try:
        pipeline.set_direction(conn, paper_id, str(form.get("direction") or ""))
    except ValueError as e:
        return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id, meta_error=str(e)))
    return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id, direction_saved=True))


@router.post("/papers/{paper_id}/metadata")
async def paper_metadata(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    raw_authors = str(form.get("authors") or "")
    authors = [a.strip() for a in raw_authors.replace("\r", "").replace(";", "\n").split("\n") if a.strip()]
    year_raw = str(form.get("year") or "").strip()
    error = None
    try:
        year = int(year_raw) if year_raw else None
        pipeline.update_metadata(conn, paper_id, title=str(form.get("title") or ""), authors=authors, year=year,
                                 venue=str(form.get("venue") or "").strip() or None, kind=str(form.get("kind") or "") or None,
                                 doi=str(form.get("doi") or "") or None)
    except ValueError as e:
        error = str(e) if "invalid literal" not in str(e) else "年份必须是数字"
    return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id, meta_error=error,
                                                                              meta_saved=error is None, editing=error is not None))


@router.post("/papers/{paper_id}/lookup")
async def paper_lookup(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    res = await run_in_threadpool(pipeline.lookup, conn, paper_id, http=_http(request))
    msg = "已通过 Crossref/OpenAlex 核验。" if res.status == "verified" else (
        f"仍未核验；找到 {len(res.candidates)} 个候选记录。" if res.candidates else "仍未核验；没有找到候选记录，可以手动填写元数据。")
    return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id, lookup_msg=msg, lookup_errors=res.errors))


@router.post("/papers/{paper_id}/candidates/{index}")
async def paper_accept_candidate(request: Request, paper_id: str, index: int, conn: sqlite3.Connection = Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    try:
        errors = await run_in_threadpool(pipeline.accept_candidate, conn, paper_id, index, http=_http(request))
    except IndexError:
        raise HTTPException(404, "没有这个候选记录")
    return render(request, conn, "partials/_paper_card.html", **card_context(conn, paper_id, lookup_errors=errors,
                                                                              lookup_msg=None if errors else "已采用该候选记录。"))


# ---------------------------------------------------------------- summary and map fields

@router.get("/papers/{paper_id}/summary")
def paper_summary(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "partials/_summary_card.html", **summary_context(conn, paper_id))


@router.post("/papers/{paper_id}/summarize")
def paper_summarize(request: Request, paper_id: str, mode: str = Form(""), conn: sqlite3.Connection = Depends(get_conn)):
    """重新生成 follows papers.summary_mode; 「读全文生成完整概要」 posts mode=full and reads the whole paper (card 0122)."""
    _paper_or_404(conn, paper_id)
    pipeline.enqueue_summary(conn, paper_id, force=True, mode="full" if mode == "full" else None)
    return render(request, conn, "partials/_summary_card.html", **summary_context(conn, paper_id))


@router.post("/papers/{paper_id}/adopt/{column}")
def paper_adopt(request: Request, paper_id: str, column: str, conn: sqlite3.Connection = Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    error = None
    try:
        pipeline.adopt(conn, paper_id, column)
    except KeyError:
        raise HTTPException(404, f"不能采纳到 {column}")
    except LookupError as e:
        error = str(e)
    resp = render(request, conn, "partials/_summary_card.html",
                  **summary_context(conn, paper_id, fields_error=error, adopted=None if error else column))
    if column == "direction" and not error:
        resp.headers["HX-Trigger"] = "paper-direction-changed"
    return resp


@router.post("/papers/{paper_id}/fields")
async def paper_fields(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _paper_or_404(conn, paper_id)
    form = await request.form()
    values = {k: str(form.get(k) or "") for k in pipeline.USER_FIELDS if k in form}
    projects = [str(p) for p in form.getlist("projects")] if form.get("projects_present") else None
    error = None
    try:
        pipeline.update_user_fields(conn, paper_id, values, projects)
    except ValueError as e:
        error = str(e)
    return render(request, conn, "partials/_summary_card.html",
                  **summary_context(conn, paper_id, fields_error=error, fields_saved=error is None))


# ---------------------------------------------------------------- translation

@router.get("/papers/{paper_id}/translation")
def paper_translation(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return render(request, conn, "partials/_translation.html", **translation_context(conn, paper_id))


@router.post("/papers/{paper_id}/translate")
async def paper_translate(request: Request, paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    p = _paper_or_404(conn, paper_id)
    form = await request.form()
    lang_to = str(form.get("lang") or "") or pipeline.default_translate_target(p["language"])
    if lang_to not in pipeline.TRANSLATE_TARGETS:
        raise HTTPException(400, "目标语言只能是 en 或 zh")
    msg, error = None, None
    try:
        ids = await run_in_threadpool(pipeline.enqueue_translation, conn, paper_id, lang_to, force=bool(form.get("force")))
        msg = f"已排入 {len(ids)} 个翻译任务。" if ids else "没有需要翻译的新分段（已有译文或任务正在进行）。"
    except (ValueError, LookupError) as e:
        error = str(e)
    return render(request, conn, "partials/_translation.html",
                  **translation_context(conn, paper_id, trans_msg=msg, trans_error=error))


# ---------------------------------------------------------------- reader and page images

READER_VIEWS = {"orig": "原文", "trans": "译文", "both": "对照"}
SIDE_TABS = ("marks", "summary", "info")


def _bare_label(label: str | None) -> str:
    return re.sub(r"^(?:p{1,2}\.?\s*)", "", (label or "").strip().lower()).strip("[]() ")


def _split_paragraphs(text: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Whitespace collapsed to single spaces and trimmed, with the original index of every kept character."""
    out: list[str] = []
    idx: list[int] = []
    space = True
    for i, ch in enumerate(text):
        if ch.isspace():
            if not space:
                out.append(" ")
                idx.append(i)
            space = True
        else:
            out.append(ch)
            idx.append(i)
            space = False
    if out and out[-1] == " ":
        out.pop()
        idx.pop()
    return "".join(out), idx


def highlight(paras: list[str], quotes: list[tuple[str, str]]) -> list[list[dict[str, Any]]]:
    """The page's paragraphs as segments, each {text, ids}: the parts that a 划线 quote covers carry its id
    (docs/design/PAPERS.md §F). Quotes are matched with whitespace normalised over the whole page, so a
    quote that crosses a paragraph break is cut into one mark per paragraph; a quote not found (an older
    reflow, a typed quote) is simply not marked."""
    norms, maps, starts, pos = [], [], [], 0
    for para in paras:
        n, m = _norm_map(para)
        norms.append(n)
        maps.append(m)
        starts.append(pos)
        pos += len(n) + 1
    page = " ".join(norms)
    spans: list[tuple[int, int, int, str]] = []
    for ex_id, quote in quotes:
        q, _ = _norm_map(quote or "")
        if len(q) < 4:
            continue
        k = page.find(q)
        if k < 0:
            continue
        for i, n in enumerate(norms):
            lo, hi = max(k, starts[i]), min(k + len(q), starts[i] + len(n))
            if lo < hi:
                spans.append((i, maps[i][lo - starts[i]], maps[i][hi - starts[i] - 1] + 1, ex_id))
    out = []
    for i, para in enumerate(paras):
        mine = [x for x in spans if x[0] == i]
        cuts = sorted({0, len(para), *(x[1] for x in mine), *(x[2] for x in mine)})
        out.append([{"text": para[a:b], "ids": [x[3] for x in mine if x[1] <= a and b <= x[2]]}
                    for a, b in zip(cuts, cuts[1:]) if b > a])
    return out


def _excerpt_groups(excerpts: list[sqlite3.Row], pages: list) -> list[dict[str, Any]]:
    """The 划线 tab, one group per page in page order; excerpts without a page come last."""
    labels = {pg.index: pg.label for pg in pages}
    groups: list[dict[str, Any]] = []
    for e in excerpts:
        key = e["page_index"]
        if not groups or groups[-1]["page_index"] != key:
            groups.append({"page_index": key, "label": labels.get(key, e["locator"]) if key is not None else None, "items": []})
        groups[-1]["items"].append(e)
    return groups


@router.get("/papers/{paper_id}")
@router.get("/papers/{paper_id}/read")
def paper_page(request: Request, paper_id: str, page: str | None = None, view: str = "orig", lang: str = "",
               note: str = "", pdf_error: str = "", file_error: str = "", from_: str | None = Query(None, alias="from"),
               conn: sqlite3.Connection = Depends(get_conn)):
    """Read the original PDF beside the summary (card 0129, docs/design/LIBRARY-UI.md §3.2): one header row, the
    PDF and a side panel with 概要 (four properties, then the summary) and 信息 (citation, files, translation,
    tools, old excerpts). Legacy /read links and #info / #marks keep working. ?page= opens at a page; ?find= is
    read by paper-reader.js, which searches from there."""
    # The first open moves an unread paper with pages to 在读 (PAPERS.md §H.2); every open is stamped.
    row = _paper_or_404(conn, paper_id)
    auto_reading = row["last_opened_at"] is None and read_status(row["read_depth"]) == "unread" \
        and pipeline.primary_file(conn, paper_id) is not None
    conn.execute("UPDATE papers SET last_opened_at = ?" + (", read_depth = 'skimmed'" if auto_reading else "") + " WHERE id = ?",
                 (now_iso(), paper_id))
    # Papers the feed brought in arrive without a summary (card 0156, Leo: 「我点了你再读写概要」): opening the reader of
    # a paper with a file and no summary asks for one, at the papers.summary_mode depth (light by default). A summary
    # job already queued or running means it is on its way, so a reload queues nothing; list rows never come here.
    if not row["ai_summary_json"] and pipeline.primary_file(conn, paper_id) is not None and not conn.execute(
            "SELECT 1 FROM ai_jobs WHERE kind = ? AND target_id = ? AND status IN ('queued', 'claimed')",
            (pipeline.SUMMARY_KIND, paper_id)).fetchone():
        pipeline.enqueue_summary(conn, paper_id)
    ctx = card_context(conn, paper_id)
    ctx.update(summary_context(conn, paper_id))
    walk = _neighbours(conn, _paper_or_404(conn, paper_id), from_)
    ctx.update(translation_context(conn, paper_id))
    language = ctx["paper"].get("language")
    f = ctx["file"]
    pages = pipeline.load_pages(conn, f["id"]) if f else []
    available = pipeline.translated_langs(conn, paper_id)
    lang = lang if lang in pipeline.TRANSLATE_TARGETS else (available[0] if available else pipeline.default_translate_target(language))
    translations = pipeline.translations_for(conn, f["id"], lang) if f else {}
    view = view if view in READER_VIEWS else "orig"
    view_note = None
    if view != "orig" and not translations:
        view_note = f"还没有{pipeline.LANG_NAMES.get(lang, lang)}译文，先显示原文。"
        view = "orig"
    initial = None
    if page is not None:
        hit = next((pg for pg in pages if pg.label == page), None)
        if hit is None:
            # JSTOR and some publishers label pages "p. 381" or "p. [379]"; links (summaries, excerpts) say "381".
            want = _bare_label(page)
            hit = next((pg for pg in pages if _bare_label(pg.label) == want), None)
        if hit is None and page.isdigit() and 1 <= int(page) <= len(pages):
            hit = pages[int(page) - 1]
        initial = hit.index if hit else None
    trans_paras = {i: _split_paragraphs(t) for i, t in translations.items()}
    excerpts = conn.execute("SELECT id, locator, page_index, quote, my_note, kind, quote_verified, project_slug, created_at "
                            "FROM excerpts WHERE paper_id = ? ORDER BY page_index IS NULL, page_index, created_at", (paper_id,)).fetchall()
    from ...files import datafiles
    project_names = {str(x["slug"]): str(x.get("name") or x["slug"]) for x in (datafiles.read("projects") or [])}
    linked = ctx["paper_projects"]
    # card 0073: a paper that came in through 推送 says from which journal. The library link alone is not enough:
    # scheduler.link_library also points feed items at papers Leo had added before the push saw them.
    feed = conn.execute("SELECT f.journal_name, f.journal FROM feed_papers f JOIN papers p ON p.id = f.paper_id "
                        "WHERE f.paper_id = ? AND p.created_at >= f.first_seen LIMIT 1", (paper_id,)).fetchone()
    from_feed = (feed["journal_name"] or feed["journal"]) if feed else None
    return render(request, conn, "pages/paper.html", title=ctx["paper"]["title"], pages=pages, from_feed=from_feed,
                  page_labels=[pg.label for pg in pages],
                  trans_paras=trans_paras, initial_page=initial, excerpts=excerpts, view=view, views=READER_VIEWS,
                  excerpt_groups=_excerpt_groups(excerpts, pages),
                  trans_lang=lang, translations=translations, trans_langs=available, view_note=view_note, page_param=page,
                  note=note, pdf_error=pdf_error[:200], file_error=file_error[:200], project_names=project_names,
                  byline=byline(ctx["paper"]["authors"], ctx["paper"].get("year")),
                  linked_projects=[(s, project_names.get(s, s)) for s in linked],
                  other_projects=[(s, n) for s, n in project_names.items() if s not in linked],
                  direction_list=directions.load(), read_state=read_status(ctx["paper"].get("read_depth")), states=STATES,
                  auto_reading=auto_reading, **walk, **ctx, **subnav("/papers"))


@router.get("/papers/{paper_id}/page/{n}.png")
def paper_page_png(paper_id: str, n: int, conn: sqlite3.Connection = Depends(get_conn)):
    f = pipeline.primary_file(conn, paper_id)
    if f is None or not 0 <= n < f["page_count"]:
        raise HTTPException(404, "没有这一页")
    paths = get_paths()
    out = paths.cache / "pages" / paper_id / f"{f['sha256'][:12]}-{n}.png"
    if not out.exists():
        pdf = paths.data / f["rel_path"]
        if not pdf.exists():
            raise HTTPException(404, "PDF 文件缺失")
        extract.render_page_png(pdf, n, out)
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/papers/{paper_id}/pdf")
def paper_pdf(paper_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Serve only the paper's stored primary file, including byte ranges for PDF.js."""
    _paper_or_404(conn, paper_id)
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        raise HTTPException(404, "还没有 PDF")
    root = get_paths().data.resolve()
    path = (root / f["rel_path"]).resolve()
    # A corrupt record or symlink must not expose a file outside the library.
    if not path.is_relative_to(root / "papers") or not path.is_file() or path.suffix.lower() != ".pdf":
        raise HTTPException(404, "PDF 文件缺失")
    return FileResponse(path, media_type="application/pdf", filename="paper.pdf",
                        content_disposition_type="inline", headers={"Cache-Control": "private, no-store",
                                                                    "X-Content-Type-Options": "nosniff"})
