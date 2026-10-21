"""The public export reads selected committed blobs, never a user's working files."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('public_export', Path(__file__).resolve().parents[2] / 'scripts/export-public.py')
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def commit(root):
    exporter.git(root, 'add', '.')
    exporter.git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'Fixture')
    return exporter.git(root, 'rev-parse', 'HEAD').decode().strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    exporter.git(root, 'init', '-q', '-b', 'main')
    for name in ['pyproject.toml', 'app/research_lab/__init__.py',
                 *['docs/public/' + p for p in exporter.DOCUMENTS]]:
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text('synthetic\n')
    return root


def test_export_excludes_private_blobs_and_dirty_files(source, tmp_path, monkeypatch):
    private = ['.env', '.rl-content-root', 'app/.env', 'app/data/lab.sqlite', '_data/projects.yml',
               '_artifacts/note.md', 'projects/private.md', 'dev/cards/0001-private.md', 'private/raw.txt']
    for name in private:
        p = source / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('private sentinel: ' + 'sk-' + 'a' * 30)
    pdf_engine = source / 'app/research_lab/web/static/pdfjs/build/pdf.mjs'
    pdf_engine.parent.mkdir(parents=True)
    pdf_engine.write_text('vendored PDF engine')
    head = commit(source)
    blocked = {exporter.git(source, 'rev-parse', head + ':' + p).decode().strip() for p in private}
    original = exporter.git
    def guarded(root, *args):
        assert not (args[:2] == ('cat-file', 'blob') and args[2] in blocked)
        return original(root, *args)
    monkeypatch.setattr(exporter, 'git', guarded)
    (source / 'app/research_lab/__init__.py').write_text('uncommitted')
    (source / 'app/research_lab/untracked.py').write_text('untracked')
    out = tmp_path / 'candidate'
    report = exporter.export(source, out, head)
    assert (out / 'app/research_lab/__init__.py').read_text() == 'synthetic\n'
    assert not (out / 'app/research_lab/untracked.py').exists()
    assert all(not (out / p).exists() for p in private)
    assert exporter.git(out, 'rev-list', '--count', 'HEAD').strip() == b'1'
    assert exporter.git(out, 'remote').strip() == b''
    assert (out / pdf_engine.relative_to(source)).read_text() == 'vendored PDF engine'
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.txt', 'third-party-sources.json', 'CODE_OF_CONDUCT.md', 'PREVIEW.md',
                 '.github/ISSUE_TEMPLATE/bug-report.yml', '.github/ISSUE_TEMPLATE/feature-request.yml'):
        assert (out / name).read_text() == 'synthetic\n'
        assert not (out / 'docs/public' / name).exists()
    assert report['source_commit'] == head
    assert json.loads((out / 'SOURCE-MANIFEST.json').read_text()) == report
    for path, record in report['files'].items():
        assert hashlib.sha256((out / path).read_bytes()).hexdigest() == record['sha256']


@pytest.mark.parametrize('kind', ['link', 'credential', 'fixture_credential', 'env', '.sqlite', '.duckdb', '.duckdb.wal', '.sqlite-wal', '.sqlite-shm', '.db-journal', '.DB'])
def test_export_rejects_unsafe_selected_files(source, tmp_path, kind):
    path = source / 'app/research_lab/unsafe.py'
    if kind == 'link':
        path.symlink_to('/tmp/unread-target')
    elif kind == 'env':
        path = source / 'app/research_lab/.env'
        path.write_text('private')
    elif kind.startswith('.'):
        path = source / ('app/research_lab/example' + kind)
        path.write_bytes(b'SQLite')
    else:
        if kind == 'fixture_credential':
            path = source / 'app/tests/test_autofix.py'
            path.parent.mkdir(parents=True)
        path.write_text('sk-' + 'X' * 32)
    commit(source)
    out = tmp_path / 'candidate'
    with pytest.raises(ValueError) as exc:
        exporter.export(source, out)
    assert 'X' * 32 not in str(exc.value)
    assert not out.exists()


def test_export_preserves_existing_destination(source, tmp_path):
    commit(source)
    out = tmp_path / 'candidate'
    out.mkdir()
    marker = out / 'my-notes.txt'
    marker.write_text('keep')
    with pytest.raises(FileExistsError):
        exporter.export(source, out)
    assert marker.read_text() == 'keep'


def test_content_snapshot_is_synthetic(content_snapshot):
    fixtures = Path(__file__).parent / 'fixtures/content'
    assert (content_snapshot / '_data/projects.yml').read_bytes() == (fixtures / '_data/projects.yml').read_bytes()
    assert not list(content_snapshot.rglob('*.sqlite'))
    assert not list(content_snapshot.rglob('.env*'))


def test_frontend_notice_evidence_matches_bundled_assets():
    root = Path(__file__).resolve().parents[2]
    docs = root / 'docs/public' if (root / 'docs/public').is_dir() else root
    inventory = json.loads((docs / 'third-party-sources.json').read_text())
    bundle = (docs / 'THIRD-PARTY-NOTICES.txt').read_bytes()
    assert inventory['status'] == 'partial-inventory-for-private-review'
    assert inventory['limitations']
    for item in inventory['notices']:
        original = bundle[item['offset']:item['offset'] + item['bytes']]
        assert hashlib.sha256(original).hexdigest() == item['sha256']
        assert item['source'].startswith('https://raw.githubusercontent.com/')
        for name, expected in item['assets'].items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
    # Existing engine and editor notices must survive the same export boundary.
    for path in (root / 'app/research_lab/web/static').rglob('*LICENSE*'):
        assert exporter.selected(path.relative_to(root).as_posix())


def test_the_real_tree_passes_the_export_scan():
    """Card 0205: a key-shaped test value blocked an export; every selected file of this checkout must pass the scan."""
    root = Path(__file__).resolve().parents[2]
    try:
        listing = exporter.git(root, 'ls-files', '-s', '-z')
    except Exception:  # noqa: BLE001 - a source tarball has no Git index
        pytest.skip('not a Git checkout')
    checked = 0
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        meta, path = entry.decode().split('\t', 1)
        mode = meta.split()[0]
        if exporter.selected(path):
            body = b'' if mode == '120000' else (root / path).read_bytes()   # a link fails on its mode alone
            exporter.validate(path, mode, body)
            checked += 1
    assert checked > 100


def test_readme_screenshots_ship_beside_the_readme_and_nothing_else_rides_along(source, tmp_path):
    """Card 0206: PNGs directly under docs/public/screenshots/ land in docs/screenshots/, where README.md links them."""
    shots = source / 'docs/public/screenshots'
    (shots / 'nested').mkdir(parents=True)
    (shots / 'reader.png').write_bytes(b'\x89PNG synthetic')
    (shots / 'notes.md').write_text('private draft')
    (shots / 'nested/raw.png').write_bytes(b'\x89PNG nested')
    commit(source)
    out = tmp_path / 'candidate'
    report = exporter.export(source, out)
    assert (out / 'docs/screenshots/reader.png').read_bytes() == b'\x89PNG synthetic'
    assert 'docs/screenshots/reader.png' in report['files']
    assert not any('notes.md' in p or 'nested' in p or p.startswith('docs/public') for p in report['files'])


def test_the_readme_links_only_screenshots_that_ship():
    import re
    root = Path(__file__).resolve().parents[2]
    # The private tree keeps them under docs/public/; the exported repository has README.md at its root.
    readme, shots = ((root / 'docs/public/README.md', root / 'docs/public/screenshots') if (root / 'docs/public').is_dir()
                     else (root / 'README.md', root / 'docs/screenshots'))
    links = re.findall(r'!\[[^\]]*\]\(docs/screenshots/([^)]+)\)', readme.read_text())
    assert links and all((shots / name).is_file() and name.endswith('.png') for name in links)
