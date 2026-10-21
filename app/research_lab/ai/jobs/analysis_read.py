"""analysis.read: a one-paragraph reading draft, caveats and next checks for an analysis run.

Built from the run's result tables (never the raw rows) and its replication plan. Stored in
`analysis_runs.ai_reading` only; the user's `reading`, the result and any check stay untouched."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...db.connection import now_iso
from ..provider import ChatRequest, JobSpec, system_rules
from ..registry import register

MAX_ROWS = 30


class Result(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reading: str = Field(min_length=1, max_length=2500)
    caveats: list[str] = Field(default_factory=list, max_length=8)
    next_checks: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("caveats", "next_checks", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]


def _tables_text(result: dict[str, Any]) -> str:
    out = []
    for t in result.get("tables", []):
        out.append(f"### {t.get('title')} (n={t.get('n')}, missing/dropped={t.get('missing')})")
        if t.get("note"):
            out.append(f"note: {t['note']}")
        out.append(" | ".join(map(str, t.get("columns", []))))
        for row in t.get("rows", [])[:MAX_ROWS]:
            out.append(" | ".join("" if v is None else str(v) for v in row))
        if len(t.get("rows", [])) > MAX_ROWS:
            out.append(f"… {len(t['rows']) - MAX_ROWS} more rows")
        out.append("")
    return "\n".join(out)


def _plan_text(conn: sqlite3.Connection, plan_id: str | None) -> str:
    if not plan_id:
        return "(no replication plan attached)"
    from ...replication import plans
    p = plans.get(conn, plan_id)
    if not p:
        return "(plan not found)"
    pl = p["plan"]
    vars_ = "; ".join(f"{v['role']}: {v['name']} ({v.get('measure') or '?'})" for v in pl.get("variables", []))
    return (f"Plan {plan_id} for paper {p.get('paper_title') or p.get('paper_id') or '—'} (status {p['status']}, origin {p['origin']})\n"
            f"unit: {pl.get('unit_of_analysis') or '—'}; period: {pl.get('period') or '—'}\n"
            f"variables: {vars_ or '—'}\nmodel: {pl.get('model', {}).get('family')} — {pl.get('model', {}).get('specification') or '—'}\n"
            f"sample rule: {pl.get('sample_rule') or '—'}")


class AnalysisRead(JobSpec):
    kind = "analysis.read"
    title = "Reading draft for an analysis run"
    Result = Result
    target_type = "run"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        row = conn.execute("SELECT * FROM analysis_runs WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise LookupError(f"no run {target_id}")
        if not row["result_json"]:
            raise LookupError(f"run {target_id} has no result")
        result = json.loads(row["result_json"])
        if result.get("error"):
            raise LookupError(f"run {target_id} ended with an error")
        summary = json.dumps(result.get("summary", {}), ensure_ascii=False)
        notes = "\n".join(f"- {n}" for n in result.get("notes", [])) or "- (none)"
        user = (
            f"Analysis run {target_id}: {result.get('method_label')} on dataset {result.get('dataset', {}).get('id')} "
            f"({result.get('dataset', {}).get('name')}).\nSettings: {result.get('params_label')}\n"
            f"Summary: {summary}\nRun notes:\n{notes}\n\nResult tables:\n{_tables_text(result)}\n"
            f"Replication plan context:\n{_plan_text(conn, row['plan_id'])}\n\n"
            "Task: draft a one-paragraph plain reading of these numbers for the user to compare with their own. "
            "Report only what the tables show (direction, size, uncertainty, n and missing data); do not say the "
            "hypothesis is confirmed or rejected, do not give a process-tracing verdict, and do not add facts about "
            "cases or literature. `caveats`: statistical limits visible here (sample size, listwise deletion, "
            "specification, multiple comparisons, causal language to avoid). `next_checks`: concrete robustness or "
            "diagnostic runs the user could do with these methods. Write in Simplified Chinese (this job overrides the "
            "English rule); keep variable names as they appear."
        )
        req = ChatRequest(messages=[{"role": "system", "content": system_rules()}, {"role": "user", "content": user}],
                          max_tokens=1500)
        return req, []

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        payload = {**result.model_dump(), "ai_run_id": run_id, "created_at": now_iso()}
        cur = conn.execute("UPDATE analysis_runs SET ai_reading = ? WHERE id = ?",
                           (json.dumps(payload, ensure_ascii=False), target_id))
        if cur.rowcount == 0:
            raise LookupError(f"no run {target_id}")

    def example(self) -> dict[str, Any]:
        return {"reading": "在 118 个国家—年观测中（因缺失删除 12 个），竞争强度每增加一个单位，出席概率的对数几率平均提高 0.42，"
                           "95% 置信区间为 [0.10, 0.74]，不包含零；模型解释力有限（伪 R² = 0.08）。",
                "caveats": ["样本量较小，置信区间较宽", "列表删除可能改变样本构成", "系数是关联而非因果效应"],
                "next_checks": ["改用线性概率模型比较", "加入年份固定效应", "检查被删除观测是否集中在特定年份"]}


register(AnalysisRead())
