"""Immutable source snapshots and draft revisions; only adopt() writes a research artifact.

The task context chooses the session. Models cannot choose another conversation's state or
accept their own draft. Citation checks establish provenance, not semantic entailment.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
from datetime import date
from urllib.parse import quote

from ..config import get_paths
from ..db.connection import now_iso
from ..files import artifacts, datafiles
from ..files.weeks import week_of
from ..files.yamlio import atomic_write
from .models import Checkpoint, Draft

KINDS = {'literature-map': '文献地图', 'case-timeline': '案例时间轴'}


class ResearchError(ValueError):
    pass


class StaleDraft(ResearchError):
    pass


def get_session(conn, session_id):
    return conn.execute('SELECT * FROM research_sessions WHERE id=?', (session_id,)).fetchone()


def attach(conn, task_id, skill, thread_id, prompt, parent_id=None):
    if skill not in KINDS:
        return
    parent = conn.execute('SELECT research_session_id, skill FROM agent_tasks WHERE id=?', (parent_id,)).fetchone()
    sid = parent['research_session_id'] if parent and parent['skill'] == skill else None
    if not sid:
        sid = 'RS-' + secrets.token_hex(8)
        conn.execute('INSERT INTO research_sessions(id,thread_id,kind,question,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                     (sid, thread_id, skill, prompt, now_iso(), now_iso()))
    conn.execute('UPDATE agent_tasks SET research_session_id=? WHERE id=?', (sid, task_id))


def session_for_task(conn, task_id):
    task = conn.execute('SELECT research_session_id,status FROM agent_tasks WHERE id=?', (task_id,)).fetchone()
    if not task or not task['research_session_id']:
        raise ResearchError('This tool requires a literature-map or case-timeline task.')
    if task['status'] in ('cancelled', 'failed'):
        raise ResearchError('This task has stopped; no further research writes are allowed.')
    return get_session(conn, task['research_session_id'])


def latest(conn, session_id):
    return conn.execute('SELECT * FROM research_drafts WHERE session_id=? ORDER BY revision DESC LIMIT 1',
                        (session_id,)).fetchone()


def save_checkpoint(conn, session_id, checkpoint):
    parsed = Checkpoint.model_validate(checkpoint)
    raw = parsed.model_dump_json()
    if len(raw) > 20000:
        raise ResearchError('Checkpoint exceeds 20000 characters; keep evidence in the source ledger.')
    conn.execute('UPDATE research_sessions SET checkpoint_json=?,updated_at=? WHERE id=?',
                 (raw, now_iso(), session_id))
    return parsed


def add_source(conn, session_id, *, title, url, kind, body, metadata=None):
    sha = hashlib.sha256(body.encode()).hexdigest()
    meta = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    ident = 'SRC-' + hashlib.sha256(f'{session_id}\0{url}\0{kind}\0{sha}\0{meta}'.encode()).hexdigest()[:24]
    conn.execute('INSERT OR IGNORE INTO research_sources VALUES(?,?,?,?,?,?,?,?,?)',
                 (ident, session_id, title, url, kind, body, sha, meta, now_iso()))
    return conn.execute('SELECT * FROM research_sources WHERE id=?', (ident,)).fetchone()


def citation_rows(conn, session_id, citations):
    out = []
    for item in citations:
        rid = item.read_id if hasattr(item, 'read_id') else item['read_id']
        row = conn.execute('SELECT r.*,s.title,s.url,s.kind,s.sha256,s.retrieved_at,s.metadata_json,s.body '
                           'FROM research_reads r JOIN research_sources s ON s.id=r.source_id '
                           'WHERE r.id=? AND s.session_id=?', (rid, session_id)).fetchone()
        if row is None:
            raise ResearchError('Citation must refer to a source passage actually read in this research session.')
        d = dict(row)
        d['supports'] = item.supports if hasattr(item, 'supports') else item['supports']
        d['passage'] = d.pop('body')[d['start_offset']:d['end_offset']]
        out.append(d)
    return out


def save_draft(conn, task_id, payload):
    session = session_for_task(conn, task_id)
    draft = Draft.model_validate(payload)
    citation_rows(conn, session['id'], draft.citations)
    if draft.status == 'review' and not draft.citations:
        raise ResearchError('A review draft needs at least one read source. Save a working draft when retrieval is blocked.')
    with_transaction = not conn.in_transaction
    if with_transaction:
        conn.execute('BEGIN IMMEDIATE')
    try:
        old = latest(conn, session['id'])
        revision = old['revision'] if old else 0
        if revision != draft.expected_revision:
            raise StaleDraft('A newer revision exists. Read the research state before saving again.')
        cp = save_checkpoint(conn, session['id'], draft.checkpoint.model_dump())
        ident = 'RD-' + secrets.token_hex(8)
        conn.execute('INSERT INTO research_drafts(id,session_id,task_id,revision,title,body,citations_json,checkpoint_json,status,created_at) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (ident, session['id'], task_id, revision + 1, draft.title.strip(), draft.body,
                      json.dumps([c.model_dump() for c in draft.citations], ensure_ascii=False), cp.model_dump_json(),
                      draft.status, now_iso()))
        if with_transaction:
            conn.execute('COMMIT')
    except Exception:
        if with_transaction:
            conn.execute('ROLLBACK')
        raise
    return conn.execute('SELECT * FROM research_drafts WHERE id=?', (ident,)).fetchone()


def state(conn, session_id, offset=0, limit=6000):
    session = get_session(conn, session_id)
    if not session:
        raise ResearchError('Research session not found.')
    d = latest(conn, session_id)
    value = {'question': session['question'], 'kind': session['kind'],
             'checkpoint': json.loads(session['checkpoint_json']),
             'revision': d['revision'] if d else 0,
             'draft': {'title': d['title'], 'body': d['body'], 'status': d['status'],
                       'citations': json.loads(d['citations_json'])} if d else None,
             'sources': [dict(r) for r in conn.execute(
                 'SELECT id,title,url,kind,sha256,retrieved_at FROM research_sources WHERE session_id=? ORDER BY retrieved_at,id', (session_id,))],
             'searches': [dict(r) for r in conn.execute(
                 'SELECT backend,query,cutoff,status,error,created_at FROM research_searches WHERE session_id=? ORDER BY id', (session_id,))]}
    raw = json.dumps(value, ensure_ascii=False, indent=1)
    offset = max(0, offset)
    end = min(offset + limit, len(raw))
    return {'state_slice': raw[offset:end], 'offset': offset, 'next_offset': end if end < len(raw) else None,
            'total_chars': len(raw), 'revision': d['revision'] if d else 0}


def prompt_context(conn, task_id):
    task = conn.execute('SELECT research_session_id FROM agent_tasks WHERE id=?', (task_id,)).fetchone()
    if not task or not task['research_session_id']:
        return ''
    session = get_session(conn, task['research_session_id'])
    d = latest(conn, session['id'])
    return ('\n\nThis is a persistent research workflow. Read research_state (continue by next_offset) before proceeding. '
            'Its sources, exclusions and draft are authoritative task state across engines. '
            'Save working progress before the tool budget is exhausted. A draft is not accepted until Leo uses the review page. '
            f'Kind: {session["kind"]}; current revision: {d["revision"] if d else 0}.')


def _contained_markdown(body):
    # AI notes must not create sibling Claim / earned sections or managed-block markers.
    body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
    body = re.sub(r'^ {0,3}(#{1,6})\s+', lambda m: '#' * min(6, len(m[1]) + 2) + ' ', body, flags=re.M)
    return body.strip().replace("<", "&lt;").replace(">", "&gt;")


def adopt(conn, draft_id, revision, *, title, body, project, direction):
    """Human-only endpoint. Creates a new artifact per adopted revision; never overwrites one.

    A durable acceptance intent reserves the ref/path before writing. A retry after a crash
    recognizes exactly that file and completes the database update without creating duplicates.
    """
    d = conn.execute('SELECT * FROM research_drafts WHERE id=?', (draft_id,)).fetchone()
    if d is None:
        raise ResearchError('Draft not found.')
    if d['status'] == 'accepted':
        return d['artifact_ref']
    if len(body.strip()) < 20 or len(body) > 50000 or not title.strip() or len(title) > 180:
        raise ResearchError('Title or body is empty or too long.')
    projects = datafiles.read('projects') or []
    directions = datafiles.read('directions') or []
    if project and project not in {str(p['slug']) for p in projects}:
        raise ResearchError('Unknown project.')
    if direction and direction not in {str(p['slug']) for p in directions}:
        raise ResearchError('Unknown direction.')
    inferred = next((str(p.get('direction') or '') for p in projects if str(p['slug']) == project), '')
    if inferred and direction and inferred != direction:
        raise ResearchError('The selected direction does not match the project.')
    direction = direction or inferred
    paths = get_paths()
    conn.execute('BEGIN IMMEDIATE')
    try:
        # Reload under the write lock: double clicks are idempotent.
        d = conn.execute('SELECT * FROM research_drafts WHERE id=?', (draft_id,)).fetchone()
        intent = conn.execute('SELECT * FROM research_adoptions WHERE draft_id=?', (draft_id,)).fetchone()
        if not intent:
            current = latest(conn, d['session_id'])
            if current['id'] != draft_id or d['revision'] != revision:
                raise StaleDraft('A newer draft exists. Review the latest revision before adopting.')
            if d['status'] != 'review':
                raise ResearchError('This is a working draft; request a source-cited review draft first.')
            if conn.execute("SELECT 1 FROM agent_tasks WHERE research_session_id=? AND status IN ('queued','running')",
                            (d['session_id'],)).fetchone():
                raise StaleDraft('Research is still running. Wait for completion before adopting.')
            sources = citation_rows(conn, d['session_id'], json.loads(d['citations_json']))
            if not sources:
                raise ResearchError('A review draft needs read sources.')
            session = get_session(conn, d['session_id'])
            a = artifacts.load(paths.templates / 'artifact.md')
            week = week_of()
            ref = artifacts.next_ref(paths, week)
            reserved = {r[0] for r in conn.execute('SELECT artifact_ref FROM research_adoptions')}
            while ref in reserved:
                ref = ref[:-2] + f'{int(ref[-2:]) + 1:02d}'
            a.front.update(ref=ref, title=title.strip(), week=week,
                           type='literature-map' if session['kind'] == 'literature-map' else 'case-map',
                           level='seed', project=project or None, direction=direction or None,
                           created=date.today().isoformat(), status='draft')
            a.front['research_session'] = session['id']
            a.front['research_revision'] = revision
            a.front['reviewed_at'] = now_iso()
            previous = conn.execute("SELECT artifact_ref FROM research_drafts WHERE session_id=? AND status='accepted' "
                                    'ORDER BY revision DESC LIMIT 1', (session['id'],)).fetchone()
            if previous:
                a.front['related'] = [previous[0]]
            def esc(s):
                return html.escape(str(s), quote=False).replace('[', r'\[').replace(']', r'\]').replace('\n', ' ')
            notes = ['本轮助手整理经研究者审阅采纳；个人判断与证据状态仍由研究者填写。', '',
                     f'[返回原对话](/chat/{session["thread_id"]}) · 第 {revision} 版', '', _contained_markdown(body),
                     '', '### 来源与核读范围', '']
            for i, s in enumerate(sources, 1):
                notes.append(f'{i}. [{esc(s["title"])}](<{quote(s["url"], safe="/:?&=%#@+;,$-_.!~*()")}>) · {esc(s["locator"])} · '
                             f'获取于 {s["retrieved_at"][:10]} · 内容校验值 {s["sha256"][:12]}\n'
                             f'   对应内容：{esc(s["supports"])}')
            cp = Checkpoint.model_validate_json(d['checkpoint_json'])
            for heading, items in [('排除与范围限制', cp.exclusions), ('仍待核查', cp.uncertainties), ('下一轮', cp.next_actions)]:
                if items:
                    notes += ['', '### ' + heading, '', *['- ' + esc(x) for x in items]]
            artifacts.set_ai_notes(a, '\n'.join(notes))
            rel = f'_artifacts/{week}-research-{draft_id[3:].lower()}.md'
            content = a.text()
            conn.execute('INSERT INTO research_adoptions VALUES(?,?,?,?,?,?,?)',
                         (draft_id, ref, rel, content, title.strip(), body, now_iso()))
            intent = conn.execute('SELECT * FROM research_adoptions WHERE draft_id=?', (draft_id,)).fetchone()
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    dest = paths.root / intent['relative_path']
    if dest.exists():
        if dest.read_text(encoding='utf-8') != intent['content']:
            raise StaleDraft('The reserved artifact was edited. It has not been overwritten.')
    else:
        atomic_write(dest, intent['content'], expected_sha='')
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute("UPDATE research_drafts SET status='accepted',artifact_ref=?,accepted_at=?,title=?,body=? WHERE id=?",
                     (intent['artifact_ref'], now_iso(), intent['reviewed_title'], intent['reviewed_body'], draft_id))
        artifacts.index(conn, paths)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    return intent['artifact_ref']
