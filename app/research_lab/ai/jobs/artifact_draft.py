"""artifact.draft: evidence bullets, an outline and questions from the excerpts the user ticked.

Method-layer rule 6: nothing here writes "What did I figure out" or "Claim". Evidence bullets stay
in the job result until the user inserts them; apply() only replaces `## AI notes` and proposes
`related` links as associations.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from pydantic import BaseModel, Field

from ...config import get_paths
from ...db.connection import get_setting, now_iso
from ...files import artifacts, datafiles
from .. import registry
from ..provider import SYSTEM_RULES, ChatRequest, JobSpec

EXC_RE = re.compile(r"EXC-\d{4}-\d{4}")
MAX_BODY_CHARS = 12000


class EvidenceBullet(BaseModel):
    excerpt_id: str
    text: str = Field(max_length=300, description="One sentence built only from that excerpt, ending with [EXC-…]")


class DraftResult(BaseModel):
    evidence_bullets: list[EvidenceBullet] = Field(default_factory=list)
    what_i_figured_out_candidates: list[str] = Field(default_factory=list, max_length=2)
    outline: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    related_artifacts: list[str] = Field(default_factory=list)


def _one_line(s: str, limit: int = 400) -> str:
    return " ".join(str(s or "").lstrip("#> ").split())[:limit]


def _selection(conn: sqlite3.Connection, ref: str) -> list[str]:
    from ...weekly import SELECTION_KEY
    return [str(i) for i in get_setting(conn, SELECTION_KEY.format(ref=ref), []) or []]


def _allowed_from_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?",
                       (run_id,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0]).get("params") or None
    except ValueError:
        return None


def notes_markdown(result: DraftResult, run_id: int) -> str:
    lines = [f"<!-- rl:ai-notes artifact.draft run {run_id} · ai-suggested · the level checker ignores this section -->", ""]
    if result.outline:
        lines += ["**Outline**", ""] + [f"{i}. {_one_line(x)}" for i, x in enumerate(result.outline, 1)] + [""]
    if result.questions:
        lines += ["**Questions**", ""] + [f"- {_one_line(x)}" for x in result.questions] + [""]
    if result.what_i_figured_out_candidates:
        lines += ["**Possible \"What did I figure out\" lines** (suggestions only; the section stays yours)", ""]
        lines += [f"- {_one_line(x)}" for x in result.what_i_figured_out_candidates] + [""]
    if len(lines) == 2:
        lines.append("_The selected excerpts did not support an outline or questions._")
    return "\n".join(lines)


class ArtifactDraft(JobSpec):
    kind = "artifact.draft"
    title = "Draft evidence, outline and questions for an artifact"
    Result = DraftResult
    target_type = "artifact"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        paths = get_paths()
        artifacts.index(conn, paths)
        row = conn.execute("SELECT * FROM artifacts WHERE ref = ?", (target_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown artifact {target_id}")
        path = paths.root / row["path"]
        a = artifacts.load(path)
        ids = _selection(conn, target_id)
        excerpts = []
        if ids:
            q = ("SELECT e.id, e.quote, e.my_note, e.locator, e.kind, p.id AS paper_id, p.title, p.year, p.authors_json "
                 f"FROM excerpts e JOIN papers p ON p.id = e.paper_id WHERE e.id IN ({','.join('?' * len(ids))}) "
                 "AND e.private = 0")
            from ...weekly import citation
            for r in conn.execute(q, ids).fetchall():
                excerpts.append({"id": r["id"], "kind": r["kind"], "quote": r["quote"], "note": r["my_note"],
                                 "locator": r["locator"], "citation": f"{citation(r)} {r['title']}".strip()})
        ids = [e["id"] for e in excerpts]
        tpl_path = paths.templates / "artifact.md"
        tpl_sections = [h for h, _, _ in artifacts.sections(artifacts.load(tpl_path).body)] if tpl_path.exists() else []
        project = datafiles.project(str(a.get("project", "")), paths) or {}
        other_rows = conn.execute("SELECT ref, title FROM artifacts WHERE ref != ? ORDER BY ref", (target_id,)).fetchall()
        others = [r["ref"] for r in other_rows]
        other_list = [f"- {r['ref']}: {r['title']}" for r in other_rows]
        body = a.body if len(a.body) <= MAX_BODY_CHARS else a.body[:MAX_BODY_CHARS] + "\n[…truncated]"
        schema = json.dumps(DraftResult.model_json_schema(), ensure_ascii=False)
        user = "\n".join([
            f"Task: help Leo draft this week's artifact {target_id} ({a.get('type')} · {a.get('level')}).",
            f"Project question: {project.get('question', '(none)')}",
            "",
            "## Selected excerpts (the ONLY evidence you may use)",
            json.dumps(excerpts, ensure_ascii=False, indent=1),
            "",
            "## Template sections for this artifact type",
            ", ".join(tpl_sections),
            "",
            "## Current file body",
            body,
            "",
            "## Other artifacts you may propose as related (refs only from this list)",
            "\n".join(other_list) or "(none)",
            "",
            "Rules:",
            "- evidence_bullets: one bullet per useful excerpt, at most 300 characters, paraphrasing only what that "
            "excerpt says, with its locator, ending with the excerpt id in brackets, e.g. [EXC-2026-0001]. "
            "Cite only the excerpt ids listed above. Never quote text that is not in the excerpt.",
            "- Do NOT write the 'What did I figure out' or 'Claim' sections. You may offer at most 2 short "
            "what_i_figured_out_candidates; Leo decides whether to write his own.",
            "- outline: the order in which the sections could argue, as short lines.",
            "- questions: open questions the excerpts raise, phrased as questions.",
            "- related_artifacts: refs from the list above that this artifact builds on or contests; else [].",
            "",
            "Return a json object matching this schema:",
            schema,
            "Example json:",
            json.dumps(self.example(), ensure_ascii=False),
        ])
        req = ChatRequest(messages=[{"role": "system", "content": SYSTEM_RULES}, {"role": "user", "content": user}],
                          max_tokens=3000, params={"excerpt_ids": ids, "artifact_sha": a.sha,
                                                   "known_refs": others})
        return req, [str(path)]

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        assert isinstance(result, DraftResult)
        paths = get_paths()
        artifacts.index(conn, paths)
        params = _allowed_from_run(conn, run_id) or {}
        allowed = set(params.get("excerpt_ids") or _selection(conn, target_id))
        kept = []
        for b in result.evidence_bullets:
            cited = set(EXC_RE.findall(b.text))
            if b.excerpt_id in allowed and cited <= allowed:
                kept.append(b)
        result.evidence_bullets = kept
        known = {r[0] for r in conn.execute("SELECT ref FROM artifacts WHERE ref != ?", (target_id,))}
        result.related_artifacts = [r for r in dict.fromkeys(result.related_artifacts) if r in known]

        row = conn.execute("SELECT path FROM artifacts WHERE ref = ?", (target_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown artifact {target_id}")
        a = artifacts.load(paths.root / row["path"])
        artifacts.set_ai_notes(a, notes_markdown(result, run_id))
        artifacts.save(a)
        for ref in result.related_artifacts:
            conn.execute(
                "INSERT OR IGNORE INTO associations(src_type, src_id, rel, dst_type, dst_id, origin, status, reason, ai_run_id, created_at) "
                "VALUES ('artifact', ?, 'related', 'artifact', ?, 'ai-suggested', 'proposed', ?, ?, ?)",
                (target_id, ref, "artifact.draft", run_id, now_iso()))
        artifacts.index(conn, paths)

    def example(self) -> dict[str, Any]:
        return {
            "evidence_bullets": [{"excerpt_id": "EXC-2026-0014",
                                  "text": "Tan (2023, p. 11): US leader-level attendance at the EAS lapsed in 5 of 17 "
                                          "summits with no proposal to move the chair [EXC-2026-0014]"}],
            "what_i_figured_out_candidates": ["Attendance can fall without the chair rule being questioned."],
            "outline": ["Pattern: attendance drops", "Rule persists", "Which camp predicts that?"],
            "questions": ["Has a non-ASEAN chair ever been formally proposed for the EAS?"],
            "related_artifacts": [],
        }


registry.register(ArtifactDraft())
