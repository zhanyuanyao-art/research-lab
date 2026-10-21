"""Method views: milestone ladder, literature map, evidence matrix, checks, concept ledger."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ...db.connection import now_iso
from ...files import artifacts, datafiles, deliverables
from ...files import tasks as tasks_file
from ...files.weeks import week_add, week_of
from ...pm import progress
from ...files.yamlio import StaleFileError, sha256_text
from ...intake import pipeline
from ...method import checks as checks_mod
from ...method import concepts as concepts_mod
from ...method import exports, hunt, mapgraph, matrix, milestones
from ... import i18n
from ..context import get_conn, render, templates

router = APIRouter()


def _project(slug: str) -> dict:
    p = datafiles.project(slug)
    if not p:
        raise HTTPException(404, f"_data/projects.yml 中没有项目 {slug!r}")
    return p


def _done(request: Request, url: str, error: str | None = None) -> Response:
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    if request.headers.get("hx-request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _s(form, name: str) -> str:
    return str(form.get(name) or "").strip()


def _labels() -> i18n.Labels:
    """Partials rendered on their own (htmx swaps) do not get base_context, so they need `L` passed in."""
    return i18n.Labels(datafiles.read_all())


def _ladder_ctx(conn: sqlite3.Connection, slug: str) -> dict:
    ms = milestones.evaluate(conn, slug)
    return {"L": _labels(), "milestones": ms, "next": milestones.next_unmet(conn, slug),
            "suggestion": milestones.stage_suggestion(conn, slug, ms), "slug": slug}


@router.get("/projects")
def projects_index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    artifacts.index(conn)
    keys = milestones.stage_keys()
    rows = []
    for p in datafiles.read("projects") or []:
        slug = str(p["slug"])
        counts = milestones.project_counts(conn, slug)
        n_art = conn.execute("SELECT COUNT(*) FROM artifacts WHERE project = ?", (slug,)).fetchone()[0]
        rows.append({**dict(p), "next": milestones.next_unmet(conn, slug), "counts": counts,
                     "paper_count": counts["papers"], "artifact_count": n_art,
                     "stage_index": keys.index(p["stage"]) if p["stage"] in keys else -1, "stage_total": len(keys),
                     "camps": len(mapgraph.camps(conn, slug)), "placed": len(mapgraph.paper_camps(conn, slug))})
    order = [str(d.get("slug")) for d in (datafiles.read("directions") or [])]
    groups = [{"direction": d, "rows": [r for r in rows if r.get("direction") == d]} for d in order]
    groups = [g for g in groups if g["rows"]]
    loose = [r for r in rows if r.get("direction") not in order]
    if loose:
        groups.append({"direction": None, "rows": loose})
    return render(request, conn, "pages/projects.html", rows=rows, groups=groups, saved=request.query_params.get("saved"), title="课题")


# ---- editing projects (_data/projects.yml and projects/<slug>/*.md) ------------------------------

PROJECT_FORM_FIELDS = ("name", "question", "stage", "direction", "since", "note")
FILE_LABELS = {"index.md": "说明 index.md", "literature.md": "文献表 literature.md", "evidence.md": "证据表 evidence.md"}

# index.md is also a Jekyll page for the static site: front matter, a Liquid artifact list, and template
# headings in English. The 说明 card shows only what Leo wrote.
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_LIQUID = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
_KICKER = re.compile(r"^<p class=\"kicker\">.*?</p>\s*$", re.M)
# Sections the hub already shows itself (the file list, the 产出 tab).
_SITE_ONLY = {"files", "artifacts"}
_HEADINGS = {"question": "研究问题", "why it is interesting": "为什么值得做", "current answer": "目前的回答"}
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def readme_text(text: str, slug: str) -> str:
    """The 说明 card's Markdown: index.md minus front matter, Liquid, the H1 and stage kicker (the page
    header carries both), site-only sections and placeholder-only sections; English template headings
    in Chinese; relative links pointed at this 课题's files (Jekyll's `literature/` 404s in the app)."""
    text = _LIQUID.sub("", _KICKER.sub("", _FRONT_MATTER.sub("", text.replace("\r\n", "\n"))))
    text = re.sub(r"\A\s*# [^\n]*\n", "", text)
    out: list[str] = []
    for i, part in enumerate(re.split(r"(?m)^(?=## )", text)):
        head, _, body = part.partition("\n") if i or part.startswith("## ") else ("", "", part)
        if head:
            name = head[3:].strip()
            key = re.sub(r"\s*\(.*\)$", "", name).strip().lower()
            if key in _SITE_ONLY or body.strip() in ("", "...", "…"):
                continue
            head = "## " + _HEADINGS.get(key, name)
        out.append((head + "\n" if head else "") + body)

    def link(m: re.Match) -> str:
        url = m.group(1)
        if re.match(r"^(?:[a-z]+:|/|#)", url):
            return m.group(0)
        name = url.strip("./").removesuffix("/")
        name = name if name.endswith(".md") else name + ".md"
        return f"](/projects/{slug}/files/{name})" if name in FILE_LABELS else f"](/projects/{slug})"
    return _LINK.sub(link, "".join(out)).strip()


def _entry_text(values: dict) -> str:
    return "".join(f"{k}: {values.get(k) or ''}\n" for k in ("slug", *PROJECT_FORM_FIELDS))


def _project_form(request: Request, conn: sqlite3.Connection, *, p: dict | None, values: dict, error: str | None = None,
                  conflict: dict | None = None, code: int = 200):
    sha = datafiles.file_sha("projects")
    resp = render(request, conn, "pages/project_edit.html", p=p, values=values, sha=sha, error=error, conflict=conflict,
                  title=f"编辑课题 · {p['name']}" if p else "新建课题")
    resp.status_code = code
    return resp


@router.get("/projects/new")
def project_new(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    from datetime import date
    values = {"stage": "idea", "since": date.today().strftime("%Y-%m"), "direction": request.query_params.get("direction", "")}
    return _project_form(request, conn, p=None, values=values)


@router.post("/projects/new")
async def project_create(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    values = {k: str(form.get(k) or "") for k in ("slug", *PROJECT_FORM_FIELDS)}
    try:
        datafiles.add_project(values, _s(form, "sha") or None)
    except StaleFileError:
        return _project_form(request, conn, p=None, values=values, code=409,
                             error="projects.yml 在此页面打开后已被修改，本次没有写入；请确认后再提交一次。")
    except datafiles.DataFileError as e:
        return _project_form(request, conn, p=None, values=values, error=str(e), code=400)
    return RedirectResponse(f"/projects/{values['slug'].strip()}?saved=created", status_code=303)


@router.get("/projects/{slug}/edit")
def project_edit(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    return _project_form(request, conn, p=p, values={k: str(p.get(k) or "") for k in ("slug", *PROJECT_FORM_FIELDS)})


@router.post("/projects/{slug}/edit")
async def project_update(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    form = await request.form()
    changes = {k: str(form.get(k) or "") for k in PROJECT_FORM_FIELDS if k in form}
    values = {"slug": slug, **changes}
    try:
        datafiles.update_project(slug, changes, _s(form, "sha"))
    except StaleFileError:
        disk = datafiles.project(slug) or {}
        from ... import weekly
        diff = weekly.diff_text(_entry_text({k: str(disk.get(k) or "") for k in ("slug", *PROJECT_FORM_FIELDS)}),
                                _entry_text(values), "磁盘上的当前版本", "你的编辑")
        return _project_form(request, conn, p=disk or p, values=values, code=409, conflict={"diff": diff},
                             error="projects.yml 在此页面打开后已被修改，本次没有写入。下面是差异；你的输入仍保留在表单里，再次保存会以它为准。")
    except datafiles.DataFileError as e:
        return _project_form(request, conn, p=p, values=values, error=str(e), code=400)
    return RedirectResponse(f"/projects/{slug}?saved=project", status_code=303)


def _file_page(request: Request, conn: sqlite3.Connection, p: dict, name: str, *, text: str | None = None,
               error: str | None = None, conflict: dict | None = None, saved: str | None = None, code: int = 200):
    disk, sha = datafiles.read_project_file(p["slug"], name)
    resp = render(request, conn, "pages/project_file.html", p=p, name=name, label=FILE_LABELS[name], files=FILE_LABELS,
                  text=disk if text is None else text, mine=text, sha=sha, error=error, conflict=conflict, saved=saved,
                  from_editor=False, title=f"{p['name']} · {FILE_LABELS[name]}", **_file_conflict_vars(p["slug"], name))
    resp.status_code = code
    return resp


@router.get("/projects/{slug}/files/{name}")
def project_file(slug: str, name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    if name not in FILE_LABELS:
        raise HTTPException(404, f"不能在应用内编辑 {name}")
    return _file_page(request, conn, p, name, saved=request.query_params.get("saved"))


@router.get("/projects/{slug}/files/{name}/source")
def project_file_source(slug: str, name: str, conn: sqlite3.Connection = Depends(get_conn)):
    """The text and sha as they are on disk now (the editor's 载入磁盘版本)."""
    _project(slug)
    if name not in FILE_LABELS:
        raise HTTPException(404)
    text, sha = datafiles.read_project_file(slug, name)
    return Response(text, media_type="text/markdown; charset=utf-8", headers={"X-RL-Sha": sha})


@router.post("/projects/{slug}/files/{name}")
async def project_file_save(slug: str, name: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Same contract as the artifact body save: the editor script sends `X-RL-Editor: 1` and gets
    200 → status fragment + X-RL-Sha, 409 → conflict fragment; a plain form post is redirected."""
    p = _project(slug)
    if name not in FILE_LABELS:
        raise HTTPException(404, f"不能在应用内编辑 {name}")
    form = await request.form()
    text = str(form.get("text") or "").replace("\r\n", "\n")
    from_editor = request.headers.get("x-rl-editor") == "1"
    try:
        new_sha = datafiles.save_project_file(slug, name, text, _s(form, "sha"))
    except StaleFileError:
        from ... import weekly
        disk, current_sha = datafiles.read_project_file(slug, name)
        conflict = {"diff": weekly.diff_text(disk, text, "磁盘上的当前版本", "你的编辑"), "current_sha": current_sha}
        if from_editor:
            resp = render(request, conn, "partials/_editor_conflict.html", conflict=conflict, from_editor=True, mine=text,
                          **_file_conflict_vars(slug, name))
            resp.status_code = 409
            resp.headers["X-RL-Current-Sha"] = current_sha
            return resp
        return _file_page(request, conn, p, name, text=text, code=409, conflict=conflict,
                          error=f"{name} 在此页面打开后已被修改，本次没有写入。你的文本仍在下方；再次保存会覆盖磁盘版本。")
    if not from_editor:
        return RedirectResponse(f"/projects/{slug}/files/{name}?saved=1", status_code=303)
    stamp = datetime.now().strftime("%H:%M")
    return HTMLResponse(f"<span class='ok'>● 已保存 {stamp}</span>", headers={"X-RL-Sha": new_sha, "X-RL-Saved-At": stamp})


def _file_conflict_vars(slug: str, name: str) -> dict:
    return {"conflict_action": f"/projects/{slug}/files/{name}", "conflict_field": "text",
            "conflict_back": f"/projects/{slug}/files/{name}"}


def _authors(row: sqlite3.Row) -> str:
    try:
        names = json.loads(row["authors_json"] or "[]")
    except (TypeError, ValueError):
        names = []
    first = names[0] if names else "作者不详"
    if isinstance(first, dict):
        first = first.get("family") or first.get("name") or "作者不详"
    return f"{first} 等" if len(names) > 1 else str(first)


def _hub(conn: sqlite3.Connection, slug: str) -> dict:
    """Everything a 课题 collects: its papers, highlights, outputs, datasets and the three project files."""
    # Papers and datasets the 课题's issues name under their descriptions count as 间接关联 too (card 0099); read
    # only, paper_projects stays what Leo chose.
    from ...files import issues as issues_file
    named = issues_file.project_material_ids(slug)
    pids = sorted(set(mapgraph.project_paper_ids(conn, slug)) | {
        pid for pid in named['paper'] if conn.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone()})
    marks = ",".join("?" * len(pids))
    papers = []
    if pids:
        linked = {r[0] for r in conn.execute("SELECT paper_id FROM paper_projects WHERE project_slug = ?", (slug,))}
        for r in conn.execute(f"SELECT p.*, (SELECT COUNT(*) FROM excerpts e WHERE e.paper_id = p.id) AS n_excerpts, "
                              f"(SELECT COUNT(*) FROM paper_files f WHERE f.paper_id = p.id) AS n_files "
                              f"FROM papers p WHERE p.id IN ({marks}) ORDER BY p.year DESC, p.title", pids):
            papers.append({**dict(r), "authors": _authors(r), "linked": r["id"] in linked})
    excerpts = conn.execute(
        "SELECT e.id, e.quote, e.my_note, e.locator, e.status, e.quote_verified, e.created_at, e.paper_id, p.title AS paper_title, p.year "
        f"FROM excerpts e JOIN papers p ON p.id = e.paper_id WHERE e.project_slug = ?{' OR e.paper_id IN (' + marks + ')' if pids else ''} "
        "ORDER BY e.created_at DESC", (slug, *pids)).fetchall()
    dids = sorted(named['dataset'])
    clauses = ([f"paper_id IN ({marks})"] if pids else []) + ([f"id IN ({','.join('?' * len(dids))})"] if dids else [])
    datasets = conn.execute(f"SELECT id, name, rows, cols, paper_id, created_at FROM datasets WHERE {' OR '.join(clauses)} "
                            "ORDER BY created_at DESC", [*pids, *dids]).fetchall() if clauses else []
    candidates = conn.execute(f"SELECT id, title, year FROM papers{' WHERE id NOT IN (' + marks + ')' if pids else ''} "
                              "ORDER BY created_at DESC LIMIT 300", pids).fetchall()
    files = []
    for name, label in FILE_LABELS.items():
        try:
            text, _sha = datafiles.read_project_file(slug, name)
        except OSError:
            text = ""
        files.append({"name": name, "label": label.split(" ")[0], "text": text, "chars": len(text.strip()),
                      "readme": readme_text(text, slug) if name == "index.md" else "",
                      "generated": "<!-- rl:" in text or "导出" in text})
    return {"hub_papers": papers, "hub_excerpts": excerpts, "hub_datasets": datasets, "hub_candidates": candidates, "hub_files": files,
            "hub_deliverables": deliverables.list_files(slug)}


@router.post("/projects/{slug}/deliverables/open")
async def deliverable_open(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Open a draft / private file with the system's default application, or show it in Finder."""
    _project(slug)
    form = await request.form()
    try:
        path = deliverables.resolve(slug, _s(form, "rel"))
    except deliverables.DeliverableError as e:
        return _done(request, f"/projects/{slug}?tab=files", str(e))
    deliverables.open_with_system(path, reveal=_s(form, "how") == "reveal")
    return _done(request, f"/projects/{slug}?tab=files")


@router.post("/projects/{slug}/deliverables/upload")
async def deliverable_upload(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _project(slug)
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return _done(request, f"/projects/{slug}?tab=files", "请选择一个文件")
    try:
        deliverables.save_upload(slug, up.filename, await up.read())
    except deliverables.DeliverableError as e:
        return _done(request, f"/projects/{slug}?tab=files", str(e))
    return _done(request, f"/projects/{slug}?tab=files&saved=upload")


@router.post("/projects/{slug}/deliverables/export")
async def deliverable_export(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Export one of the 课题's outputs to drafts/<title>.docx and open it in Word."""
    from ... import weekly
    _project(slug)
    form = await request.form()
    ref = _s(form, "ref")
    a = weekly.load_ref(conn, ref) if ref else None
    if a is None or str(a.get("project") or "") != slug:
        return _done(request, f"/projects/{slug}?tab=files", "请选择这个课题的一份产出")
    try:
        path = deliverables.export_docx(a, slug)
    except deliverables.DeliverableError as e:
        return _done(request, f"/projects/{slug}?tab=files", str(e))
    deliverables.open_with_system(path)
    return _done(request, f"/projects/{slug}?tab=files&saved=export")


def _tasks_ctx(slug: str) -> dict:
    """The 课题's 待办 as the issue tree (all view, this 课题 only) plus one progress sentence per parent
    (docs/design/TASKS.md §C 课题页). Rendered by partials/_task_tree.html, so the names match /tasks."""
    current = week_of()
    rows = [t for t in tasks_file.read() if t.get("project") == slug]
    tree = tasks_file.tree(rows, "all", current, project_order=[slug])
    open_rows = [t for t in rows if tasks_file.is_open(t)]
    kids: dict[str, list[dict]] = {}
    for t in rows:
        if t.get("parent"):
            kids.setdefault(t["parent"], []).append(t)
    parents = [{"task": t, **progress.describe(t, kids.get(t["id"], []))}
               for t in open_rows if kids.get(t["id"]) or t.get("file") or t.get("words")]
    for block in tree:
        for n in block["items"] + block["closed"]:
            d = next((x for x in parents if x["task"]["id"] == n["task"]["id"]), None)
            if d:
                n["sentence"] = d["sentence"]
    return {"task_tree": tree, "task_parents": parents, "task_counts": {
                "open": len(open_rows), "week": sum(1 for t in open_rows if tasks_file.due_this_week(t))},
            "view": "all", "filter_project": slug, "next_week": week_add(current, 1), "sha": tasks_file.file_sha()}


@router.get("/projects/{slug}")
def project_page(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    artifacts.index(conn)
    ctx = _ladder_ctx(conn, slug)
    from ...ai import claude_code, queue
    # 全部生成概要 says how much of today's Claude Code allowance it would use (Claude Code mode only; by the
    # mode, not the route, so "0 left" still shows once the cap is reached or AI is paused)
    claude_left = (max(0, claude_code.daily_runs(conn) - claude_code.runs_today(conn))
                   if queue.mode(conn) == "claude" else None)
    return render(request, conn, "pages/project.html", p=p, claude_left=claude_left, **ctx, **_hub(conn, slug), **_tasks_ctx(slug),
                  counts=milestones.project_counts(conn, slug), camps=mapgraph.camps(conn, slug),
                  claims=conn.execute("SELECT * FROM claims WHERE project_slug = ? AND status != 'rejected' ORDER BY kind, code, id",
                                      (slug,)).fetchall(),
                  cases=conn.execute("SELECT * FROM cases WHERE project_slug = ? ORDER BY is_observation DESC, COALESCE(sort, 999), date_start",
                                     (slug,)).fetchall(),
                  concepts=concepts_mod.for_project(conn, slug), error=request.query_params.get("error"),
                  project_artifacts=conn.execute("SELECT * FROM artifacts WHERE project = ? ORDER BY week DESC, ref DESC",
                                                 (slug,)).fetchall(),
                  stage_index=_stage_index(p.get("stage")), tab=request.query_params.get("tab"),
                  saved=request.query_params.get("saved"), title=p["name"])


@router.post("/projects/{slug}/papers")
async def project_attach_paper(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Put a library paper into this 课题 (the same paper_projects link the import form and paper page write)."""
    _project(slug)
    form = await request.form()
    pid = _s(form, "paper_id")
    if not pid or not conn.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone():
        return _done(request, f"/projects/{slug}?tab=papers", "请选择一篇文献")
    conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (pid, slug))
    return _done(request, f"/projects/{slug}?tab=papers&saved=paper")


@router.post("/projects/{slug}/papers/{paper_id}/detach")
def project_detach_paper(slug: str, paper_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _project(slug)
    conn.execute("DELETE FROM paper_projects WHERE paper_id = ? AND project_slug = ?", (paper_id, slug))
    return _done(request, f"/projects/{slug}?tab=papers")


def _stage_index(stage: str | None) -> int:
    keys = [str(s["key"]) for s in (datafiles.read("stages") or [])]
    return keys.index(stage) if stage in keys else -1


@router.post("/projects/{slug}/confirm")
async def confirm(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        milestones.confirm(conn, slug, _s(form, "milestone"), _s(form, "item"), bool(form.get("checked")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return templates().TemplateResponse(request, "partials/_milestone_ladder.html", _ladder_ctx(conn, slug))


@router.post("/projects/{slug}/stage")
async def move_stage(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _project(slug)
    form = await request.form()
    stage = _s(form, "stage")
    if stage != milestones.stage_suggestion(conn, slug):
        return _done(request, f"/projects/{slug}", "这次阶段推进所需的把关里程碑尚未全部通过")
    datafiles.set_project_stage(slug, stage)
    milestones.evaluate(conn, slug)
    return _done(request, f"/projects/{slug}")


@router.post("/projects/{slug}/claims")
async def add_claim(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    statement, kind, code = _s(form, "statement"), _s(form, "kind") or "explanation", _s(form, "code") or None
    if not statement or kind not in ("explanation", "mechanism", "definition", "claim"):
        return _done(request, f"/projects/{slug}", "主张需要填写内容并选择类型")
    try:
        conn.execute("INSERT INTO claims(project_slug, code, kind, statement, is_own, created_at) VALUES (?,?,?,?,?,?)",
                     (slug, code, kind, statement, 1 if form.get("is_own") else 0, now_iso()))
    except sqlite3.IntegrityError:
        return _done(request, f"/projects/{slug}", f"编号 {code} 已被使用")
    if kind == "explanation":
        matrix.ensure_cells(conn, slug)
    return _done(request, f"/projects/{slug}")


@router.post("/projects/{slug}/claims/{claim_id}")
async def edit_claim(slug: str, claim_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    if _s(form, "statement"):
        conn.execute("UPDATE claims SET statement = ? WHERE id = ? AND project_slug = ?", (_s(form, "statement"), claim_id, slug))
    return _done(request, f"/projects/{slug}")


@router.post("/projects/{slug}/claims/{claim_id}/{action}")
def claim_action(slug: str, claim_id: int, action: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if action not in ("accept", "reject"):
        raise HTTPException(404)
    conn.execute("UPDATE claims SET status = ? WHERE id = ? AND project_slug = ?",
                 ("accepted" if action == "accept" else "rejected", claim_id, slug))
    return _done(request, f"/projects/{slug}")


@router.post("/projects/{slug}/cases")
async def add_case(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    label = _s(form, "label")
    if not label:
        return _done(request, f"/projects/{slug}", "案例需要一个名称")
    cid = _s(form, "id") or re.sub(r"[^A-Za-z0-9+]+", "-", label).strip("-")[:40]
    obs = 1 if form.get("is_observation") else 0
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM cases WHERE project_slug = ?", (slug,)).fetchone()[0]
    try:
        conn.execute("INSERT INTO cases(id, kind, project_slug, label, date_start, date_end, is_observation, selection_note, sort) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, "episode" if obs or _s(form, "kind") == "episode" else "forum", slug, label, _s(form, "date_start") or None,
                      _s(form, "date_end") or None, obs, _s(form, "selection_note") or None, sort))
    except sqlite3.IntegrityError:
        return _done(request, f"/projects/{slug}", f"案例 id {cid} 已存在")
    if obs:
        matrix.ensure_cells(conn, slug)
    return _done(request, f"/projects/{slug}")


@router.post("/projects/{slug}/camps")
async def add_camp(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    label = _s(form, "label")
    if not label:
        return _done(request, f"/projects/{slug}", "阵营需要一个名称")
    cid = concepts_mod.slugify(label)
    if conn.execute("SELECT 1 FROM camps WHERE id = ?", (cid,)).fetchone():
        cid = f"{slug}-{cid}"
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM camps WHERE project_slug = ?", (slug,)).fetchone()[0]
    try:
        conn.execute("INSERT INTO camps(id, project_slug, label, core_claim, sort) VALUES (?,?,?,?,?)",
                     (cid, slug, label, _s(form, "core_claim") or None, sort))
    except sqlite3.IntegrityError:
        return _done(request, f"/projects/{slug}", f"阵营 {label} 已存在")
    n = conn.execute("SELECT COUNT(*) FROM camps WHERE project_slug = ?", (slug,)).fetchone()[0]
    return _done(request, request.headers.get("hx-current-url", f"/projects/{slug}").split("?")[0],
                 "阵营超过 7 个：文献地图会读起来像一份清单" if n > 7 else None)


@router.post("/projects/{slug}/export/{what}")
def export(slug: str, what: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    artifacts.index(conn)
    if what == "literature":
        ref = exports.export_literature_map(conn, slug)
    elif what == "evidence":
        ref = exports.export_evidence(conn, slug)
    else:
        raise HTTPException(404)
    return _done(request, f"/artifacts/{ref}")


# ---- literature map ---------------------------------------------------------------------------

@router.get("/projects/{slug}/map")
def map_page(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    el = mapgraph.elements(conn, slug)
    ms = {m.id: m for m in milestones.evaluate(conn, slug)}
    target = 20
    return render(request, conn, "pages/map.html", p=p, slug=slug, el=el, camps=mapgraph.camps(conn, slug),
                  checks=mapgraph.map_checks(conn, slug), m3=ms["M3"], target=target,
                  options=mapgraph.node_options(conn, slug), error=request.query_params.get("error"), title="文献地图")


@router.get("/api/projects/{slug}/map.json")
def map_json(slug: str, conn: sqlite3.Connection = Depends(get_conn)):
    return JSONResponse(mapgraph.elements(conn, slug))


NODE_PANEL = """{% autoescape true %}
<div class="node-panel">
  <p class="kicker">{{ node_types.get(d.type, d.type) }}</p>
  <h3>{{ d.label }}</h3>
  {% if d.paper %}
    <p>{{ d.paper.title }}{% if d.paper.year %} ({{ d.paper.year }}){% endif %}</p>
    <p class="muted mono">{{ d.paper.metadata_status }}{% if d.camp %} · 阵营 {{ d.camp }}{% endif %}</p>
    {#- 我的判断 lives here, on the literature map, not in the reader (docs/design/PAPERS.md §B, card 0036):
        the fields place the paper on this map. The camp is this 课题's assignment (map/assign). -#}
    <form class="stack map-camp" hx-post="/projects/{{ slug }}/map/assign" hx-swap="none" hx-on::after-request="htmx.ajax('GET', '/projects/{{ slug }}/map/node?id=paper:{{ d.id }}', {target: this.closest('.node-panel'), swap: 'outerHTML'})">
      <input type="hidden" name="paper_id" value="{{ d.id }}">
      <label>阵营 <select name="camp_id" onchange="this.form.requestSubmit()"><option value="">未归阵营</option>{% for c in camps %}<option value="{{ c.id }}"{% if c.label == d.camp or c.id == d.camp %} selected{% endif %}>{{ c.label }}</option>{% endfor %}</select></label>
    </form>
    <form class="stack map-fields" hx-post="/projects/{{ slug }}/map/paper/{{ d.id }}" hx-target="closest .node-panel" hx-swap="outerHTML">
      {% if saved %}<p class="flash small">已保存。</p>{% endif %}{% if error %}<p class="crit small">{{ error }}</p>{% endif %}
      <label>与谜题的关系 <select name="relation_to_puzzle"><option value="">—</option>{% for r, name in relations.items() %}<option value="{{ r }}"{% if r == d.paper.relation_to_puzzle %} selected{% endif %}>{{ name }}</option>{% endfor %}</select></label>
      {% for key, label, kind in map_fields %}
      <label>{{ label }}{% if kind == 'long' %}<textarea name="{{ key }}" rows="2">{{ d.paper[key] or '' }}</textarea>{% else %}<input type="text" name="{{ key }}" value="{{ d.paper[key] or '' }}">{% endif %}</label>
      {% endfor %}
      <button class="btn small" type="submit">保存判断</button>
    </form>
    {% if d.excerpts %}<ul class="excerpt-list">{% for e in d.excerpts %}<li><a href="/excerpts/{{ e.id }}">{{ e.id }} · {{ e.locator }}</a><p class="q">{{ e.quote[:200] }}</p></li>{% endfor %}</ul>
    {% else %}<p class="muted">还没有片段。</p>{% endif %}
    <p><a class="btn small" href="/papers/{{ d.id }}">打开文献</a></p>
  {% elif d.camp_row %}
    <p>{{ d.camp_row.core_claim or '还没有写核心主张。' }}</p>
    <p class="muted">{{ d.members|length }} 篇文献</p>
  {% elif d.claim %}
    <p>{{ d.claim.statement }}</p><p class="muted mono">{{ L.claim_kind[d.claim.kind] }} · {{ d.claim.origin }} · {{ L.claim_status[d.claim.status] }}</p>
  {% elif d.case %}
    <p>{{ d.case.date_start or '' }} {{ d.case.selection_note or '' }}</p>
  {% endif %}
  {% if d.edges %}<ul class="edge-list">{% for e in d.edges %}<li class="{{ 'suggested' if e.status == 'proposed' else '' }}">{{ '→' if e.outgoing else '←' }} {{ rels.get(e.rel, e.rel|replace('_',' ')) }} {{ e.other }}{% if e.status == 'proposed' %} <span class="chip">建议</span>
    <button class="btn small" hx-post="/projects/{{ slug }}/map/edges/{{ e.id }}/accept">接受</button>
    <button class="btn small secondary" hx-post="/projects/{{ slug }}/map/edges/{{ e.id }}/reject">拒绝</button>{% endif %}</li>{% endfor %}</ul>{% endif %}
</div>{% endautoescape %}"""
NODE_TYPES = {"paper": "文献", "camp": "阵营", "claim": "主张", "case": "案例", "puzzle": "谜题"}
PAPER_FIELDS = {"claim_text": "核心主张", "mechanism": "机制", "cannot_explain": "解释不了什么", "relation_to_puzzle": "与谜题的关系"}
# 我的判断 on the map's paper card: (column, label, short | long), in the order Leo reads them
MAP_FIELDS = (("claim_text", "核心主张", "long"), ("mechanism", "机制", "long"), ("cannot_explain", "解释不了什么", "long"),
              ("concept_usage", "概念用法", "long"), ("method", "方法", "short"), ("period", "时段", "short"),
              ("level_of_analysis", "分析层次", "short"))
RELATION_NAMES = {"supports": "支持", "rivals": "竞争解释", "scope-condition": "适用条件", "irrelevant": "无关"}
RELS = {"advances": "提出", "belongs": "属于", "contests": "争论", "via": "经由", "studies": "研究", "uses": "使用",
        "fails_on": "解释不了", "bears_on": "关系到"}


@router.get("/projects/{slug}/map/node", response_class=HTMLResponse)
def map_node(slug: str, id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _node_panel(conn, slug, id)


def _node_panel(conn: sqlite3.Connection, slug: str, node_id: str, **extra: Any) -> HTMLResponse:
    d = mapgraph.node_detail(conn, slug, node_id)
    if not d:
        return HTMLResponse("<p class='muted'>找不到这个节点。</p>")
    camps = conn.execute("SELECT id, label FROM camps WHERE project_slug = ? ORDER BY label", (slug,)).fetchall()
    return HTMLResponse(templates().env.from_string(NODE_PANEL).render(
        d=d, slug=slug, L=_labels(), node_types=NODE_TYPES, paper_fields=PAPER_FIELDS, rels=RELS, camps=camps,
        map_fields=MAP_FIELDS, relations=RELATION_NAMES, **extra))


@router.post("/projects/{slug}/map/paper/{paper_id}", response_class=HTMLResponse)
async def map_paper_fields(slug: str, paper_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    """Save 我的判断 for one paper from its card on this 课题's literature map; the card comes back."""
    _project(slug)
    if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        raise HTTPException(404, "没有这篇文献")
    form = await request.form()
    keys = [k for k, _l, _t in MAP_FIELDS] + ["relation_to_puzzle"]
    try:
        pipeline.update_user_fields(conn, paper_id, {k: _s(form, k) for k in keys if k in form})
    except ValueError as e:
        return _node_panel(conn, slug, f"paper:{paper_id}", error=i18n.error_zh(str(e)))
    return _node_panel(conn, slug, f"paper:{paper_id}", saved=True)


@router.post("/projects/{slug}/map/assign")
async def map_assign(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        mapgraph.assign_camp(conn, slug, _s(form, "paper_id"), _s(form, "camp_id"))
    except ValueError as e:
        return _done(request, f"/projects/{slug}/map", str(e))
    return _done(request, f"/projects/{slug}/map")


@router.post("/projects/{slug}/map/contests")
async def map_contests(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        mapgraph.add_edge(conn, _s(form, "src"), _s(form, "dst"), "contests", reason=_s(form, "note") or None)
    except ValueError as e:
        return _done(request, f"/projects/{slug}/map", str(e))
    return _done(request, f"/projects/{slug}/map")


@router.post("/projects/{slug}/map/edges/{assoc_id}/{action}")
def map_edge(slug: str, assoc_id: int, action: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if action not in ("accept", "reject"):
        raise HTTPException(404)
    try:
        mapgraph.set_edge_status(conn, assoc_id, "accepted" if action == "accept" else "rejected")
    except ValueError as e:
        return _done(request, f"/projects/{slug}/map", str(e))
    return _done(request, f"/projects/{slug}/map")


# ---- evidence matrix ----------------------------------------------------------------------------

def _matrix_side(conn: sqlite3.Connection, slug: str) -> dict:
    return {"L": _labels(), "hunt": hunt.hunt_list(conn, slug), "coverage": matrix.coverage(conn, slug),
            "verdicts": matrix.verdicts(conn, slug), "slug": slug}


@router.get("/projects/{slug}/matrix")
def matrix_page(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    matrix.ensure_cells(conn, slug)
    return render(request, conn, "pages/matrix.html", p=p, g=matrix.grid(conn, slug), glyphs=matrix.GLYPHS,
                  test_labels=matrix.TEST_LABELS, **_matrix_side(conn, slug), title="证据矩阵")


@router.get("/projects/{slug}/hunt")
def hunt_partial(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates().TemplateResponse(request, "partials/_hunt_list.html", _matrix_side(conn, slug))


def _cell_form(request: Request, conn: sqlite3.Connection, slug: str, cell_id: int, error: str | None = None,
               saved: bool = False):
    c = matrix.cell(conn, cell_id)
    if not c or c["project_slug"] != slug:
        raise HTTPException(404)
    resp = templates().TemplateResponse(request, "partials/_cell_form.html", {
        "L": _labels(), "c": c, "slug": slug, "statuses": matrix.STATUSES, "glyphs": matrix.GLYPHS, "test_types": matrix.TEST_TYPES,
        "test_labels": matrix.TEST_LABELS, "error": error, "saved": saved})
    if saved:
        resp.headers["HX-Trigger"] = "matrix-changed"
    return resp


@router.get("/projects/{slug}/cells/{cell_id}")
def cell_get(slug: str, cell_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _cell_form(request, conn, slug, cell_id)


@router.get("/projects/{slug}/cells/{cell_id}/glyph")
def cell_glyph(slug: str, cell_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    c = matrix.cell(conn, cell_id)
    if not c:
        raise HTTPException(404)
    c["n_excerpts"] = len(c["excerpts"])
    return templates().TemplateResponse(request, "partials/_cell.html", {"L": _labels(), "c": c, "slug": slug, "test_labels": matrix.TEST_LABELS})


@router.post("/projects/{slug}/cells/{cell_id}")
async def cell_post(slug: str, cell_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        fields = {k: form.get(k) for k in ("implication", "test_type", "discriminating", "feasibility", "hint") if k in form}
        c = matrix.cell(conn, cell_id)
        if c and "implication" in fields and (fields["implication"] or "").strip() == (c["implication"] or "").strip():
            fields.pop("implication")
        if c and "hint" in fields and (fields["hint"] or "").strip() == (c["hint"] or "").strip():
            fields.pop("hint")
        matrix.update_cell(conn, cell_id, **fields)
        if _s(form, "status"):
            matrix.set_status(conn, cell_id, _s(form, "status"))
    except (matrix.CellRuleError, ValueError) as e:
        return _cell_form(request, conn, slug, cell_id, error=str(e))
    return _cell_form(request, conn, slug, cell_id, saved=True)


@router.post("/projects/{slug}/cells/{cell_id}/excerpts")
async def cell_attach(slug: str, cell_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        matrix.attach_excerpt(conn, cell_id, _s(form, "excerpt_id"))
    except matrix.CellRuleError as e:
        return _cell_form(request, conn, slug, cell_id, error=str(e))
    return _cell_form(request, conn, slug, cell_id, saved=True)


@router.post("/projects/{slug}/cells/{cell_id}/excerpts/{excerpt_id}/detach")
def cell_detach(slug: str, cell_id: int, excerpt_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        matrix.detach_excerpt(conn, cell_id, excerpt_id)
    except matrix.CellRuleError as e:
        return _cell_form(request, conn, slug, cell_id, error=str(e))
    return _cell_form(request, conn, slug, cell_id, saved=True)


@router.post("/projects/{slug}/cells/{cell_id}/search")
async def cell_search(slug: str, cell_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        matrix.log_search(conn, cell_id, _s(form, "where"), _s(form, "terms"), _s(form, "result_note"))
    except matrix.CellRuleError as e:
        return _cell_form(request, conn, slug, cell_id, error=str(e))
    return _cell_form(request, conn, slug, cell_id, saved=True)


# ---- checks -------------------------------------------------------------------------------------

def _check_card(request: Request, conn: sqlite3.Connection, slug: str, check_id: int, error: str | None = None):
    k = checks_mod.get(conn, check_id)
    if not k:
        return HTMLResponse("")
    return templates().TemplateResponse(request, "partials/_check_card.html", {
        "L": _labels(), "k": k, "slug": slug, "statuses": checks_mod.STATUSES, "status_labels": checks_mod.STATUS_LABELS, "error": error})


@router.get("/projects/{slug}/checks")
def checks_page(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    from ...ai import queue
    claims = conn.execute("SELECT * FROM claims WHERE project_slug = ? AND status = 'accepted' ORDER BY kind, code, id", (slug,)).fetchall()
    jobs = [j for j in queue.list_jobs(conn, kind="checks.derive", limit=20) if j["status"] in ("queued", "claimed")]
    return render(request, conn, "pages/checks.html", p=p, slug=slug, checks=checks_mod.list_checks(conn, slug), claims=claims,
                  jobs=jobs, statuses=checks_mod.STATUSES, status_labels=checks_mod.STATUS_LABELS,
                  error=request.query_params.get("error"), title="实证检验")


@router.post("/projects/{slug}/checks")
async def check_create(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        checks_mod.create(conn, slug, title=_s(form, "title"), prediction=_s(form, "prediction"),
                          claim_id=int(form["claim_id"]) if _s(form, "claim_id") else None,
                          data_needed=_s(form, "data_needed"), method=_s(form, "method"), would_weaken_if=_s(form, "would_weaken_if"))
    except checks_mod.CheckRuleError as e:
        return _done(request, f"/projects/{slug}/checks", str(e))
    return _done(request, f"/projects/{slug}/checks")


@router.post("/projects/{slug}/checks/derive")
async def check_derive(slug: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    target = _s(form, "target")
    if not target:
        return _done(request, f"/projects/{slug}/checks", "请选择一个主张来推导检验")
    if target.startswith("ART-"):
        artifacts.index(conn)
    try:
        job = checks_mod.derive(conn, target)
    except (LookupError, ValueError) as e:
        return _done(request, f"/projects/{slug}/checks", str(e))
    return _done(request, f"/projects/{slug}/checks", None if job else "这个主张已有一个进行中的推导任务")


@router.post("/projects/{slug}/checks/{check_id}")
async def check_update(slug: str, check_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    try:
        checks_mod.update(conn, check_id, **{k: form.get(k) for k in checks_mod.FIELDS if k in form})
        if _s(form, "status"):
            checks_mod.set_status(conn, check_id, _s(form, "status"))
    except checks_mod.CheckRuleError as e:
        return _check_card(request, conn, slug, check_id, error=str(e))
    return _check_card(request, conn, slug, check_id)


@router.post("/projects/{slug}/checks/{check_id}/{action}")
def check_action(slug: str, check_id: int, action: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        if action == "accept":
            checks_mod.accept(conn, check_id)
        elif action == "discard":
            checks_mod.discard(conn, check_id)
            return HTMLResponse("")
        elif action == "export":
            artifacts.index(conn)
            exports.export_check(conn, check_id)
        else:
            raise HTTPException(404)
    except (checks_mod.CheckRuleError, ValueError) as e:
        return _check_card(request, conn, slug, check_id, error=str(e))
    return _check_card(request, conn, slug, check_id)


# ---- concept ledger -----------------------------------------------------------------------------

@router.get("/projects/{slug}/concepts/{concept_id}")
def concept_page(slug: str, concept_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    p = _project(slug)
    c = concepts_mod.get(conn, concept_id)
    if not c:
        raise HTTPException(404, f"没有概念 {concept_id!r}")
    return render(request, conn, "pages/concept.html", p=p, slug=slug, c=c, usages=concepts_mod.usages(conn, concept_id),
                  flags=concepts_mod.flags(conn, concept_id), ladder=concepts_mod.LADDER, ladder_labels=concepts_mod.LADDER_LABELS,
                  error=request.query_params.get("error"), title=f"概念：{c['label']}")


@router.post("/projects/{slug}/concepts/{concept_id}/{action}")
async def concept_action(slug: str, concept_id: str, action: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    url = f"/projects/{slug}/concepts/{concept_id}"
    try:
        if action == "definition":
            concepts_mod.set_definition(conn, concept_id, _s(form, "text"), _s(form, "reason"), _s(form, "artifact_ref") or None)
        elif action == "dimension":
            concepts_mod.add_dimension(conn, concept_id, _s(form, "label"), _s(form, "description"))
        elif action == "dimension-edit":
            concepts_mod.update_dimension(conn, concept_id, _s(form, "key"), label=_s(form, "label"),
                                          description=_s(form, "description"),
                                          indicators=str(form.get("indicators") or "").splitlines())
        elif action == "dimension-remove":
            concepts_mod.remove_dimension(conn, concept_id, _s(form, "key"))
        elif action == "competing":
            concepts_mod.add_competing(conn, concept_id, _s(form, "a"), _s(form, "b"), _s(form, "note"))
        elif action == "competing-remove":
            concepts_mod.remove_competing(conn, concept_id, int(_s(form, "index") or -1))
        elif action == "ladder":
            concepts_mod.set_ladder(conn, concept_id, _s(form, "ladder"))
        elif action == "usage-dimension":
            concepts_mod.tag_usage_dimension(conn, concept_id, _s(form, "excerpt_id"), _s(form, "key"), bool(form.get("on")))
        else:
            raise HTTPException(404)
    except ValueError as e:
        return _done(request, url, str(e))
    return _done(request, url)
