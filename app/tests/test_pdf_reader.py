"""The real PDF surface, protected file delivery and feed-to-library reading contract."""
from pathlib import Path
import json

import httpx
import pytest

from research_lab.intake import pipeline
from research_lab.db.connection import now_iso


@pytest.fixture
def paper(conn, sample_pdf):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))) as http:
        return pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id


def test_pdf_bytes_and_range(client, conn, paper, sample_pdf):
    response = client.get(f"/papers/{paper}/pdf")
    assert response.status_code == 200 and response.content == sample_pdf.read_bytes()
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['content-disposition'].startswith('inline;')
    response = client.get(f"/papers/{paper}/pdf", headers={'Range': 'bytes=0-7'})
    assert response.status_code == 206 and response.content == sample_pdf.read_bytes()[:8]
    assert response.headers['content-range'].startswith('bytes 0-7/')


@pytest.mark.parametrize('path', ['../outside.pdf', 'lab.sqlite', '/tmp/outside.pdf', 'papers/missing.pdf'])
def test_pdf_never_serves_outside_paper_storage(client, conn, paper, path):
    conn.execute('UPDATE paper_files SET rel_path=? WHERE paper_id=?', (path, paper))
    assert client.get(f'/papers/{paper}/pdf').status_code == 404


def test_pdf_rejects_symlink_escape(client, conn, paper, tmp_path):
    from research_lab.config import get_paths
    outside = tmp_path / 'outside.pdf'
    outside.write_bytes(b'%PDF-private')
    link = get_paths().papers / 'escape.pdf'
    link.symlink_to(outside)
    conn.execute('UPDATE paper_files SET rel_path=? WHERE paper_id=?', ('papers/escape.pdf', paper))
    assert client.get(f'/papers/{paper}/pdf').status_code == 404


def test_pdf_missing_paper_and_metadata_only(client, conn):
    assert client.get('/papers/missing/pdf').status_code == 404
    ts = now_iso()
    conn.execute("INSERT INTO papers(id,title,title_hash,created_at,updated_at) VALUES ('biblio','Only metadata','biblio',?,?)", (ts,ts))
    assert client.get('/papers/biblio/pdf').status_code == 404
    html = client.get('/papers/biblio').text
    assert '待补原文' in html and '添加 PDF' in html and '生成概要</button>' not in html


def test_summary_starts_with_question_and_argument_without_writing_fields(client, conn, paper):
    summary = {'research_question': {'text': '为什么维持？', 'pages': ['244']},
               'argument': {'text': '核心解释', 'pages': ['245']},
               'mechanism': {'text': '可核对机制', 'pages': ['246']}}
    conn.execute('UPDATE papers SET ai_summary_json=? WHERE id=?', (json.dumps(summary),paper))
    html = client.get(f'/papers/{paper}').text
    card = html.split('id="summary-card"')[1].split('</section>')[0]
    main = card.split('<dl class="sum">')[1].split('</dl>')[0]
    assert '为什么维持' in main and '核心解释' in main and '可核对机制' not in main
    more = card.split('data-summary-section="more"')[1].split('</details>')[0]
    assert more.startswith('>') and '可核对机制' in more
    review = card.split('data-summary-section="review"')[1].split('</details>')[0]
    assert review.startswith('>') and '/adopt/' in review
    assert '/adopt/' not in card.split('data-summary-section="review"')[0]
    assert pipeline.get_paper(conn, paper)['claim_text'] is None
    assert '<section class="pdf-page"' not in html and 'id="excerpt-drawer"' not in html
    assert 'data-hotkey="excerpt"' not in html
    assert 'id="side-summary"' in html and 'id="side-summary" hidden' not in html
    assert 'data-page-label="245"' in html
    fragment = client.get(f'/papers/{paper}/summary').text
    assert '核心解释' in fragment and f'/papers/{paper}/summarize' not in fragment      # Regenerate is in the ⋯ menu (card 0129)
    menu = html.split('<details class="menu more">')[1].split('</details>')[0]
    assert f'hx-post="/papers/{paper}/summarize"' in menu and '>Regenerate</button>' in menu.replace('</svg>', '>')


@pytest.mark.parametrize('label,index', [('245',1), ('p. [245]',1), ('1',0), ('3',2)])
def test_reader_page_labels_and_physical_fallback(client, paper, label, index):
    html = client.get(f'/papers/{paper}/read', params={'page':label}).text
    assert f'data-initial-page="{index}"' in html


def test_pdf_assets_are_self_hosted_and_matching(client):
    root = Path(__file__).resolve().parents[1] / 'research_lab/web/static/pdfjs'
    assert '6.4.299' in (root/'README.md').read_text()
    assert (root/'LICENSE').is_file()
    css = (root/'web/pdf_viewer.css').read_text()
    assert '@scope (#pdf-container)' in css and ':root' not in css
    for file in ('build/pdf.mjs','build/pdf.worker.mjs','web/pdf_viewer.mjs','web/pdf_viewer.css'):
        response = client.get('/static/pdfjs/'+file)
        assert response.status_code == 200
        if file.endswith('.mjs'):
            assert 'javascript' in response.headers['content-type']
    assert (root/'cmaps/Adobe-GB1-UCS2.bcmap').exists()
    assert any((root/'standard_fonts').glob('*.pfb'))
    assert any((root/'wasm').glob('*.wasm'))


@pytest.mark.parametrize('with_pdf', [True, False])
def test_feed_read_reuses_library_paper(client, conn, sample_pdf, with_pdf):
    from .test_feeds import _feed_paper, _import_transport
    item = _feed_paper(conn, pdf_url='https://oa.example.org/sample.pdf' if with_pdf else None)
    calls = []
    with httpx.Client(transport=_import_transport(sample_pdf.read_bytes() if with_pdf else None, calls)) as http:
        client.app.state.intake_http = http
        response = client.post(f'/feed/papers/{item}/read')
        assert response.status_code == 200
        dest = response.headers['HX-Redirect']
        page = client.get(dest)
        assert page.status_code == 200 and ('id="pdf-viewer"' in page.text) == with_pdf
        if not with_pdf:
            assert '待补原文' in page.text and '添加 PDF' in page.text
        again = client.post(f'/feed/papers/{item}/read')
        assert again.headers['HX-Redirect'] == dest
        assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 1
        plain = client.post(f'/feed/papers/{item}/read', headers={'HX-Request':'','Origin':'http://127.0.0.1:4100'}, follow_redirects=False)
        assert plain.status_code == 303 and plain.headers['location'] == dest


def test_field_feedback_remains_visible_before_a_summary_exists(client, conn, paper):
    error = client.post(f'/papers/{paper}/adopt/mechanism')
    assert error.status_code == 200 and '还没有可采纳' in error.text and 'role="alert"' in error.text
    saved = client.post(f'/papers/{paper}/fields', data={'method':'process tracing'})
    assert saved.status_code == 200 and '已保存。' in saved.text
    assert pipeline.get_paper(conn, paper)['method'] == 'process tracing'
