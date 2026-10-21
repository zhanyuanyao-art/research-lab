"""Research review lives under the assistant; adoption is a user action, never an agent tool."""
from __future__ import annotations

import json
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from markdown_it import MarkdownIt
from markupsafe import Markup

from ...agent import tasks, drivers
from ...chat import service
from ...config import env
from ...research import store
from ..context import get_conn, render

router = APIRouter()
MD = MarkdownIt('commonmark', {'html': False, 'linkify': False}).enable('table')
SOURCE_LABELS = {'paper-text': '文献正文', 'paper-metadata': '书目或摘要', 'artifact': '研究笔记',
                 'literature-metadata': '书目或摘要', 'web-snippet': '搜索摘要', 'web-text': '网页正文', 'pdf-text': 'PDF正文'}


def _session(conn, sid):
    s = store.get_session(conn, sid)
    if s is None:
        raise HTTPException(404, '这次研究已不存在')
    return s


def _review(request, conn, sid, error=None, status=200, form=None):
    s = _session(conn, sid)
    d = store.latest(conn, sid)
    citations = store.citation_rows(conn, sid, json.loads(d['citations_json'])) if d else []
    response = render(request, conn, 'pages/research_review.html', title='审阅产出', session=s, draft=d,
                      kind_title=store.KINDS[s['kind']], citations=citations, source_labels=SOURCE_LABELS,
                      preview=Markup(MD.render((form or {}).get('body', d['body'] if d else ''))),
                      checkpoint=json.loads(d['checkpoint_json'] if d else s['checkpoint_json']),
                      searches=[dict(r) for r in conn.execute('SELECT * FROM research_searches WHERE session_id=? ORDER BY id DESC', (sid,))],
                      web_search_ready=bool(env('BRAVE_SEARCH_API_KEY')), error=error, form=form or {},
                      running=bool(conn.execute("SELECT 1 FROM agent_tasks WHERE research_session_id=? AND status IN ('queued','running')", (sid,)).fetchone()))
    response.status_code = status
    return response


@router.get('/chat/research/{session_id}/review')
def review(session_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return _review(request, conn, session_id)


@router.get('/chat/research/{session_id}/sources/{read_id}')
def passage(session_id: str, read_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    s = _session(conn, session_id)
    try:
        row = store.citation_rows(conn, session_id, [{'read_id': read_id, 'supports': ''}])[0]
    except store.ResearchError:
        raise HTTPException(404, '找不到这段来源') from None
    return render(request, conn, 'pages/research_source.html', title=row['title'], session=s, source=row,
                  source_label=SOURCE_LABELS.get(row['kind'], '来源'))


@router.post('/chat/research/{session_id}/adopt')
async def adopt(session_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    _session(conn, session_id)
    async with request.form(max_fields=12) as values:
        form = {str(k): str(v) for k,v in values.items()}
    ident = form.get('draft_id', '')
    d = conn.execute('SELECT * FROM research_drafts WHERE id=? AND session_id=?', (ident, session_id)).fetchone()
    if d is None:
        raise HTTPException(404, '找不到这版产出')
    try:
        ref = store.adopt(conn, ident, int(form.get('revision', '0')), title=form.get('title', ''), body=form.get('body', ''),
                          project=form.get('project', ''), direction=form.get('direction', ''))
    except (store.ResearchError, ValueError) as exc:
        message = ('内容已有更新或任务仍在进行，请重新核对最新版本。' if isinstance(exc, store.StaleDraft)
                   else '暂时无法采纳。请检查标题、正文、课题与方向；工作中的初稿需要先补齐来源并提交审阅。')
        return _review(request, conn, session_id, message, 409 if isinstance(exc, store.StaleDraft) else 400,
                       {"conflict_body": form.get("body", "")} if isinstance(exc, store.StaleDraft) else form)
    return RedirectResponse('/artifacts/' + ref, status_code=303)


@router.post('/chat/research/{session_id}/feedback')
async def feedback(session_id: str, request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    s = _session(conn, session_id)
    async with request.form(max_fields=4) as values:
        text = str(values.get('feedback') or '').strip()
    if not text or len(text) > 10000:
        return _review(request, conn, session_id, '请填写本轮需要修改或补查的内容。', 400)
    parent = conn.execute('SELECT * FROM agent_tasks WHERE research_session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (session_id,)).fetchone()
    if parent['status'] in tasks.OPEN:
        return _review(request, conn, session_id, '助手仍在处理这一轮，请完成后再提交修改意见。', 409)
    if not s['thread_id']:
        raise HTTPException(400, '这次研究没有关联对话')
    service.add_message(conn, s['thread_id'], 'user', text)
    m = service.add_message(conn, s['thread_id'], 'assistant', '', card_kind='task')
    tid = tasks.create(conn, text, thread_id=s['thread_id'], message_id=m.id, skill=s['kind'], parent_id=parent['id'])
    service.update_message(conn, m.id, card_ref=tid)
    if drivers.active_engine(conn):
        tasks.RUNNER.submit(tid)
    else:
        tasks._finish(conn, tid, 'waiting', report='修改意见已保存。请在设置中连接助手，然后回到本对话回复“继续”。')
    return RedirectResponse('/chat/' + s['thread_id'], status_code=303)
