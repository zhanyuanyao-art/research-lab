"""methods.replicate: a structured replication plan from a paper's methods / data / design text.

The result lands as a `replication_plans` row with origin 'ai-suggested' and status 'draft'; it counts
only after the user edits it and marks it reviewed. The schema has no place for results or quotes."""
from __future__ import annotations

import sqlite3
from typing import Any

from pydantic import BaseModel

from ...replication import plans
from ..provider import ChatRequest, JobSpec
from ..registry import register


class MethodsReplicate(JobSpec):
    kind = "methods.replicate"
    title = "Replication plan from a paper's methods section"
    Result = plans.Result
    target_type = "paper"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        return plans.build(conn, target_id)

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        plans.apply(result, run_id, conn, target_id)  # type: ignore[arg-type]

    def example(self) -> dict[str, Any]:
        return plans.EXAMPLE


register(MethodsReplicate())
