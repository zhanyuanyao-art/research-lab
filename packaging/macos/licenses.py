"""Collect installed dependency notices without importing application modules."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys
import sysconfig
import tomllib


def canonical(name: str) -> str:
    return re.sub(r'[-_.]+', '-', name).lower()


def notice(path: str) -> bool:
    name = Path(path).name.lower()
    return bool(re.search(r'(?:^|[._-])(?:licen[cs]es?|copying|copyright|notices?)(?:[._-]|$)', name)) and not name.endswith(('.so', '.yml', '.dylib', '.pyc', '.py'))


def collect(source: Path, target: Path) -> dict:
    target.mkdir(parents=True, exist_ok=False)
    lock = tomllib.loads((source / 'uv.lock').read_text())
    pinned = {canonical(p['name']): p for p in lock['package']}
    packages, missing = [], []
    supplement = json.loads((source / 'mac-third-party-sources.json').read_text())
    raw_notices = (source / 'MAC-THIRD-PARTY-NOTICES.txt').read_bytes()
    for row in supplement['notices']:
        body = raw_notices[row['offset']:row['offset'] + row['bytes']]
        if hashlib.sha256(body).hexdigest() != row['sha256']:
            raise ValueError('Supplemental notice hash mismatch')
    build = Path(sys.base_prefix) / 'BUILD'
    runtime = {'version': sys.version.split()[0], 'standalone_build': build.read_text().strip() if build.exists() else None}
    if runtime != supplement['runtime']:
        raise ValueError('Runtime changed; review native dependency notices first')

    def save(path: str, body: bytes) -> dict:
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        return {'path': path, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body)}

    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        name = canonical(dist.metadata['Name'])
        if name == 'research-lab':
            continue
        pin = pinned.get(name)
        if pin is None or dist.version != pin['version']:
            raise ValueError(f'Build environment differs from uv.lock: {name} {dist.version}')
        prefix = f'python/{name}-{dist.version}'
        record = {'name': name, 'version': dist.version,
                  'license': dist.metadata.get('License-Expression') or dist.metadata.get('License'),
                  'upstream': dist.metadata.get_all('Project-URL') or [dist.metadata.get('Home-page')],
                  'sdist': pin.get('sdist'), 'files': []}
        for item in dist.files or ():
            # Preserve the relative path, since bundled vendors often share the name LICENSE.
            if (notice(str(item)) or str(item).endswith('pdfcorefonts/readme.txt')) and '..' not in Path(str(item)).parts:
                f = Path(dist.locate_file(item))
                if f.is_file():
                    record['files'].append(save(f'{prefix}/{item}', f.read_bytes()))
        record['supplemental_notices'] = [i for i, row in enumerate(supplement['notices'])
                                             if row.get('package') == name and row.get('version', dist.version) == dist.version]
        if not record['files'] and not record['supplemental_notices']:
            missing.append(name)
        packages.append(record)

    save('python-runtime/LICENSE.txt', (Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt').read_bytes())
    frontend = []
    for path in sorted((source / 'app/research_lab/web/static').rglob('*')):
        if path.is_file() and notice(path.name):
            frontend.append(save('frontend/' + str(path.relative_to(source / 'app/research_lab/web/static')), path.read_bytes()))
    for name in ('LICENSE', 'MAC-THIRD-PARTY-NOTICES.txt', 'mac-third-party-sources.json', 'THIRD-PARTY-NOTICES.txt', 'third-party-sources.json', 'THIRD-PARTY.md', 'LICENSE-DECISION.md'):
        save(name, (source / name).read_bytes())
    if missing:
        raise ValueError('Missing dependency notices: ' + ', '.join(missing))
    report = {'status': 'private-review-only', 'python': sys.version.split()[0],
              'scope': 'Build-environment superset; bundle-modules.json records the actual PyInstaller payload.',
              'packages': packages, 'frontend': frontend, 'missing_notices': missing,
              'application_license': 'AGPL-3.0-or-later',
              'release_gates': ['Final public release approval', 'PyMuPDF/MuPDF corresponding source with AGPL binaries', 'Developer ID / notarization']}
    (target / 'inventory.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report
