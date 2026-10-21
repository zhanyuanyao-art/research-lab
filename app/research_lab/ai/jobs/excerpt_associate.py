"""`excerpt.associate`: retrieval (related + anchors, ≤12 candidates) → one LLM call that proposes
tags, links to candidates only, a candidate claim, a next question and insertion targets.

apply() writes proposed associations and a proposed claim. It never touches the quote, evidence
cells, or any artifact file; next_question and targets stay in result_json for the panel.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ... import excerpts as ex, i18n
from ...db.connection import get_setting, now_iso
from ...method.seed import ASEAN
from ...search import anchors as anchor_mod
from ...search.related import excerpt_title, is_meaningful, related
from .. import registry
from ..provider import ChatRequest, JobSpec, system_rules

MAX_CANDIDATES = 12
SNIPPET = 600
REJECTIONS = 20
CANDIDATE_RE = re.compile(r"^- id: (\S+) · type: (\w+)", re.M)
YEAR_TAG_RE = re.compile(r"^(\d{4})$|^([A-Z][A-Z0-9+]{1,10}(?:-[A-Z]{2,5})?)-(\d{4})$")


class Tags(BaseModel):
    uses: list[str] = Field(default_factory=list)
    cases: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    camp: str | None = None


class Link(BaseModel):
    target_type: Literal["excerpt", "artifact", "anchor", "claim", "case"]
    target_id: str
    relation: Literal["supports", "contradicts", "same-case", "same-concept", "defines", "mechanism", "parallel"]
    reason: str

    @field_validator("reason")
    @classmethod
    def _thirty_words(cls, v: str) -> str:
        words = v.split()
        v = " ".join(words[:30]) + ("…" if len(words) > 30 else "")
        return v if len(v) <= 90 else v[:89].rstrip() + "…"  # Chinese has no spaces to count words by


class ClaimDraft(BaseModel):
    text: str
    strength: Literal["seed", "developed"]


class Target(BaseModel):
    kind: Literal["artifact_section", "matrix_cell", "map_edge"]
    ref: str
    note: str


class AssociateResult(BaseModel):
    tags: Tags = Field(default_factory=Tags)
    links: list[Link] = Field(default_factory=list)
    claim: ClaimDraft | None = None
    next_question: str | None = None
    targets: list[Target] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _no_quotes(cls, data: Any) -> Any:
        # method-layer rule 2: a quote can only come from the user
        if isinstance(data, dict) and ("quote" in data or "quotes" in data):
            raise ValueError("an AI result may not carry a quote field")
        return data


def _candidates(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[list[dict], int, int]:
    """(candidates, n_excerpts, n_anchors). Anchors lead while the corpus is under 20 excerpts."""
    n_excerpts = conn.execute("SELECT COUNT(*) FROM excerpts WHERE status != 'discarded'").fetchone()[0]
    floor = float(get_setting(conn, "related.bm25_floor", 0.2))
    min_related = int(get_setting(conn, "related.min_candidates", 2))
    rel = [c for c in related(conn, excerpt_id=row["id"], limit=20) if is_meaningful(c, floor)]
    if len(rel) < min_related:
        rel = []
    rel_items = [{"id": c.id, "type": c.type, "title": c.title, "text": c.snippet[:SNIPPET]} for c in rel]
    anc = anchor_mod.ranked(conn, f"{row['quote']}\n{row['my_note']}", row["project_slug"])
    anc_items = [{"id": a.id, "type": "anchor", "title": a.label, "text": a.text[:SNIPPET]} for a in anc]
    if n_excerpts < 20:
        head = anc_items[:8]
        out = head + rel_items[: MAX_CANDIDATES - len(head)]
    else:
        head = rel_items[:9]
        out = head + anc_items[: MAX_CANDIDATES - len(head)]
    return out, n_excerpts, sum(1 for c in out if c["type"] == "anchor")


def _vocab_block(conn: sqlite3.Connection, project: str | None) -> tuple[str, list[dict]]:
    """Vocabularies for the prompt, plus the claims/cases that are valid link targets."""
    camps = [f"{r['id']} ({r['label']})" for r in conn.execute("SELECT id, label FROM camps WHERE project_slug = ? ORDER BY sort, id", (project or "",))]
    concepts = [f"{r['id']} ({r['label']})" for r in conn.execute("SELECT id, label FROM concepts ORDER BY id")]
    # The built-in camps and cases are the ASEAN-centrality codebook; other excerpts get only their project's own.
    codebook = project == ASEAN
    built_camps = ", ".join(ex.CAMPS) if codebook else ""
    built_cases = ", ".join(ex.CASES) if codebook else ""
    lines = [
        "## Vocabularies",
        f"- A uses: {', '.join(ex.USES)}",
        "- B camps: " + ("; ".join(x for x in (built_camps, ", ".join(camps)) if x) or "(none; leave camp empty)"),
        "- C cases: " + (built_cases + ", plus " if built_cases else "") + "YYYY episode tags (e.g. <CASE>-2017)",
        f"- concept ledger: {', '.join(concepts) if concepts else '(empty; propose short lowercase keys)'}",
    ]
    targets: list[dict] = []
    for r in conn.execute("SELECT id, code, kind, statement FROM claims WHERE project_slug = ? AND status = 'accepted' "
                          "ORDER BY kind, code, id", (project or "",)):
        targets.append({"id": str(r["id"]), "type": "claim", "title": f"{r['code'] or r['kind']}", "text": r["statement"][:SNIPPET]})
    for r in conn.execute("SELECT id, label FROM cases WHERE project_slug = ? OR project_slug IS NULL ORDER BY sort, id", (project or "",)):
        targets.append({"id": r["id"], "type": "case", "title": r["label"], "text": r["label"]})
    return "\n".join(lines), targets


def _rejections(conn: sqlite3.Connection) -> list[str]:
    out = []
    for r in conn.execute("SELECT src_id, rel, dst_type, dst_id, reason, reject_reason FROM associations "
                          "WHERE status = 'rejected' AND reject_reason IS NOT NULL AND reject_reason != 'removed' "
                          "ORDER BY id DESC LIMIT ?", (REJECTIONS,)):
        why = f" (it said: {r['reason']})" if r["reason"] else ""
        out.append(f"- {r['src_id']} → {r['dst_type']} {r['dst_id']} as {r['rel']}: rejected as {r['reject_reason']}{why}")
    return out


def _cand_lines(items: list[dict]) -> list[str]:
    out = []
    for c in items:
        text = " ".join(c["text"].split())
        out.append(f"- id: {c['id']} · type: {c['type']} · {c['title']}\n  {text}")
    return out


def allowed_targets(request_json: str | dict) -> set[tuple[str, str]]:
    req = json.loads(request_json) if isinstance(request_json, str) else request_json
    text = "\n".join(m.get("content", "") for m in req.get("messages", []) if m.get("role") == "user")
    return {(t, i) for i, t in CANDIDATE_RE.findall(text)}


class ExcerptAssociate(JobSpec):
    kind = "excerpt.associate"
    title = "Associate an excerpt with related material"
    target_type = "excerpt"
    Result = AssociateResult

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        row = ex.get(conn, target_id)
        if row is None:
            raise LookupError(f"no excerpt {target_id}")
        t = ex.tags(conn, target_id)
        cands, n_excerpts, n_anchors = _candidates(conn, row)
        vocab, link_targets = _vocab_block(conn, row["project_slug"])
        week_art = ex.current_week_artifact(conn)
        expl = ", ".join(f"{e['code'] or e['claim_id']} {e['stance'] or ''}".strip() for e in t["explanations"]) or "none"
        parts = [
            f"# Excerpt {row['id']}",
            f"Paper: {excerpt_title(_as_title_row(row))} — {row['paper_title']} ({row['paper_id']})",
            f"Kind: {row['kind']} · project: {row['project_slug'] or '-'} · quote verified against the PDF text: "
            f"{'yes' if row['quote_verified'] else 'no'}",
            "Quote (pasted by the user; never rewrite, extend or repeat it as your own text):",
            f"“{row['quote']}”",
            f"User's note: {row['my_note']}",
            f"Current tags: uses [{', '.join(t['uses'])}] · camp {t['camp'] or '-'} · cases [{', '.join(t['cases'])}] · "
            f"concepts [{', '.join(t['concepts'])}] · explanations [{expl}]",
            "",
            vocab,
            "",
            "## Candidates (link only to these ids; target_type must equal the type shown)",
            *(_cand_lines(cands) or ["(none)"]),
        ]
        if link_targets:
            parts += ["", "## Claims and cases (also valid link targets)", *_cand_lines(link_targets)]
        parts += ["", "## This week's artifact"]
        if week_art:
            parts.append(f"{week_art['ref']} · {week_art['title']} · type {week_art['type']} · sections: Evidence, "
                         f"Why it matters, What this challenges, Next question (never Claim or What did I figure out)")
        else:
            parts.append("none yet for this week (an artifact_section target creates a Seed)")
        rej = _rejections(conn)
        if rej:
            parts += ["", "## Suggestions the user rejected before (do not propose links like these)", *rej]
        parts += ["", "## Task", TASK.format(n_excerpts=n_excerpts, n_anchors=n_anchors)]
        req = ChatRequest(messages=[{"role": "system", "content": system_rules().replace("Write English.", ZH_RULE)},
                                    {"role": "user", "content": "\n".join(parts)}],
                          max_tokens=2500, temperature=0.2)
        return req, []

    def apply(self, result: AssociateResult, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        row = conn.execute("SELECT * FROM excerpts WHERE id = ?", (target_id,)).fetchone()
        if row is None:
            raise LookupError(f"no excerpt {target_id}")
        job = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?", (run_id,)).fetchone()
        if job is None:
            job = conn.execute("SELECT request_json FROM ai_jobs WHERE kind = ? AND target_id = ? ORDER BY created_at DESC LIMIT 1",
                               (self.kind, target_id)).fetchone()
        allowed = allowed_targets(job["request_json"]) if job else set()
        ts = now_iso()

        def propose(rel: str, dst_type: str, dst_id: str, reason: str | None = None, stance: str | None = None) -> None:
            conn.execute(
                "INSERT INTO associations(src_type, src_id, rel, dst_type, dst_id, stance, origin, status, reason, ai_run_id, created_at) "
                "VALUES ('excerpt', ?, ?, ?, ?, ?, 'ai-suggested', 'proposed', ?, ?, ?) "
                "ON CONFLICT(src_type, src_id, rel, dst_type, dst_id) DO UPDATE SET ai_run_id = excluded.ai_run_id, "
                "reason = excluded.reason WHERE associations.status = 'proposed'",
                (target_id, rel, dst_type, dst_id, stance, reason, run_id, ts))

        current_uses = set(json.loads(row["uses_json"] or "[]"))
        for u in result.tags.uses:
            if u in ex.USES and u not in current_uses:
                propose("about", "use", u)
        case_ids = {r[0] for r in conn.execute("SELECT id FROM cases")}
        for c in result.tags.cases:
            c = ex.norm_case(conn, c)
            if c in ex.CASES or c in case_ids or YEAR_TAG_RE.match(c):
                propose("about", "case", c)
        placeholders = self.example()["tags"]                 # copied back from the example, not proposals
        for c in result.tags.concepts:
            key = ex.norm_concept(conn, c)
            if key and len(key) <= 60 and c not in placeholders["concepts"]:
                propose("about", "concept", key)
        if result.tags.camp:
            camp = ex.norm_camp(conn, result.tags.camp)
            known = camp in ex.CAMPS or conn.execute("SELECT 1 FROM camps WHERE id = ?", (camp,)).fetchone()
            if camp and known and camp != row["camp"]:
                propose("about", "camp", camp)

        for link in result.links:
            if (link.target_type, link.target_id) not in allowed:
                continue
            if link.target_type == "excerpt" and link.target_id == target_id:
                continue
            stance = link.relation if link.target_type == "claim" and link.relation in ("supports", "contradicts") else None
            propose(link.relation, link.target_type, link.target_id, link.reason, stance)

        if result.claim and result.claim.text.strip():
            project = row["project_slug"] or ""
            conn.execute(
                "INSERT INTO claims(project_slug, kind, statement, origin, status, strength, source_excerpts_json, ai_run_id, created_at) "
                "VALUES (?, 'claim', ?, 'ai-suggested', 'proposed', ?, ?, ?, ?)",
                (project, " ".join(result.claim.text.split()), result.claim.strength, json.dumps([target_id]), run_id, ts))

    def example(self) -> dict[str, Any]:
        # Shape only, no topic: a sample about one topic pulls every excerpt toward it (card 0202).
        return {
            "tags": {"uses": ["evidence"], "cases": ["case-id"], "concepts": ["concept-key"], "camp": "camp-id"},
            "links": [{"target_type": "anchor", "target_id": "anchor:now:2", "relation": "supports",
                       "reason": "这条划线为什么对论证有用，一句话。"}],
            "claim": {"text": "只靠这条划线和所连材料就能检验的一句判断。", "strength": "seed"},
            "next_question": "能推进课题的一个问题。",
            "targets": [{"kind": "artifact_section", "ref": "ART-2026-0001 § Evidence", "note": "放在哪里，一句话"}],
        }


def _as_title_row(row: sqlite3.Row) -> dict:
    return {"authors_json": row["paper_authors"], "paper_year": row["paper_year"], "paper_title": row["paper_title"],
            "locator": row["locator"], "kind": row["kind"]}


ZH_RULE = ("Write reasons, the claim, next_question and target notes in Simplified Chinese; ids, case ids, camp keys, "
           "relation names and artifact section names stay exactly as given.")

TASK = """Place this excerpt in the user's research. Work only from the excerpt, the note and the candidates above.
- tags: propose vocabulary tags the user has not set (uses from A, cases from C, concept keys, one camp from B or the project camps). Empty lists when unsure.
- links: at most 5, each to a candidate id above with the exact target_type shown; relation is one of supports, contradicts, same-case, same-concept, defines, mechanism, parallel; reason ≤ 30 words saying why the link matters for the argument, not what the texts say. Prefer anchors when no excerpt is truly related. Never link to an id that is not listed.
- claim: one sentence the user could test, resting only on this excerpt and the linked material; strength "seed" unless two or more independent excerpts support it, then "developed". null if nothing defensible.
- next_question: one question that would move the project forward (a search, a case to check, a definition to settle), or null. It is shown as "to verify": do not assert facts about events.
- targets: where this excerpt could go: artifact_section (ref "<artifact ref> § <section>", never Claim or What did I figure out), matrix_cell (ref "<claim code> × <case id>"), map_edge (ref "<paper> → <relation> → <camp>"); note ≤ 20 words. Suggest; never set evidence status.
Do not include the quote or any quotation in your reply.
(Corpus: {n_excerpts} excerpts; {n_anchors} anchors in the candidate list.)"""



# ---------------------------------------------------------------- the association panel

def describe_target(conn: sqlite3.Connection, dst_type: str, dst_id: str) -> dict[str, str | None]:
    """Title, text and link for an association target, for display."""
    if dst_type == "excerpt":
        r = ex.get(conn, dst_id)
        if r:
            title = f"{ex.cite_zh(r['paper_authors'], r['paper_year'], r['paper_title'])} · {ex.locator_label(r['locator'])}"
            return {"title": title, "text": ex.one_line(r["my_note"], 220),
                    "href": f"/excerpts/{r['id']}"}
    elif dst_type == "artifact":
        r = conn.execute("SELECT ref, title FROM artifacts WHERE ref = ?", (dst_id,)).fetchone()
        if r:
            return {"title": r["title"], "text": None, "href": f"/artifacts/{r['ref']}"}
    elif dst_type == "anchor":
        a = anchor_mod.resolve(conn, dst_id)
        if a:
            return {"title": _anchor_label_zh(a.label), "text": a.text, "href": None}
    elif dst_type == "claim":
        r = conn.execute("SELECT id, code, kind, statement FROM claims WHERE id = ?", (int(dst_id) if dst_id.isdigit() else -1,)).fetchone()
        if r:
            return {"title": r["code"] or i18n.CLAIM_KIND.get(r["kind"], r["kind"]), "text": r["statement"], "href": None}
    elif dst_type == "case":
        r = conn.execute("SELECT id, label FROM cases WHERE id = ?", (dst_id,)).fetchone()
        return {"title": f"案例 · {dst_id}", "text": r["label"] if r and r["label"] != dst_id else None, "href": None}
    return {"title": dst_id, "text": None, "href": None}


_ANCHOR_ZH = ((re.compile(r"^now\.yml · need to find out #(\d+)$"), r"本周待查 #\1"),
              (re.compile(r"^now\.yml · question$"), "本周研究问题"),
              (re.compile(r"^(.*) · project question$"), r"\1 · 课题问题"),
              (re.compile(r"^landscape camp · (.*)$"), r"学派 · \1"),
              (re.compile(r"^matrix column · (.*)$"), r"案例 · \1"))


def _anchor_label_zh(label: str) -> str:
    """Anchor labels are written for the prompt; the panel shows them in Chinese."""
    for pat, repl in _ANCHOR_ZH:
        if pat.match(label):
            return pat.sub(repl, label)
    return label


def _target_label(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """Readable name for an insertion target; artifact refs become the output's title."""
    if kind == "artifact_section":
        art_ref = ref.split("§", 1)[0].strip()
        r = conn.execute("SELECT title FROM artifacts WHERE ref = ?", (art_ref,)).fetchone()
        return f"《{r['title'] if r else art_ref}》· {_section_of(ref)}"
    return ref


def _section_of(ref: str) -> str:
    return ref.split("§", 1)[1].strip() if "§" in ref else "Evidence"


def panel(conn: sqlite3.Connection, excerpt_id: str, job: sqlite3.Row | None = None) -> dict[str, Any]:
    """Everything the association panel renders for the latest `excerpt.associate` job."""
    from .. import queue
    job = job or queue.latest_for(conn, ExcerptAssociate.kind, excerpt_id)
    out: dict[str, Any] = {"excerpt_id": excerpt_id, "job": job, "done": bool(job and job["status"] == "done")}
    if not out["done"]:
        return out
    result = AssociateResult.model_validate_json(job["result_json"])
    run_ids = [r[0] for r in conn.execute("SELECT id FROM ai_runs WHERE job_id = ? AND valid = 1", (job["id"],))] or \
              [r[0] for r in conn.execute("SELECT id FROM ai_runs WHERE job_id = ?", (job["id"],))]
    marks = ",".join("?" * len(run_ids)) or "NULL"
    rows = conn.execute(f"SELECT * FROM associations WHERE src_type='excerpt' AND src_id=? AND ai_run_id IN ({marks}) ORDER BY id",
                        (excerpt_id, *run_ids)).fetchall()
    links, tag_rows = [], []
    for r in rows:
        item = {"assoc": r, **describe_target(conn, r["dst_type"], r["dst_id"])}
        (tag_rows if r["rel"] == "about" else links).append(item)
    claim = conn.execute(f"SELECT * FROM claims WHERE ai_run_id IN ({marks}) AND source_excerpts_json LIKE ? ORDER BY id DESC LIMIT 1",
                         (*run_ids, f'%"{excerpt_id}"%')).fetchone()
    cand = allowed_targets(job["request_json"])
    n_excerpts = conn.execute("SELECT COUNT(*) FROM excerpts WHERE status != 'discarded'").fetchone()[0]
    out.update(
        result=result, links=links, tags=tag_rows, claim=claim,
        claim_strength=result.claim.strength if result.claim else None,
        next_question=result.next_question,
        targets=[{"kind": t.kind, "ref": t.ref, "label": _target_label(conn, t.kind, t.ref), "note": t.note,
                  "section": _section_of(t.ref)} for t in result.targets],
        n_excerpts=n_excerpts, n_anchors=sum(1 for t, _ in cand if t == "anchor"),
        n_related=sum(1 for t, _ in cand if t in ("excerpt", "artifact")),
        anchors_only=bool(links) and all(l["assoc"]["dst_type"] == "anchor" for l in links),
    )
    return out


registry.register(ExcerptAssociate())
