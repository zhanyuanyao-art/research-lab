"""paper.translate: display-only, per-page machine translation of a paper (SPEC-v2).

target_id is `paper_id:lang_to`. A paper is split into chunks of whole pages (≤ ~2,500 source
tokens each, page markers kept) and each chunk is its own job carrying `params.page_range`; there
is no parent batch, progress is the share of chunks whose pages all have a translation. Results
land in paper_translations only: excerpts keep quoting paper_pages, and the reader marks
translated text `data-no-excerpt` so it can never become a quote.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config import get_paths
from ...db.connection import now_iso
from ..provider import ChatRequest, JobSpec
from ..registry import register

log = logging.getLogger("research_lab.ai.translate")

KIND = "paper.translate"
CHUNK_TOKENS = 2500
MAX_OUTPUT_TOKENS = 8000
TARGETS = ("en", "zh")
LANG_EN = {"zh": "Simplified Chinese", "en": "English", "ja": "Japanese", "ko": "Korean"}

SYSTEM = (
    "You are a careful academic translator inside Leo's Research Lab, a PhD workbench for International "
    "Relations of East Asia. Translate only the text given. Do not summarize, omit, comment on or add "
    "anything. Reply with a single JSON object matching the schema in the request and nothing else."
)


class TranslatedPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    page_index: int
    text: str

    @field_validator("page_index", mode="before")
    @classmethod
    def _index(cls, v: Any) -> int:
        return int(str(v).strip())


class TranslationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pages: list[TranslatedPage] = Field(min_length=1)


EXAMPLE = {"pages": [{"page_index": 0, "text": "ASEAN Centrality Reconsidered\n\nAbstract: This article examines ..."},
                     {"page_index": 1, "text": "I. Introduction\n\nSince the end of the Cold War ..."}]}


def est_tokens(text: str) -> int:
    """~1 token per CJK character, ~4 characters per token otherwise (DeepSeek/Claude tokenizers)."""
    from ...intake.extract import CJK_ANY_RE
    cjk = len(CJK_ANY_RE.findall(text))
    return cjk + (len(text) - cjk) // 4


def parse_target(target_id: str) -> tuple[str, str]:
    paper_id, _, lang_to = (target_id or "").rpartition(":")
    if not paper_id or lang_to not in TARGETS:
        raise ValueError(f"paper.translate target must be paper_id:{'|'.join(TARGETS)}, got {target_id!r}")
    return paper_id, lang_to


def plan(pages: list, max_tokens: int | None = None) -> list[tuple[int, int]]:
    """Inclusive (first, last) page_index ranges over pages with text. A page is never split; a page
    over the budget is a chunk of its own."""
    max_tokens = max_tokens or CHUNK_TOKENS
    chunks: list[tuple[int, int]] = []
    start = last = None
    used = 0
    for p in pages:
        if not p.text.strip():
            continue
        n = est_tokens(p.text)
        if start is not None and used + n > max_tokens:
            chunks.append((start, last))
            start, used = None, 0
        if start is None:
            start = p.index
        last = p.index
        used += n
    if start is not None:
        chunks.append((start, last))
    return chunks


def _paper_file_pages(conn: sqlite3.Connection, paper_id: str):
    from ...intake import pipeline
    paper = pipeline.get_paper(conn, paper_id)
    if paper is None:
        raise LookupError(paper_id)
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        raise LookupError(f"paper {paper_id} has no file to translate")
    return paper, f, pipeline.load_pages(conn, f["id"])


def _params_from_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?",
                       (run_id,)).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row[0]).get("params") or {}
    except ValueError:
        return {}


def chunk_key(paper_id: str, lang_to: str, rng: tuple[int, int]) -> str:
    return f"{KIND}:{paper_id}:{lang_to}:{rng[0]}-{rng[1]}"


class PaperTranslate(JobSpec):
    kind = KIND
    title = "Translate a paper's pages"
    Result = TranslationResult
    target_type = "paper_translation"

    def build_chunk(self, conn: sqlite3.Connection, paper_id: str, lang_to: str,
                    rng: tuple[int, int]) -> tuple[ChatRequest, list[str]]:
        from ...intake import pipeline
        from ...intake.extract import reflow
        paper, f, pages = _paper_file_pages(conn, paper_id)
        lang_from = pipeline.ensure_language(conn, paper_id) or "other"
        chosen = [p for p in pages if rng[0] <= p.index <= rng[1] and p.text.strip()]
        if not chosen:
            raise ValueError(f"no text on pages {rng[0]}–{rng[1]} of {paper_id}")
        # The model sees the same paragraphs the reader shows (hard line breaks and double spacing already
        # resolved), so its translation lines up paragraph for paragraph in the 对照 view.
        block = "\n\n".join(f"=== page_index {p.index} · p. {p.label} ===\n{reflow(p.text)}" for p in chosen)
        src = LANG_EN.get(lang_from, "the source language")
        dst = LANG_EN[lang_to]
        cache = get_paths().cache
        cache.mkdir(parents=True, exist_ok=True)
        input_path = cache / f"{paper_id}-translate-{lang_to}-{rng[0]}-{rng[1]}.txt"
        input_path.write_text(block, encoding="utf-8")
        user = "\n".join([
            f"Task: translate the pages below from {src} into {dst}, so Leo can read them side by side with the original.",
            f"Paper: {paper['title']} ({paper_id}) · pages p. {chosen[0].label}–{chosen[-1].label} of {len(pages)}.",
            "",
            "Rules:",
            '- Return {"pages": [{"page_index": <integer from the marker>, "text": "<translation>"}]}, one entry per page '
            "marker, in order. Never merge or split pages.",
            "- Translate everything on the page faithfully and completely. The source is already one paragraph per line, "
            "separated by blank lines: return exactly one translated paragraph per source paragraph, in order, "
            "separated by blank lines, so the two can be read side by side.",
            "- Keep footnote numbers, citations such as (Author 2019), numbers, dates, URLs and page references unchanged.",
            f"- Personal and place names: the standard {dst} rendering; when unsure, add the original in parentheses once per page.",
            "- Bibliography / reference-list entries stay in the original language.",
            "- Running headers, footers and page numbers may be dropped.",
            "",
            f"The same pages are saved at {input_path}.",
            "",
            "# PAGES",
            block,
        ])
        out_tokens = min(MAX_OUTPUT_TOKENS, max(1000, int(est_tokens(block) * 2) + 400))
        req = ChatRequest(messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                          max_tokens=out_tokens, json_mode=True, temperature=0.1,
                          params={"schema_name": "TranslationResult", "page_range": [rng[0], rng[1]],
                                  "page_indexes": [p.index for p in chosen], "file_id": f["id"],
                                  "lang_from": lang_from, "lang_to": lang_to})
        return req, [str(input_path)]

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        """Generic entry (`queue.enqueue`): the first chunk. The app enqueues every chunk via enqueue_chunks."""
        paper_id, lang_to = parse_target(target_id)
        _paper, _f, pages = _paper_file_pages(conn, paper_id)
        chunks = plan(pages)
        if not chunks:
            raise ValueError(f"{paper_id} has no page text to translate")
        return self.build_chunk(conn, paper_id, lang_to, chunks[0])

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        assert isinstance(result, TranslationResult)
        from ...intake import pipeline
        paper_id, lang_to = parse_target(target_id)
        params = _params_from_run(conn, run_id)
        f = pipeline.primary_file(conn, paper_id)
        file_id = int(params.get("file_id") or (f["id"] if f else 0))
        if not conn.execute("SELECT 1 FROM paper_files WHERE id = ? AND paper_id = ?", (file_id, paper_id)).fetchone():
            raise LookupError(f"file {file_id} of {paper_id} no longer exists")
        allowed = set(params.get("page_indexes") or [])
        if not allowed:
            lo, hi = (params.get("page_range") or [0, 10 ** 6])[:2]
            allowed = {r[0] for r in conn.execute("SELECT page_index FROM paper_pages WHERE file_id = ? AND page_index BETWEEN ? AND ?",
                                                  (file_id, lo, hi))}
        kept: dict[int, str] = {}
        for p in result.pages:
            if p.page_index in allowed and p.text.strip():
                kept[p.page_index] = p.text.strip()
        if not kept:
            raise ValueError("the translation covers none of this chunk's pages")
        ts = now_iso()
        conn.executemany(
            "INSERT INTO paper_translations(paper_id, file_id, page_index, lang_from, lang_to, text, ai_run_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(file_id, page_index, lang_to) DO UPDATE SET text=excluded.text, "
            "lang_from=excluded.lang_from, ai_run_id=excluded.ai_run_id, created_at=excluded.created_at",
            [(paper_id, file_id, i, params.get("lang_from"), lang_to, t, run_id, ts) for i, t in sorted(kept.items())])
        result.pages = [TranslatedPage(page_index=i, text=t) for i, t in sorted(kept.items())]

    def example(self) -> dict[str, Any]:
        return EXAMPLE


SPEC = register(PaperTranslate())


def _latest_by_key(conn: sqlite3.Connection, target_id: str) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute("SELECT id, status, dedupe_key, error FROM ai_jobs WHERE kind = ? AND target_id = ? "
                          "ORDER BY created_at, id", (KIND, target_id)):
        if r["dedupe_key"]:
            out[r["dedupe_key"]] = r
    return out


def enqueue_chunks(conn: sqlite3.Connection, paper_id: str, lang_to: str, *, force: bool = False) -> list[str]:
    """Queue one job per chunk still missing a translation (every chunk with `force`) and dispatch them.
    Chunks whose job is already open are left alone. Returns the new job ids."""
    from .. import dispatch, queue
    from ...intake import pipeline
    if lang_to not in TARGETS:
        raise ValueError(f"lang_to must be one of {', '.join(TARGETS)}")
    _paper, f, pages = _paper_file_pages(conn, paper_id)
    if pipeline.ensure_language(conn, paper_id) == lang_to:
        raise ValueError("论文已经是目标语言")
    target_id = f"{paper_id}:{lang_to}"
    done = set(pipeline.translations_for(conn, f["id"], lang_to))
    latest = _latest_by_key(conn, target_id)
    schema = json.dumps(TranslationResult.model_json_schema())
    new_ids: list[str] = []
    for rng in plan(pages):
        key = chunk_key(paper_id, lang_to, rng)
        idxs = [p.index for p in pages if rng[0] <= p.index <= rng[1] and p.text.strip()]
        job = latest.get(key)
        if job is not None and job["status"] in ("queued", "claimed"):
            if not force:
                continue
            queue.cancel(conn, job["id"])
        if not force and all(i in done for i in idxs):
            continue
        req, files = SPEC.build_chunk(conn, paper_id, lang_to, rng)
        job_id = queue.new_job_id(conn)
        conn.execute(
            "INSERT INTO ai_jobs(id, kind, status, target_type, target_id, request_json, input_files_json, schema_json, dedupe_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, KIND, "queued", SPEC.target_type, target_id, json.dumps(req.to_json(), ensure_ascii=False),
             json.dumps(files), schema, key, now_iso()))
        new_ids.append(job_id)
    for job_id in new_ids:
        try:
            dispatch.dispatch(job_id)
        except Exception:  # noqa: BLE001 - stays queued for `rl agent` / `rl ai`
            log.exception("dispatch of %s failed", job_id)
    return new_ids


def status(conn: sqlite3.Connection, paper_id: str, lang_to: str) -> dict[str, Any]:
    """Progress over the chunk plan: done = every page of the chunk has a translation."""
    from ...intake import pipeline
    out: dict[str, Any] = {"lang_to": lang_to, "chunks_total": 0, "chunks_done": 0, "chunks_open": 0,
                           "chunks_failed": 0, "chunks_claimed": 0, "pages_total": 0, "pages_done": 0, "open_jobs": [], "errors": []}
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return out
    pages = pipeline.load_pages(conn, f["id"])
    done = set(pipeline.translations_for(conn, f["id"], lang_to))
    latest = _latest_by_key(conn, f"{paper_id}:{lang_to}")
    for rng in plan(pages):
        idxs = [p.index for p in pages if rng[0] <= p.index <= rng[1] and p.text.strip()]
        out["chunks_total"] += 1
        out["pages_total"] += len(idxs)
        out["pages_done"] += sum(1 for i in idxs if i in done)
        job = latest.get(chunk_key(paper_id, lang_to, rng))
        if all(i in done for i in idxs):
            out["chunks_done"] += 1
        elif job is not None and job["status"] in ("queued", "claimed"):
            out["chunks_open"] += 1
            out["chunks_claimed"] += job["status"] == "claimed"
            out["open_jobs"].append(job["id"])
        elif job is not None and job["status"] == "failed":
            out["chunks_failed"] += 1
            out["errors"].append(f"{job['id']}: {(job['error'] or '')[:160]}")
    out["started"] = bool(latest) or out["pages_done"] > 0
    out["complete"] = out["chunks_total"] > 0 and out["chunks_done"] == out["chunks_total"]
    return out
