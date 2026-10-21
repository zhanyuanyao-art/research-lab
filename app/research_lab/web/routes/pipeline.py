"""Research pipeline: every project's stage, the movement rules, and the read-only archive."""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from ...files import datafiles
from ... import i18n
from ...method import milestones
from ..context import get_conn, render

router = APIRouter()


def movement_rules() -> list[dict[str, str]]:
    rules: dict[str, list[str]] = {}
    for mid, title, gates, _t, _e in milestones.LADDER:
        rules.setdefault(gates, []).append(f"{mid} {i18n.MILESTONE.get(mid, title)}")
    keys = milestones.stage_keys()
    out = []
    for i, k in enumerate(keys[:-1]):
        gating = rules.get(k)
        out.append({"from": k, "to": keys[i + 1],
                    "rule": ("当 " + "、".join(gating) + " 全部通过时：应用提出建议，由你确认") if gating
                    else "由你决定；这一步没有里程碑把关"})
    return out


@router.get("/pipeline")
def pipeline(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    keys = milestones.stage_keys()
    rows = []
    for p in datafiles.read("projects") or []:
        slug = str(p["slug"])
        rows.append({**dict(p), "stage_index": keys.index(p["stage"]) if p["stage"] in keys else -1,
                     "next": milestones.next_unmet(conn, slug), "suggestion": milestones.stage_suggestion(conn, slug)})
    return render(request, conn, "pages/pipeline.html", rows=rows, rules=movement_rules(),
                  archive=[dict(a) for a in (datafiles.read("archive") or [])], title="研究管线")
