"""The paperclip sends a PDF through the description drop's library flow, other files still attach (card 0178); a linked
paper's chip carries Replicate: its newest replication plan, or the new-plan page, which queues nothing by itself."""
import re
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from research_lab.files import issues as I
from research_lab.intake import pipeline

SLUG = 'us-rok-reassurance'
STATIC = Path(__file__).parents[1] / 'research_lab/web/static'


@pytest.fixture
def issue(repo):
    return I.create('DCL 复现研讨', SLUG, '', I.revision())


@pytest.fixture
def offline(client):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        client.app.state.intake_http = http
        yield http


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(pipeline.metadata, 'resolve', lambda *a, **k: pytest.fail('metadata lookup'))


def paperclip(client, ref):
    """The paperclip's form as the issue page renders it: (action for other files, URL a PDF goes to)."""
    page = client.get(f'/issues/{ref}').text
    tag = re.search(r'<form [^>]*class="issue-attach-form"[^>]*>', page).group(0)
    return re.search(r'action="([^"]+)"', tag).group(1), re.search(r'data-library-drop="([^"]+)"', tag).group(1)


def send_pdf(client, ref, name, data):
    """What issues.js sends for a PDF chosen with the paperclip: the upload queue's first request, mode auto."""
    _, url = paperclip(client, ref)
    return client.post(url, files={'file': (name, data)}, data={'revision': I.revision(ref), 'mode': 'auto'},
                       headers={'HX-Request': 'true', 'Accept': 'application/json'})


def linked(ref, key):
    return I._entry(I._metadata(), ref).get(key, [])


def strip(page):
    return page.split('class="issue-mats-strip"', 1)[1].split('data-issue-uploads', 1)[0]


def test_paperclip_routes_pdfs_through_the_library_drop():
    js = (STATIC / 'issues.js').read_text()
    handler = js.split("input.matches('[data-attach-input]')", 1)[1].split('});', 1)[0]
    assert 'input.form.dataset.libraryDrop' in handler
    assert r'/\.pdf$/i.test(file.name)' in handler and 'uploadRow(file, drop)' in handler
    assert 'input.form.requestSubmit()' in handler


def test_paperclip_offers_html_on_the_attachment_path(client, issue):
    """Leo 10-16: HTML may be attached (the server's types are card 0180's). Not a PDF, so the form posts it to
    /attachments; attachments open in the system app and are never served to the page."""
    page = client.get(f'/issues/{issue}').text
    tag = re.search(r'<input [^>]*data-attach-input[^>]*>', page).group(0)
    types = re.search(r'accept="([^"]+)"', tag).group(1).split(',')
    assert '.html' in types and '.htm' in types and '.pdf' in types


def test_paperclip_pdf_not_in_library_offers_the_choice(client, issue, conn, sample_pdf, no_network, monkeypatch):
    assert paperclip(client, issue) == (f'/issues/{issue}/attachments', f'/issues/{issue}/drop')
    monkeypatch.setattr(pipeline, 'ingest', lambda *a, **k: pytest.fail('imported before the choice'))
    before = I.revision(issue)
    r = send_pdf(client, issue, 'Smith 2024.pdf', sample_pdf.read_bytes())
    assert r.status_code == 200 and r.json()['state'] == 'choice' and r.json()['importable']
    assert I.revision(issue) == before
    assert not linked(issue, 'attachments') and not linked(issue, 'materials')
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0


def test_paperclip_pdf_already_in_library_is_linked(client, issue, conn, sample_pdf, offline, monkeypatch):
    pid = pipeline.ingest(conn, sample_pdf, ai=False, http=offline).paper_id      # offline: every lookup gets a 404
    monkeypatch.setattr(pipeline.metadata, 'resolve', lambda *a, **k: pytest.fail('metadata lookup'))
    monkeypatch.setattr(pipeline, 'ingest', lambda *a, **k: pytest.fail('same PDF reimported'))
    r = send_pdf(client, issue, 'renamed.pdf', sample_pdf.read_bytes())
    assert r.status_code == 200 and r.json()['state'] == 'done' and r.json()['note'] == '已在文献库，已关联'
    assert linked(issue, 'materials') == ['paper:' + pid] and not linked(issue, 'attachments')
    # The strip the page swaps in already carries the new paper's Replicate.
    assert f'href="/data/plans/new?paper={pid}"' in r.json()['materials']


@pytest.mark.parametrize('name', ['讲稿.docx', 'figure.png'])
def test_paperclip_non_pdf_still_attaches(client, issue, conn, name, monkeypatch):
    monkeypatch.setattr(pipeline, 'find_existing', lambda *a: pytest.fail('a non-PDF went to the library'))
    action, _ = paperclip(client, issue)
    r = client.post(action, files={'file': (name, b'example')}, data={'revision': I.revision(issue)},
                    headers={'HX-Request': 'true'}, follow_redirects=False)
    assert r.status_code == 303
    assert linked(issue, 'attachments') == ['private/' + name] and not linked(issue, 'materials')


def test_replicate_opens_the_newest_plan_or_the_new_plan_page(client, issue, conn):
    now = datetime.now().isoformat()
    for pid, title in (('lee2024', 'Alliance reassurance'), ('kim2023', 'Hedging in Seoul'), ('gone2020', 'Deleted later')):
        conn.execute('INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
                     (pid, title, pid, '["Lee, Min"]', 2024, now, now))
    conn.execute('INSERT INTO datasets(id, name, source_kind, original_name, rows, cols, created_at) VALUES (?,?,?,?,?,?,?)',
                 ('DS-2026-0001', 'ATOP', 'csv', 'atop.csv', 48, 12, now))
    # Newest by creation, not by id.
    for plan_id, created in (('RP-2026-0007', '2026-10-01T09:00:00'), ('RP-2026-0003', '2026-10-15T09:00:00')):
        conn.execute('INSERT INTO replication_plans(id, paper_id, status, plan_json, origin, created_at, updated_at) '
                     'VALUES (?,?,?,?,?,?,?)', (plan_id, 'kim2023', 'draft', '{}', 'user', created, created))
    conn.commit()
    for item in ('paper:lee2024', 'paper:kim2023', 'paper:gone2020', 'dataset:DS-2026-0001'):
        I.set_material(conn, issue, item, True, I.revision(issue))
    conn.execute("DELETE FROM papers WHERE id = 'gone2020'")
    conn.commit()

    chips = strip(client.get(f'/issues/{issue}').text)
    assert 'href="/data/plans/new?paper=lee2024"' in chips
    assert 'href="/data/plans/RP-2026-0003"' in chips and 'RP-2026-0007' not in chips
    assert chips.count('>Replicate</a>') == 2          # neither the dataset nor the deleted paper
    assert 'title="打开这篇文献最新的复现方案"' in chips

    # The issue page and the page Replicate opens only read: generation starts from that page's own button.
    assert client.get('/data/plans/new?paper=lee2024').status_code == 200
    assert conn.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
