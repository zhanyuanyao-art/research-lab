"""GET /search: one page of hits over papers, page text, issues, artifacts and excerpts, with a tab per kind
(docs/design/LIBRARY-UI.md 3.5, card 0133).

Two indexes per text: the unicode61/porter tables (English stemming) and the trigram tables
`pages_fts_cjk` / `excerpts_fts_cjk` (Chinese, Japanese and Korean without word segmentation).
Hits from both are merged per row, keeping the higher score. Trigram MATCH needs at least three
characters per term, so shorter terms (most two-character Chinese words) fall back to LIKE.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from markupsafe import Markup, escape

from ..context import get_conn, render

router = APIRouter()
log = logging.getLogger("research_lab.search")

HIT_OPEN, HIT_CLOSE = "\x02", "\x03"
PAGES_PER_PAPER = 5
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
LIKE_SCORE = 0.5          # rank for substring-only hits (no bm25 available)


def fts_query(q: str | None) -> str | None:
    """Words AND-ed as quoted FTS5 strings, the last one as a prefix, so user input never hits FTS syntax."""
    words = re.findall(r"\w[\w'’-]*", q or "", re.UNICODE)
    if not words:
        return None
    parts = ['"%s"' % w.replace('"', "") for w in words]
    parts[-1] += "*"
    return " ".join(parts)


def has_cjk(text: str | None) -> bool:
    return bool(CJK_RE.search(text or ""))


def cjk_terms(q: str | None) -> list[str]:
    """Whitespace-separated terms with quotes stripped; punctuation inside a Chinese phrase is kept."""
    return [t for t in (w.replace('"', "").strip() for w in (q or "").split()) if t]


def like_pattern(term: str) -> str:
    """A LIKE pattern matching `term` literally anywhere; pair it with ESCAPE '\\'."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def cjk_query(terms: list[str]) -> tuple[str | None, list[str]]:
    """(trigram MATCH string for terms of 3+ characters, LIKE patterns for shorter ones)."""
    long_terms = [t for t in terms if len(t) >= 3]
    match = " ".join('"%s"' % t for t in long_terms) or None
    likes = [like_pattern(t) for t in terms if len(t) < 3]
    return match, likes


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)?")
_MD_MARKUP = re.compile(r"<!--.*?(?:-->|$)|<[^>\x02\x03]+>|(?m:^\s*(?:[*+-]|\d+[.)]|>)\s+)|(?<!\S)#{1,6}\s+|(?<!\S)[*+-]\s+|\]\([^)]*\)?|^\S*/\S*\)", re.S)


def plain_text(raw: str) -> str:
    """Markdown and HTML reduced to their words; hit markers survive. Snippets cut text mid-syntax,
    so dangling `](…` and `…)` fragments go too."""
    text = _MD_MARKUP.sub(" ", _MD_LINK.sub(r"\1", re.sub(r"\*\*|__|(?<!\s)[*`~]|[*`~](?!\s)", "", raw)))
    return re.sub(r"\s+", " ", text).strip()


def snippet_html(raw: str | None) -> Markup:
    return Markup(str(escape(plain_text(raw or ""))).replace(HIT_OPEN, "<mark>").replace(HIT_CLOSE, "</mark>"))


def make_snippet(text: str, terms: list[str], width: int = 36) -> str:
    """Python-side snippet with hit markers, for rows found by LIKE (FTS snippet() needs a MATCH)."""
    text = re.sub(r"\s+", " ", text or "")
    lower = text.lower()
    pos = min((lower.find(t.lower()) for t in terms if t and lower.find(t.lower()) >= 0), default=-1)
    if pos < 0:
        return text[: width * 2] + ("…" if len(text) > width * 2 else "")
    lo, hi = max(0, pos - width), min(len(text), pos + width * 2)
    out = text[lo:hi]
    for t in sorted({t for t in terms if t}, key=len, reverse=True):
        out = re.sub(re.escape(t), lambda m: f"{HIT_OPEN}{m.group(0)}{HIT_CLOSE}", out, flags=re.I)
    return ("…" if lo else "") + out + ("…" if hi < len(text) else "")


def _rows(conn: sqlite3.Connection, sql: str, match: str) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, (HIT_OPEN, HIT_CLOSE, match)).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("search query failed: %s", e)
        return []


def _cjk_rows(conn: sqlite3.Connection, table: str, select: str, joins: str, text_col: str, terms: list[str],
              limit: int, weights: str = "") -> list[dict[str, Any]]:
    """Rows from a trigram table as dicts with `rid`, `score` and `snip` (hit-marked)."""
    match, likes = cjk_query(terms)
    if not match and not likes:
        return []
    where, args = [], []
    if match:
        where.append(f"{table} MATCH ?")
        args.append(match)
    for pat in likes:
        where.append(f"{table}.{text_col} LIKE ? ESCAPE '\\'")
        args.append(pat)
    rank = f"bm25({table}{weights})" if match else "0"
    sql = (f"SELECT {table}.rowid AS rid, {rank} AS rank, {table}.{text_col} AS body, {select} FROM {table} {joins} "
           f"WHERE {' AND '.join(where)} ORDER BY rank LIMIT ?")
    try:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    except sqlite3.OperationalError as e:
        log.warning("cjk search query failed: %s", e)
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["score"] = -float(r["rank"]) if match else LIKE_SCORE
        d["snip"] = make_snippet(r["body"] or "", terms)
        out.append(d)
    return out


def _merge(*groups: list[dict[str, Any]], key: str = "rid") -> list[dict[str, Any]]:
    """One entry per row, the higher score wins; sorted best first."""
    best: dict[Any, dict[str, Any]] = {}
    for group in groups:
        for d in group:
            cur = best.get(d[key])
            if cur is None or d["score"] > cur["score"]:
                best[d[key]] = d
    return sorted(best.values(), key=lambda d: d["score"], reverse=True)


def _scored(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r, score=-float(r["rank"])) for r in rows]


def search_all(conn: sqlite3.Connection, q: str, *, pages_per_paper: int = PAGES_PER_PAPER) -> dict[str, Any]:
    match = fts_query(q)
    terms = cjk_terms(q)
    if not match and not terms:
        return {"projects": [], "papers": [], "excerpts": [], "artifacts": [], "pages": [], "total": 0}
    try:
        from ...files import artifacts
        artifacts.index(conn)
    except Exception:  # noqa: BLE001 - a malformed artifact must not break search
        log.exception("artifact index refresh failed")

    uni_papers = _scored(_rows(conn,
        "SELECT p.rowid AS rid, p.id, p.title, p.year, p.authors_json, p.metadata_status, "
        "snippet(papers_fts, -1, ?, ?, '…', 16) AS snip, bm25(papers_fts, 2.0, 1.0, 1.0) AS rank "
        "FROM papers_fts JOIN papers p ON p.rowid = papers_fts.rowid WHERE papers_fts MATCH ? "
        "ORDER BY rank LIMIT 20", match)) if match else []
    cjk_papers: list[dict[str, Any]] = []
    if has_cjk(q):
        where = " AND ".join("(p.title LIKE ? ESCAPE '\\' OR p.abstract LIKE ? ESCAPE '\\')" for _ in terms)
        args = [a for t in terms for a in (like_pattern(t), like_pattern(t))]
        for r in conn.execute(f"SELECT p.rowid AS rid, p.id, p.title, p.year, p.authors_json, p.metadata_status, p.abstract "
                              f"FROM papers p WHERE {where} LIMIT 20", args):
            cjk_papers.append(dict(r, score=LIKE_SCORE + 1.0, snip=make_snippet(f"{r['title']} {r['abstract'] or ''}", terms)))
    papers = [dict(d, snippet=snippet_html(d["snip"]), authors=json.loads(d["authors_json"] or "[]"))
              for d in _merge(uni_papers, cjk_papers)[:20]]

    uni_exc = _scored(_rows(conn,
        "SELECT e.rowid AS rid, e.id, e.paper_id, e.locator, e.private, p.title AS paper_title, "
        "snippet(excerpts_fts, -1, ?, ?, '…', 20) AS snip, bm25(excerpts_fts, 1.0, 0.6) AS rank "
        "FROM excerpts_fts JOIN excerpts e ON e.rowid = excerpts_fts.rowid LEFT JOIN papers p ON p.id = e.paper_id "
        "WHERE excerpts_fts MATCH ? ORDER BY rank LIMIT 20", match)) if match else []
    cjk_exc = _cjk_rows(conn, "excerpts_fts_cjk",
                        "e.id, e.paper_id, e.locator, e.private, p.title AS paper_title",
                        "JOIN excerpts e ON e.rowid = excerpts_fts_cjk.rowid LEFT JOIN papers p ON p.id = e.paper_id",
                        "quote", terms, 20, ", 1.0, 0.6")
    excerpts = [dict(d, snippet=snippet_html(d["snip"])) for d in _merge(uni_exc, cjk_exc)[:20]]

    uni_arts = _scored(_rows(conn,
        "SELECT a.ref AS rid, a.ref, a.title, a.week, a.type, a.level, a.project, snippet(artifacts_fts, -1, ?, ?, '…', 16) AS snip, "
        "bm25(artifacts_fts, 2.0, 0.8) AS rank "
        "FROM artifacts_fts JOIN artifacts a ON a.rowid = artifacts_fts.rowid WHERE artifacts_fts MATCH ? "
        "ORDER BY rank LIMIT 20", match)) if match else []
    # unicode61 keeps a run of Chinese as one token, so 信号 only matches text that starts a run with it;
    # artifacts are few enough to scan with LIKE (no trigram table for them).
    cjk_arts: list[dict[str, Any]] = []
    if has_cjk(q):
        where = " AND ".join("(a.title LIKE ? ESCAPE '\\' OR a.body LIKE ? ESCAPE '\\')" for _ in terms)
        args = [a for t in terms for a in (like_pattern(t), like_pattern(t))]
        for r in conn.execute(f"SELECT a.ref AS rid, a.ref, a.title, a.week, a.type, a.level, a.project, a.body FROM artifacts a "
                              f"WHERE {where} LIMIT 20", args):
            title_hit = all(t.lower() in (r["title"] or "").lower() for t in terms)
            cjk_arts.append(dict(r, score=LIKE_SCORE + (1.0 if title_hit else 0.0), snip=make_snippet(plain_text(r["body"] or r["title"] or ""), terms)))
    arts = [dict(d, snippet=snippet_html(d["snip"])) for d in _merge(uni_arts, cjk_arts)[:20]]

    from ...files import datafiles
    projects = []
    for p in datafiles.read("projects") or []:
        hay = f"{p.get('name') or ''} {p.get('question') or ''} {p.get('note') or ''}"
        if terms and all(t.lower() in hay.lower() for t in terms):
            projects.append({"slug": p.get("slug"), "name": p.get("name") or p.get("slug"),
                             "snippet": snippet_html(make_snippet(f"{p.get('question') or ''} {p.get('note') or ''}".strip(), terms))})

    uni_pages = _scored(_rows(conn,
        "SELECT pp.rowid AS rid, pp.page_index, pp.page_label, f.paper_id, p.title, p.year, "
        "snippet(pages_fts, 0, ?, ?, '…', 20) AS snip, bm25(pages_fts) AS rank "
        "FROM pages_fts JOIN paper_pages pp ON pp.rowid = pages_fts.rowid JOIN paper_files f ON f.id = pp.file_id "
        "JOIN papers p ON p.id = f.paper_id WHERE pages_fts MATCH ? ORDER BY rank LIMIT 80", match)) if match else []
    cjk_pages = _cjk_rows(conn, "pages_fts_cjk", "pp.page_index, pp.page_label, f.paper_id, p.title, p.year",
                          "JOIN paper_pages pp ON pp.rowid = pages_fts_cjk.rowid JOIN paper_files f ON f.id = pp.file_id "
                          "JOIN papers p ON p.id = f.paper_id", "text", terms, 80)
    grouped: dict[str, dict[str, Any]] = {}
    for r in _merge(uni_pages, cjk_pages):
        g = grouped.setdefault(r["paper_id"], {"paper_id": r["paper_id"], "title": r["title"], "year": r["year"], "hits": [],
                                               "count": 0})
        g["count"] += 1
        if len(g["hits"]) < pages_per_paper:
            g["hits"].append({"page_index": r["page_index"], "page_label": r["page_label"], "snippet": snippet_html(r["snip"])})
    pages = list(grouped.values())
    total = len(projects) + len(papers) + len(excerpts) + len(arts) + sum(len(g["hits"]) for g in pages)
    return {"projects": projects, "papers": papers, "excerpts": excerpts, "artifacts": arts, "pages": pages, "total": total}


# ---------------------------------------------------------------- the /search page

TABS = (("all", "全部"), ("papers", "文献"), ("pages", "正文"), ("issues", "议题"), ("artifacts", "产出"), ("excerpts", "划线"))
ALL_LIMIT = 5             # rows per kind on the 全部 tab; each kind's own tab shows them all
PAGE_HITS_SHOWN = 3       # page hits under one paper; the rest are counted (「还有 2 处」)


def paper_lines(conn: sqlite3.Connection, ids: list[str], project_names: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Each paper as partials/_paper_line.html shows it in the library (card 0128): the same columns as the list's
    query, through the same routes/papers._line(), so the rows line up with the 文献 list."""
    from .papers import SUMMARY_MODE_SQL, _line
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT p.id, p.title, p.authors_json, p.year, p.venue, p.kind, p.doi, p.language, p.direction, p.read_depth, "
        f"p.created_at, p.last_opened_at, {SUMMARY_MODE_SQL} AS summary_mode, "
        "(SELECT COUNT(*) FROM paper_files f WHERE f.paper_id = p.id) AS n_files, "
        "(SELECT GROUP_CONCAT(pp.project_slug) FROM paper_projects pp WHERE pp.paper_id = p.id) AS project_slugs "
        f"FROM papers p WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: _line(r, project_names) for r in rows}


def issue_hits(q: str, limit: int = 50) -> list[dict[str, Any]]:
    """Tasks and 课题 whose number, title or description holds every term; the number is shown with each."""
    from ...files import datafiles, issues
    terms = [t.lower() for t in cjk_terms(q)]
    if not terms:
        return []
    projects = {str(p.get("slug")): p for p in datafiles.read("projects") or []}
    rows = issues.project_roots() + issues.read()
    out = []
    for r in rows:
        # outputs are issues too in the unified view, but they have their own 产出 tab: count them once
        if r.get("archived") or r.get("artifact"):
            continue
        extra = ""
        if r.get("project_root"):
            p = projects.get(r.get("project") or "", {})
            extra = f"{p.get('question') or ''} {p.get('note') or ''}"
        hay = f"{r.get('key') or ''} {r.get('title') or ''} {r.get('description') or ''} {extra}".lower()
        if all(t in hay for t in terms):
            body = f"{r.get('description') or ''} {extra}".strip()
            title_hit = all(t in str(r.get("title") or "").lower() or t in str(r.get("key") or "").lower() for t in terms)
            out.append({"ref": r["id"], "key": r.get("key") or "", "title": r.get("title") or "", "status": r.get("status") or "todo",
                        "url": issues.key_url(r["id"], r.get("key") or ""), "project": r.get("project") or "",
                        "project_root": bool(r.get("project_root")), "due": r.get("due") or "",
                        "snippet": snippet_html(make_snippet(plain_text(body), cjk_terms(q))) if body and not title_hit else "",
                        "score": 2 if title_hit else 1})
    out.sort(key=lambda d: (-d["score"], not d["project_root"], d["key"] or "~"))
    return out[:limit]


def page_results(conn: sqlite3.Connection, q: str, project_names: dict[str, str]) -> dict[str, Any]:
    """Everything the page shows for `q`: search_all's hits with their library rows, the issues, and a count per tab."""
    r = search_all(conn, q, pages_per_paper=PAGE_HITS_SHOWN)
    lines = paper_lines(conn, [p["id"] for p in r["papers"]] + [g["paper_id"] for g in r["pages"]], project_names)
    r["papers"] = [dict(p, line=lines[p["id"]]) for p in r["papers"] if p["id"] in lines]
    r["pages"] = [dict(g, line=lines[g["paper_id"]], more=max(0, g["count"] - len(g["hits"])))
                  for g in r["pages"] if g["paper_id"] in lines]
    r["issues"] = issue_hits(q)
    page_hits = sum(g["count"] for g in r["pages"])
    r["counts"] = {"papers": len(r["papers"]), "pages": page_hits, "page_papers": len(r["pages"]), "issues": len(r["issues"]),
                   "artifacts": len(r["artifacts"]), "excerpts": len(r["excerpts"])}
    r["counts"]["all"] = sum(v for k, v in r["counts"].items() if k != "page_papers")
    return r


@router.get("/search/quick")
def quick_search(request: Request, q: str = "", conn=Depends(get_conn)):
    """Bounded jump results; typing must not refresh the artifact index or call a model."""
    from urllib.parse import quote, urlencode
    from ...files import issues
    from ..context import templates
    q = q.strip()[:200]
    papers, issue_rows, pages = [], [], []
    terms, match = cjk_terms(q), fts_query(q)
    if terms:
        where = " AND ".join("title LIKE ? ESCAPE '\\'" for _ in terms)
        papers = [dict(r) for r in conn.execute(
            f"SELECT id,title,year FROM papers WHERE {where} ORDER BY updated_at DESC,id LIMIT 5",
            [like_pattern(t) for t in terms])]
        issue_rows = issue_hits(q, limit=5)
        ref = issues.resolve(q) if issues.normal_key(q) else None
        if ref:
            row = issues.get(ref)
            if row and not row.get('archived'):
                issue_rows = [{'ref': ref, 'key': row.get('key'), 'title': row['title'],
                               'url': issues.url(ref), 'project_root': row.get('project_root')}] + [
                                   r for r in issue_rows if r['ref'] != ref]
                issue_rows = issue_rows[:5]
        sql = ("SELECT pp.rowid AS rid, pp.page_index, pp.page_label, f.paper_id, p.title, "
               "snippet(pages_fts,0,?,?,'…',14) AS snip,bm25(pages_fts) AS rank "
               "FROM pages_fts JOIN paper_pages pp ON pp.rowid=pages_fts.rowid "
               "JOIN paper_files f ON f.id=pp.file_id JOIN papers p ON p.id=f.paper_id "
               "WHERE pages_fts MATCH ? AND f.is_primary=1 ORDER BY rank LIMIT 5")
        uni = _scored(_rows(conn, sql, match)) if match else []
        cjk = _cjk_rows(conn, 'pages_fts_cjk', 'pp.page_index, pp.page_label, f.paper_id, p.title',
                        'JOIN paper_pages pp ON pp.rowid=pages_fts_cjk.rowid JOIN paper_files f ON f.id=pp.file_id '
                        'AND f.is_primary=1 JOIN papers p ON p.id=f.paper_id', 'text', terms, 5) if has_cjk(q) else []
        for r in _merge(uni, cjk)[:5]:
            pages.append({'title': r['title'], 'detail': f"p. {r['page_label']} · {plain_text(r['snip']).replace(HIT_OPEN, '').replace(HIT_CLOSE, '')}",
                          'url': '/papers/' + quote(r['paper_id'], safe='') + '/read?' +
                                 urlencode({'page': r['page_label'], 'find': q}) + '#p-' + str(r['page_index'])})
    groups = [
        ('文献', [{'title': r['title'], 'detail': str(r['year'] or ''), 'url': '/papers/' + quote(r['id'], safe='')} for r in papers]),
        ('课题与议题', [{'title': r['title'], 'detail': r.get('key') or ('课题' if r.get('project_root') else ''), 'url': r['url']} for r in issue_rows]),
        ('全文', pages),
        ('操作', [{'title': 'View all results', 'detail': '⌘↵', 'url': '/search?' + urlencode({'q': q})},
                  {'title': 'New issue', 'url': '/issues/new'}, {'title': 'Import PDF', 'action': 'import'},
                  {'title': 'Coach', 'url': '/guide'}]),
    ]
    return templates().TemplateResponse(request, 'partials/_palette.html',
        {'request': request, 'palette_groups': groups, 'palette_empty': bool(q and not (papers or issue_rows or pages))})


@router.get("/search")
def search(request: Request, q: str = "", tab: str = "all", conn: sqlite3.Connection = Depends(get_conn)):
    from ...files import datafiles, issues
    # An issue key typed into ⌘K (APUS-0002, apus-2, or a key from before a change of direction) opens the issue.
    ref = issues.resolve(q) if issues.normal_key(q) else None
    if ref:
        return RedirectResponse(issues.url(ref) if ref.startswith("PRJ-") else issues.key_url(ref), status_code=303)
    from .papers import READ_STATUS_LABELS, SUMMARY_MODE_NAMES, label_context
    names = {str(p.get("slug")): str(p.get("name") or p.get("slug")) for p in datafiles.read("projects") or []}
    results = page_results(conn, q, names) if q.strip() else None
    tab = tab if tab in dict(TABS) else "all"
    # partials/_paper_line.html reads status_labels, summary_mode_names and direction_names; results mix directions,
    # so each row shows its 方向 mark (show_dir)
    return render(request, conn, "pages/search.html", q=q, results=results, project_names=names, tab=tab, tabs=TABS,
                  limit=ALL_LIMIT if tab == "all" else None, title=f"搜索 · {q}" if q else "搜索",
                  status_labels=READ_STATUS_LABELS, summary_mode_names=SUMMARY_MODE_NAMES, show_dir=True,
                  direction_names=label_context()["direction_names"])
