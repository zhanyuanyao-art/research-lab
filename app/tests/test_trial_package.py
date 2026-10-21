import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / 'packaging/macos/trial.py'
    spec = importlib.util.spec_from_file_location('trial_package', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def snapshot(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'app/research_lab').mkdir(parents=True)
    (source / 'app/research_lab/example.py').write_text('safe source')
    manifest = {'source_commit': 'a' * 40, 'files': {'app/research_lab/example.py': {'sha256': hashlib.sha256(b'safe source').hexdigest()}}}
    (source / 'SOURCE-MANIFEST.json').write_text(json.dumps(manifest))
    return source, manifest


def test_build_input_is_bound_to_export_and_excludes_extras(tmp_path):
    source, manifest = snapshot(tmp_path)
    (source / 'not-in-export.txt').write_text('do not copy')
    actual, files = module().verified_files(source)
    assert actual == manifest
    assert files == [source / 'app/research_lab/example.py']
    (source / 'app/research_lab/example.py').write_text('modified after export')
    with pytest.raises(ValueError, match='changed'):
        module().verified_files(source)


def test_build_rejects_manifest_traversal_and_symlink(tmp_path):
    source, manifest = snapshot(tmp_path)
    (source / 'app/research_lab/alias.py').symlink_to(source / 'app/research_lab/example.py')
    for bad in ('../app.py', '/app.py', 'app/research_lab/alias.py', '.git/config', '.rl-content-root', '_data/secret.yml', 'app/data/lab.sqlite'):
        manifest['files'] = {bad: {'sha256': hashlib.sha256(b'safe source').hexdigest()}}
        (source / 'SOURCE-MANIFEST.json').write_text(json.dumps(manifest))
        with pytest.raises(ValueError):
            module().verified_files(source)


def test_mac_supplement_preserves_exact_notices_and_assets():
    root = Path(__file__).resolve().parents[2]
    docs = root / 'docs/public' if (root / 'docs/public').is_dir() else root
    data = json.loads((docs / 'mac-third-party-sources.json').read_text())
    raw = (docs / 'MAC-THIRD-PARTY-NOTICES.txt').read_bytes()
    for row in data['notices']:
        assert hashlib.sha256(raw[row['offset']:row['offset'] + row['bytes']]).hexdigest() == row['sha256']
    assert len(data['frontend']['dependencies']) == 15
    assert all(row['notices'] for row in data['frontend']['dependencies'])
    assert hashlib.sha256((root / 'app/research_lab/web/static/icons.svg').read_bytes()).hexdigest() == data['icons_sha256']
    assert hashlib.sha256((root / 'app/research_lab/web/static/vendor/toastui-editor-all.min.js').read_bytes()).hexdigest() == data['frontend']['all_min_sha256']
    proxy = next(n for n in data['notices'] if n.get('package') == 'proxy-tools')
    assert proxy['proof']['matches_sdist']


def test_dependency_sources_require_exact_bytes(tmp_path):
    raw = b'synthetic source archive'
    (tmp_path / 'source.tar.gz').write_bytes(raw)
    (tmp_path / 'mac-third-party-sources.json').write_text(json.dumps({'pdf_sources': [
        {'file': 'source.tar.gz', 'sha256': hashlib.sha256(raw).hexdigest()}]}))
    inventory = (tmp_path / 'mac-third-party-sources.json').read_bytes()
    # A changed original inventory must not affect the verified build inputs.
    (tmp_path / 'mac-third-party-sources.json').write_text('{"pdf_sources": []}')
    assert module().dependency_sources(inventory, tmp_path) == {'source.tar.gz': raw}
    with pytest.raises(ValueError, match='inventory is empty'):
        module().dependency_sources(b'{"pdf_sources": []}', tmp_path)
    (tmp_path / 'source.tar.gz').write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        module().dependency_sources(inventory, tmp_path)
