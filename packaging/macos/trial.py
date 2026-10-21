#!/usr/bin/env python3
"""Build an isolated Mac trial from an unchanged public source export."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib


def verified_files(source: Path) -> tuple[dict, list[Path]]:
    manifest = json.loads((source / 'SOURCE-MANIFEST.json').read_text())
    spec = importlib.util.spec_from_file_location('trial_exporter', Path(__file__).resolve().parents[2] / 'scripts/export-public.py')
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    paths = []
    for name, record in manifest['files'].items():
        parts = PurePosixPath(name).parts
        if not parts or name.startswith('/') or any(x in ('.', '..', '.git', '.venv', '.rl-content-root', 'private') for x in parts):
            raise ValueError('Unsafe source manifest path')
        if not exporter.selected(name) and name not in exporter.DOCUMENTS.values():
            raise ValueError('File is outside the public export boundary')
        f = source / name
        if f.is_symlink() or any(p.is_symlink() for p in f.parents if p != source.parent):
            raise ValueError('Source contains a symlink')
        body = f.read_bytes()
        exporter.validate(name, record.get('mode', '100644'), body)
        if hashlib.sha256(body).hexdigest() != record['sha256']:
            raise ValueError(f'Source changed after export: {name}')
        paths.append(f)
    return manifest, paths


def dependency_sources(inventory: bytes, directory: Path) -> dict[str, bytes]:
    entries = json.loads(inventory)['pdf_sources']
    if not entries:
        raise ValueError('Dependency source inventory is empty')
    contents = {}
    for entry in entries:
        name = entry['file']
        if Path(name).name != name or not name.endswith('.tar.gz'):
            raise ValueError('Unsafe dependency source filename')
        path = directory / name
        if path.is_symlink():
            raise ValueError('Dependency source is a symlink')
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry['sha256']:
            raise ValueError(f'Dependency source hash mismatch: {name}')
        contents[name] = body
    return contents


def build(source: Path, output: Path, pdf_sources: Path) -> dict:
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('This trial builder requires an Apple Silicon Mac')
    manifest, paths = verified_files(source)
    inputs = {str(path.relative_to(source)): path.read_bytes() for path in paths}
    for name, body in inputs.items():
        if hashlib.sha256(body).hexdigest() != manifest['files'][name]['sha256']:
            raise ValueError(f'Source changed while copying: {name}')
    dependencies = dependency_sources(inputs['mac-third-party-sources.json'], pdf_sources)
    output.mkdir(parents=True, exist_ok=False)
    src = output / 'build.noindex/source'
    src.mkdir(parents=True)
    for name, body in inputs.items():
        dest = src / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        dest.chmod(int(manifest['files'][name].get('mode', '100644'), 8) & 0o777)
    manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
    (src / 'SOURCE-MANIFEST.json').write_bytes(manifest_bytes)
    version = tomllib.loads((src / 'pyproject.toml').read_text())['project']['version']
    tag = f'{version}-{manifest["source_commit"][:12]}'
    stamp = {'version': version, 'commit': manifest['source_commit'], 'dirty': False, 'mode': 'trial',
             'distribution': 'trial', 'date': datetime.now(timezone.utc).date().isoformat()}
    env = {**os.environ, 'PYTHONPATH': str(src / 'app'), 'PYTHON_DOTENV_DISABLED': '1',
           'RL_DISTRIBUTION': 'trial', 'RL_APP_NAME': 'Research Lab Trial',
           'RL_BUNDLE_ID': 'io.github.leoyao0701.researchlab.trial', 'RL_CLAUDE_BIN': 'none',
           'RL_AI_WORKER': '0', 'RL_FEEDS_AUTO': '0', 'RL_PM_AUTO': '0', 'RL_AUTOFIX': '0',
           'RL_REPO_ROOT': str(output / 'build.noindex/unused-workspace'),
           'RL_DATA_DIR': str(output / 'build.noindex/unused-workspace/app/data'),
           'DEEPSEEK_API_KEY': '', 'BRAVE_SEARCH_API_KEY': ''}
    from licenses import collect
    report = collect(src, src / 'packaging/macos/build/licenses')
    (src / 'app/research_lab/web/static/build.json').write_text(json.dumps(stamp))

    def run(args):
        subprocess.run(args, cwd=src, env=env, check=True)

    run([sys.executable, str(src / 'packaging/macos/make_icon.py')])
    run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN',
         str(src / 'packaging/macos/ResearchLab.spec'), '--distpath', str(output / 'dist.noindex'),
         '--workpath', str(output / 'build.noindex/pyinstaller')])
    app = output / 'dist.noindex/Research Lab Trial.app'
    run(['codesign', '--force', '--deep', '--sign', '-', str(app)])
    run(['codesign', '--verify', '--deep', '--strict', str(app)])
    # The source archive contains only verified inputs, never a developer's Git history or build cache.
    archive = output / f'ResearchLab-Trial-{tag}-source.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name, body in {**inputs, 'SOURCE-MANIFEST.json': manifest_bytes}.items():
            info = tarfile.TarInfo('research-lab/' + name)
            info.size = len(body)
            info.mode = int(manifest['files'].get(name, {}).get('mode', '100644'), 8) & 0o777
            tar.addfile(info, io.BytesIO(body))
    stage = output / 'build.noindex/dmg'
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / 'Applications').symlink_to('/Applications')
    shutil.copy2(src / 'MAC-TRIAL.md', stage / 'READ-ME-FIRST.md')
    shutil.copy2(archive, stage)
    (stage / 'dependency-sources').mkdir()
    for name, body in dependencies.items():
        (stage / 'dependency-sources' / name).write_bytes(body)
    (stage / 'dependency-sources/README.txt').write_text(
        'Official PyMuPDF and MuPDF sources matching the bundled 1.28.2 engines.\n'
        'Sources and SHA-256: application source mac-third-party-sources.json.\n'
        'For upstream build instructions see each archive README and PyMuPDF setup.py.\n'
        'Build MuPDF from the supplied source using PYMUPDF_SETUP_MUPDF_BUILD=/path/to/mupdf-source.\n'
        'Application build recipe and uv.lock accompany the application source archive.\n'
        'Application: AGPL-3.0-or-later. Public release approval and signed binary delivery remain separate.\n')
    dmg = output / f'ResearchLab-Trial-{tag}-arm64.dmg'
    run(['hdiutil', 'create', '-quiet', '-volname', 'Research Lab Trial', '-srcfolder', str(stage), '-format', 'UDZO', str(dmg)])
    shutil.rmtree(stage)
    lsregister = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
    subprocess.run([lsregister, '-u', str(app)], capture_output=True, check=False)
    deliver = {'source_commit': manifest['source_commit'], 'version': version, 'architecture': 'arm64',
               'macos_minimum': '14.0', 'signing': 'ad-hoc', 'notarized': False,
               'status': 'private-review-only', 'application_license': 'AGPL-3.0-or-later', 'missing_notices': report['missing_notices'],
               'dependency_sources': {name: hashlib.sha256(body).hexdigest() for name, body in dependencies.items()},
               'files': {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in (archive, dmg)}}
    (output / 'DELIVERY.json').write_text(json.dumps(deliver, ensure_ascii=False, indent=2) + '\n')
    return deliver


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--pdf-sources', type=Path, required=True, help='Directory containing the two pinned upstream source archives')
    args = parser.parse_args()
    print(json.dumps(build(args.source.resolve(), args.output.absolute(), args.pdf_sources.resolve()), indent=2))
