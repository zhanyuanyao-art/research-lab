"""Research workflows: source provenance, portable state, and explicit versioned adoption."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from research_lab.agent import skills, tasks, tools
from research_lab.chat import service, router
from research_lab.research import retrieval, store, net
from research_lab.files import artifacts
from research_lab.db.connection import connect


def session(conn, kind='literature-map'):
    thread = service.create_thread(conn)
    mid = service.add_message(conn, thread, 'assistant', '', card_kind='task')
    tid = tasks.create(conn, '建立特定议题的文献地图', thread_id=thread, message_id=mid.id, skill=kind)
    service.update_message(conn, mid.id, card_ref=tid)
    sid = tasks.get(conn, tid)['research_session_id']
    return tid, sid, thread


def evidence(conn, sid, body='A documented statement about an event. ' * 80):
    s = store.add_source(conn, sid, title='A primary source', url='https://example.org/report', kind='web-text', body=body)
    return retrieval.read(conn, sid, s['id'])


def payload(read, revision=0, status='review'):
    return {'title': '待审阅的研究产出', 'body': '这是基于已核读材料的研究初稿。关键结论仍需要由研究者结合来源复核。',
            'citations': [{'read_id': read['read_id'], 'supports': '这段材料支持初稿中描述的行动，但不能证明其动机。'}],
            'checkpoint': {'scope': '限定在当前材料与截止日', 'exclusions': ['不能把CERF项目算作中国资助'],
                           'uncertainties': ['执行日期仍待核查'], 'next_actions': ['补查原始许可函']},
            'expected_revision': revision, 'status': status}


def review_draft(conn):
    tid, sid, thread = session(conn)
    d = store.save_draft(conn, tid, payload(evidence(conn, sid)))
    conn.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (tid,))
    return tid, sid, thread, d


def adopt(conn, d, **kw):
    return store.adopt(conn, d['id'], d['revision'], title=d['title'], body=kw.get('body', d['body']), project='', direction='')


def test_research_routes_take_priority_and_are_shared():
    assert skills.pick('搜索美伊文献，做一份文献地图并比较研究设计').name == 'literature-map'
    assert router.match('搜索美伊文献，做一份文献地图') is None
    assert skills.pick('整理救灾案例时间轴').name == 'case-timeline'
    assert router.match('/search disaster') is not None
    for name in ('literature-map', 'case-timeline'):
        assert set(skills.get(name).tools) <= set(tools.TOOLS)
        assert all('adopt' not in n for n in skills.get(name).tools)


def test_offline_chat_retains_research_session_and_follow_up(conn):
    first = router.route(conn, '建立美伊文献地图')
    tid = first[0].thread_id
    task = tasks.latest_in_thread(conn, tid)
    assert task['status'] == 'waiting' and task['research_session_id']
    router.route(conn, '继续', thread_id=tid)
    follow = tasks.latest_in_thread(conn, tid)
    assert follow['skill'] == 'literature-map'
    assert follow['research_session_id'] == task['research_session_id']


def test_state_survives_new_task_and_paginates(conn):
    tid, sid, thread = session(conn)
    text = 'first page. ' * 1000 + 'IMPORTANT_COUNTEREXAMPLE'
    read = evidence(conn, sid, text)
    d = store.save_draft(conn, tid, payload(read))
    follow = tasks.create(conn, '继续', thread_id=thread, parent_id=tid)
    assert tasks.get(conn, follow)['research_session_id'] == sid
    assert 'research_state' in store.prompt_context(conn, follow)
    state = store.state(conn, sid)
    assert 'CERF' in state['state_slice'] and state['revision'] == 1
    next_read = retrieval.read(conn, sid, read['source_id'], offset=10000)
    assert 'IMPORTANT_COUNTEREXAMPLE' in next_read['text']
    assert next_read['read_id'] != read['read_id']


def test_draft_never_writes_archive_before_human_adoption(conn, repo):
    before = set((repo / '_artifacts').glob('*.md'))
    tid, sid, thread, d = review_draft(conn)
    assert set((repo / '_artifacts').glob('*.md')) == before
    assert d['status'] == 'review' and d['artifact_ref'] is None
    assert 'adopt' not in tools.TOOLS


def test_read_citations_are_scoped_and_not_invented(conn):
    tid, sid, _ = session(conn)
    tid2, sid2, _ = session(conn)
    foreign = evidence(conn, sid2)
    with pytest.raises(store.ResearchError, match='actually read'):
        store.save_draft(conn, tid, payload(foreign))
    invented = {'read_id': 'READ-invented'}
    with pytest.raises(store.ResearchError, match='actually read'):
        store.save_draft(conn, tid, payload(invented))
    no_source = payload(invented)
    no_source['citations'] = []
    with pytest.raises(store.ResearchError, match='at least one'):
        store.save_draft(conn, tid, no_source)
    no_source['status'] = 'working'
    assert store.save_draft(conn, tid, no_source)['status'] == 'working'


def test_stale_revision_and_running_task_cannot_be_adopted(conn):
    tid, sid, _ = session(conn)
    read = evidence(conn, sid)
    d = store.save_draft(conn, tid, payload(read))
    with pytest.raises(store.StaleDraft, match='still running'):
        adopt(conn, d)
    with pytest.raises(store.StaleDraft, match='newer revision'):
        store.save_draft(conn, tid, payload(read))
    store.save_draft(conn, tid, payload(read, revision=1))
    conn.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (tid,))
    with pytest.raises(store.StaleDraft, match='newer draft'):
        adopt(conn, d)


def test_adopt_is_idempotent_and_protects_earned_sections(conn, repo):
    tid, sid, thread, d = review_draft(conn)
    body = '## Claim\nAn AI statement must stay in AI notes.\n<script>alert(1)</script>\n\n## What did I figure out\nDo not fill this section.'
    ref = adopt(conn, d, body=body)
    assert adopt(conn, d) == ref
    row = conn.execute('SELECT * FROM artifacts WHERE ref=?', (ref,)).fetchone()
    a = artifacts.load(repo / row['path'])
    assert a.get('type') == 'literature-map' and a.get('level') == 'seed'
    assert artifacts.missing_earned(a.body) == ['What did I figure out', 'Claim']
    assert '<script>' not in a.body and '#### Claim' in a.body
    assert '/chat/' + thread in a.body
    assert conn.execute('SELECT count(*) FROM research_adoptions').fetchone()[0] == 1


def test_adoption_retry_recovers_file_without_duplicate(conn, repo, monkeypatch):
    tid, sid, thread, d = review_draft(conn)
    before = set((repo / '_artifacts').glob('*.md'))
    original = artifacts.index
    monkeypatch.setattr(artifacts, 'index', lambda *args, **kw: (_ for _ in ()).throw(RuntimeError('after file write')))
    with pytest.raises(RuntimeError):
        adopt(conn, d)
    pending = conn.execute('SELECT * FROM research_adoptions').fetchone()
    assert (repo / pending['relative_path']).exists()
    monkeypatch.setattr(artifacts, 'index', original)
    assert adopt(conn, d) == pending['artifact_ref']
    assert set((repo / '_artifacts').glob('*.md')) - before == {repo / pending['relative_path']}


def test_crossref_search_does_not_claim_fulltext(conn, monkeypatch):
    tid, sid, _ = session(conn)
    requests = []
    def response(req):
        requests.append(req)
        return httpx.Response(200, json={'message': {'items': [{'DOI': '10.1234/test', 'title': ['A real test record'], 'abstract': '<p>Abstract only</p>'}]}})
    monkeypatch.setattr(retrieval, 'TRANSPORT', httpx.MockTransport(response))
    r = retrieval.search(conn, sid, 'literature', 'assurance dilemma', cutoff='2026-09-24')
    assert r['status'] == 'ok' and 'metadata/abstract only' in r['results'][0]['reading']
    assert requests[0].url.params['filter'] == 'until-pub-date:2026-09-24'
    read = retrieval.read(conn, sid, r['results'][0]['source'])
    assert read['kind'] == 'literature-metadata' and read['available_pages'] == 0
    monkeypatch.delenv('BRAVE_SEARCH_API_KEY', raising=False)
    r = retrieval.search(conn, sid, 'web', 'Nepal 2015 clearance')
    assert r['status'] == 'blocked'
    assert conn.execute('SELECT status FROM research_searches ORDER BY id DESC LIMIT 1').fetchone()[0] == 'blocked'


def test_search_failure_logged_and_not_empty(conn, monkeypatch):
    _, sid, _ = session(conn)
    monkeypatch.setattr(retrieval, 'TRANSPORT', httpx.MockTransport(lambda req: httpx.Response(429)))
    r = retrieval.search(conn, sid, 'literature', 'disaster diplomacy')
    assert r['status'] == 'error' and '429' in r['error']


def test_public_fetch_rejects_local_sources_and_dns(monkeypatch):
    for url in ('file:///etc/passwd', 'http://localhost/x', 'http://example.org:4100/', 'https://u:p@example.org/'):
        with pytest.raises(net.FetchError):
            net.validate_url(url)
    monkeypatch.setattr(net.socket, 'getaddrinfo', lambda *a, **k: [(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(net.FetchError, match='non-public'):
        net.public_addresses('rebind.example.org',80)


def test_review_ui_and_adoption(client):
    conn = connect()
    tid, sid, thread, d = review_draft(conn)
    before = conn.execute('SELECT count(*) FROM artifacts').fetchone()[0]
    conn.close()
    page = client.get('/chat/' + thread).text
    assert '审阅产出' in page and '尚未归档' in page
    page = client.get('/chat/research/' + sid + '/review')
    assert page.status_code == 200 and 'Adopt' in page.text and '查看留存原文' in page.text
    r = client.post('/chat/research/' + sid + '/adopt', data={'draft_id':d['id'],'revision':1,'title':d['title'],'body':d['body'],'project':'','direction':''}, follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'].startswith('/artifacts/')
    assert client.get(r.headers['location']).status_code == 200
    page = client.get('/chat/' + thread).text
    assert '打开已采纳产出' in page


def test_feedback_keeps_research_skill_even_with_method_keywords(client):
    conn = connect()
    tid, sid, thread, d = review_draft(conn)
    conn.close()
    r = client.post('/chat/research/' + sid + '/feedback', data={'feedback':'请修改研究设计和方法的对照'}, follow_redirects=False)
    assert r.status_code == 303
    conn = connect()
    latest = tasks.latest_in_thread(conn, thread)
    assert latest['skill'] == 'literature-map' and latest['research_session_id'] == sid
    assert latest['status'] == 'waiting'
    conn.close()


def test_research_state_passes_from_claude_mcp_to_deepseek(client, monkeypatch):
    from research_lab.ai import dispatch
    conn = connect()
    tid, sid, thread, d = review_draft(conn)
    conn.execute("UPDATE agent_tasks SET engine='claude',session_id='prior-claude' WHERE id=?", (tid,))
    ctx = tasks.Context(tid, skills.get('literature-map'))
    tasks.ACTIVE[ctx.token] = ctx
    try:
        result = client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                             'params':{'name':'research_state','arguments':{}}},
                             headers={'Authorization':f'Bearer {ctx.token}'}).json()['result']
        assert not result['isError'] and 'CERF' in result['content'][0]['text']
    finally:
        tasks.ACTIVE.pop(ctx.token)
    seen = []
    def reply(req):
        seen.append(json.loads(req.content))
        message = ({'content':'','tool_calls':[{'id':'r1','type':'function',
                    'function':{'name':'research_state','arguments':'{}'}}]} if len(seen)==1 else {'content':'已恢复范围和待核查事项。'})
        return httpx.Response(200,json={'choices':[{'message':message}], 'usage':{}})
    monkeypatch.setenv('DEEPSEEK_API_KEY','test-key')
    monkeypatch.setattr(dispatch,'_TRANSPORT',httpx.MockTransport(reply))
    follow = tasks.create(conn,'继续核对资助来源',thread_id=thread,parent_id=tid)
    tasks.run(follow)
    current = tasks.get(conn,follow)
    assert current['engine']=='deepseek' and current['status']=='done' and current['research_session_id']==sid
    assert 'research_state' in seen[0]['messages'][-1]['content']
    restored = json.loads(seen[1]['messages'][-1]['content'])
    assert restored['revision']==1 and 'CERF' in restored['state_slice']
    assert d['id']==store.latest(conn,sid)['id'] and store.latest(conn,sid)['artifact_ref'] is None
    conn.close()


def test_pdf_reading_positions_and_errors_are_recorded(conn, sample_pdf, monkeypatch):
    tid,sid,_=session(conn,'case-timeline')
    monkeypatch.setattr(net,'fetch',lambda _: (sample_pdf.read_bytes(),'application/pdf','https://example.org/report.pdf'))
    ctx=tasks.Context(tid,skills.get('case-timeline'))
    text,error=tasks.execute_tool(conn,ctx,'read_research_source',{'source':'https://example.org/report.pdf','page':2,'limit':100})
    read=json.loads(text)
    assert not error and '印刷页 245' in read['locator'] and read['available_pages']==3
    more=retrieval.read(conn,sid,read['source_id'],page=2,offset=read['next_offset'])
    assert more['read_id']!=read['read_id'] and more['locator']!=read['locator']
    text,error=tasks.execute_tool(conn,ctx,'read_research_source',{'source':read['source_id'],'page':8})
    assert error and 'not available' in text
    assert conn.execute("SELECT status FROM research_searches WHERE backend='read'").fetchone()[0]=='error'
