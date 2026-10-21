"""harvest.review: which artifacts of a period connect, and whether pipeline movement rules are met.

apply() stores proposed artifact↔artifact associations only; the stage move stays a user action
on the project page.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from ...config import get_paths
from ...db.connection import now_iso
from ...files import artifacts, datafiles, harvests
from ...files.yamlio import dump_yaml
from .. import registry
from ..provider import ChatRequest, JobSpec, system_rules

SECTIONS = ("What did I figure out", "Claim", "Next question")
FRONT_KEYS = ("ref", "title", "week", "type", "level", "project", "status", "version", "related")

# Condensed from docs/design/method-layer.md §1 (the milestone ladder that gates stage moves).
MOVEMENT_RULES = """\
A project's stage moves forward only when every milestone gating that stage is done; the user confirms the move.
- exploring (M1 Terrain surveyed): >=12 papers in the literature map over >=3 camps; a landscape artifact at developed.
- exploring -> puzzle (M2 Puzzle stated): a puzzle artifact naming the pattern, >=3 dated observations each linked to an excerpt, and the expectation it violates.
- puzzle (M3 Literature map v1): >=20 papers, every camp >=3 papers, >=4 contests edges; literature-map artifact.
- puzzle (M4 Concept fixed v0): concept artifact with >=4 sourced usages, a working definition with named dimensions and indicators.
- puzzle -> evidence (M5 Question and rivals): a question artifact plus >=3 rival explanations and the user's own, each with >=2 observable implications; research-design seed.
- evidence (M6 Observation frame): case-map artifact with 8-15 dated observations and a selection rule.
- evidence -> argument (M7 Evidence coverage): evidence artifact; matrix coverage >=70% of priority cells; every rival has an attempted hoop test; >=1 contradicting cell or a searched-none log.
- argument (M8 First empirical check): analysis artifact from a reproducible check.
- argument (M9 Mechanism and strongest rival): mechanism artifact with >=3 excerpt-backed links; counterargument artifact on the best-scoring rival.
- argument -> draft (M10 Design defended): research-design at developed; an advisor-question artifact whose answer is logged.
- draft (M11 Paper-ready package): >=3 paper-ready artifacts (lit-review passage, case section, table or figure).
"""


class Connection(BaseModel):
    a: str
    b: str
    why: str = Field(max_length=500)


class PipelineCheck(BaseModel):
    project: str
    rule: str
    met: bool
    missing: str = ""


class HarvestReviewResult(BaseModel):
    connections: list[Connection] = Field(default_factory=list, max_length=3)
    pipeline: list[PipelineCheck] = Field(default_factory=list)
    suggested_next: list[str] = Field(default_factory=list, max_length=4)


def period_artifacts(conn: sqlite3.Connection, period: str) -> list[dict[str, Any]]:
    paths = get_paths()
    artifacts.index(conn, paths)
    start, end = harvests.period_range(period)
    out = []
    for row in harvests.covered_artifacts(conn, start, end, counting_only=True):
        path = paths.root / row["path"]
        if not path.exists():
            continue
        a = artifacts.load(path)
        item: dict[str, Any] = {k: (list(map(str, a.get(k) or [])) if k == "related" else str(a.get(k, ""))) for k in FRONT_KEYS}
        for sec in SECTIONS:
            text = artifacts.section_body(a.body, sec)
            item[sec] = (text or "").strip()[:1500]
        out.append(item)
    return out


def _params_from_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?",
                       (run_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0]).get("params") or {}
    except ValueError:
        return {}


class HarvestReview(JobSpec):
    kind = "harvest.review"
    title = "Connections and pipeline check for a harvest period"
    Result = HarvestReviewResult
    target_type = "harvest"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        harvests.kind_of(target_id)
        items = period_artifacts(conn, target_id)
        refs = [i["ref"] for i in items]
        projects = datafiles.read("projects") or []
        start, end = harvests.period_range(target_id)
        user = "\n".join([
            f"Task: review the user's {harvests.kind_of(target_id)} harvest {target_id} ({start} to {end}).",
            "",
            "## Counting artifacts in the period (the ONLY refs you may use)",
            json.dumps(items, ensure_ascii=False, indent=1),
            "",
            "## projects.yml",
            dump_yaml(projects) if projects else "(none)",
            "",
            "## Stage movement rules",
            MOVEMENT_RULES,
            "Rules:",
            "- connections: at most 3 pairs of refs from the list above that started to connect, with one or two "
            "sentences on why, based only on their text. Fewer (or none) if the material does not support it.",
            "- pipeline: for each project with artifacts here, the next movement rule, whether it is met, and what is missing.",
            "- suggested_next: at most 4 artifacts worth writing next month, each one line (type: topic).",
            "",
            "Return a json object matching this schema:",
            json.dumps(HarvestReviewResult.model_json_schema(), ensure_ascii=False),
            "Example json:",
            json.dumps(self.example(), ensure_ascii=False),
        ])
        req = ChatRequest(messages=[{"role": "system", "content": system_rules()}, {"role": "user", "content": user}],
                          max_tokens=2500, params={"period": target_id, "refs": refs})
        return req, []

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        assert isinstance(result, HarvestReviewResult)
        params = _params_from_run(conn, run_id)
        allowed = set(params.get("refs") or [i["ref"] for i in period_artifacts(conn, target_id)])
        known = {r[0] for r in conn.execute("SELECT ref FROM artifacts")}
        allowed &= known
        kept, seen = [], set()
        for c in result.connections:
            pair = frozenset((c.a, c.b))
            if c.a in allowed and c.b in allowed and c.a != c.b and pair not in seen:
                kept.append(c)
                seen.add(pair)
        result.connections = kept
        slugs = {str(p.get("slug")) for p in datafiles.read("projects") or []}
        result.pipeline = [p for p in result.pipeline if p.project in slugs]
        for c in kept:
            conn.execute(
                "INSERT OR IGNORE INTO associations(src_type, src_id, rel, dst_type, dst_id, origin, status, reason, ai_run_id, created_at) "
                "VALUES ('artifact', ?, 'related', 'artifact', ?, 'ai-suggested', 'proposed', ?, ?, ?)",
                (c.a, c.b, c.why, run_id, now_iso()))

    def example(self) -> dict[str, Any]:
        return {
            "connections": [{"a": "ART-2026-4001", "b": "ART-2026-4201",
                             "why": "Both locate centrality in rules rather than participation; 4201's counterexample tests 4001's claim."}],
            "pipeline": [{"project": "asean-centrality", "rule": "exploring -> puzzle (M2)", "met": False,
                          "missing": "A puzzle artifact with three dated observations."}],
            "suggested_next": ["puzzle: chair rule survives falling attendance", "counterargument: great-power acceptance"],
        }


registry.register(HarvestReview())
