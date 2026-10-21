"""Batch YAML changes preserve preimages on failure and coordinate app writers."""
from pathlib import Path
import os
import subprocess
import sys
import threading

import pytest
from research_lab.files import yamlio as Y


def test_batch_preflight_rejects_stale_or_missing_without_writing(tmp_path):
    a, b = tmp_path / 'a.yml', tmp_path / 'b.yml'
    a.write_text('old')
    with pytest.raises(Y.StaleFileError):
        Y.atomic_write_many([(a, 'new', Y.sha256_text('old')), (b, 'new', Y.sha256_text('gone'))])
    assert a.read_text() == 'old' and not b.exists()
    assert not list(tmp_path.glob('.rl-*'))


def test_batch_failure_rolls_back_written_file_without_allocating_again(tmp_path, monkeypatch):
    a, b = tmp_path / 'a.yml', tmp_path / 'b.yml'
    a.write_text('a'); b.write_text('b')
    original = Y.os.replace
    def fail(src, dst):
        if dst == b:
            monkeypatch.setattr(Y.tempfile, 'mkstemp', lambda **kw: (_ for _ in ()).throw(OSError('full disk')))
            raise OSError('failed second write')
        return original(src, dst)
    monkeypatch.setattr(Y.os, 'replace', fail)
    with pytest.raises(OSError):
        Y.atomic_write_many([(a, 'new a', None), (b, 'new b', None)])
    assert a.read_text() == 'a' and b.read_text() == 'b'
    assert not list(tmp_path.glob('.rl-*'))


def test_external_edit_is_not_overwritten_and_recovery_copy_survives(tmp_path, monkeypatch):
    a, b = tmp_path / 'a.yml', tmp_path / 'b.yml'
    a.write_text('original a'); b.write_text('original b')
    original = Y.os.replace
    def fail(src, dst):
        if dst == b:
            a.write_text('external edit')
            raise OSError('second write')
        return original(src, dst)
    monkeypatch.setattr(Y.os, 'replace', fail)
    with pytest.raises(Y.StaleFileError, match='recovery backup retained'):
        Y.atomic_write_many([(a, 'new a', None), (b, 'new b', None)])
    assert a.read_text() == 'external edit' and b.read_text() == 'original b'
    backups = list(tmp_path.glob('.rl-recovery-*'))
    assert len(backups) == 1 and backups[0].read_text() == 'original a'


def test_directory_lock_is_reentrant_and_blocks_other_processes(tmp_path):
    path, signal = tmp_path / 'a.yml', tmp_path / 'started'
    path.write_text('old')
    code = "from pathlib import Path; from research_lab.files.yamlio import atomic_write; import sys; Path(sys.argv[2]).touch(); atomic_write(Path(sys.argv[1]), 'child')"
    proc = None
    try:
        with Y.write_lock(tmp_path):
            with Y.write_lock(tmp_path):
                Y.atomic_write(path, 'parent')
                proc = subprocess.Popen([sys.executable, '-c', code, str(path), str(signal)], env=os.environ.copy())
                import time
                until = time.monotonic() + 3
                while not signal.exists() and time.monotonic() < until:
                    threading.Event().wait(.01)
                assert signal.exists() and proc.poll() is None and path.read_text() == 'parent'
        assert proc.wait(timeout=3) == 0
        assert path.read_text() == 'child'
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill(); proc.wait()


def test_create_only_sentinel_remains_compatible(tmp_path):
    path = tmp_path / 'new.md'
    assert Y.atomic_write(path, 'accepted draft', '') == Y.sha256_text('accepted draft')
    with pytest.raises(Y.StaleFileError):
        Y.atomic_write(path, 'overwrite', '')
    assert path.read_text() == 'accepted draft'
    path.write_text('')
    with pytest.raises(Y.StaleFileError):
        Y.atomic_write(path, 'overwrite empty existing', '')
