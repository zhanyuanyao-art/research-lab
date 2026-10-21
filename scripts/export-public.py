#!/usr/bin/env python3
"""Export a fixed source snapshot for private review, never the research workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

PREFIXES = ('app/research_lab/', 'app/tests/', 'scripts/vendor/editor/')
EXACT = ('packaging/macos/ResearchLab.spec', 'packaging/macos/build.sh', 'packaging/macos/install.sh',
         'packaging/macos/trial.py', 'packaging/macos/licenses.py', 'packaging/macos/launcher.py', 'packaging/macos/make_icon.py', 'pyproject.toml', 'uv.lock', '.python-version', 'scripts/export-public.py',
         'scripts/agent-worktree.sh', 'scripts/app-copies.sh', 'scripts/new-artifact.sh', 'scripts/serve.sh',
         'templates/artifact.md', 'templates/harvest.md', 'templates/project/index.md',
         'templates/project/literature.md', 'templates/project/evidence.md', 'docs/APP-GUIDE.md')
DOCUMENTS = {'LICENSE': 'LICENSE', 'README.md': 'README.md', 'CONTRIBUTING.md': 'CONTRIBUTING.md', 'SECURITY.md': 'SECURITY.md',
             'REVIEW.md': 'REVIEW.md', 'AGENTS.md': 'AGENTS.md', 'DEV.md': 'dev/README.md',
             'MAC-TRIAL.md': 'MAC-TRIAL.md', 'LICENSE-DECISION.md': 'LICENSE-DECISION.md',
             'THIRD-PARTY.md': 'THIRD-PARTY.md', 'CODE_OF_CONDUCT.md': 'CODE_OF_CONDUCT.md',
             'PREVIEW.md': 'PREVIEW.md', 'bug-report.yml': '.github/ISSUE_TEMPLATE/bug-report.yml',
             'feature-request.yml': '.github/ISSUE_TEMPLATE/feature-request.yml',
             'MAC-THIRD-PARTY-NOTICES.txt': 'MAC-THIRD-PARTY-NOTICES.txt', 'mac-third-party-sources.json': 'mac-third-party-sources.json',
             'THIRD-PARTY-NOTICES.txt': 'THIRD-PARTY-NOTICES.txt', 'third-party-sources.json': 'third-party-sources.json', 'gitignore.txt': '.gitignore', 'ci.yml': '.github/workflows/test.yml'}
# README screenshots ship next to it; PNG only, so nothing else rides along under that folder.
SCREENSHOTS = ('docs/public/screenshots/', 'docs/screenshots/')
FORBIDDEN = {'.git', '.rl-content-root', '.venv', 'node_modules', '__pycache__', 'private'}
CREDENTIALS = re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{30,}'
                         r'|\bgithub_pat_[A-Za-z0-9_]{30,}|\bAKIA[0-9A-Z]{16}'
                         r'|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')
# Exact synthetic scanner fixtures may resemble credentials; paths alone never bypass the scan.
FIXTURE_HASHES = {'app/tests/test_autofix.py': ['03aafb028d538b06c4341bf8cf2a692a3c6d1f36e34e0e0b376f407c6117a246', '03d104c669e3c7b6be7f989db8b12c8b910d3be8c1e2a73c9369d3cc0ba803b5', '1a5d44a2dca19669d72edf4c4f1c27c4c1ca4b4408fbb17f6ce4ad452d78ddb3', '8bc690a41e6eef76b08ec8a7d01cab87039f0406b48ff4947e2e9f6a72136fae', '922000e9bdfb3a96f37a9b20e358b805ae2b0696e86147058e0ed3586feeea4d', '949a0eca02907f675812d79d696689f6ef9bc0742afcbc06fef6493158669911', 'a3f1936b6daf8a551285e79e76a4b9be8e3726f1ca026625ba6175b8ed3c3fa5'], 'app/tests/test_check_report.py': ['8bc690a41e6eef76b08ec8a7d01cab87039f0406b48ff4947e2e9f6a72136fae', 'a3f1936b6daf8a551285e79e76a4b9be8e3726f1ca026625ba6175b8ed3c3fa5']}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout


def selected(path: str) -> bool:
    if path.startswith(SCREENSHOTS[0]):
        return path.endswith('.png') and '/' not in path.removeprefix(SCREENSHOTS[0])
    return path in EXACT or path.startswith(PREFIXES) or path in {'docs/public/' + p for p in DOCUMENTS}


def validate(path: str, mode: str, body: bytes) -> None:
    parts = PurePosixPath(path).parts
    if mode not in ('100644', '100755') or any(p in FORBIDDEN for p in parts):
        raise ValueError(f'Unsupported file: {path}')
    if any(p.startswith('.env') for p in parts) or re.search(r'\.(?:sqlite3?|db|duckdb)(?:-(?:wal|shm|journal)|\.wal)?$|\.(?:wal|pyc)$', path, re.I):
        raise ValueError(f'Private file type: {path}')
    for match in CREDENTIALS.finditer(body.decode('utf-8', errors='replace')):
        digest = hashlib.sha256(match.group().encode()).hexdigest()
        if digest not in FIXTURE_HASHES.get(path, ()):
            # A failed scan reports locations, never the matched value.
            raise ValueError(f'Credential pattern requires review: {path}')


def export(repo: Path, target: Path, ref: str = 'HEAD') -> dict:
    commit = git(repo, 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()
    files = {}
    for entry in git(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        path = raw_path.decode()
        if not selected(path):
            continue
        if kind != 'blob':
            raise ValueError(f'Unsupported object: {path}')
        body = git(repo, 'cat-file', 'blob', oid)
        validate(path, mode, body)
        if path.startswith(SCREENSHOTS[0]):
            name = SCREENSHOTS[1] + path.removeprefix(SCREENSHOTS[0])
        elif path.startswith('docs/public/'):
            name = DOCUMENTS[path.removeprefix('docs/public/')]
        else:
            name = path
        files[name] = (mode, body)
    required = {'pyproject.toml', 'app/research_lab/__init__.py', *DOCUMENTS.values()}
    if missing := required - files.keys():
        raise ValueError('Missing candidate files: ' + ', '.join(sorted(missing)))
    manifest = {'source_commit': commit, 'status': 'private-review-only', 'files': {
        p: {'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body), 'mode': mode}
        for p, (mode, body) in sorted(files.items())}}
    # A new folder is mandatory; never replace an existing candidate or workspace.
    target.mkdir(parents=True, exist_ok=False)
    try:
        for name, (mode, body) in files.items():
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)
            dest.chmod(0o755 if mode == '100755' else 0o644)
        (target / 'SOURCE-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
        git(target, 'init', '-q', '-b', 'main')
        git(target, 'add', '.')
        git(target, '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Research Lab Export',
            '-c', 'user.email=export@example.invalid', '-c', 'commit.gpgsign=false',
            'commit', '-qm', 'Prepare source snapshot for private review')
    except Exception:
        shutil.rmtree(target)
        raise
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--ref', default='HEAD')
    args = parser.parse_args()
    try:
        report = export(args.repo, args.target, args.ref)
    except (ValueError, FileExistsError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'{exc}\n')
    print(f"Exported {len(report['files'])} files from {report['source_commit'][:12]} for private review.")
