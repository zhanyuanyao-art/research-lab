"""Description drops route files without importing an unknown PDF implicitly."""
from pathlib import Path

import httpx
import pytest

from research_lab.config import get_paths
from research_lab.files import issues as I
from research_lab.files.yamlio import StaleFileError
from research_lab.intake import pipeline

SLUG = 'us-rok-reassurance'


@pytest.fixture
def issue(repo):
    return I.create('核对拖入材料', SLUG, '', I.revision())


@pytest.fixture
def offline(client):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        client.app.state.intake_http = http
        yield http


def drop(client, ref, name, data=b'example', mode='auto', **values):
    return client.post(f'/issues/{ref}/drop', files={'file': (name, data)},
                       data={'revision': I.revision(ref), 'mode': mode, **values},
                       headers={'HX-Request': 'true', 'Accept': 'application/json'})


def values(ref, key):
    return I._entry(I._metadata(), ref).get(key, [])


@pytest.mark.parametrize('suffix', ['docx', 'doc', 'pages', 'odt', 'rtf', 'xlsx', 'xls', 'pptx', 'key'])
def test_office_is_saved_once_as_output(client, issue, suffix):
    r = drop(client, issue, '../../draft.' + suffix.upper())
    assert r.status_code == 200 and r.json()['state'] == 'done'
    assert r.json()['revision'] == I.revision(issue)
    rel = values(issue, 'attachments')[0]
    assert rel == 'drafts/draft.' + suffix.upper()
    assert (get_paths().projects / SLUG / rel).read_bytes() == b'example'
    assert not (get_paths().root / 'private' / SLUG / Path(rel).name).exists()
    assert '>产出</button>' in r.json()['materials']


@pytest.mark.parametrize('suffix', ['epub', 'mobi', 'azw3', 'azw', 'djvu', 'fb2', 'png'])
def test_other_supported_files_stay_private(client, issue, suffix):
    assert drop(client, issue, 'source.' + suffix).status_code == 200
    assert values(issue, 'attachments') == ['private/source.' + suffix]


def test_unknown_pdf_is_read_only_until_choice(client, issue, conn, sample_pdf, monkeypatch):
    monkeypatch.setattr(pipeline.metadata, 'resolve', lambda *a, **k: pytest.fail('preflight called metadata'))
    before = I.revision(issue)
    r = drop(client, issue, 'source.pdf', sample_pdf.read_bytes())
    assert r.json()['state'] == 'choice' and r.json()['importable']
    assert I.revision(issue) == before
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0
    assert not values(issue, 'attachments') and not values(issue, 'materials')
    assert not list((get_paths().cache / 'incoming').glob('*'))
    assert not list(get_paths().data.rglob('*.pdf'))


def test_attach_choice_does_not_import(client, issue, conn, sample_pdf, monkeypatch):
    monkeypatch.setattr(pipeline, 'ingest', lambda *a, **k: pytest.fail('attachment imported'))
    r = drop(client, issue, 'source.pdf', sample_pdf.read_bytes(), mode='attach')
    assert r.status_code == 200 and values(issue, 'attachments') == ['private/source.pdf']
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 0


def test_import_choice_keeps_issue_project_and_direction(client, issue, conn, sample_pdf, offline):
    r = drop(client, issue, 'source.pdf', sample_pdf.read_bytes(), mode='import')
    assert r.status_code == 200 and r.json()['state'] == 'done'
    pid = values(issue, 'materials')[0].removeprefix('paper:')
    p = conn.execute('SELECT * FROM papers WHERE id=?', (pid,)).fetchone()
    assert p['direction'] == I.get(issue)['direction']
    assert conn.execute('SELECT project_slug FROM paper_projects WHERE paper_id=?', (pid,)).fetchone()[0] == SLUG
    assert not values(issue, 'attachments')
    assert conn.execute('SELECT COUNT(*) FROM paper_files').fetchone()[0] == 1
    assert not list((get_paths().cache / 'incoming').glob('*'))


def test_same_pdf_only_links_existing_record(client, issue, conn, sample_pdf, offline, monkeypatch):
    original = pipeline.ingest(conn, sample_pdf, ai=False, http=offline).paper_id
    monkeypatch.setattr(pipeline, 'ingest', lambda *a, **k: pytest.fail('same PDF reimported'))
    r = drop(client, issue, 'renamed.pdf', sample_pdf.read_bytes())
    assert r.json()['note'] == '已在文献库，已关联'
    assert values(issue, 'materials') == ['paper:' + original]
    assert not values(issue, 'attachments')
    assert conn.execute('SELECT COUNT(*) FROM paper_files').fetchone()[0] == 1


@pytest.mark.parametrize('clue', ['doi', 'title'])
def test_metadata_duplicates_are_checked_without_network(client, issue, conn, sample_pdf, offline, monkeypatch, clue):
    from dataclasses import replace
    pid = pipeline.ingest(conn, sample_pdf, ai=False, http=offline).paper_id
    ex = pipeline.extract.extract(sample_pdf)
    if clue == 'doi':
        conn.execute('UPDATE papers SET doi=? WHERE id=?', ('10.1234/own', pid)); conn.commit()
        ex = replace(ex, dois=['10.1234/own'], title_guess='Different rendering')
    else:
        ex = replace(ex, dois=[])
    monkeypatch.setattr(pipeline.extract, 'sha256_file', lambda path: 'new-file-hash')
    monkeypatch.setattr(pipeline.extract, 'extract', lambda path: ex)
    monkeypatch.setattr(pipeline.metadata, 'resolve', lambda *a, **k: pytest.fail('network lookup'))
    r = drop(client, issue, 'different.pdf', sample_pdf.read_bytes())
    if clue == 'doi':
        assert r.json()['state'] == 'duplicate' and not values(issue, 'materials')
        assert r.json()['paper_id'] == pid
    else:
        assert r.json()['state'] == 'done' and values(issue, 'materials') == ['paper:' + pid]


def test_ambiguous_dois_do_not_link_arbitrary_reference(client, issue, conn, sample_pdf, offline, monkeypatch):
    from dataclasses import replace
    pid = pipeline.ingest(conn, sample_pdf, ai=False, http=offline).paper_id
    conn.execute('UPDATE papers SET doi=? WHERE id=?', ('10.1234/cited', pid)); conn.commit()
    ex = replace(pipeline.extract.extract(sample_pdf), dois=['10.1234/cited', '10.1234/other'], title_guess='Another paper')
    monkeypatch.setattr(pipeline.extract, 'sha256_file', lambda path: 'new-hash')
    monkeypatch.setattr(pipeline.extract, 'extract', lambda path: ex)
    assert drop(client, issue, 'new.pdf', sample_pdf.read_bytes()).json()['state'] == 'choice'
    assert not values(issue, 'materials')


def test_later_duplicate_asks_before_linking(client, issue, conn, sample_pdf, offline, monkeypatch):
    pid = pipeline.ingest(conn, sample_pdf, ai=False, http=offline).paper_id
    monkeypatch.setattr(pipeline, 'find_existing', lambda *a: None)
    def duplicate(*a, **kw):
        assert kw['hold_duplicate'] is False
        return pipeline.IngestResult(None, False, pid, None, reason='similar title')
    monkeypatch.setattr(pipeline, 'ingest', duplicate)
    r = drop(client, issue, 'new.pdf', sample_pdf.read_bytes(), mode='import')
    assert r.json()['state'] == 'duplicate' and not values(issue, 'materials')
    assert drop(client, issue, 'new.pdf', sample_pdf.read_bytes(), mode='link', paper_id=pid).status_code == 200
    assert values(issue, 'materials') == ['paper:' + pid]


def test_stale_drop_is_refused_before_any_write(client, issue, conn, sample_pdf, monkeypatch):
    monkeypatch.setattr(pipeline, 'find_existing', lambda *a: pytest.fail('stale preflight'))
    r = drop(client, issue, 'a.pdf', sample_pdf.read_bytes(), revision='old')
    assert r.status_code == 409 and not values(issue, 'materials')
    assert drop(client, issue, 'a.docx', revision='old').status_code == 409
    assert not values(issue, 'attachments')


def test_concurrent_change_during_import_keeps_import_but_refuses_link(client, issue, conn, sample_pdf, offline, monkeypatch):
    ingest = pipeline.ingest
    def changed(*a, **kw):
        out = ingest(*a, **kw)
        I.update(issue, 'title', 'Changed elsewhere', I.revision(issue))
        return out
    monkeypatch.setattr(pipeline, 'ingest', changed)
    r = drop(client, issue, 'a.pdf', sample_pdf.read_bytes(), mode='import')
    assert r.status_code == 409 and r.json()['state'] == 'conflict'
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 1
    assert not values(issue, 'materials') and I.get(issue)['title'] == 'Changed elsewhere'


def test_attachment_failure_preserves_saved_copy(repo, issue, monkeypatch):
    I.attach_upload(issue, 'draft.docx', b'original', I.revision(issue), folder_key='drafts')
    def fail(*a):
        raise StaleFileError('changed')
    monkeypatch.setattr(I, '_save_list', fail)
    with pytest.raises(I.AttachmentLinkError, match="已保存"):
        I.attach_upload(issue, 'draft.docx', b'new', I.revision(issue), folder_key='drafts')
    folder = get_paths().projects / SLUG / 'drafts'
    assert (folder / 'draft.docx').read_bytes() == b'original'
    assert (folder / 'draft-2.docx').read_bytes() == b'new'


def test_rejected_types_caps_and_invalid_pdf_choices(client, issue, monkeypatch):
    assert drop(client, issue, 'run.sh').status_code == 400
    assert drop(client, issue, 'a.docx', mode='import').status_code == 400
    assert drop(client, issue, 'broken.pdf').json()['importable'] is False
    assert drop(client, issue, 'broken.pdf', mode='import').status_code == 400
    assert drop(client, issue, 'broken.pdf', mode='attach').status_code == 200
    monkeypatch.setattr(I, 'ATTACH_MAX', 5)
    assert drop(client, issue, 'large.docx', b'123456').status_code == 400


def test_description_drop_has_own_queue_and_global_import_remains(client, issue):
    page = client.get(f'/issues/{issue}').text
    assert f'data-issue-drop="/issues/{issue}/drop" data-no-global-drop' in page
    assert 'data-issue-uploads' in page and 'global-drop-form' in page
    static = Path(__file__).parents[1] / 'research_lab/web/static'
    assert '[data-issue-drop]' in (static / 'app.js').read_text()
    assert "e.stopPropagation()" in (static / 'issues.js').read_text()


def test_drop_import_duplicate_does_not_leave_pending_files(conn, sample_pdf, monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as http:
        pid = pipeline.ingest(conn, sample_pdf, http=http, ai=False).paper_id
        monkeypatch.setattr(pipeline.dedupe, 'by_sha', lambda *a: None)
        r = pipeline.ingest(conn, sample_pdf, http=http, ai=False, hold_duplicate=False)
    assert r.paper_id is None and r.duplicate_of == pid and r.pending is None
    assert not list((get_paths().cache / 'incoming').glob('*'))


def test_single_cited_doi_is_not_an_identity_match(client, issue, conn, offline, tmp_path):
    import pymupdf
    from research_lab.intake import dedupe
    conn.execute("INSERT INTO papers(id,title,title_hash,doi,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                 ('cited', 'The cited article', dedupe.title_hash('The cited article'), '10.1234/cited', '2026', '2026'))
    conn.commit()
    pdf = pymupdf.open(); page = pdf.new_page()
    page.insert_text((72,72), 'A different study\nWe refer to https://doi.org/10.1234/cited')
    pdf.set_metadata({'title':'A different study'})
    data = pdf.tobytes(); pdf.close()
    r = drop(client, issue, 'different.pdf', data)
    assert r.json()['state'] == 'duplicate' and r.json()['paper_id'] == 'cited'
    assert not values(issue, 'materials')
    r = drop(client, issue, 'different.pdf', data, mode='import')
    assert r.json()['state'] == 'done'
    assert values(issue, 'materials') != ['paper:cited']


def test_competing_uploads_claim_distinct_names(repo, issue, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from research_lab.files import deliverables
    barrier = Barrier(2)
    opened = Barrier(2)
    original_open = Path.open
    def simultaneous_open(path, mode="r", *args, **kwargs):
        if path.name == "shared.docx" and mode in ("xb", "wb"):
            opened.wait(timeout=5)
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", simultaneous_open)
    def issue_upload():
        barrier.wait()
        I.attach_upload(issue, 'shared.docx', b'issue', I.revision(issue), folder_key='drafts')
    def project_upload():
        barrier.wait()
        return deliverables.save_upload(SLUG, 'shared.docx', b'project')
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(issue_upload), pool.submit(project_upload)
        a.result(); project = b.result()
    attached = get_paths().projects / SLUG / values(issue, 'attachments')[0]
    assert attached != project
    assert attached.read_bytes() == b'issue' and project.read_bytes() == b'project'


def test_link_failure_does_not_delete_an_external_edit(repo, issue, monkeypatch):
    target = get_paths().projects / SLUG / 'drafts' / 'draft.docx'
    def changed(*a):
        target.write_bytes(b'edited outside')
        raise StaleFileError('changed')
    monkeypatch.setattr(I, '_save_list', changed)
    with pytest.raises(I.AttachmentLinkError):
        I.attach_upload(issue, 'draft.docx', b'upload', I.revision(issue), folder_key='drafts')
    assert target.read_bytes() == b'edited outside'


def test_saved_attachment_conflict_is_explicit(client, issue, monkeypatch):
    def fail(*a):
        raise StaleFileError('changed')
    monkeypatch.setattr(I, '_save_list', fail)
    r = drop(client, issue, 'draft.docx')
    assert r.status_code == 409 and '已保存到课题文件夹' in r.json()['error']


def test_confirmed_keep_both_uses_existing_pipeline(client, issue, conn, sample_pdf, offline, monkeypatch):
    first = pipeline.ingest(conn, sample_pdf, http=offline, ai=False).paper_id
    monkeypatch.setattr(pipeline, 'find_existing', lambda *a: None)
    data = sample_pdf.read_bytes() + b'\n'
    assert drop(client, issue, 'second.pdf', data, mode='import').json()['state'] == 'duplicate'
    assert not values(issue, 'materials')
    r = drop(client, issue, 'second.pdf', data, mode='separate')
    assert r.status_code == 200 and r.json()['state'] == 'done'
    assert values(issue, 'materials') != ['paper:' + first]
    assert conn.execute('SELECT COUNT(*) FROM papers').fetchone()[0] == 2
