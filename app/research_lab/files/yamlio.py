"""Round-trip YAML and guarded atomic writes. ruamel keeps comments and quoting so the
hand-maintained _data/*.yml files diff by one line when the app edits them."""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def yaml_rt() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml(path: Path) -> Any:
    return yaml_rt().load(path.read_text(encoding="utf-8"))


def dump_yaml(data: Any) -> str:
    buf = io.StringIO()
    yaml_rt().dump(data, buf)
    text = buf.getvalue()
    # The repo writes nested lists as `  - item` but root-level lists at column 0; ruamel applies
    # one sequence offset to both, so a root sequence is dedented back by that offset.
    if isinstance(data, list):
        text = "\n".join(line[2:] if line.startswith("  ") else line for line in text.split("\n"))
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StaleFileError(RuntimeError):
    """The file changed on disk since it was rendered; refuse to overwrite user edits."""


_directory_locks: dict[Path, threading.RLock] = {}
_lock_guard = threading.Lock()
_lock_local = threading.local()


@contextmanager
def write_lock(directory: Path):
    """Coordinate app writers without locking an inode that a write replaces."""
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    with _lock_guard:
        lock = _directory_locks.setdefault(directory, threading.RLock())
    with lock:
        active = getattr(_lock_local, 'active', set())
        if directory in active:
            yield
            return
        fd = os.open(directory, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _lock_local.active = active | {directory}
            yield
        finally:
            _lock_local.active = active
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _stage(path: Path, body: bytes, prefix='.rl-') -> Path:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix='.tmp')
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(body)
        os.chmod(staged, 0o644)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged


def atomic_write_many(changes: list[tuple[Path, str, str | None]]) -> list[str]:
    """Commit sibling files with rollback on write failure; not a crash transaction."""
    if not changes:
        return []
    directory = changes[0][0].parent.resolve()
    if any(p.parent.resolve() != directory for p, _, _ in changes):
        raise ValueError('Batch writes must share a directory')
    if len({p.name for p, _, _ in changes}) != len(changes):
        raise ValueError('Duplicate batch path')
    with write_lock(directory):
        prepared, written, retained = [], [], set()
        try:
            for path, text, expected in changes:
                before = path.read_bytes() if path.exists() else None
                current = path.read_text(encoding='utf-8') if before is not None else ''
                # An empty expected value is the existing create-only contract.
                stale = before is not None if expected == '' else expected is not None and sha256_text(current) != expected
                if stale:
                    raise StaleFileError(f'{path.name} changed on disk since it was loaded')
                body = text.encode('utf-8')
                backup = _stage(path, before, '.rl-recovery-') if before is not None else None
                # Register backups before staging, so a failed allocation is cleaned up too.
                item = {'path': path, 'before': before, 'body': body, 'backup': backup, 'stage': None}
                prepared.append(item)
                item['stage'] = _stage(path, body)
            # Hand edits do not take the lock; check their bytes again before replacing anything.
            if any((i['path'].read_bytes() if i['path'].exists() else None) != i['before'] for i in prepared):
                raise StaleFileError('A batch file changed while preparing the write')
            for item in prepared:
                os.replace(item['stage'], item['path'])
                written.append(item)
        except Exception:
            rollback_error = None
            for item in reversed(written):
                path, backup = item['path'], item['backup']
                try:
                    if not path.exists() or path.read_bytes() != item['body']:
                        raise StaleFileError('External edit interrupted rollback; recovery backup retained')
                    if backup is None:
                        path.unlink()
                    else:
                        os.replace(backup, path)
                except Exception as error:
                    if backup is not None:
                        retained.add(backup)
                    rollback_error = error
            if rollback_error is not None:
                raise rollback_error
            raise
        finally:
            for item in prepared:
                for tmp in (item['stage'], item['backup']):
                    if tmp is not None and tmp not in retained:
                        tmp.unlink(missing_ok=True)
        return [sha256_text(text) for _, text, _ in changes]


def atomic_write(path: Path, text: str, expected_sha: str | None = None) -> str:
    return atomic_write_many([(path, text, expected_sha)])[0]


def save_yaml(path: Path, data: Any, expected_sha: str | None = None) -> str:
    return atomic_write(path, dump_yaml(data), expected_sha)
