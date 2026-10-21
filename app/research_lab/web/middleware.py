"""Localhost hardening: trusted Host header and an HTMX/Origin check on every mutation."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from ..config import HOST, PORT


def allowed_origins(port: int) -> set[str]:
    return {f"http://{HOST}:{port}", f"http://localhost:{port}"}


class MutationGuard(BaseHTTPMiddleware):
    """A cross-site form cannot add the HX-Request header or spoof Origin, so this blocks CSRF and
    DNS-rebinding without per-form tokens. GETs never change state."""

    def __init__(self, app, port: int = PORT):
        super().__init__(app)
        self.port = port

    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        if host.split(":")[0] not in (HOST, "localhost"):
            return PlainTextResponse("forbidden host", status_code=403)
        # /mcp authenticates each call with a per-task bearer token (routes/mcp.py).
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path != "/mcp":
            origin = request.headers.get("origin")
            hx = request.headers.get("hx-request") == "true"
            if not hx and (origin is None or origin not in allowed_origins(self.port)):
                return PlainTextResponse("mutation must come from the app", status_code=403)
        return await call_next(request)
