"""Quick navigation is bounded, local, and points to the same issue / reader routes."""
import re
from pathlib import Path

import httpx

from research_lab.files import issues as I, artifacts
from research_lab.intake import pipeline


def test_panel_is_available_on_each_destination(client):
    for url in ('/', '/papers', '/issues', '/data', '/settings', '/chat'):
        html = client.get(url).text
        assert html.count('id="command-palette"') == 1
        assert 'palette.js' in html and 'aria-controls="palette-results"' in html


def test_empty_search_only_offers_actions_and_never_indexes(client, monkeypatch):
    monkeypatch.setattr(artifacts, 'index', lambda *a: (_ for _ in ()).throw(AssertionError('indexing on keystroke')))
    html = client.get('/search/quick').text
    assert 'New issue' in html and 'Import PDF' in html and 'View all results' in html
    assert '<dialog' not in html and '<html' not in html
    assert 'aria-label="文献"' not in html


def test_issue_keys_and_project_titles_jump_directly(client, repo):
    ref = I.create('核对快速跳转', 'us-rok-reassurance', '', I.revision())
    I.sync_keys(create=True)
    key = I.get(ref)['key']
    for q in (key, key.lower(), '核对快速'):
        html = client.get('/search/quick', params={'q':q}).text
        assert '核对快速跳转' in html and key in html
    html = client.get('/search/quick', params={'q':'US–ROK'}).text
    assert 'US–ROK Reassurance' in html and 'aria-label="课题与议题"' in html


def test_title_and_page_hits_open_the_reader(client, conn, sample_pdf):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    html = client.get('/search/quick', params={'q':'ASEAN'}).text
    assert f'/papers/{pid}' in html and 'aria-label="全文"' in html
    assert '/read?page=' in html and 'find=ASEAN' in html and '#p-' in html


def test_results_are_bounded_and_escape_stored_titles(client, conn, monkeypatch):
    monkeypatch.setattr(artifacts, 'index', lambda *a: (_ for _ in ()).throw(AssertionError('indexing on keystroke')))
    for i in range(12):
        conn.execute('INSERT INTO papers(id,title,title_hash,created_at,updated_at) VALUES(?,?,?,?,?)',
                     (f'p{i}', f'Sample <img src=x onerror=alert(1)> {i}', f'h{i}', '2026', '2026'))
    conn.commit()
    html = client.get('/search/quick', params={'q':'Sample'}).text
    group = html.split('aria-label="文献"',1)[1].split('</div>',1)[0]
    assert group.count('class="palette-hit"') == 5
    assert '<img src=x' not in html and '&lt;img' in html
    assert '没有匹配结果' in client.get('/search/quick', params={'q':'%_nohit'}).text


def test_cjk_fulltext_and_literal_wildcards(client, conn, sample_pdf):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
    fid = conn.execute('SELECT id FROM paper_files WHERE paper_id=?', (pid,)).fetchone()[0]
    conn.execute('UPDATE paper_pages SET text=? WHERE file_id=? AND page_index=0', ('区域合作与经济信号，5%_x。', fid))
    conn.commit()
    html = client.get('/search/quick', params={'q':'信号'}).text
    assert 'aria-label="全文"' in html and '经济信号' in html
    assert '没有匹配结果' in client.get('/search/quick', params={'q':'经济_缺失'}).text


def test_keyboard_binding_moves_out_of_app_script():
    static = Path(__file__).parents[1] / 'research_lab/web/static'
    app = (static / 'app.js').read_text()
    assert "e.key.toLowerCase() === 'k'" not in app
    palette = (static / 'palette.js').read_text()
    for expected in ('AbortController', 'turn !== generation', 'compositionstart', 'aria-activedescendant', 'ArrowDown'):
        assert expected in palette
