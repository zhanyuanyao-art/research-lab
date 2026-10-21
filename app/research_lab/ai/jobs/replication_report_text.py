"""replication.report_text: the words of a replication report that only a reader of the paper can write (card 0196).

Everything else in the report and the deck is assembled by rules (replication/report_content.py): the methods come from
the reviewed chapters, the results from the run and its comparison. This job writes the rest, for one plan, in one
request: the study's question and competing explanations, its findings as the paper states them, the data and how the key
variable is measured, one sentence per detected method on how this paper uses it, and the takeaways and questions for
the seminar. Stored as exports/ai_text.json beside the deck; the deck reads it when present.

Request layout follows DeepSeek's caching (docs/design/REPLICATION-v5.md, card 0197): the same system message and the
same opening paper text as methods.replicate, so a second request for one paper is mostly a cache hit; what changes
(plan, methods, comparison) comes after it, the task last. Output is the cost, so the fields are short and capped.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ...db.connection import now_iso
from ..provider import ChatRequest, JobSpec, system_rules
from ..registry import register

MAX_TOKENS = 4000
SHORT, LINE = 24, 90
SUPPORT = ("支持", "部分支持", "不支持")
CAPS = {"models": 4, "sources": 4, "measurement": 3, "findings": 4, "takeaways": 4, "questions": 5}


_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _cut(v: Any, n: int) -> str:
    s = " ".join(_CONTROL.sub(" ", str(v or "")).split())
    return s[:n]


def _cut_all(v: Any, n: int = LINE) -> Any:
    """Every string inside the model's answer, cut: lists and objects too, not only the top level."""
    if isinstance(v, str):
        return _cut(v, n)
    if isinstance(v, list):
        return [_cut_all(x, n) for x in v[:12]]
    if isinstance(v, dict):
        return {_cut(k, 24): _cut_all(x, n) for k, x in list(v.items())[:16]}
    return v


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tag: str = ""
    name: str = ""
    claim: str = ""
    measure: str = ""
    support: str = ""          # 支持 | 部分支持 | 不支持 | "" — what the paper's own results say about this explanation

    @field_validator("support", mode="before")
    @classmethod
    def _support(cls, v: Any) -> str:
        v = str(v or "").strip()
        return v if v in SUPPORT else ""


class Source(BaseModel):
    model_config = ConfigDict(extra="ignore")
    big: str = ""
    name: str = ""
    what: str = ""


class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    what: str = ""


class Result(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title_zh: str = ""
    study_sub: str = ""
    question_title: str = ""
    question: str = ""
    models: list[Model] = Field(default_factory=list, max_length=4)
    findings: list[str] = Field(default_factory=list, max_length=4)
    data_sub: str = ""
    unit: str = ""
    sources: list[Source] = Field(default_factory=list, max_length=4)
    measurement_title: str = ""
    measurement_lede: str = ""
    measurement: list[Step] = Field(default_factory=list, max_length=3)
    measurement_note: str = ""
    methods_sub: str = ""
    method_usage: dict[str, str] = Field(default_factory=dict)
    takeaways: list[str] = Field(default_factory=list, max_length=4)
    questions: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("models", "sources", "measurement", "findings", "takeaways", "questions", mode="before")
    @classmethod
    def _cap(cls, v: Any, info: ValidationInfo) -> list[Any]:
        """A longer list than asked for is cut, not refused: refusing would pay for a whole second answer."""
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return v[:CAPS[info.field_name]]

    @field_validator("method_usage", mode="before")
    @classmethod
    def _usage(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        return {str(k)[:20]: str(x) for k, x in list(v.items())[:12]}


def _paper_block(conn: sqlite3.Connection, paper_id: str) -> str:
    from ...replication import plans
    block = getattr(plans, "paper_block", None)       # card 0197 shares this opening with methods.replicate
    if block is not None:
        return block(conn, paper_id)
    mat = plans.methods_material(conn, paper_id)
    text = "\n\n".join(f"[p. {label}]\n{body}" for label, body in mat["pieces"])
    return f"<paper_text>\n{text}\n</paper_text>"


def _context(conn: sqlite3.Connection, plan_id: str) -> tuple[str, str]:
    """(paper id, the plan-specific context): the plan's design fields, the methods the code uses, and how the
    replication came out. Every string is outside text, so it is cut and fenced."""
    from ...replication import coverage, method_notes, plans
    from ...replication import workspace as W
    plan = plans.get(conn, plan_id)
    if plan is None or not plan.get("paper_id"):
        raise LookupError(f"no plan with a paper: {plan_id}")
    pl = plan.get("plan") or {}
    lines = [f"unit: {_cut(pl.get('unit_of_analysis'), 200)}", f"period: {_cut(pl.get('period'), 120)}",
             f"sample: {_cut(pl.get('sample_rule'), 200)}",
             "model: " + _cut(json.dumps(pl.get("model") or {}, ensure_ascii=False), 600)]
    for d in (pl.get("data_sources") or [])[:6]:
        if isinstance(d, dict):
            lines.append(f"data source: {_cut(d.get('name'), 120)} — {_cut(d.get('url_or_where'), 200)}")
    scripts, _ = coverage.author_texts(plan_id)
    found = method_notes.detect(scripts)
    keys = [f"{f.key} ({f.chapter.name if f.chapter else f.key}; scripts: {', '.join(_cut(os.path.basename(s), 60) for s in f.scripts[:3])})"
            for f in found]
    run = W.latest_valid(conn, plan_id)
    summary = ""
    if run is not None:
        comp = ((run.get("workspace") or {}).get("comparison") or {}).get("summary") or {}
        summary = _cut(comp.get("text") or json.dumps(comp.get("counts") or {}, ensure_ascii=False), 300)
    ctx = ("<plan>\n" + "\n".join(lines) + "\n</plan>\n\n"
           "Methods found in the author's code, by key (write method_usage for these keys only):\n"
           + ("\n".join(f"- {k}" for k in keys) or "- (none found)") + "\n\n"
           + (f"How the replication came out: {summary}\n\n" if summary else ""))
    return plan["paper_id"], ctx


EXAMPLE = {
    "title_zh": "顾问与外交决策中的倾向聚合",
    "study_sub": "谁的倾向决定了外交决策",
    "question_title": "一群人开会，结果听谁的",
    "question": "国安会的决定是集体做出的，与会者的鹰派或鸽派倾向怎样变成一个决定",
    "models": [{"tag": "涌现模型", "name": "大家的平均", "claim": "决策反映所有与会者倾向的平均",
                "measure": "与会者平均鹰派程度", "support": "不支持"},
               {"tag": "顾问模型", "name": "谁发言多听谁", "claim": "总统依赖顾问提供信息和方案，发言越多影响越大",
                "measure": "顾问鹰派程度，按发言次数加权", "support": "支持"}],
    "findings": ["顾问越鹰派，会议做出的冲突性决策越多", "总统本人的倾向在会议层面不显著"],
    "data_sub": "四十年的国安会会议记录",
    "unit": "一次会议是一个观测；月度分析另用美国卷入争端的月度数据",
    "sources": [{"big": "2,685", "name": "场国安会会议", "what": "逐场整理的会议记录，含与会者和发言"}],
    "measurement_title": "鹰派程度怎么测",
    "measurement_lede": "历史人物没法发问卷，借问卷受访者的答案按背景相似来推",
    "measurement": [{"name": "训练", "what": "用传记特征预测问卷受访者的鹰派得分"},
                    {"name": "外推", "what": "把同一模型用在国安会成员身上"},
                    {"name": "汇总", "what": "每场会议算平均、总统、顾问加权三种值"}],
    "measurement_note": "复现包直接给了每人的分数，训练这一步没有附代码",
    "methods_sub": "两个因变量，两种回归",
    "method_usage": {"poisson": "数一场会议做出几项冲突性决策，用在表 3 的奇数列和表 5"},
    "takeaways": ["顾问模型最能解释会议层面的决策"],
    "questions": ["发言多就等于影响大吗，沉默的关键人物会不会被低估"],
}


class ReportText(JobSpec):
    kind = "replication.report_text"
    title = "Words of a replication report"
    Result = Result
    target_type = "plan"

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        paper_id, ctx = _context(conn, target_id)
        user = (_paper_block(conn, paper_id) + "\n\n" + ctx +
                "Task: write the words of a replication report on this paper for a seminar of IR graduate students, "
                "as one json object with exactly the fields of the example below. Work only from the paper text above "
                "and the plan; never invent a number, a dataset or a finding the text does not state, and leave a "
                "field empty rather than guess. models: the competing explanations or hypotheses as the paper frames "
                "them, 2 to 4, each with a short tag, a plain name, the claim, the variable that measures it, and support: "
                "支持, 部分支持 or 不支持 as the paper's own results judge it, or empty when the paper does not say. "
                "findings: what the paper reports, at most 4. sources: the data the paper uses, with a short headline "
                "number when the text prints one (big). measurement: how the key variable is built, up to 3 steps. "
                "method_usage: one sentence per method key listed above, on what this paper uses it for. takeaways: "
                "at most 4. questions: 3 to 5 questions worth discussing about the design, the measurement or the "
                "assumptions, each answerable from the paper. Style: Simplified Chinese (this job overrides the "
                "English rule), plain and short; titles at most 16 characters, every other value at most 60; no "
                "parentheses for asides; keep variable names as the paper writes them.\n\n"
                "Example of the json shape (content is from another paper; do not copy it):\n"
                + json.dumps(EXAMPLE, ensure_ascii=False))
        req = ChatRequest(messages=[{"role": "system", "content": system_rules()}, {"role": "user", "content": user}],
                          max_tokens=MAX_TOKENS)
        return req, []

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        from ...replication import deck
        from ...replication import workspace as W
        data = result.model_dump()
        out = {k: (_cut(v, SHORT if k.endswith(("_title", "_sub")) or k == "title_zh" else LINE)
                   if isinstance(v, str) else _cut_all(v)) for k, v in data.items()}
        out["ai_run_id"], out["created_at"] = run_id, now_iso()
        path = deck.exports_dir(target_id) / "ai_text.json"
        W._checked(W.workspace_dir(target_id), path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".ai_text.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def example(self) -> dict[str, Any]:
        return EXAMPLE


register(ReportText())
