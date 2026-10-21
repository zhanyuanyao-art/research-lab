"""paper.summarize: a page-cited reading aid for one paper (architecture §6 step 9).

Two depths (card 0122, Leo 10-11: 「导入后默认只读第一页，消耗 token 少些」), chosen by the setting
papers.summary_mode or forced for one run (the paper page's Full summary forces full; 重新生成 keeps a full one full):
  light  metadata, the abstract and the first page with real text (cover pages skipped), about 1,500 tokens;
         only research_question and argument, every other field left empty, mode='light'
  full   the full text minus references, or an outline for books and long texts (FULL_TEXT_TOKENS)

The result lands in papers.ai_summary_json only; user-owned map fields change only when the user
clicks "采纳" on the paper page. Every prose field is written in Simplified Chinese whatever the
paper's language (SPEC-v2); page labels stay as printed. The result also proposes a research
direction, stored with the summary as a suggestion until the user adopts it into papers.direction.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ...config import get_paths
from ...db.connection import now_iso
from ..provider import ChatRequest, JobSpec, system_rules
from ..registry import register

FULL_TEXT_TOKENS = 30_000
SECTION_TOKENS = 5_000
BOOK_PAGES = 120
CHARS_PER_TOKEN = 4
LIGHT_TOKENS = 1_500
LIGHT_ABSTRACT_SHARE = 0.4      # of the light budget; the first page gets the rest
LIGHT_SCAN_PAGES = 15           # front matter longer than this is not looked through for the first page
MODES = ("light", "full")
LIGHT_FIELDS = ("research_question", "argument")
FORBIDDEN_KEYS = {"quote", "quotes", "quotation", "quotations", "verbatim"}


def _pages_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [v]
    return [str(x).strip() for x in v if str(x).strip()]


class Cited(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str | None = None
    pages: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _from_plain(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, str):
            return {"text": v}
        return v

    @field_validator("pages", mode="before")
    @classmethod
    def _pages(cls, v: Any) -> list[str]:
        return _pages_list(v)


class CampGuess(BaseModel):
    model_config = ConfigDict(extra="ignore")
    camp: str | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    why: str | None = None
    pages: list[str] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> str:
        return str(v or "low").strip().lower()

    @field_validator("pages", mode="before")
    @classmethod
    def _pages(cls, v: Any) -> list[str]:
        return _pages_list(v)


class ReadFirst(BaseModel):
    model_config = ConfigDict(extra="ignore")
    page: str
    why: str

    @field_validator("page", mode="before")
    @classmethod
    def _page(cls, v: Any) -> str:
        return str(v).strip()


def _find_forbidden(obj: Any, path: str = "") -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in FORBIDDEN_KEYS:
                return f"{path}.{k}".lstrip(".")
            hit = _find_forbidden(v, f"{path}.{k}")
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = _find_forbidden(v, f"{path}[{i}]")
            if hit:
                return hit
    return None


class DirectionGuess(BaseModel):
    model_config = ConfigDict(extra="ignore")
    slug: str | None = None
    why: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_plain(cls, v: Any) -> Any:
        if v is None:
            return {}
        if isinstance(v, str):
            return {"slug": v}
        return v

    @field_validator("slug", mode="before")
    @classmethod
    def _slug(cls, v: Any) -> str | None:
        s = str(v or "").strip()
        return s or None


class PaperSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    research_question: Cited = Field(default_factory=Cited)
    argument: Cited
    mechanism: Cited = Field(default_factory=Cited)
    cases: list[str] = Field(default_factory=list)
    period: Cited = Field(default_factory=Cited)
    method: Cited = Field(default_factory=Cited)
    level_of_analysis: Cited = Field(default_factory=Cited)
    concept_usage: Cited = Field(default_factory=Cited)
    camp_guess: CampGuess = Field(default_factory=CampGuess)
    contests: list[str] = Field(default_factory=list)
    cannot_explain_guess: Cited = Field(default_factory=Cited)
    relevance_to_project: Cited = Field(default_factory=Cited)
    read_first: list[ReadFirst] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    direction: DirectionGuess = Field(default_factory=DirectionGuess)
    mode: Literal["light", "full"] = "full"      # set by apply() from the job, never trusted from the model

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, v: Any) -> str:
        return "light" if str(v or "").strip().lower() == "light" else "full"

    @model_validator(mode="before")
    @classmethod
    def _no_quotes(cls, data: Any) -> Any:
        hit = _find_forbidden(data)
        if hit:
            raise ValueError(f"AI results may not carry quote fields (found '{hit}'); quotes are user-pasted only")
        return data


# The example shows the shape only; its text describes each field. A sample paper pulls unrelated papers toward its
# topic (Leo, card 0201: 「本文不讨论东盟中心地位」), so it names no topic, region or concept.
EXAMPLE = {
    "research_question": {"text": "论文要回答的问题，一句话。", "pages": ["2"]},
    "argument": {"text": "论文对这个问题的核心回答，一到两句。", "pages": ["3", "17"]},
    "mechanism": {"text": "论文依赖的因果路径。", "pages": ["4"]},
    "cases": ["论文研究的案例或事件的简短名称"],
    "period": {"text": "论文覆盖的时间段", "pages": ["11"]},
    "method": {"text": "论文怎样取得证据。", "pages": ["9"]},
    "level_of_analysis": {"text": "体系、地区、国家或领导人层次", "pages": []},
    "concept_usage": {"text": "论文自己的核心概念在文中指什么，怎样观察或测量。", "pages": ["4"]},
    "camp_guess": {"camp": "camp-id", "confidence": "medium", "why": "为什么归入这个流派，一句话。", "pages": ["3"]},
    "contests": ["论文反驳的流派、主张或作者"],
    "cannot_explain_guess": {"text": "论文的论点解释不了的一件事。", "pages": []},
    "relevance_to_project": {"text": "论文与上面课题问题的关系；没有课题时为 null。", "pages": ["11"]},
    "read_first": [{"page": "4", "why": "先读这一页的理由"}],
    "key_terms": ["中文译名 (original term)"],
    "direction": {"slug": "direction-slug", "why": "为什么归入这个研究方向，一句话。"},
}

FIELD_GUIDE = """Fields (each object field is {"text": string or null, "pages": [printed page labels]}):
- research_question: the question the paper asks.
- argument: its core answer in one or two sentences.
- mechanism: the causal pathway it relies on.
- cases: cases, events or episodes it studies (short labels).
- period: the time span covered.
- method: how it gets its evidence (e.g. process tracing, coded dataset, interpretive history).
- level_of_analysis: system, region, state or leader.
- concept_usage: the paper's own central concept: what it means in this paper and how the paper observes or measures it.
- camp_guess: {"camp": a camp id from the list when one fits (copy the id exactly), else a short new label; "confidence": "low"|"medium"|"high"; "why": one sentence; "pages": [...]}.
- contests: camps, claims or authors the paper argues against.
- cannot_explain_guess: one thing its argument cannot account for.
- relevance_to_project: one paragraph tying the paper to the project question above; null text when no project is assigned.
- read_first: up to four {"page": label, "why": short reason} worth reading first.
- key_terms: up to eight terms of art it uses.
- direction: {"slug": one slug from the research directions listed above, or null when none fits; "why": one sentence}."""

LANGUAGE_RULES = (
    "- Write every text value (text, why, cases, contests, key_terms, read_first.why) in Simplified Chinese, "
    "whatever language the paper is in. Keep page labels exactly as printed, and keep camp ids and direction "
    "slugs exactly as listed (they are identifiers, not prose).",
    "- key_terms: for a paper not written in Chinese, give each term as 中文译名 (original term), e.g. "
    "\"安全困境 (security dilemma)\"; for a Chinese paper give the Chinese term alone.",
    "- Personal names, journal names and forum acronyms may stay in their original form.",
)


def system_prompt() -> str:
    """The shared rules say 'Write English.'; summaries are Chinese by decision of SPEC-v2."""
    zh = "Write all prose in Simplified Chinese; identifiers, page labels and slugs stay as given."
    return system_rules().replace("Write English.", zh)


def directions_block() -> str:
    from ...intake import directions
    rows = directions.load()
    if not rows:
        return "No research directions are defined; return null for direction."
    lines = ["Research directions (pick at most one slug for `direction`):"]
    for d in rows:
        kws = ", ".join(str(k) for k in (d.get("keywords") or []))
        lines.append(f"- {d['slug']}: {d.get('name', '')} / {d.get('name_en', '')}" + (f" — keywords: {kws}" if kws else ""))
    return "\n".join(lines)


def _est_tokens(pieces: list[tuple[str, str]]) -> int:
    return sum(len(t) for _, t in pieces) // CHARS_PER_TOKEN


# ---------------------------------------------------------------- light reading (card 0122)

_FORCED: ContextVar[str | None] = ContextVar("summary_mode", default=None)


@contextmanager
def forced_mode(mode: str | None) -> Iterator[None]:
    """Build the jobs queued inside this block at `mode` whatever the setting says (None: the setting)."""
    token = _FORCED.set(mode if mode in MODES else None)
    try:
        yield
    finally:
        _FORCED.reset(token)


def summary_mode(conn: sqlite3.Connection) -> str:
    from ...tunables import tunable
    forced = _FORCED.get()
    return forced or (tunable(conn, "papers.summary_mode") or "light")


_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")


def light_tokens(text: str) -> int:
    """A CJK character is about a token, other text about four characters a token: chars // 4 alone would let a
    Chinese first page run four times over the light budget."""
    cjk = len(_CJK.findall(text))
    return cjk + (len(text) - cjk) // CHARS_PER_TOKEN


def _clip(text: str, budget: int) -> str:
    """The longest prefix of text within `budget` light tokens, cut at a line or sentence end when one is near."""
    if light_tokens(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if light_tokens(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    end = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("。"))
    return (cut[: end + 1] if end > lo * 0.6 else cut).rstrip() + " …"


# Lines every page of a download carries (JSTOR's and OUP's footers): removed before a page is judged.
_FOOTER = re.compile(r"(?im)^.*(?:this content downloaded from|all use subject to\s+https?://about\.jstor\.org"
                     r"|downloaded from\s+https?://|^\s*stable url:|^\s*accessed:\s).*$")
# A page that is a cover by itself: JSTOR's front sheet, Taylor & Francis's 「To cite this article」 sheet.
_COVER = re.compile(r"jstor is a not-for-profit|your use of the jstor archive|terms and conditions of use"
                    r"|full terms & conditions of access|to cite this article:|this article was downloaded by", re.I)
# A copyright or catalogue page (books): a cover only when little prose stands beside it, since an article's first
# page often carries a 「© … All rights reserved」 line.
_IMPRINT = re.compile(r"all rights reserved|library of congress|cataloging[- ]in[- ]publication|isbn[\s:-]*[\dx]"
                      r"|版权所有|图书在版编目|cip\s*数据", re.I)
_TOC_LINE = re.compile(r"(?:\.{3,}|…{2,}|\s)\s*[\divxlc]+\s*$", re.I)
MIN_PROSE = 500            # characters of sentence-length lines (CJK counted four-fold) for a page to count


def _prose(text: str) -> int:
    """How much running text a page holds: lines long enough to be sentences, CJK weighted like tokens."""
    n = 0
    for line in text.splitlines():
        line = line.strip()
        cjk = len(_CJK.findall(line))
        if cjk >= 15 or len(line) >= 40:
            n += cjk * 4 + (len(line) - cjk)
    return n


def substantive(text: str) -> bool:
    """Real body text, not a cover sheet, a title or copyright page, or a table of contents."""
    if _COVER.search(text):
        return False
    clean = _FOOTER.sub("", text)
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    if lines and sum(bool(_TOC_LINE.search(ln)) for ln in lines) * 2 >= len(lines) and len(lines) >= 5:
        return False
    prose = _prose(clean)
    if _IMPRINT.search(clean) and prose < MIN_PROSE * 3:
        return False
    return prose >= MIN_PROSE


def first_page(pages: list) -> Any | None:
    """The first page with real body text among the front pages; failing that, the one with the most prose."""
    front = pages[:LIGHT_SCAN_PAGES]
    for p in front:
        if substantive(p.text):
            return p
    return max(front, key=lambda p: _prose(_FOOTER.sub("", p.text)), default=None)


def _pages_block(pieces: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"=== p. {label} ===\n{text}" for label, text in pieces)


class PaperSummarize(JobSpec):
    kind = "paper.summarize"
    title = "Summarize a paper"
    Result = PaperSummary
    target_type = "paper"

    def _project_context(self, conn: sqlite3.Connection, paper_id: str) -> tuple[str, list[sqlite3.Row]]:
        from ...files import datafiles
        slugs = [r[0] for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ? ORDER BY project_slug", (paper_id,))]
        try:
            now = datafiles.read("now") or {}
        except Exception:  # noqa: BLE001
            now = {}
        lines: list[str] = []
        for slug in slugs:
            try:
                proj = datafiles.project(slug) or {}
            except Exception:  # noqa: BLE001
                proj = {}
            lines.append(f"Project: {proj.get('name', slug)} ({slug}). Project question: {proj.get('question', '(not set)')}")
            if str(now.get("project")) == slug:
                if now.get("question"):
                    lines.append(f"Current research question: {now['question']}")
                if now.get("puzzle"):
                    lines.append(f"Current puzzle: {now['puzzle']}")
        camps = conn.execute(
            f"SELECT id, label, core_claim FROM camps WHERE project_slug IN ({','.join('?' * len(slugs))}) ORDER BY sort, id",
            slugs).fetchall() if slugs else []
        return ("\n".join(lines) or "Project: none assigned. Leave relevance_to_project null."), camps

    def material(self, conn: sqlite3.Connection, paper_id: str, depth: str = "full") -> tuple[str, str]:
        """(mode, text) that goes to the model; exposed for tests and `rl paper show`. depth: light | full."""
        from ...intake import pipeline, sections
        paper = pipeline.get_paper(conn, paper_id)
        if paper is None:
            raise LookupError(paper_id)
        f = pipeline.primary_file(conn, paper_id)
        pages = pipeline.load_pages(conn, f["id"]) if f else []
        secs = pipeline.load_sections(conn, f["id"]) if f else []
        authors = json.loads(paper["authors_json"] or "[]")
        if depth == "light":
            return self._light(paper, authors, pages, secs)
        meta = [f"Title: {paper['title']}", f"Authors: {'; '.join(authors) or 'unknown'}",
                f"Year: {paper['year'] or 'unknown'}", f"Venue: {paper['venue'] or 'unknown'}",
                f"Kind: {paper['kind'] or 'unknown'}", f"DOI: {paper['doi'] or 'none'}",
                f"Metadata: {paper['metadata_status']}", f"Language: {paper['language'] or 'unknown'}",
                f"Pages: {len(pages)}"
                + (f" (printed {pages[0].label}–{pages[-1].label})" if pages else "")]
        body = sections.body_without_references(pages, secs)
        page_count = f["page_count"] if f else 0
        is_book = paper["kind"] == "book" or page_count > BOOK_PAGES
        if not is_book and _est_tokens(body) < FULL_TEXT_TOKENS:
            mode = "full text minus references"
            parts = ["## Metadata", *meta, "", "## Text", _pages_block(body)]
        else:
            mode = "outline: metadata, abstract, headings, introduction and conclusion"
            cap = SECTION_TOKENS * CHARS_PER_TOKEN
            abstract = paper["abstract"] or " ".join(t for _, t in sections.truncate(sections.section_text(pages, secs, "abstract"), 3000))
            headings = [f"- p. {pages[s.page_start].label if s.page_start < len(pages) else s.page_start + 1}: {s.heading}"
                        for s in secs if s.heading and s.kind != "references"]
            intro = sections.section_text(pages, secs, "introduction") or body[:3]
            concl = sections.section_text(pages, secs, "conclusion") or body[-2:]
            parts = ["## Metadata", *meta, "", "## Abstract", abstract or "(none found)", "",
                     "## Headings", *(headings or ["(none detected)"]), "",
                     "## Introduction", _pages_block(sections.truncate(intro, cap)), "",
                     "## Conclusion", _pages_block(sections.truncate(concl, cap))]
        return mode, "\n".join(parts)

    def _light(self, paper, authors: list[str], pages: list, secs: list) -> tuple[str, str]:
        from ...intake import sections
        meta = ["## Metadata", f"Title: {paper['title']}", f"Authors: {'; '.join(authors) or 'unknown'}",
                f"Year: {paper['year'] or 'unknown'}", f"Journal: {paper['venue'] or 'unknown'}"]
        abstract = (paper["abstract"] or "").strip() or " ".join(
            t for _, t in sections.truncate(sections.section_text(pages, secs, "abstract"), 3000)).strip()
        budget = LIGHT_TOKENS - light_tokens("\n".join(meta)) - 40          # 40 for the headings below
        abstract = _clip(abstract, int(budget * LIGHT_ABSTRACT_SHARE)) if abstract else ""
        parts = [*meta, "", "## Abstract", abstract or "(none found)"]
        page = first_page(pages)
        if page is not None:
            body = _clip(_FOOTER.sub("", page.text).strip(), budget - light_tokens(abstract))
            parts += ["", "## First page of the text", f"=== p. {page.label} ===\n{body}"]
        return "light: metadata, abstract and the first page of text", "\n".join(parts)

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        depth = summary_mode(conn)
        if depth == "light":
            return self._build_light(target_id, conn)
        mode, text = self.material(conn, target_id)
        project_ctx, camps = self._project_context(conn, target_id)
        cache = get_paths().cache
        cache.mkdir(parents=True, exist_ok=True)
        input_path = cache / f"{target_id}-summary-input.txt"
        input_path.write_text(text, encoding="utf-8")
        camp_lines = [f"- {c['id']}: {c['label']}" + (f" — {c['core_claim']}" if c["core_claim"] else "") for c in camps]
        user = "\n".join([
            "Task: read the paper material below and return a reading aid as one JSON object.",
            "",
            project_ctx,
            "Known camps in this project's literature map:" if camp_lines else "No camps are defined yet; propose a short label.",
            *camp_lines,
            "",
            directions_block(),
            "",
            "Rules:",
            *LANGUAGE_RULES,
            "- Use only the material below. Put the printed page labels from the '=== p. X ===' markers in each field's pages list.",
            "- Paraphrase in your own words. Never copy sentences from the text; there is no quote field.",
            "- Anything about events, dates or who did what is shown to the user as 'to verify'; keep it brief and page-cited.",
            "- When the material does not support a field, use null text and an empty pages list.",
            "- Describe only what this paper does. Never write what it does not discuss, and never bring in a topic, "
            "region, case or concept the paper itself does not raise; relevance_to_project is the only field that "
            "relates the paper to a project.",
            "",
            FIELD_GUIDE,
            "",
            "Example of the JSON shape (content is illustrative, not about this paper):",
            json.dumps(EXAMPLE, ensure_ascii=False),
            "",
            f"Material mode: {mode}. The same material is saved at {input_path}.",
            "",
            "# MATERIAL",
            text,
        ])
        req = ChatRequest(messages=[{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}],
                          max_tokens=4000, json_mode=True,
                          params={"schema_name": "PaperSummary", "mode": mode, "summary_mode": "full"})
        return req, [str(input_path)]

    def _build_light(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        """Two fields from a short material: no project, camps or directions in the prompt either."""
        mode, text = self.material(conn, target_id, "light")
        cache = get_paths().cache
        cache.mkdir(parents=True, exist_ok=True)
        input_path = cache / f"{target_id}-summary-input.txt"
        input_path.write_text(text, encoding="utf-8")
        user = "\n".join([
            "Task: from the paper material below, return one JSON object with only research_question and argument.",
            "",
            "Rules:",
            "- Write every text value in Simplified Chinese, whatever language the paper is in; keep page labels "
            "exactly as printed. Personal names, journal names and acronyms may stay in their original form.",
            "- The material is the metadata, the abstract and the first page of the text only. Say what the paper "
            "asks and its core answer as far as these show; when they do not show it, use null text and no pages.",
            "- Put the printed page labels from the '=== p. X ===' marker in pages; the abstract has no page.",
            "- Paraphrase in your own words. Never copy sentences from the text; there is no quote field.",
            "",
            "Fields (each {\"text\": string or null, \"pages\": [printed page labels]}):",
            "- research_question: the question the paper asks.",
            "- argument: its core answer in one or two sentences.",
            "",
            "Example of the JSON shape (content is illustrative, not about this paper):",
            json.dumps({k: EXAMPLE[k] for k in LIGHT_FIELDS}, ensure_ascii=False),
            "",
            "# MATERIAL",
            text,
        ])
        req = ChatRequest(messages=[{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}],
                          max_tokens=800, json_mode=True,
                          params={"schema_name": "PaperSummary", "mode": mode, "summary_mode": "light"})
        return req, [str(input_path)]

    @staticmethod
    def _job_mode(conn: sqlite3.Connection, run_id: int) -> str:
        row = conn.execute("SELECT j.request_json FROM ai_runs r JOIN ai_jobs j ON j.id = r.job_id WHERE r.id = ?",
                           (run_id,)).fetchone()
        try:
            params = json.loads(row[0]).get("params") or {} if row else {}
        except (ValueError, AttributeError):
            params = {}
        return "light" if params.get("summary_mode") == "light" else "full"

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        from ...intake import directions
        assert isinstance(result, PaperSummary)
        result.mode = self._job_mode(conn, run_id)
        if result.mode == "light":
            # Only what the first page can carry; anything else the model filled in is guessed from too little.
            for name, info in PaperSummary.model_fields.items():
                if name not in (*LIGHT_FIELDS, "mode"):
                    setattr(result, name, info.get_default(call_default_factory=True))
        if result.direction.slug and directions.clean(result.direction.slug) is None:
            result.direction = DirectionGuess()          # an invented slug is dropped, not stored
        if result.camp_guess.camp == EXAMPLE["camp_guess"]["camp"]:
            result.camp_guess.camp = None                 # the example's placeholder copied back
        cur = conn.execute("UPDATE papers SET ai_summary_json = ?, ai_summary_run_id = ?, updated_at = ? WHERE id = ?",
                           (result.model_dump_json(), run_id, now_iso(), target_id))
        if cur.rowcount == 0:
            raise LookupError(f"paper {target_id} no longer exists")

    def example(self) -> dict[str, Any]:
        return EXAMPLE


register(PaperSummarize())


def depth_of(summary: dict | None) -> str | None:
    """light | full for a stored summary (one from before card 0122 has no mode and was read in full); None for none."""
    if not summary:
        return None
    return "light" if summary.get("mode") == "light" else "full"


def refresh_queued(conn: sqlite3.Connection) -> int:
    """Jobs still waiting that were built before light reading existed (no summary_mode in their request) are built
    again under the current setting (card 0122: 「排队中的照新规则跑」). A paper that already had a summary was queued
    by Leo's 重新生成, which then read in full: that request is kept as it was (card 0126). Claimed or finished jobs
    are left alone."""
    spec = PaperSummarize()
    n = 0
    for job in conn.execute("SELECT j.id, j.target_id, j.request_json, p.ai_summary_json FROM ai_jobs j "
                            "LEFT JOIN papers p ON p.id = j.target_id WHERE j.kind = ? AND j.status = 'queued'",
                            (spec.kind,)).fetchall():
        try:
            old = json.loads(job["request_json"])
            if "summary_mode" in (old.get("params") or {}):
                continue
            if job["ai_summary_json"]:
                old["params"] = {**(old.get("params") or {}), "summary_mode": "full"}
                conn.execute("UPDATE ai_jobs SET request_json = ? WHERE id = ? AND status = 'queued'",
                             (json.dumps(old, ensure_ascii=False), job["id"]))
                continue
            req, files = spec.build(job["target_id"], conn)
        except Exception:  # noqa: BLE001 - a paper gone or unreadable keeps its old request
            continue
        conn.execute("UPDATE ai_jobs SET request_json = ?, input_files_json = ? WHERE id = ? AND status = 'queued'",
                     (json.dumps(req.to_json(), ensure_ascii=False), json.dumps(files), job["id"]))
        n += 1
    return n
