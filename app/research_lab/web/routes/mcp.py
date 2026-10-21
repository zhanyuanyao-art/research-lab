"""Local MCP endpoint (streamable HTTP, JSON responses) serving the lab tools to a running task.

Only a task's own bearer token opens it, and only that skill's tools are listed or callable; the
token disappears when the task ends. The mutation guard lets /mcp through because browsers cannot
forge the Authorization header cross-site; this route checks the token instead.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ...agent import tasks, tools
from ...config import APP_VERSION
from ...db.connection import connect

router = APIRouter()

PROTOCOL = "2025-06-18"


def _ok(rid: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}, status_code=status)


@router.get("/mcp")
def mcp_get() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request):
    auth = request.headers.get("authorization", "")
    ctx = tasks.ACTIVE.get(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
    if ctx is None:
        return _err(None, -32001, "unauthorized", status=401)
    try:
        msg = await request.json()
    except ValueError:
        return _err(None, -32700, "parse error", status=400)
    if isinstance(msg, list):  # batches are not used by Claude Code; answer one at a time
        return _err(None, -32600, "batch requests are not supported", status=400)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:  # notifications (initialized, cancelled, …) need no answer
        return Response(status_code=202)
    if method == "initialize":
        return _ok(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                         "capabilities": {"tools": {"listChanged": False}},
                         "serverInfo": {"name": "research-lab", "version": APP_VERSION}})
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        listed = [tools.TOOLS[n] for n in ctx.skill.tools if n in tools.TOOLS]
        return _ok(rid, {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema} for t in listed]})
    if method == "tools/call":
        conn = connect()
        try:
            text, is_error = tasks.execute_tool(conn, ctx, str(params.get("name") or ""), params.get("arguments") or {})
        finally:
            conn.close()
        return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})
    return _err(rid, -32601, f"method not found: {method}")
