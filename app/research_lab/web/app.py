"""FastAPI application factory. Routers live in research_lab/web/routes/<name>.py and expose
`router`; missing modules are skipped so modules can be added independently."""
from __future__ import annotations

import importlib
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..config import PORT, get_paths
from ..db.connection import connect, migrate
from .. import feeds  # noqa: F401 - registers the feeds.enrich job before the Claude Code worker can meet one
from ..feeds import scheduler as _feeds_scheduler
from ..pm import scheduler as _pm_scheduler
from .middleware import MutationGuard

ROUTERS = ("research", "today", "papers", "excerpts", "week", "projects", "artifacts", "harvest", "agent", "search",
           "settings", "partials", "now", "pipeline", "digest", "chat", "data", "mcp", "dev", "feed", "tasks", "issues",
           "imports", "replicate", "missing", "run_all")


def create_app(port: int | None = None) -> FastAPI:
    port = port or int(os.environ.get("RL_PORT") or PORT)
    paths = get_paths()
    paths.ensure_dirs()
    conn = connect()
    migrate(conn)
    from ..paper_collections import migrate_legacy
    migrate_legacy(conn)
    from ..agent import drivers, tasks
    from ..ai import claude_code, lease
    drivers.BASE_URL["value"] = f"http://127.0.0.1:{port}"  # the MCP endpoint tasks call back into
    lease.PORT = port
    tasks.recover(conn)
    # RL_AI_WORKER=0 (the tests) keeps this process from hosting the background worker: a worker left
    # running by one test's app would otherwise pick up another test's jobs.
    if os.environ.get("RL_AI_WORKER", "1") != "0":
        claude_code.WORKER.hosted = True
        claude_code.WORKER.kick()  # take the lease if free, drain what was queued while no process ran it
    conn.close()
    app = FastAPI(title="Research Lab", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(MutationGuard, port=port)
    app.mount("/static", StaticFiles(directory=str(paths.web_static)), name="static")
    if paths.site_assets.exists():
        app.mount("/site-assets", StaticFiles(directory=str(paths.site_assets)), name="site-assets")
    from ..distribution import is_trial
    for name in ROUTERS:
        if is_trial() and name in {"dev", "digest"}:
            continue
        try:
            mod = importlib.import_module(f"research_lab.web.routes.{name}")
        except ModuleNotFoundError as e:
            if e.name != f"research_lab.web.routes.{name}":
                raise
            continue
        app.include_router(mod.router)
    _feeds_scheduler.install(app)  # six-hourly 推送 refresh in a background thread (off when RL_FEEDS_AUTO=0)
    _pm_scheduler.install(app)     # 周五进度 once a week, no model call (off when RL_PM_AUTO=0)
    return app
