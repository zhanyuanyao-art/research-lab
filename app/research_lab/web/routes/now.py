from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ... import weekly
from ...files.yamlio import StaleFileError
from ..context import get_conn, render

router = APIRouter()


def _fields(now: dict) -> list[dict]:
    out = []
    for key, value in now.items():
        is_list = isinstance(value, list)
        text = "\n".join(str(x) for x in value) if is_list else ("" if value is None else str(value))
        out.append({"key": key, "is_list": is_list, "value": text, "long": is_list or len(text) > 70})
    return out


@router.get("/now")
def now_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    now, sha = weekly.now_form()
    return render(request, conn, "pages/now.html", title="Now", fields=_fields(now), now=now, sha=sha,
                  saved=request.query_params.get("saved"))


@router.post("/now")
async def now_save(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    form = await request.form()
    submitted = {k[2:]: str(v) for k, v in form.items() if k.startswith("f_")}
    try:
        _, changed = weekly.save_now(submitted, str(form.get("sha", "")))
    except (StaleFileError, weekly.WeeklyError) as e:
        now, sha = weekly.now_form()
        fields = _fields(now)
        for f in fields:
            if f["key"] in submitted:
                f["value"] = submitted[f["key"]]
        stale = isinstance(e, StaleFileError)
        proposed = weekly.now_changes(now, submitted)
        resp = render(request, conn, "pages/now.html", title="Now", fields=fields, now=now, sha=sha,
                      error=("本页加载后，磁盘上的 now.yml 被改动了，没有写入任何内容。表单保留了你的文本，"
                             "预览显示的是文件的当前内容。" if stale else str(e)),
                      pending=proposed if stale else None)
        resp.status_code = 409 if stale else 400
        return resp
    return RedirectResponse(f"/now?saved={','.join(changed) or 'nothing'}", status_code=303)
