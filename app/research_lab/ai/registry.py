"""Job registry. Job modules under research_lab/ai/jobs register themselves on import."""
from __future__ import annotations

import importlib
import pkgutil

from .provider import JobSpec

_JOBS: dict[str, JobSpec] = {}
_loaded = False


def register(spec: JobSpec) -> JobSpec:
    _JOBS[spec.kind] = spec
    return spec


def load_all() -> dict[str, JobSpec]:
    global _loaded
    if not _loaded:
        from . import jobs as jobs_pkg
        for m in pkgutil.iter_modules(jobs_pkg.__path__):
            importlib.import_module(f"{jobs_pkg.__name__}.{m.name}")
        _loaded = True
    return dict(_JOBS)


def get(kind: str) -> JobSpec:
    load_all()
    return _JOBS[kind]
