"""checks.derive: propose up to three empirical checks for a claim (or an artifact's claim).

Output lands as `checks` rows with origin 'ai-suggested' and status 'planned'; the UI shows
them dashed until the user accepts or discards them. No facts, no sources, no results."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from ...db.connection import now_iso
from ..provider import SYSTEM_RULES, ChatRequest, JobSpec
from ..registry import register


class DerivedCheck(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    prediction: str = Field(min_length=10, description="What the claim predicts, stated before looking at the result")
    data_needed: str = Field(description="Data or document class needed, and where it could come from")
    method: str = Field(description="How to confront the prediction with the data (comparison, process-tracing test, network measure)")
    would_weaken_if: str = Field(description="Which observation would weaken the claim")


class Result(BaseModel):
    checks: list[DerivedCheck] = Field(default_factory=list, max_length=3)


def _artifact_target(conn: sqlite3.Connection, ref: str) -> dict[str, Any]:
    from ...files.artifacts import section_body
    row = conn.execute("SELECT ref, title, type, project, body FROM artifacts WHERE ref = ?", (ref,)).fetchone()
    if not row:
        raise LookupError(f"artifact {ref} not indexed")
    body = row["body"] or ""
    claim = (section_body(body, "Claim") or "").strip()
    evidence = (section_body(body, "Evidence") or "").strip()
    return {"project": row["project"], "claim_id": None, "label": f"{row['ref']} · {row['title']} ({row['type']})",
            "statement": claim[:1500] or (row["title"] or ""), "context": evidence[:2500]}


def _claim_target(conn: sqlite3.Connection, claim_id: int) -> dict[str, Any]:
    c = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if not c:
        raise LookupError(f"claim {claim_id} not found")
    lines = []
    for r in conn.execute(
            "SELECT ec.status, ec.implication, ec.implication_origin, ec.test_type, ca.label FROM evidence_cells ec "
            "JOIN cases ca ON ca.id = ec.case_id WHERE ec.claim_id = ? ORDER BY ca.sort, ca.date_start", (claim_id,)):
        imp = r["implication"] or "(no implication written)"
        if r["implication_origin"] == "placeholder":
            imp = "(placeholder) " + imp
        lines.append(f"- {r['label']}: status {r['status']}, test {r['test_type'] or 'none'}; implication: {imp}")
    ex = conn.execute(
        "SELECT e.id, e.quote, e.my_note, e.locator FROM cell_excerpts ce JOIN evidence_cells ec ON ec.id = ce.cell_id "
        "JOIN excerpts e ON e.id = ce.excerpt_id WHERE ec.claim_id = ? AND e.private = 0 ORDER BY e.id LIMIT 10", (claim_id,)).fetchall()
    ex_lines = [f"- {e['id']} ({e['locator']}): \"{e['quote'][:300]}\" — note: {e['my_note'][:200]}" for e in ex]
    context = "Evidence matrix row:\n" + ("\n".join(lines) or "- (no cells)")
    if ex_lines:
        context += "\n\nThe user's excerpts attached to this row:\n" + "\n".join(ex_lines)
    return {"project": c["project_slug"], "claim_id": c["id"],
            "label": f"{c['code'] or 'claim'} ({c['kind']}{', own explanation' if c['is_own'] else ''})",
            "statement": c["statement"], "context": context}


def resolve_target(conn: sqlite3.Connection, target_id: str) -> dict[str, Any]:
    if target_id.startswith("ART-"):
        return _artifact_target(conn, target_id)
    return _claim_target(conn, int(target_id))


class ChecksDerive(JobSpec):
    kind = "checks.derive"
    title = "Derive empirical checks from a claim"
    Result = Result
    target_type = "claim"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        t = resolve_target(conn, target_id)
        existing = [r["title"] for r in conn.execute("SELECT title FROM checks WHERE project_slug = ?", (t["project"],))]
        user = (
            f"Project: {t['project']}\nTarget: {t['label']}\n\nClaim:\n{t['statement']}\n\n{t['context']}\n\n"
            f"Existing checks (do not repeat): {json.dumps(existing, ensure_ascii=False)}\n\n"
            "Propose at most three empirical checks that could weaken this claim. A check is a structured "
            "confrontation of the claim with evidence the user can assemble in year one: a structured focused "
            "comparison, a process-tracing test (straw-in-the-wind, hoop, smoking gun, doubly decisive), a "
            "crisp-set truth table, or a network measure. For each, state the prediction before any result, the "
            "data needed (document class or dataset, never a specific finding), the method, and which observation "
            "would weaken the claim. Do not state what the data show, do not cite sources, and do not write any "
            "part of the user's argument. Return {\"checks\": []} when the material does not support a check."
        )
        req = ChatRequest(messages=[{"role": "system", "content": SYSTEM_RULES}, {"role": "user", "content": user}],
                          max_tokens=2000)
        return req, []

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        t = resolve_target(conn, target_id)
        now = now_iso()
        for c in result.checks[:3]:  # type: ignore[attr-defined]
            conn.execute(
                "INSERT INTO checks(project_slug, claim_id, title, prediction, data_needed, method, would_weaken_if, status, "
                "origin, ai_run_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["project"], t["claim_id"], c.title.strip(), c.prediction.strip(), c.data_needed.strip() or None,
                 c.method.strip() or None, c.would_weaken_if.strip() or None, "planned", "ai-suggested", run_id, now, now))

    def example(self) -> dict[str, Any]:
        return {"checks": [{
            "title": "Bypass ratio against ASEAN betweenness, 1994–2025",
            "prediction": "If centrality is a brokerage position, ASEAN's betweenness among great powers falls as the share "
                          "of great-power dyads sharing a non-ASEAN forum rises.",
            "data_needed": "State × forum × year membership for ASEAN-led forums and for non-ASEAN minilaterals (public membership lists).",
            "method": "Yearly one-mode projection; betweenness of the ASEAN node against the bypass ratio.",
            "would_weaken_if": "Betweenness stays flat or rises while the bypass ratio grows.",
        }]}


register(ChecksDerive())
