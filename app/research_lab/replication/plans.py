"""Replication plans: `build()` packs a paper's methods/data/design text for methods.replicate,
`Result` is the plan schema, `apply()` stores the suggestion as an ai-suggested draft, and the
editing helpers back the plan page (checklist, variables, attached datasets, review).

A plan never carries results of ours. An AI plan counts only after the user marks it reviewed, which flips
origin to 'user' (method-layer §7 rule 5); only reviewed plans export to a research-design artifact.

Replication v2 step 1 (card 0157, docs/design/REPLICATION-v2.md): a plan also names its 复现目标, the numbers the
paper reports (replication_targets, never plan_json), its data links and its identifying assumptions. A target
comes from the paper's text only: every row has the page it is printed on, and apply() keeps an AI row only when
its value is on that page, so a number the model made up never lands. Targets count once Leo marks them reviewed;
until then the later steps stay shut."""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..config import get_paths
from ..db.connection import next_sequence_id, now_iso

STATUSES = ("draft", "reviewed", "in-progress", "done")
STATUS_LABELS = {"draft": "草稿", "reviewed": "已审阅", "in-progress": "进行中", "done": "完成"}
ROLES = ("DV", "IV", "control", "instrument", "moderator")
ROLE_LABELS = {"DV": "因变量", "IV": "自变量", "control": "控制变量", "instrument": "工具变量", "moderator": "调节变量"}
FAMILIES = ("OLS", "logit", "probit", "panel-FE", "event-history", "QCA", "process-tracing", "SNA", "descriptive", "other")
LEVELS = ("low", "medium", "high")
LEVEL_LABELS = {"low": "低", "medium": "中", "high": "高"}
METHODS_CHARS = 24_000
FALLBACK_CHARS = 20_000         # keyword-picked pages when no methods section is found: ~4–5 pages. With the results
                                # (RESULTS_CHARS at most) and the availability windows the request tops out near
                                # 50k characters, about 13k tokens
SCRAMBLED_LINE = 300            # a stored line this wide is a sideways page read in the wrong order
METHOD_HEADING_RE = re.compile(
    r"(method|data\b|dataset|design|empiric|measur|variable|operationali|sample|sampling|model|estimat|"
    r"identification|case selection|research strategy|coding|研究设计|研究方法|方法|数据|资料|变量|样本|模型|实证|测量|编码|案例选择)",
    re.I)
# With no methods section found, the methods pages are the ones that talk about design, data and measurement. Phrases
# that head such a passage weigh most, estimating and measuring verbs and variable talk next, the bare words data /
# model least ("model" runs through the theory too). Points per 1000 characters; a page under METHOD_PAGE_MIN is not
# methods material, and when no page reaches it the text from page 2 on goes, as before.
METHOD_KEYWORDS = (
    (re.compile(r"research design|empirical (?:strategy|approach|design|analysis|specification)|identification strategy|"
                r"estimation strategy|data and (?:methods?|measures?|research design)|(?:data|sample) (?:collection|description|"
                r"construction)|case selection|unit of analysis|operationali[sz]", re.I), 6),
    (re.compile(r"\bwe (?:estimate|measure|code|collect|construct|compile|regress|run|fit|model|analy[sz]e)\w*|"
                r"dependent variable|independent variable|control variables?|standard errors|regression|specification|"
                r"measur\w+|variables?\b|data ?set|survey|sample", re.I), 2),
    (re.compile(r"\bdata\b|\bmodels?\b|\bestimat\w+", re.I), 1),
)
METHOD_PAGE_MIN = 3.0
METHOD_PAGE_MAX_CHARS = 9_000   # a page this long is a diagram or a scrambled page, not running text
FORBIDDEN_KEYS = {"quote", "quotes", "quotation", "verbatim", "results", "findings", "coefficients", "verdict"}
# The plan's output cap. Only generated tokens are billed, so a high cap costs nothing on a plan that ends sooner;
# a low one cuts the JSON and pays for a second reply (run 344 of 10-16 stopped inside its targets at the 3500 cap
# card 0181 then raised; the two plans after it used 4,247 and 4,365 tokens of 8000 before their repairs).
PLAN_MAX_TOKENS = 16_000
MODEL_SPEC_CHARS = 1_500        # `model` sent as a list is merged into one specification of at most this many characters
# The table blocks of the results pages, after the methods (about 1–2万 token in all). Card 0170: an APSR paper's
# whole pages are about 6k characters each, so 12k held two pages and the main regression tables never arrived.
RESULTS_CHARS = 24_000
DA_PAGES = 3                    # pages that say where the replication files are, best first
DA_CHARS = 1_800                # per page: from the paragraph that names the files, not the whole page
# A caption line, not a sentence that starts with "Table 2 presents…": the number is followed by punctuation, a
# capitalized title or the end of the line.
CAPTION_LINE_RE = re.compile(
    r"^\s*(?:(?:TABLE|FIGURE|Table|Figure|Tab\.|Fig\.)\s*(?:[A-Z]\.?\s*)?(?:\d+|[IVX]{1,4}(?![A-Za-z]))[a-z]?"
    r"(?:\s*[.:—–-]|\s+[A-Z(]|\s*$)"
    r"|[表图]\s*\d+)")
TABLE_CAPTION_RE = re.compile(r"\s*(?:TABLE|Table|Tab\.|表)")
_SE_RE = re.compile(r"\(\s*[-−–]?\d*\.\d+\s*\)")                    # (0.051)
_STARRED_RE = re.compile(r"\d\s*(?:\*|∗|†)+")                         # 0.086∗∗
_ROW_RE = re.compile(r"\b(?:observations|obs\.|n\s*=|r-?squared|r2|r²|adj(?:usted)?\.? r|fixed effects|fe|constant|"
                     r"clustered|log[- ]likelihood|aic|bic|controls)\b", re.I)
_NUM_TOKEN_RE = re.compile(r"^[(\[]?[-−–]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)[)\]]?(?:\*|∗|†)*$")   # 0.086∗∗ (0.037) -.15 4,126
LINK_KINDS = ("dataverse", "osf", "github", "author", "other")
LINK_KIND_LABELS = {"dataverse": "Dataverse", "osf": "OSF", "github": "GitHub", "author": "作者主页", "other": "其他"}
LINK_ACCESS = ("public", "request")
LINK_ACCESS_LABELS = {"public": "公开", "request": "需申请"}
# Where a paper says its replication files are. Journals stamp the margin ("Published online by Cambridge University
# Press"), so the stored text can break a DOI over lines and drop those words into the gap: DVN/\nonline   GZW94R.
# One line break at most between the parts of a DOI, never a blank line (a caption can follow a DOI that ends a line);
# stamp words may sit in the gap. The id is six uppercase letters or digits, and a stitched one must have a digit.
_GAP = r"[ \t]*(?:\r?\n[ \t]*)?"
_STRAY = r"(?:[A-Za-z]{2,14}(?:[ \t]+|(?=\r?\n)))"
_DVN_RE = re.compile(
    rf"(?i:10\.7910{_GAP}/{_GAP}DVN{_GAP}/)[ \t]*{_STRAY}{{0,3}}?(?:\r?\n[ \t]*{_STRAY}{{0,3}}?)?"
    r"([A-Z0-9]{6}|[A-Z0-9]{1,5}-?[ \t]*\r?\n[ \t]*[A-Z0-9]{1,5})(?![A-Za-z0-9])")
_URL_RE = re.compile(r"""https?://[^\s<>"'`]+""", re.I)
_BARE_URL_RE = re.compile(r"""(?<![/\w.@-])(?:(?:www\.)?github\.com|osf\.io|gitlab\.com|zenodo\.org|figshare\.com|datadryad\.org)/[^\s<>"'`]+""", re.I)
_DATA_DOI_RE = re.compile(r"""\b10\.(?:5281|17605|3886|5061|6084)/[^\s<>"'`]+""")     # Zenodo, OSF, ICPSR, Dryad, figshare
REPO_HOSTS = ("dataverse", "osf.io", "github.com", "gitlab.com", "zenodo.org", "figshare.com", "datadryad.org", "icpsr")
PUBLISHER_HOSTS = ("cambridge.org", "jstor.org", "wiley.com", "tandfonline.com", "sciencedirect.com", "springer.com",
                   "oup.com", "sagepub.com", "doi.org")
_DA_HEAD_RE = re.compile(
    r"^\s*(?:(?:data|code|software|replication|materials?)(?:\s*(?:,|and|&)\s*(?:data|code|software|materials?))*\s+"
    r"(?:availability|access)|(?:replication|reproducibility)\s+(?:files?|materials?|data|packages?|archives?|code)|"
    r"availability\s+of\s+(?:data|replication|materials?|code))\b", re.I)
_DA_STRONG_RE = re.compile(
    r"data availability statement|availability of (?:the )?replication|(?:data|code) (?:and|&) (?:code|data) availability|"
    r"code availability|replication (?:files?|materials?|data|packages?|archives?|code)|dataverse|10\.7910\s*/\s*DVN", re.I)
_DA_WEAK_RE = re.compile(r"data availability|availability of (?:the )?data", re.I)     # also plain prose: «data availability is low»
_DA_REPO_RE = re.compile(r"dataverse|osf\.io|github\.com|gitlab\.com|zenodo|figshare|datadryad|openicpsr|10\.7910\s*/\s*DVN", re.I)
ASSUMPTION_STATUSES = ("待检验", "成立", "不成立", "无法检验")


class PlanError(ValueError):
    pass


class StalePlan(PlanError):
    """A section of the plan changed since the form was drawn: nothing is written (the route answers 409)."""


class StaleTargets(PlanError):
    """The 复现目标 changed since the form was drawn: nothing is written (the route answers 409)."""


# ---------------------------------------------------------------- result schema

def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


class DataSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)
    access: str = ""
    url_or_where: str = ""


class Variable(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["DV", "IV", "control", "instrument", "moderator"]
    name: str = Field(min_length=1)
    measure: str = ""
    source: str = ""

    @field_validator("role", mode="before")
    @classmethod
    def _role(cls, v: Any) -> Any:
        m = {"dv": "DV", "dependent": "DV", "outcome": "DV", "iv": "IV", "independent": "IV", "treatment": "IV",
             "control": "control", "controls": "control", "covariate": "control", "instrument": "instrument",
             "iv-instrument": "instrument", "moderator": "moderator"}
        return m.get(_lower(v), v)


def _family_of(v: Any) -> str:
    key = _lower(v)
    for f in FAMILIES:
        if key == f.lower():
            return f
    aliases = {"fe": "panel-FE", "fixed effects": "panel-FE", "panel": "panel-FE", "survival": "event-history",
               "sna": "SNA", "network": "SNA", "qca": "QCA", "ols": "OLS", "regression": "OLS"}
    return aliases.get(key, "other")


class Model(BaseModel):
    model_config = ConfigDict(extra="ignore")
    family: Literal["OLS", "logit", "probit", "panel-FE", "event-history", "QCA", "process-tracing", "SNA", "descriptive", "other"] = "other"
    specification: str = ""

    @model_validator(mode="before")
    @classmethod
    def _one_object(cls, data: Any) -> Any:
        """DeepSeek sent `model` as a list of {family, specification}, one per estimated model, in both plan runs that
        succeeded on 10-16 and 10-17, and each paid for a whole repair reply. One object holds them: the family they
        share (other when they differ, each item then naming its own in front of its text) and the specifications
        joined, at most MODEL_SPEC_CHARS. A bare string is the specification."""
        if isinstance(data, str):
            return {"specification": data}
        if not isinstance(data, list):
            return data
        items = [x if isinstance(x, dict) else {"specification": x} for x in data if x not in (None, "", {})]
        families = [_family_of(x.get("family")) for x in items]
        shared = families[0] if len(set(families)) == 1 else "other"
        parts: list[str] = []
        size = 0
        for item, family in zip(items, families):
            if size > MODEL_SPEC_CHARS:
                break
            text = _text(item.get("specification"))
            if shared == "other" and family != "other":
                text = f"{family}：{text}" if text else family
            if text and text not in parts:
                parts.append(text)
                size += len(text) + 1
        joined = "；".join(parts)
        if len(joined) > MODEL_SPEC_CHARS:
            joined = joined[:MODEL_SPEC_CHARS - 1].rstrip("；，, ") + "…"
        return {"family": shared, "specification": joined}

    @field_validator("family", mode="before")
    @classmethod
    def _family(cls, v: Any) -> Any:
        return _family_of(v)


class Feasibility(BaseModel):
    model_config = ConfigDict(extra="ignore")
    level: Literal["low", "medium", "high"] = "medium"
    why: str = ""

    @field_validator("level", mode="before")
    @classmethod
    def _level(cls, v: Any) -> Any:
        return _lower(v) or "medium"


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


def _text(v: Any) -> str:
    return " ".join(str(v if v is not None else "").split())


class Target(BaseModel):
    """One number the paper reports, in the format of REPLICATION-v2.md 附 (table, model, term, value, se, ci, sig, n,
    page, note), every value as printed. Not a result of ours: the page shows it as 论文报告值. value and se may be
    empty (a figure that only shows a direction); the page never is."""
    model_config = ConfigDict(extra="ignore")
    table: str = ""
    model: str = ""
    term: str = ""
    value: str = ""
    se: str = ""
    ci: str = ""
    sig: str = ""
    n: str = ""
    page: str = Field(min_length=1)
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        pick = lambda *keys: next((data[k] for k in keys if data.get(k) not in (None, "")), "")  # noqa: E731
        return {"table": pick("table", "label", "figure", "table_or_figure"), "model": pick("model", "column", "specification"),
                "term": pick("term", "coefficient", "variable"), "value": pick("value", "estimate", "reported_value"),
                "se": pick("se", "std_error", "standard_error"), "ci": pick("ci", "confidence_interval", "interval"),
                "sig": pick("sig", "significance", "stars", "p_value"), "n": pick("n", "N", "observations"),
                "page": pick("page", "page_label", "pages"), "note": pick("note", "notes", "comment")}

    @field_validator("table", "model", "term", "value", "se", "ci", "sig", "n", "page", "note", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> str:
        if isinstance(v, (list, tuple)):        # a CI given as [lo, hi]
            v = "[" + ", ".join(str(x) for x in v) + "]"
        return _text(v)

    @field_validator("page", mode="after")
    @classmethod
    def _page_label(cls, v: str) -> str:
        """The page as the PDF labels it, the key page_texts() checks against: models write 「p. 157」, 「pp. 157–158」,
        「Page 157」 or 「第157页」, and a label that does not match drops every value on that page."""
        m = re.match(r"^\s*(?:pp?\.?|pages?|第)?\s*([0-9ivxlcdm]+)", v, re.I)
        return m.group(1) if m else v

    @model_validator(mode="after")
    def _something(self) -> "Target":
        if not (self.value or self.note):
            raise ValueError("a target needs the reported value, or a note saying what the paper shows instead")
        return self


class Link(BaseModel):
    """Where the replication data lives, as the paper prints it: http(s) and public-looking only (feeds.net)."""
    model_config = ConfigDict(extra="ignore")
    url: str
    kind: Literal["dataverse", "osf", "github", "author", "other"] = "other"
    access: Literal["public", "request"] = "public"
    note: str = ""

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v: Any) -> str:
        from ..feeds import net
        url = _text(v).rstrip(".,;)")
        if not net.looks_public(url):
            raise ValueError(f"not a public http(s) link: {url[:80]}")
        return url

    @field_validator("access", mode="before")
    @classmethod
    def _access(cls, v: Any) -> Any:
        key = _lower(v) or "public"
        return "request" if key in ("request", "restricted", "申请", "需申请", "付费", "paid", "on request") else "public"

    @model_validator(mode="after")
    def _kind(self) -> "Link":
        if self.kind == "other":
            self.kind = link_kind(self.url)
        return self


ASSUMPTION_NAME_KEYS = ("name", "label", "title")
ASSUMPTION_BODY_KEYS = ("assumption", "statement", "description", "content", "detail", "details", "explanation")


class Assumption(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(min_length=1)
    status: str = "待检验"

    @model_validator(mode="before")
    @classmethod
    def _shapes(cls, data: Any) -> Any:
        """Models name the field differently (card 0172: all five of an acceptance run were dropped as empty). A bare
        string is the text; name and description both given read 「name：description」; otherwise the first of the
        usual keys, or the only string in the object."""
        if isinstance(data, str):
            return {"text": data}
        if not isinstance(data, dict) or _text(data.get("text")):
            return data
        d = dict(data)
        name = next((_text(d[k]) for k in ASSUMPTION_NAME_KEYS if _text(d.get(k))), "")
        body = next((_text(d[k]) for k in ASSUMPTION_BODY_KEYS if _text(d.get(k))), "")
        if name and body:
            d["text"] = body if name == body else f"{name}：{body}"
        elif name or body:
            d["text"] = name or body
        else:
            strings = [v for k, v in d.items() if k != "status" and isinstance(v, str) and v.strip()]
            if len(strings) == 1:
                d["text"] = strings[0]
        return d

    @field_validator("text", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _text(v)

    @field_validator("status", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return v if v in ASSUMPTION_STATUSES else "待检验"


def link_kind(url: str) -> str:
    host = re.sub(r"^https?://", "", url.lower()).split("/")[0]
    if "dataverse" in host or url.lower().startswith(("https://doi.org/10.7910", "http://doi.org/10.7910")):
        return "dataverse"
    if host.endswith("osf.io"):
        return "osf"
    if host.endswith("github.com") or host.endswith("githubusercontent.com"):
        return "github"
    return "other"


def _keep_valid(rows: Any, model: type[BaseModel]) -> tuple[list[dict], int]:
    """The rows that pass `model`; a row that does not (no page, a private link) is dropped, not the whole plan."""
    kept, dropped = [], 0
    for row in rows if isinstance(rows, list) else []:
        try:
            kept.append(model.model_validate(row).model_dump())
        except Exception:  # noqa: BLE001 - pydantic errors come in a few classes
            dropped += 1
    return kept, dropped


class Result(BaseModel):
    """What methods.replicate returns. No findings, no quotes, no conclusions; the only numbers are `targets`,
    each one as the paper prints it, with its page."""
    model_config = ConfigDict(extra="ignore")
    data_sources: list[DataSource] = Field(default_factory=list)
    unit_of_analysis: str = ""
    period: str = ""
    variables: list[Variable] = Field(default_factory=list)
    sample_rule: str = ""
    model: Model = Field(default_factory=Model)
    steps: list[str] = Field(default_factory=list)
    robustness: list[str] = Field(default_factory=list)
    feasibility: Feasibility = Field(default_factory=Feasibility)
    to_obtain: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _guard(cls, data: Any) -> Any:
        hit = _find_forbidden(data)
        if hit:
            raise ValueError(f"a replication plan may not carry '{hit}'; plans describe what to do, never what was found")
        if isinstance(data, dict):
            data = dict(data)
            notes = []
            for key, model, what in (("targets", Target, "条复现目标缺页码，或既没有数值也没有备注"), ("links", Link, "个链接不是公开的 http(s) 地址"),
                                     ("assumptions", Assumption, "条识别假设是空的")):
                if key in data:
                    data[key], dropped = _keep_valid(data[key], model)
                    if dropped:
                        notes.append(f"助手给出的 {dropped} {what}，已舍弃")
            if notes:
                data["caveats"] = [*(data.get("caveats") or []), *notes]
        return data

    @field_validator("steps", "robustness", "to_obtain", "caveats", mode="before")
    @classmethod
    def _str_list(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        out = []
        for x in v:
            if isinstance(x, dict):
                x = x.get("text") or x.get("step") or next(iter(x.values()), "")
            s = str(x).strip()
            if s:
                out.append(s)
        return out


EXAMPLE = {
    "data_sources": [{"name": "ASEAN 对话伙伴关系与论坛成员名单", "access": "公开", "url_or_where": "东盟秘书处网站历年主席声明"}],
    "unit_of_analysis": "国家—论坛—年",
    "period": "1994–2022",
    "variables": [
        {"role": "DV", "name": "领导人级别出席", "measure": "0/1，是否由政府首脑出席", "source": "峰会主席声明"},
        {"role": "IV", "name": "大国竞争强度", "measure": "论文第 9 页定义的年度指数", "source": "作者构造"},
        {"role": "control", "name": "主办国", "measure": "分类变量", "source": "峰会记录"},
    ],
    "sample_rule": "东亚峰会全部成员国—年，排除当年未召开峰会的年份",
    "model": {"family": "logit", "specification": "出席 ~ 竞争强度 + 主办国 + 年份固定效应，按国家聚类标准误"},
    "steps": ["收集历年主席声明并编码出席级别", "按论文定义重建竞争强度指数", "合并为国家—年面板", "估计基准 logit 模型"],
    "robustness": ["改用 OLS 线性概率模型", "去掉 2020–2021 线上峰会年份"],
    "feasibility": {"level": "medium", "why": "成员名单公开，但竞争强度指数的构造细节需要向作者索取"},
    "to_obtain": ["竞争强度指数的编码手册", "作者的复制数据（若有）"],
    "caveats": ["论文未说明线上峰会如何编码"],
    "targets": [{"table": "表 2", "model": "(1)", "term": "大国竞争强度", "value": "0.42", "se": "0.11", "ci": "",
                 "sig": "***", "n": "1,204", "page": "12", "note": ""}],
    "links": [{"url": "https://doi.org/10.7910/DVN/EXAMPLE", "kind": "dataverse", "access": "public", "note": "作者的复现数据"}],
    "assumptions": [{"text": "竞争强度对出席的影响不受主办国轮换的时点驱动", "status": "待检验"}],
}
# The shape of a reply, sent with every methods.replicate request (a Claude Code session in agent mode is shown the
# whole EXAMPLE as well). One item per list keeps it short. Compact, because one of the two plan replies of 10-16/17
# came back indented (474 line breaks) and that whitespace is output, which is most of what a plan costs. No link:
# a copied example URL would pass Link's check and land in the plan as if the paper had printed it.
EXAMPLE_SHAPE = json.dumps({k: [] if k == "links" else v[:1] if isinstance(v, list) else v for k, v in EXAMPLE.items()},
                           ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- material for the prompt

def _directions() -> list[dict[str, Any]]:
    from ..files import datafiles
    try:
        return [dict(d) for d in (datafiles.read("directions") or []) if isinstance(d, dict) and d.get("slug")]
    except Exception:  # noqa: BLE001 - a broken yml must not break plan building
        return []


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def paper_direction(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """The paper's own direction if set, else the direction of its first project."""
    if _has_column(conn, "papers", "direction"):
        row = conn.execute("SELECT direction FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row and row[0]:
            return str(row[0])
    from ..files import datafiles
    projects = {str(p.get("slug")): p for p in (datafiles.read("projects") or []) if isinstance(p, dict)}
    for r in conn.execute("SELECT project_slug FROM paper_projects WHERE paper_id = ? ORDER BY project_slug", (paper_id,)):
        d = (projects.get(r[0]) or {}).get("direction")
        if d:
            return str(d)
    return None


def _slice(pages: dict[int, Any], start: Any, end: Any | None, last: int) -> list[tuple[str, str]]:
    out = []
    stop = end.page_start if end else last
    for idx in range(start.page_start, stop + 1):
        p = pages.get(idx)
        if p is None:
            continue
        lines = p.text.splitlines()
        # a heading that shares its line with the other column: the section's text also runs in that column above
        # the heading's line, so its whole first page goes in
        inline = getattr(start, "inline", False)
        lo = start.line_no if idx == start.page_start and start.line_no >= 0 and not inline else 0
        hi = end.line_no if end is not None and idx == end.page_start and end.line_no >= 0 else len(lines)
        chunk = "\n".join(lines[lo:hi]).strip()
        if chunk:
            out.append((p.label, chunk))
    return out


def _sections(conn: sqlite3.Connection, file_id: int, pages: list[Any]) -> list[Any]:
    """The stored sections, or a fresh detection when the stored ones have no methods heading: papers imported
    before card 0170 have no ALL-CAPS headings stored, and detection is a pure pass over the pages."""
    from ..intake import pipeline
    from ..intake import sections as secmod
    stored = pipeline.load_sections(conn, file_id)
    if any(_is_method_section(s) for s in stored):
        return stored
    fresh = secmod.detect(pages)
    return fresh if any(_is_method_section(s) for s in fresh) else stored


def _is_method_section(s: Any) -> bool:
    return s.kind in ("methods", "data") or (s.kind == "heading" and bool(s.heading) and bool(METHOD_HEADING_RE.search(s.heading)))


# ---------------------------------------------------------------- page material, cached (card 0185)
#
# Choosing the methods, results and availability pages, and reading sideways pages again from the PDF, depends on
# nothing but the stored pages and the code. A job, `rl data plan --show` and the plan page each used to do it
# again; the result is now kept under <data>/cache/replication/, one JSON file per piece, and reused while the
# file's sha256, its stored page text and sections, and the selection code are all the same. Anything that does not
# match is a miss and is recomputed and rewritten, so a stale entry cannot be served.

SELECTION_VERSION = 1       # bump when what the pickers choose changes; edits to this file or to intake/ also count
CACHE_MAX_AGE_DAYS = 90     # an entry nobody has read for this long goes (a read refreshes its time)
CACHE_MAX_BYTES = 100 * 1024 * 1024
_CACHE_NAME = re.compile(r"[0-9a-f]{24}\.[0-9a-f]{12}\.v\d+\.[0-9a-f]{12}\.[a-z]+\.json(?:\.\d+\.tmp)?")   # what _cache_path writes


@functools.lru_cache(maxsize=1)
def _code_fingerprint() -> str:
    from ..config import APP_VERSION
    from ..intake import extract, sections
    h = hashlib.sha1(APP_VERSION.encode())
    for mod in (sys.modules[__name__], extract, sections):
        try:
            h.update(Path(mod.__file__).read_bytes())
        except (OSError, TypeError):      # a bundle without readable sources: the app version still tells releases apart
            pass
    return h.hexdigest()[:12]


def _stored_digest(conn: sqlite3.Connection, file_id: Any) -> str:
    h = hashlib.sha1()
    for r in conn.execute("SELECT page_index, page_label, text FROM paper_pages WHERE file_id = ? ORDER BY page_index", (file_id,)):
        h.update(f"{r[0]}\0{r[1]}\0{r[2]}\1".encode())
    for r in conn.execute("SELECT kind, heading, page_start, page_end FROM paper_sections WHERE file_id = ? "
                          "ORDER BY page_start, rowid", (file_id,)):
        h.update(f"{r[0]}\0{r[1]}\0{r[2]}\0{r[3]}\2".encode())
    return h.hexdigest()[:12]


def _cache_path(conn: sqlite3.Connection, f: Any, part: str) -> Path | None:
    """None when the file has no usable sha256 (nothing is cached for it)."""
    try:
        sha, file_id = str(f["sha256"]), f["id"]
    except (KeyError, IndexError, TypeError):
        return None
    if not re.fullmatch(r"[0-9a-f]{24,}", sha):
        return None
    name = f"{sha[:24]}.{_stored_digest(conn, file_id)}.v{SELECTION_VERSION}.{_code_fingerprint()}.{part}.json"
    return get_paths().cache / "replication" / name


def _cached(conn: sqlite3.Connection, f: Any, part: str, compute: Callable[[], Any], tag: str = "") -> Any:
    """compute() once per (file, stored text, code, tag). The value goes through JSON: callers get lists, not tuples."""
    path = _cache_path(conn, f, part)
    if path is None:
        return compute()
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        if blob["tag"] == tag:
            try:
                os.utime(path)                    # in use: the age cap counts from the last read
            except OSError:
                pass
            return blob["value"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    value = compute()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"tag": tag, "value": value}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        _prune_cache(conn, path, part)
    except OSError:
        pass                                      # a cache that cannot be written is only a slower run
    return value


def _prune_cache(conn: sqlite3.Connection, keep: Path, part: str) -> None:
    """After a write, drop what can no longer be used: this file's entries from other versions of the code; entries
    of a sha no paper file has any more (a paper's file was replaced or deleted); leftovers of a crashed write and
    entries unread for CACHE_MAX_AGE_DAYS; then the oldest until the folder is under CACHE_MAX_BYTES."""
    live = {str(r[0])[:24] for r in conn.execute("SELECT sha256 FROM paper_files")}
    now, kept = time.time(), []
    for p in keep.parent.iterdir():
        if not _CACHE_NAME.fullmatch(p.name):
            continue                                  # not ours
        try:
            sha = p.name.split(".", 1)[0]
            st = p.stat()
            if p != keep and ((sha == keep.name.split(".", 1)[0] and p.name.endswith(f".{part}.json"))
                              or sha not in live or now - st.st_mtime > CACHE_MAX_AGE_DAYS * 86400):
                p.unlink(missing_ok=True)
            else:
                kept.append((st.st_mtime, st.st_size, p))
        except OSError:
            continue
    total = sum(size for _, size, _ in kept)
    for _, size, p in sorted(kept):
        if total <= CACHE_MAX_BYTES:
            break
        if p != keep:
            try:
                p.unlink(missing_ok=True)
                total -= size
            except OSError:
                pass


def _tag(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=sorted).encode()).hexdigest()[:12]


def _methods_material(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any]:
    """{'source': 'sections'|'pages'|'none', 'headings': [...], 'pieces': [(page label, text)]}. source 'pages' also
    says how they were picked: 'picked': 'keywords' (the pages that talk about design, data, measurement) or 'order'
    (page 2 on, when no page does)."""
    from ..intake import pipeline
    from ..intake import sections as secmod
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return {"source": "none", "headings": [], "pieces": []}
    pages = pipeline.load_pages(conn, f["id"])
    if not pages:
        return {"source": "none", "headings": [], "pieces": []}
    secs = sorted(_sections(conn, f["id"], pages), key=lambda s: (s.page_start, s.line_no))
    by_index = {p.index: p for p in pages}
    last = pages[-1].index
    pieces: list[tuple[str, str]] = []
    headings: list[str] = []
    for i, s in enumerate(secs):
        if not _is_method_section(s):
            continue
        nxt = next((t for t in secs[i + 1:] if t.line_no >= 0 and not _is_method_section(t)), None)
        if nxt is None:
            refs = next((t for t in secs[i + 1:] if t.kind == "references"), None)
            nxt = refs
        headings.append(s.heading or s.kind)
        for piece in _slice(by_index, s, nxt, last):
            if piece not in pieces:
                pieces.append(piece)
    if pieces:
        return {"source": "sections", "headings": headings, "pieces": secmod.truncate(pieces, METHODS_CHARS)}
    read = upright_pages(conn, f)
    body = [(label, text) for (label, text) in secmod.body_without_references(read, secs)]
    # pages 2..N: the first page is title/abstract matter
    labels_after_first = {p.label for p in read[1:]} if len(read) > 1 else {p.label for p in read}
    body = [(l, t) for (l, t) in body if l in labels_after_first]
    picked = keyword_pages(body)
    if picked:
        return {"source": "pages", "picked": "keywords", "headings": [], "pieces": picked}
    return {"source": "pages", "picked": "order", "headings": [], "pieces": secmod.truncate(body, FALLBACK_CHARS)}


def methods_material(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any]:
    """_methods_material(), kept per file (see the cache note above)."""
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return _methods_material(conn, paper_id)
    mat = _cached(conn, f, "methods", lambda: _methods_material(conn, paper_id))
    return {**mat, "pieces": [tuple(x) for x in mat["pieces"]]}


def method_density(text: str) -> float:
    """Design / data / measurement talk in a page, points per 1000 characters."""
    return 1000 * sum(w * len(rx.findall(text)) for rx, w in METHOD_KEYWORDS) / max(len(text), 1)


def keyword_pages(body: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The pages that read most like a methods section, densest first until FALLBACK_CHARS, in page order. Table pages
    stay out: the results block sends their table, and a regression table is not a description of the design."""
    from ..intake import sections as secmod
    ranked = sorted(((method_density(t), i) for i, (_, t) in enumerate(body)
                     if len(t) <= METHOD_PAGE_MAX_CHARS and not looks_like_regression(t)), reverse=True)
    chosen, used = [], 0
    for density, i in ranked:
        if density < METHOD_PAGE_MIN or used + len(body[i][1]) > FALLBACK_CHARS:
            continue
        chosen.append(i)
        used += len(body[i][1])
    if not chosen and ranked and ranked[0][0] >= METHOD_PAGE_MIN:        # the best page alone is over the budget
        return secmod.truncate([body[ranked[0][1]]], FALLBACK_CHARS)
    return [body[i] for i in sorted(chosen)]


def _table_like(line: str) -> bool:
    tokens = line.split()
    nums = sum(1 for tok in tokens if _NUM_TOKEN_RE.match(tok))
    if nums >= 2 and nums / len(tokens) >= 0.4:
        return True
    return bool(_ROW_RE.search(line)) and bool(re.search(r"\d|\b(?:yes|no)\b", line, re.I))


def _prose(line: str) -> bool:
    """A running-text line: some column of it holds six or more words (table headers are short cells)."""
    return any(len(re.findall(r"[A-Za-z]{2,}", seg)) >= 6 for seg in re.split(r"\s{3,}", line.strip()))


def _notes_end(lines: list[str], last: int, stop: int | None = None) -> int:
    """The line after a table's notes: what is printed right under its last row, up to the next blank gap, the next
    caption or `stop`."""
    k, blank, kept = last + 1, 0, 0
    stop = len(lines) if stop is None else stop
    while k < stop:
        if CAPTION_LINE_RE.match(lines[k]):
            break
        if not lines[k].strip():
            blank += 1
            if blank >= 2:
                break
        else:
            blank, kept = 0, kept + 1
            if kept > 6:
                break
        k += 1
    return k


def _caption_spans(lines: list[str]) -> list[tuple[int, int]]:
    """(first line, line after the last) of each captioned table or figure: the caption through the numeric rows and
    the notes. A figure with no numeric lines gives its caption and whatever follows it up to the gap."""
    spans, i = [], 0
    while i < len(lines):
        if not CAPTION_LINE_RE.match(lines[i]):
            i += 1
            continue
        last, prose, j = None, 0, i + 1
        while j < len(lines) and j - i < 150:
            line = lines[j]
            if CAPTION_LINE_RE.match(line):
                break
            if _table_like(line):
                last, prose = j, 0
            elif _prose(line):
                prose += 1
                if prose >= 3:
                    break
            j += 1
        k = _notes_end(lines, i if last is None else last)
        spans.append((i, k))
        i = max(k, i + 1)
    return spans


def table_blocks(text: str) -> list[str]:
    """Each captioned table or figure on a page, from its caption line through the numeric rows, plus the notes
    printed right under them; the prose around it is left out."""
    lines = text.splitlines()
    return ["\n".join(lines[i:k]).strip() for i, k in _caption_spans(lines)]


MIN_TABLE_ROWS = 4          # numeric rows that make a run of lines a table


def _numeric_spans(lines: list[str], taken: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Tables with no caption on the page (the caption is on the page before, or lost): a run of numeric rows with the
    row labels between them, the few header lines above it and the notes under it. Lines in `taken` stay out."""
    def covered(j: int) -> bool:
        return any(a <= j < b for a, b in taken)

    spans, i = [], 0
    while i < len(lines):
        if covered(i) or not _table_like(lines[i]):
            i += 1
            continue
        rows, last, prose, j = 1, i, 0, i + 1
        while j < len(lines) and not covered(j) and j - i < 200:
            if _table_like(lines[j]):
                rows, last, prose = rows + 1, j, 0
            elif _prose(lines[j]):
                prose += 1
                if prose >= 3:
                    break
            j += 1
        if rows < MIN_TABLE_ROWS:
            i = last + 1
            continue
        first = i
        while first > 0 and i - first < 4 and lines[first - 1].strip() and not _prose(lines[first - 1]) and not covered(first - 1):
            first -= 1
        spans.append((first, _notes_end(lines, last, min((a for a, _ in taken if a > last), default=None))))
        i = spans[-1][1]
    return spans


def looks_like_regression(text: str) -> bool:
    """Regression output by its cells alone, caption or none: standard errors in parentheses in bulk, or a few with
    starred estimates, or starred numbers over several numeric rows (a lone `m1*` in a formula is not that)."""
    se, stars = len(_SE_RE.findall(text)), len(_STARRED_RE.findall(text))
    if se >= 6 or (se >= 3 and stars >= 3):
        return True
    return stars >= 5 and sum(1 for line in text.splitlines() if _table_like(line)) >= 4


def page_blocks(text: str) -> tuple[list[str], bool]:
    """(the table blocks of a page, whether any of them has a caption). A page that looks like regression output
    also gives its uncaptioned numeric runs, so the main table is found without its «TABLE 3»."""
    lines = text.splitlines()
    spans = _caption_spans(lines)
    captioned = bool(spans)
    if looks_like_regression(text):
        spans = sorted(spans + _numeric_spans(lines, spans))
    return ["\n".join(lines[a:b]).strip() for a, b in spans], captioned


def table_score(text: str) -> int:
    """How much a page's table material looks like regression output: standard errors in parentheses, starred
    estimates, Observations / N / R² / fixed-effects rows, lines of numbers."""
    lines = text.splitlines()
    return (3 * len(_SE_RE.findall(text)) + 2 * len(_STARRED_RE.findall(text))
            + 5 * sum(1 for line in lines if _ROW_RE.search(line)) + sum(1 for line in lines if _table_like(line)))


def _scrambled(text: str) -> bool:
    """A stored page that may have been read in the wrong order: a landscape table comes out as lines hundreds of
    characters wide, or as numbers with no caption."""
    lines = text.splitlines()
    if any(len(line) >= SCRAMBLED_LINE for line in lines):
        return True
    return looks_like_regression(text) and not any(CAPTION_LINE_RE.match(line) for line in lines)


def upright_pages(conn: sqlite3.Connection, f: sqlite3.Row | dict[str, Any]) -> list[Any]:
    """The stored pages, with each page that lies sideways in the PDF read again, turned upright, from the stored file
    (read-only; nothing is written back). Papers imported before intake turned sideways pages keep the scrambled text
    in the library, which is where the main regression table of a landscape page went missing."""
    from ..intake import extract, pipeline
    pages = pipeline.load_pages(conn, f["id"])
    try:
        path = get_paths().data / str(f["rel_path"])
    except (KeyError, IndexError, TypeError):
        return pages
    if not path.exists():
        return pages
    # the PDF is opened and every page's rotation read once per file; a paper with no sideways page caches an empty {}
    fresh = _cached(conn, f, "upright", lambda: {str(i): t for i, t in extract.sideways_texts(
        path, {p.index for p in pages if _scrambled(p.text or "")}).items()})
    return [dataclasses.replace(p, text=fresh[str(p.index)]) if str(p.index) in fresh else p for p in pages]


def _references_at(conn: sqlite3.Connection, f: sqlite3.Row | dict[str, Any], pages: list[Any]) -> int | None:
    return next((s.page_start for s in _sections(conn, f["id"], pages) if s.kind == "references"), None)


def results_material(conn: sqlite3.Connection, paper_id: str,
                     sent: set[str] | dict[str, str] = frozenset()) -> list[tuple[str, str]]:
    """_results_material(), kept per file and per `sent` (see the cache note above)."""
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return []
    rows = _cached(conn, f, "tables", lambda: _results_material(conn, paper_id, sent), tag=_tag(sent))
    return [tuple(x) for x in rows]


def _results_material(conn: sqlite3.Connection, paper_id: str,
                      sent: set[str] | dict[str, str] = frozenset()) -> list[tuple[str, str]]:
    """The tables and figures, where the reported values are: each page's table blocks (the whole page when a captioned
    page has none that can be cut out), the most regression-like pages first until RESULTS_CHARS, returned in page
    order. A page with a caption is a candidate; so is one with no caption whose cells look like regression output
    (a landscape table read upright, a table continued from the page before), and only its table block goes.
    `sent` is what already went with the methods: page labels, or label → text, in which case a page is left out
    only when its blocks are all inside that text."""
    from ..intake import sections as secmod
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return []
    pages = upright_pages(conn, f)
    refs = _references_at(conn, f, pages)
    found = []
    for p in pages:
        text = p.text or ""
        if (refs is not None and p.index >= refs) or not re.search(r"\d", text):
            continue
        blocks, captioned = page_blocks(text)
        if not blocks:
            continue
        if isinstance(sent, dict) and p.label in sent:
            if all(b in sent[p.label] for b in blocks):
                continue
        elif p.label in sent:
            continue
        body = "\n\n".join(blocks)
        # numbers a table's block missed; a figure's caption alone is enough, the prose around it is not sent
        if table_score(body) == 0 and table_score(text) > 0 and any(TABLE_CAPTION_RE.match(b) for b in blocks):
            body = text.strip()
        found.append((p.index, p.label, body, table_score(body)))
    chosen, used = [], 0
    for idx, label, body, _ in sorted(found, key=lambda c: (-c[3], c[0])):
        if used + len(body) <= RESULTS_CHARS:
            chosen.append((idx, label, body))
            used += len(body)
        elif not chosen:                        # the best page alone is over the budget: send its start
            chosen.append((idx, label, secmod.truncate([(label, body)], RESULTS_CHARS)[0][1]))
            used = RESULTS_CHARS
    return [(label, body) for _, label, body in sorted(chosen)]


def page_texts(conn: sqlite3.Connection, paper_id: str) -> dict[str, str]:
    """page label → its text, for checking a reported value against the page it is said to be on."""
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    return {str(p.label): p.text or "" for p in upright_pages(conn, f)} if f is not None else {}


# ---------------------------------------------------------------- where the replication files are

def _link_key(url: str) -> str:
    """Two spellings of one address compare equal: scheme, www., dx., case and a trailing slash do not count."""
    u = re.sub(r"^doi:\s*", "doi.org/", url.strip().lower())
    return re.sub(r"^https?://(?:www\.|dx\.)?", "", u).rstrip("/.,;")


def _dvn_id(m: re.Match[str]) -> str | None:
    """The six-character Dataverse id behind a match, whole or broken by one line break (a hyphen may end the first
    line). An id that does not follow the slash directly (a space, a line break, stamp words) or was cut by one must
    contain a digit: a capital word starting the next line («FIGURE 3») is not an id."""
    raw = m.group(1)
    ident = re.sub(r"[-\s]", "", raw)
    if len(ident) != 6 or not re.fullmatch(r"[A-Z0-9]{6}", ident):
        return None
    prefix = m.group(0)[:m.start(1) - m.start(0)]
    broken = bool(prefix[prefix.rindex("/") + 1:]) or ident != raw       # anything between the last slash and the id
    return ident if not broken or any(c.isdigit() for c in ident) else None


def find_links(statement: str) -> list[str]:
    """Replication-data addresses printed in the availability statement, by rule: Harvard Dataverse DOIs (also when a
    line break or the journal's margin stamp splits them: «doi:10.7910/DVN/ GZW94R»), Dataverse / OSF / GitHub /
    Zenodo / figshare / Dryad / ICPSR addresses, and any other address except the publisher's own. `statement` is only
    that section (availability_pages cuts it): a third-party DOI cited elsewhere on the page is not looked at.
    Returned as full https URLs, in order of appearance, without repeats."""
    found: list[tuple[int, str]] = []

    def add(pos: int, url: str) -> None:
        url = url.rstrip(".,;:)]}>'\"")
        if url and _link_key(url) not in {_link_key(u) for _, u in found}:
            found.append((pos, url))

    for m in _DVN_RE.finditer(statement):
        ident = _dvn_id(m)
        if ident:
            add(m.start(), f"https://doi.org/10.7910/DVN/{ident}")
    for m in _URL_RE.finditer(statement):
        url = m.group(0).rstrip(".,;:)]}>'\"")
        host = re.sub(r"^https?://", "", url.lower()).split("/")[0].split(":")[0]
        if "10.7910" in url:
            continue                            # a Dataverse DOI: _DVN_RE has the whole id, or it was cut short
        data_doi = host.endswith("doi.org") and bool(_DATA_DOI_RE.search(url))
        if data_doi or any(h in host for h in REPO_HOSTS) or not any(host.endswith(h) for h in PUBLISHER_HOSTS):
            add(m.start(), url)
    for m in _BARE_URL_RE.finditer(statement):
        add(m.start(), "https://" + m.group(0))
    for m in _DATA_DOI_RE.finditer(statement):
        add(m.start(), "https://doi.org/" + m.group(0))
    return [u for _, u in sorted(found)]


def merge_links(existing: list[dict[str, Any]], found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`existing` plus the rule-found rows that are not already there. A model link that is only the beginning of a
    found address (a DOI cut off before its id) is replaced by the whole one."""
    have = [_link_key(row["url"]) for row in existing]
    new = [_link_key(row["url"]) for row in found]
    kept = [row for row, k in zip(existing, have) if not any(n != k and n.startswith(k) for n in new)]
    keys = {_link_key(row["url"]) for row in kept}
    return kept + [row for row, k in zip(found, new) if k not in keys]


_REFERENCES_LINE_RE = re.compile(r"^\s*(?:references?|bibliography|works cited)(?=.{0,30}$)", re.I)   # short: a stamp can follow it


_SINGLE_HEADINGS = {"acknowledgments", "acknowledgements", "acknowledgment", "notes", "funding", "appendix", "conclusion",
                    "conclusions", "bibliography", "footnotes", "disclosure", "contributors", "supplementary"}
_HEADING_LINE_RE = re.compile(r"[A-Z][A-Za-z’'&-]*(?:\s+(?:and|of|the|for|&|[A-Z][A-Za-z’'&-]*)){0,6}")


def _heading_line(line: str) -> bool:
    """A section heading line: short, title-cased, no digits (so «GZW94R» on the line after a broken DOI is not one),
    two words or a known one-word heading."""
    s = line.strip()
    return bool(s) and len(s) <= 60 and bool(_HEADING_LINE_RE.fullmatch(s)) and (
        len(s.split()) >= 2 or s.lower() in _SINGLE_HEADINGS)


STATEMENT_LINES = 25


def _statement(lines: list[str]) -> str:
    """The availability section, from its first line to the next heading, the References line or a blank gap of two
    lines, whichever comes first. Every address in it counts as the paper's own; one further down the page is a
    footnote, a citation or somebody else's data."""
    out, blank = [], 0
    for i, line in enumerate(lines[:STATEMENT_LINES]):
        if i and (_heading_line(line) or _REFERENCES_LINE_RE.match(line)):
            break
        blank = 0 if line.strip() else blank + 1
        if blank >= 2 and out:
            break
        out.append(line)
    return "\n".join(out)


def _before_references(conn: sqlite3.Connection, f: sqlite3.Row | dict[str, Any], pages: list[Any]) -> list[tuple[Any, list[str]]]:
    """(page, its lines) for every page the paper's own text is on: the pages before the references, and on the page
    where the references begin the lines above the heading (a statement often shares it). Nothing after."""
    ref = next((s for s in _sections(conn, f["id"], pages) if s.kind == "references"), None)
    out = []
    for p in pages:
        lines = (p.text or "").splitlines()
        if ref is not None and p.index > ref.page_start:
            continue
        if ref is not None and p.index == ref.page_start:
            cut = ref.line_no if ref.line_no >= 0 else next(
                (i for i, line in enumerate(lines) if _REFERENCES_LINE_RE.match(line)), 0)
            lines = lines[:cut]
        out.append((p, lines))
    return out


def availability_pages(conn: sqlite3.Connection, paper_id: str) -> list[dict[str, Any]]:
    """_availability_pages(), kept per file (see the cache note above)."""
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return []
    return _cached(conn, f, "avail", lambda: _availability_pages(conn, paper_id))


def _availability_pages(conn: sqlite3.Connection, paper_id: str) -> list[dict[str, Any]]:
    """The pages that say where the paper's replication files are, at most DA_PAGES, in page order: each with its
    label, the full page text, `window` (what goes to the model: the paragraph that names the files, up to DA_CHARS)
    and `statement` (the section only, up to the next heading or the references: the one place a link is read from).
    A page counts when a
    «Data Availability» / «Replication files» heading is on it, or it says «replication files / materials / data»
    or «Dataverse» anywhere, or «data availability» twice or beside an address of a data repository.
    Pages after the references are left out, and on the page where they begin only the lines above the heading count:
    a cited data set is not this paper's."""
    from ..intake import pipeline
    f = pipeline.primary_file(conn, paper_id)
    if f is None:
        return []
    found = []
    for p, lines in _before_references(conn, f, upright_pages(conn, f)):
        text = "\n".join(lines)
        if not text.strip():
            continue
        head = next((i for i, line in enumerate(lines) if _DA_HEAD_RE.match(line) and len(line.strip()) <= 70), None)
        strong, weak, repo = len(_DA_STRONG_RE.findall(text)), len(_DA_WEAK_RE.findall(text)), bool(_DA_REPO_RE.search(text))
        if head is None and not (strong or weak >= 2 or (weak and repo)):
            continue
        at = head if head is not None else next(
            i for i, line in enumerate(lines)
            if _DA_STRONG_RE.search(line) or _DA_WEAK_RE.search(line) or _DA_REPO_RE.search(line))
        for _ in range(6 if head is None else 0):      # a sentence: start at its paragraph
            if at == 0 or not lines[at - 1].strip():
                break
            at -= 1
        tail = lines[at:]
        window = "\n".join(tail).strip()
        if len(window) > DA_CHARS:
            window = window[:DA_CHARS].rsplit("\n", 1)[0].rstrip() + " […]"
        statement = _statement(tail)
        score = (10 if head is not None else 0) + 3 * min(strong, 3) + min(weak, 3) + (4 if repo else 0)
        found.append({"index": p.index, "label": p.label, "text": text, "window": window, "statement": statement,
                      "score": score})
    best = sorted(found, key=lambda d: (-d["score"], d["index"]))[:DA_PAGES]
    return sorted(best, key=lambda d: d["index"])


def links_from_pages(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Link rows (url, kind, access, note) the rules find on availability_pages(), validated like any link: a
    non-public address is dropped. The model is not asked; apply() merges these into the plan."""
    rows = []
    for d in found:
        for url in find_links(d["statement"]):
            rows.append({"url": url, "note": f"第 {d['label']} 页数据可得性部分，由规则抽出"})
    return _keep_valid(rows, Link)[0]


def availability_links(conn: sqlite3.Connection, paper_id: str) -> list[dict[str, Any]]:
    return links_from_pages(availability_pages(conn, paper_id))


def _bare(text: str) -> str:
    """Numbers compared as printed, minus the typesetting: the Unicode minus and dashes, thin spaces, runs of space."""
    t = str(text or "").replace("\u2212", "-").replace("\u2013", "-").replace("\u2009", " ").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t)


def printed_on(value: str, page_text: str) -> bool:
    """Is this value printed on the page? Brackets and stars around it may differ, and 0.23 is not on a page that
    only prints 0.231. A value with spaces in it (an interval) also matches with the spaces squeezed out."""
    core = _bare(value).strip("()[]*†‡§ ")
    if not core:
        return False
    page = _bare(page_text)
    if re.search(rf"(?<![\d.]){re.escape(core)}(?!\d)", page):
        return True
    return " " in core and core.replace(" ", "") in page.replace(" ", "")


def printed_interval(ci: str, page_text: str) -> bool:
    """Both ends of an interval are printed on the page, whatever brackets or dash join them there."""
    ends = re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", _bare(ci).replace(",", " "))
    return len(ends) == 2 and all(printed_on(e, page_text) for e in ends)


def printed_count(n: str, page_text: str) -> bool:
    """A sample size is printed on the page, with or without thousands separators (1,204 = 1204 = 1 204)."""
    digits = re.sub(r"[,\s]", "", _bare(n).strip("()[]* "))
    if not digits.isdigit():
        return printed_on(n, page_text)
    head = len(digits) % 3 or 3
    groups = [digits[:head]] + [digits[i:i + 3] for i in range(head, len(digits), 3)]
    return bool(re.search(rf"(?<![\d.,]){'[, ]?'.join(groups)}(?!\d|[.,]\d)", _bare(page_text)))


def has_methods_section(conn: sqlite3.Connection, file_id: int) -> bool:
    for r in conn.execute("SELECT kind, heading FROM paper_sections WHERE file_id = ?", (file_id,)):
        if r["kind"] in ("methods", "data") or (r["kind"] == "heading" and r["heading"] and METHOD_HEADING_RE.search(r["heading"])):
            return True
    return False


def candidate_papers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Papers with a text layer, methods-section papers first."""
    rows = conn.execute(
        "SELECT p.id, p.title, p.year, p.authors_json, f.id AS file_id, f.page_count, "
        "(SELECT COUNT(*) FROM replication_plans rp WHERE rp.paper_id = p.id) AS n_plans "
        "FROM papers p JOIN paper_files f ON f.paper_id = p.id AND f.id = (SELECT id FROM paper_files WHERE paper_id = p.id "
        "ORDER BY is_primary DESC, id LIMIT 1) WHERE f.has_text_layer = 1 ORDER BY p.updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["authors"] = json.loads(r["authors_json"] or "[]")
        d["has_methods"] = has_methods_section(conn, r["file_id"])
        out.append(d)
    out.sort(key=lambda d: (not d["has_methods"],))
    return out


def _summary_lines(conn: sqlite3.Connection, paper: sqlite3.Row) -> list[str]:
    raw = paper["ai_summary_json"] if "ai_summary_json" in paper.keys() else None
    if not raw:
        return []
    try:
        s = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(s, dict) and "summary" in s and isinstance(s["summary"], dict):
        s = s["summary"]
    lines = []
    for key in ("research_question", "argument", "mechanism", "method", "period", "level_of_analysis"):
        v = s.get(key) if isinstance(s, dict) else None
        text = v.get("text") if isinstance(v, dict) else v
        if text:
            pages = v.get("pages") if isinstance(v, dict) else None
            lines.append(f"- {key}: {text}" + (f" (pp. {', '.join(map(str, pages))})" if pages else ""))
    cases = s.get("cases") if isinstance(s, dict) else None
    if cases:
        lines.append(f"- cases: {', '.join(map(str, cases))}")
    return lines


def _paper_material(conn: sqlite3.Connection, paper_id: str) -> tuple[str, list[str]]:
    """paper_block()'s text, and the replication links the rules read from the availability pages."""
    mat = methods_material(conn, paper_id)
    if not mat["pieces"]:
        raise PlanError("这篇论文没有可用的正文文本（无文本层或未导入 PDF）")
    sent: dict[str, str] = {}
    for label, body in mat["pieces"]:
        sent[label] = sent.get(label, "") + "\n" + body
    tables = results_material(conn, paper_id, sent)
    for label, body in tables:
        sent[label] = sent.get(label, "") + "\n" + body
    avail = availability_pages(conn, paper_id)
    avail_pieces = [(d["label"], d["window"]) for d in avail if d["window"] not in sent.get(d["label"], "")]
    text = "\n\n".join(f"[p. {label}]\n{body}" for label, body in mat["pieces"])
    table_text = "\n\n".join(f"[p. {label}]\n{body}" for label, body in tables)
    avail_text = "\n\n".join(f"[p. {label}]\n{body}" for label, body in avail_pieces)
    where = ("Methods / data / design sections: " + "; ".join(mat["headings"])) if mat["source"] == "sections" else \
        ("No methods section was detected; these are the pages that talk most about the design, data and measurement "
         "(picked by keywords), in page order." if mat.get("picked") == "keywords" else
         "No methods section was detected; the text is pages 2–N without the references.")
    block = (f"{where}\n\n<paper_text>\n{text}\n</paper_text>\n\n"
             + (f"Pages with the results tables and figures:\n<results_pages>\n{table_text}\n</results_pages>\n\n" if table_text else "")
             + (f"Where the paper says its replication data and files are:\n<availability>\n{avail_text}\n</availability>\n\n" if avail_text else ""))
    return block, [row["url"] for row in links_from_pages(avail)]


def paper_block(conn: sqlite3.Connection, paper_id: str) -> str:
    """The paper's own text, as a request about the paper opens its user message: the methods / data / design pages
    (<paper_text>), the results tables (<results_pages>) and the data-availability windows (<availability>), in that
    order, each with its label line; it ends with a blank line, so the caller's own text follows directly.

    DeepSeek caches a prompt by its exact opening, so two jobs that send the same system message (system_rules()) and
    start their user message with this block share the cache for the paper. It changes only with the paper's file,
    its page text or SELECTION_VERSION: the page pickers are cached per file (_cached) and nothing here reads the
    clock, the settings or the paper's summary and direction. Raises LookupError for an unknown paper and PlanError
    when the paper has no text."""
    if conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone() is None:
        raise LookupError(f"no paper {paper_id}")
    return _paper_material(conn, paper_id)[0]


def build(conn: sqlite3.Connection, paper_id: str):
    """The methods.replicate request for one paper. The user message opens with paper_block(), the part that stays
    the same from one request about the paper to the next, so DeepSeek's prefix cache covers it; what can change
    between two runs (the direction list, the reading aid, the rule links) follows it, and the task comes last."""
    from ..ai.provider import ChatRequest, system_rules
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise LookupError(f"no paper {paper_id}")
    block, rule_links = _paper_material(conn, paper_id)
    authors = ", ".join(json.loads(paper["authors_json"] or "[]")[:4])
    direction = paper_direction(conn, paper_id)
    dirs = _directions()
    if direction:
        dirs = [d for d in dirs if d["slug"] == direction] or dirs
    dir_lines = [f"- {d['slug']} ({d.get('name', '')}): {', '.join(map(str, d.get('keywords') or []))}" for d in dirs]
    summary = _summary_lines(conn, paper)
    user = (
        block
        + f"Paper: {paper['title']} ({paper['year'] or 'n.d.'}) — {authors or 'unknown authors'}\n"
        f"Direction {'of this paper' if direction else '(none set; all directions listed)'}:\n" + "\n".join(dir_lines or ["- (none)"]) + "\n\n"
        + ("The user's reading aid (Chinese summary, page-cited):\n" + "\n".join(summary) + "\n\n" if summary else "")
        + ("Replication links the program already read from these pages by rule; it adds them to the plan itself, so do "
           "not repeat them in `links`: " + ", ".join(rule_links) + "\n\n" if rule_links else "")
        + "Task: write a replication plan for this paper's empirical design, so the user can rebuild the data and "
        "re-run the analysis. Work only from the text above. Describe what the paper says it did and what the user "
        "would need to do; when the text is silent on something, say so in `caveats` or `to_obtain` rather than guessing. "
        "Never state findings, interpret results or judge the paper, and never quote it. The only numbers allowed are "
        "`targets`: the estimates the paper prints in its results tables and figures that a replication should "
        "reproduce (main models first, at most 20), each copied exactly as printed — table (Table 2 / Figure 3), model "
        "(the column, e.g. (3)), term (written as the paper writes it), value, se, ci, sig (the paper's own stars or "
        "p-value text), n, page (the label shown in [p. …] where the number is printed) and note. value and se may be "
        "empty when the paper gives none (a figure that only shows a direction: say so in note). Never compute, round, "
        "convert or infer a number; leave out anything you cannot see printed. "
        "links: replication data URLs exactly as printed in the text (kind dataverse|osf|github|author|other, access "
        "public|request, note) — never make up a URL; none printed means an empty list. "
        "assumptions: the identifying assumptions the design rests on (parallel trends, no anticipation, the timing of "
        "a staggered treatment, exclusion restriction…), each one short; status is always 待检验. "
        "Fields: data_sources (name, access: 公开/申请/付费/作者提供/未知, url_or_where as the paper describes it — do "
        "not invent URLs), unit_of_analysis, period, variables (role DV|IV|control|instrument|moderator, name, measure, "
        "source), sample_rule, model (one object, not a list: family: OLS|logit|probit|panel-FE|event-history|QCA|"
        "process-tracing|SNA|descriptive|other; specification in words or as a formula; several models go into the one "
        "specification), steps (ordered, each one action), robustness "
        "(checks the user could add), feasibility (level low|medium|high for a single researcher in year one, and why), "
        "to_obtain (data, codebooks or replication files to request), caveats (ambiguities in the text). "
        "Language: this job overrides the English rule — write every value in Simplified Chinese, keeping variable "
        "names, dataset names and formulas as the paper writes them.\n"
        "The shape of the reply, with one item in each list (give as many as the paper supports), written compact like "
        "this, without indentation or line breaks. Its values are made-up illustrations from an unrelated study and show "
        "the format only: never reuse any of them; the fields that state facts about this paper take their values from "
        "its text above. " + EXAMPLE_SHAPE
    )
    # PLAN_MAX_TOKENS is a ceiling, not a price (see there). With thinking on (a deep tier) the provider raises it to
    # its thinking floor, since the reasoning is counted against the same limit.
    req = ChatRequest(messages=[{"role": "system", "content": system_rules()}, {"role": "user", "content": user}],
                      max_tokens=PLAN_MAX_TOKENS)
    return req, []


# ---------------------------------------------------------------- records

def _plan_from_result(result: Result) -> dict[str, Any]:
    d = result.model_dump()
    d.pop("targets", None)              # 论文报告值 live in replication_targets, never in plan_json
    d["steps"] = [{"text": s, "done": False, "note": ""} for s in result.steps]
    d["datasets"] = []
    return d


def apply(result: Result, run_id: int | None, conn: sqlite3.Connection, paper_id: str) -> str:
    """Store the suggestion as an ai-suggested draft plan, with its targets. A target whose value is not printed on
    the page it cites is dropped and said so in the caveats: the model may copy numbers, never make them. Returns
    the plan id."""
    texts = page_texts(conn, paper_id) if result.targets else {}
    kept = [t for t in result.targets if all(printed_on(x, texts.get(t.page, "")) for x in (t.value, t.se) if x)]
    # the interval and the sample size are checked the same way (card 0163); a missing one costs the field, not the row
    dropped = 0
    for t in kept:
        page = texts.get(t.page, "")
        gone = [(key, label) for key, label, ok in (("ci", "区间", printed_interval), ("n", "样本量", printed_count))
                if getattr(t, key) and not ok(getattr(t, key), page)]
        for key, _ in gone:
            setattr(t, key, "")
        if gone:
            dropped += 1
            what = "和".join(label for _, label in gone)
            t.note = "；".join(x for x in (t.note, f"助手给的{what}在第 {t.page} 页找不到，已去掉") if x)
    plan = _plan_from_result(result)
    try:
        found = availability_links(conn, paper_id)          # by rule, from the data-availability pages
    except Exception:  # noqa: BLE001 - the rules are an extra; a page that cannot be read must not lose the plan
        found = []
    plan["links"] = merge_links(plan["links"], found)
    for a in plan["assumptions"]:
        a["status"] = "待检验"          # whether an assumption holds is for the later steps to test, not for the model
    if len(kept) < len(result.targets):
        plan["caveats"].append(f"助手给出的 {len(result.targets) - len(kept)} 条复现目标在所注页码上找不到它的数值或标准误，已舍弃")
    if dropped:
        plan["caveats"].append(f"{dropped} 条复现目标的区间或样本量在所注页码上找不到，已去掉这一项")
    plan_id = next_sequence_id(conn, "replication_plans", "RP")
    now = now_iso()
    conn.execute(
        "INSERT INTO replication_plans(id, paper_id, direction, status, plan_json, origin, ai_run_id, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (plan_id, paper_id, paper_direction(conn, paper_id), "draft", json.dumps(plan, ensure_ascii=False),
         "ai-suggested", run_id, now, now))
    _insert_targets(conn, plan_id, [t.model_dump() for t in kept], "ai-suggested")
    return plan_id


TARGET_FIELDS = ("table", "model", "term", "value", "se", "ci", "sig", "n", "page", "note")
_COLUMN = {"table": "label"}                      # `table` is SQL's word; the row stores it as label


def _insert_targets(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, Any]], origin: str | list[str],
                    tids: list[str] | None = None) -> None:
    """Rows in order; a row without a tid takes the plan's next one (T1, T2…), never one used before."""
    now = now_iso()
    cols = [_COLUMN.get(k, k) for k in TARGET_FIELDS]
    seq = int(conn.execute("SELECT targets_seq FROM replication_plans WHERE id = ?", (plan_id,)).fetchone()[0] or 0)
    for i, row in enumerate(rows):
        tid = (tids[i] if tids and i < len(tids) else "") or ""
        if not tid:
            seq += 1
            tid = f"T{seq}"
        conn.execute(f"INSERT INTO replication_targets(plan_id, tid, position, {', '.join(cols)}, origin, created_at, updated_at) "
                     f"VALUES (?,?,?,{','.join('?' * len(cols))},?,?,?)",
                     (plan_id, tid, i, *[str(row.get(k) or "") for k in TARGET_FIELDS],
                      origin[i] if isinstance(origin, list) else origin, now, now))
    conn.execute("UPDATE replication_plans SET targets_seq = ? WHERE id = ?", (seq, plan_id))


def targets(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    """The plan's 复现目标 as stored, in their order, with `table` as the format names it. Each is a 论文报告值 (as
    printed in the paper), not a result of ours."""
    out = []
    for r in conn.execute("SELECT * FROM replication_targets WHERE plan_id = ? ORDER BY position, id", (plan_id,)):
        d = dict(r)
        d["table"] = d.pop("label")
        out.append(d)
    return out


def _number(text: str) -> float | int | None:
    """A printed number as a number: −0.231, (0.054), 1,204, 4210; None for anything else."""
    t = _bare(text).strip().strip("()[]*†‡§ ").replace(",", "").replace("−", "-")
    try:
        x = float(t)
    except ValueError:
        return None
    return int(x) if re.fullmatch(r"-?\d+", t) else x


def _interval(text: str) -> list[float] | None:
    nums = re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?", _bare(text).replace(",", " "))
    return [float(nums[0]), float(nums[1])] if len(nums) == 2 else None


def target_records(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    """The targets in the shape of REPLICATION-v2.md 附 (what R4 compares against):
    {"id", "table", "model", "term", "value", "se", "ci", "sig", "n", "page", "value_text", "se_text", "note"},
    value / se / n as numbers or null, ci as [lo, hi] or null, page as a number when the label is one; sig and term
    as the paper prints them. value_text / se_text are the printed text (null when nothing is printed): the number
    alone loses the decimals the paper printed (0.140 is 0.14), and R4 counts them."""
    out = []
    for t in targets(conn, plan_id):
        page = t["page"]
        out.append({"id": t["tid"], "table": t["table"], "model": t["model"], "term": t["term"],
                    "value": _number(t["value"]), "se": _number(t["se"]), "ci": _interval(t["ci"]), "sig": t["sig"],
                    "n": _number(t["n"]), "page": int(page) if page.isdigit() else page,
                    "value_text": t["value"] or None, "se_text": t["se"] or None, "note": t["note"]})
    return out


def targets_version(conn: sqlite3.Connection, plan_id: str) -> str:
    """A fingerprint of the stored targets (ids, order, every field, origin). A form that carries it as
    `targets_version` is saved only while it still matches (card 0163)."""
    rows = conn.execute("SELECT tid, position, label, model, term, value, se, ci, sig, n, page, note, origin "
                        "FROM replication_targets WHERE plan_id = ? ORDER BY position, id", (plan_id,)).fetchall()
    return hashlib.sha256(json.dumps([list(r) for r in rows], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def save_targets(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, str]], *, version: str | None = None) -> int:
    """Leo's edit of the table, all rows at once, in one transaction. Each row keeps its id (`tid`) and a new one
    takes the next, never one used before; a row with no term, value or note is left out (that is how one is
    deleted). A row he changed counts as his. Targets he already reviewed stay reviewed: he is the one who changed
    them.

    Nothing is written when two rows carry the same id (PlanError), or when the table changed since the form was
    drawn (StaleTargets): with `version`, any change at all; without it, a target added or removed elsewhere, since
    the form always sends every row it showed."""
    _must(conn, plan_id)
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")      # read, check and write as one: a second save waits, then sees this one
    try:
        old = {t["tid"]: t for t in targets(conn, plan_id)}
        sent = [_text(r.get("tid")) for r in rows if _text(r.get("tid"))]
        dup = sorted({t for t in sent if sent.count(t) > 1})
        if dup:
            raise PlanError(f"复现目标的编号重复了（{'、'.join(dup)}），这次没有保存")
        if version is not None:
            if version != targets_version(conn, plan_id):
                raise StaleTargets("复现目标在别处改过，这次没有保存；刷新页面后再改")
        elif set(sent) != set(old):
            raise StaleTargets("复现目标在别处多了或少了几条，这次没有保存；刷新页面后再改")
        keep, origins, tids = [], [], []
        for r in rows:
            row = {k: _text(r.get(k)) for k in TARGET_FIELDS}
            if not (row["term"] or row["value"] or row["note"]):
                continue
            tid = _text(r.get("tid"))
            before = old.get(tid)
            tid = tid if before else ""                  # an id is only kept for a row that had it
            keep.append(row)
            tids.append(tid)
            same = before and all(before[k] == row[k] for k in TARGET_FIELDS)
            origins.append(before["origin"] if same else "user")
        conn.execute("DELETE FROM replication_targets WHERE plan_id = ?", (plan_id,))
        _insert_targets(conn, plan_id, keep, origins, tids)
        conn.execute("UPDATE replication_plans SET updated_at = ? WHERE id = ?", (now_iso(), plan_id))
        if own:
            conn.execute("COMMIT")
    except BaseException:
        if own:
            conn.execute("ROLLBACK")
        raise
    return len(keep)


def review_targets(conn: sqlite3.Connection, plan_id: str) -> None:
    """Leo has checked the targets against the paper: from now on they count, and the later steps open."""
    _must(conn, plan_id)
    if not targets(conn, plan_id):
        raise PlanError("还没有复现目标：先抽取或添加至少一条论文报告值")
    now = now_iso()
    conn.execute("UPDATE replication_targets SET origin = 'user', updated_at = ? WHERE plan_id = ?", (now, plan_id))
    conn.execute("UPDATE replication_plans SET targets_reviewed_at = ?, updated_at = ? WHERE id = ?", (now, now, plan_id))


def targets_reviewed(p: dict[str, Any]) -> bool:
    return bool(p.get("targets_reviewed_at"))


def save_links(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, str]], *, version: str | None = None) -> None:
    """The data links, all at once; an empty url leaves a row out. A link that is not a public http(s) address is
    refused with its text, so nothing else is lost silently."""
    out, bad = [], []
    for r in rows:
        if not _text(r.get("url")):
            continue
        try:
            link = Link.model_validate({"url": r.get("url"), "kind": r.get("kind") if r.get("kind") in LINK_KINDS else "other",
                                        "access": r.get("access") or "public", "note": _text(r.get("note"))})
        except Exception:  # noqa: BLE001
            bad.append(_text(r.get("url"))[:60])
            continue
        out.append(link.model_dump())
    if bad:
        raise PlanError("这些不是公开的 http(s) 链接：" + "、".join(bad))
    _edit(conn, plan_id, "links", version, lambda plan: plan.update(links=out))


def save_assumptions(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, str]], *, version: str | None = None) -> None:
    out = [{"text": _text(r.get("text")), "status": r.get("status") if r.get("status") in ASSUMPTION_STATUSES else "待检验"}
           for r in rows if _text(r.get("text"))]
    _edit(conn, plan_id, "assumptions", version, lambda plan: plan.update(assumptions=out))


def create_blank(conn: sqlite3.Connection, paper_id: str | None = None, direction: str | None = None) -> str:
    if paper_id and not conn.execute("SELECT 1 FROM papers WHERE id = ?", (paper_id,)).fetchone():
        raise PlanError(f"没有这篇论文：{paper_id}")
    plan_id = next_sequence_id(conn, "replication_plans", "RP")
    now = now_iso()
    conn.execute(
        "INSERT INTO replication_plans(id, paper_id, direction, status, plan_json, origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (plan_id, paper_id or None, direction or (paper_direction(conn, paper_id) if paper_id else None), "draft",
         json.dumps(_plan_from_result(Result()), ensure_ascii=False), "user", now, now))
    return plan_id


def enqueue(conn: sqlite3.Connection, paper_id: str, *, force: bool = False) -> str | None:
    """Queue methods.replicate for a paper and hand it to the dispatcher. None = one is already open."""
    from ..ai import dispatch, queue
    job_id = queue.enqueue(conn, "methods.replicate", paper_id, force=force)
    if job_id:
        dispatch.dispatch(job_id)
    return job_id


def plan_for_job(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute("SELECT rp.id FROM replication_plans rp JOIN ai_runs r ON r.id = rp.ai_run_id WHERE r.job_id = ? "
                       "ORDER BY rp.created_at DESC LIMIT 1", (job_id,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- what a replication has cost (card 0185)

LEDGER_STEPS = (("extract", "读论文、拟方案"), ("read", "解读分析结果"), ("workspace", "工作区里的 AI 运行"))


def _in(ids: list[Any]) -> str:
    return ",".join("?" * len(ids))


def _job_runs(conn: sqlite3.Connection, job_ids: list[str]) -> dict[str, Any]:
    """Every ai_runs row of these jobs, failed and retried attempts included: they were paid for too. Input is all
    the prompt (cached part included), output all the completion (reasoning included), as ai_runs stores them."""
    if not job_ids:
        return {"runs": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0, "models": []}
    r = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0), "
                     f"COALESCE(SUM(cache_hit_tokens), 0), COALESCE(SUM(cost_usd), 0), "
                     f"COALESCE(GROUP_CONCAT(DISTINCT model), '') FROM ai_runs WHERE job_id IN ({_in(job_ids)})",
                     job_ids).fetchone()
    return {"runs": r[0], "input": r[1], "output": r[2], "cached": r[3], "cost_usd": float(r[4]),
            "models": sorted(m for m in str(r[5]).split(",") if m)}


def _workspace_runs(conn: sqlite3.Connection, plan_ids: list[str]) -> dict[str, Any]:
    """The AI sessions of the workspace. One figure per session (the CLI reports a total), so no input / output split.
    replication_jobs has every session, including one that ended without a run; analysis_runs only adds a stored
    workspace run no job points at."""
    out: dict[str, Any] = {"runs": 0, "input": None, "output": None, "cached": None, "cost_usd": 0.0, "models": [], "tokens": 0}
    if not plan_ids:
        return out
    n, total = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM replication_jobs "
                            f"WHERE mode = 'ai' AND plan_id IN ({_in(plan_ids)})", plan_ids).fetchone()
    m, extra = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(tokens), 0) FROM analysis_runs WHERE method = 'workspace' "
                            f"AND tokens > 0 AND plan_id IN ({_in(plan_ids)}) "
                            f"AND id NOT IN (SELECT run_id FROM replication_jobs WHERE run_id IS NOT NULL)", plan_ids).fetchone()
    out.update(runs=n + m, tokens=int(total) + int(extra))
    return out


def _ledger(conn: sqlite3.Connection, extract_jobs: list[str], plan_ids: list[str]) -> dict[str, Any]:
    read_jobs = [r[0] for r in conn.execute(
        f"SELECT id FROM ai_jobs WHERE kind = 'analysis.read' AND target_id IN "
        f"(SELECT id FROM analysis_runs WHERE plan_id IN ({_in(plan_ids)}))", plan_ids)] if plan_ids else []
    found = {"extract": _job_runs(conn, extract_jobs), "read": _job_runs(conn, read_jobs),
             "workspace": _workspace_runs(conn, plan_ids)}
    steps, usd = [], 0.0
    for key, label in LEDGER_STEPS:
        d = found[key]
        d.setdefault("tokens", (d["input"] or 0) + (d["output"] or 0))
        if d["runs"] and d["tokens"]:
            steps.append({"key": key, "label": label, **d})
            usd += d["cost_usd"]
    from ..feeds.enrich import USD_CNY
    return {"tokens": sum(d["tokens"] for d in steps), "cost_usd": usd, "cost_cny": usd * USD_CNY, "steps": steps,
            "models": sorted({m for d in steps for m in d["models"]})}


def ledger(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    """The tokens one replication plan has used, by step: reading the paper into the plan (the extraction job behind
    plans.ai_run_id, with its retries), reading the analysis results, and the workspace's AI sessions.
    {tokens, cost_usd, cost_cny, models, steps: [{key, label, runs, tokens, input, output, cached, cost_usd, models}]};
    `steps` leaves out a step that cost nothing and `tokens` is 0 for a plan nothing was spent on. cost is what
    ai_runs recorded (none for Claude Code, which has no per-call price here, and none for workspace sessions)."""
    job = conn.execute("SELECT r.job_id FROM replication_plans rp JOIN ai_runs r ON r.id = rp.ai_run_id WHERE rp.id = ?",
                       (plan_id,)).fetchone()
    return _ledger(conn, [job[0]] if job else [], [plan_id])


def paper_ledger(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any]:
    """The same for everything spent on one paper's replication: every methods.replicate job on it (also those that
    produced no plan, or an earlier plan) and the later steps of all its plans. For the paper's issue page."""
    jobs = [r[0] for r in conn.execute("SELECT id FROM ai_jobs WHERE kind = 'methods.replicate' AND target_id = ?", (paper_id,))]
    plan_ids = [r[0] for r in conn.execute("SELECT id FROM replication_plans WHERE paper_id = ?", (paper_id,))]
    return _ledger(conn, jobs, plan_ids)


def _decode(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    plan = json.loads(r["plan_json"] or "{}")
    base = _plan_from_result(Result())
    base.update(plan)
    d["plan"] = base
    steps = base.get("steps") or []
    d["steps_done"] = sum(1 for s in steps if s.get("done"))
    d["steps_total"] = len(steps)
    return d


def get(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any] | None:
    r = conn.execute("SELECT rp.*, p.title AS paper_title, p.year AS paper_year FROM replication_plans rp "
                     "LEFT JOIN papers p ON p.id = rp.paper_id WHERE rp.id = ?", (plan_id,)).fetchone()
    return _decode(r) if r else None


def list_all(conn: sqlite3.Connection, paper_id: str | None = None) -> list[dict[str, Any]]:
    sql, args = ("SELECT rp.*, p.title AS paper_title, p.year AS paper_year FROM replication_plans rp "
                 "LEFT JOIN papers p ON p.id = rp.paper_id"), []
    if paper_id:
        sql += " WHERE rp.paper_id = ?"; args.append(paper_id)
    sql += " ORDER BY rp.updated_at DESC, rp.id DESC"
    return [_decode(r) for r in conn.execute(sql, args)]


# Every edit below reads the plan and writes back only its own keys of plan_json (json_set), under BEGIN IMMEDIATE:
# an import attaching a dataset while Leo saves another section can no longer drop the link (card 0168).
SECTIONS: dict[str, tuple[str, ...]] = {
    "fields": ("unit_of_analysis", "period", "sample_rule", "title", "model", "feasibility", "robustness", "to_obtain",
               "caveats"),
    "sources": ("data_sources",), "links": ("links",), "assumptions": ("assumptions",), "variables": ("variables",),
    "steps": ("steps",), "datasets": ("datasets",),
}


@contextmanager
def _locked(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def section_version(p: dict[str, Any], section: str) -> str:
    """A fingerprint of one section as stored (fields include the direction). A form that carries it is saved only
    while its section still matches: another section changing, or a dataset being attached, does not make it stale."""
    data: list[Any] = [p["plan"].get(k) for k in SECTIONS[section]]
    if section == "fields":
        data.append(p.get("direction"))
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def section_versions(p: dict[str, Any]) -> dict[str, str]:
    return {name: section_version(p, name) for name in SECTIONS}


def _patch(conn: sqlite3.Connection, plan_id: str, values: dict[str, Any]) -> None:
    """Write these top-level keys of plan_json and nothing else."""
    pairs = "".join(", ?, json(?)" for _ in values)
    args = [x for k, v in values.items() for x in (f"$.{k}", json.dumps(v, ensure_ascii=False))]
    conn.execute(f"UPDATE replication_plans SET plan_json = json_set(COALESCE(plan_json, '{{}}'){pairs}), updated_at = ? "
                 "WHERE id = ?", (*args, now_iso(), plan_id))


def _edit(conn: sqlite3.Connection, plan_id: str, section: str, version: str | None,
          change: Callable[[dict[str, Any]], Any]) -> Any:
    """Read, check the section's version (StalePlan when it moved on), change the plan, write back that section only."""
    with _locked(conn):
        p = _must(conn, plan_id)
        if version is not None and version != section_version(p, section):
            raise StalePlan("这一部分在别处改过，这次没有保存；刷新页面后再改")
        out = change(p["plan"])
        _patch(conn, plan_id, {k: p["plan"][k] for k in SECTIONS[section] if k in p["plan"]})
    return out


def _must(conn: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    p = get(conn, plan_id)
    if not p:
        raise PlanError(f"没有这个复现方案：{plan_id}")
    return p


def set_step(conn: sqlite3.Connection, plan_id: str, index: int, *, done: bool | None = None, note: str | None = None,
             text: str | None = None) -> dict[str, Any]:
    def change(plan: dict[str, Any]) -> dict[str, Any]:
        steps = plan["steps"]
        if not 0 <= index < len(steps):
            raise PlanError("没有这一步")
        if done is not None:
            steps[index]["done"] = bool(done)
        if note is not None:
            steps[index]["note"] = note.strip()
        if text is not None and text.strip():
            steps[index]["text"] = text.strip()
        return steps[index]
    return _edit(conn, plan_id, "steps", None, change)


def add_step(conn: sqlite3.Connection, plan_id: str, text: str) -> None:
    if not (text or "").strip():
        raise PlanError("步骤不能为空")
    _edit(conn, plan_id, "steps", None, lambda plan: plan["steps"].append({"text": text.strip(), "done": False, "note": ""}))


def remove_step(conn: sqlite3.Connection, plan_id: str, index: int) -> None:
    _edit(conn, plan_id, "steps", None, lambda plan: plan["steps"].pop(index) if 0 <= index < len(plan["steps"]) else None)


def save_variables(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, str]], *, version: str | None = None) -> None:
    out = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        role = r.get("role") if r.get("role") in ROLES else "control"
        out.append({"role": role, "name": name, "measure": (r.get("measure") or "").strip(), "source": (r.get("source") or "").strip()})
    _edit(conn, plan_id, "variables", version, lambda plan: plan.update(variables=out))


def save_sources(conn: sqlite3.Connection, plan_id: str, rows: list[dict[str, str]], *, version: str | None = None) -> None:
    out = [{"name": r["name"].strip(), "access": (r.get("access") or "").strip(), "url_or_where": (r.get("url_or_where") or "").strip()}
           for r in rows if (r.get("name") or "").strip()]
    _edit(conn, plan_id, "sources", version, lambda plan: plan.update(data_sources=out))


def _lines(text: str | None) -> list[str]:
    return [l.strip(" -•\t") for l in (text or "").splitlines() if l.strip(" -•\t")]


def save_fields(conn: sqlite3.Connection, plan_id: str, values: dict[str, str | None], *, version: str | None = None) -> None:
    def change(plan: dict[str, Any]) -> None:
        for k in ("unit_of_analysis", "period", "sample_rule", "title"):
            if values.get(k) is not None:
                plan[k] = values[k].strip()
        if values.get("model_family") is not None:
            fam = values["model_family"]
            plan["model"]["family"] = fam if fam in FAMILIES else "other"
        if values.get("model_specification") is not None:
            plan["model"]["specification"] = values["model_specification"].strip()
        if values.get("feasibility_level") is not None:
            lv = values["feasibility_level"]
            plan["feasibility"]["level"] = lv if lv in LEVELS else "medium"
        if values.get("feasibility_why") is not None:
            plan["feasibility"]["why"] = values["feasibility_why"].strip()
        for k in ("robustness", "to_obtain", "caveats"):
            if values.get(k) is not None:
                plan[k] = _lines(values[k])
        if values.get("direction") is not None:
            conn.execute("UPDATE replication_plans SET direction = ? WHERE id = ?", (values["direction"] or None, plan_id))
    _edit(conn, plan_id, "fields", version, change)


def attach_dataset(conn: sqlite3.Connection, plan_id: str, ds_id: str) -> None:
    """Add one dataset to the plan's list, read and written inside the write lock; nothing else in the plan moves."""
    if not conn.execute("SELECT 1 FROM datasets WHERE id = ?", (ds_id,)).fetchone():
        raise PlanError(f"没有这个数据集：{ds_id}")
    _edit(conn, plan_id, "datasets", None,
          lambda plan: plan["datasets"].append(ds_id) if ds_id not in plan["datasets"] else None)


def detach_dataset(conn: sqlite3.Connection, plan_id: str, ds_id: str) -> None:
    _edit(conn, plan_id, "datasets", None,
          lambda plan: plan["datasets"].remove(ds_id) if ds_id in plan["datasets"] else None)


def mark_reviewed(conn: sqlite3.Connection, plan_id: str) -> None:
    """User action: the plan now counts. origin → 'user', status → 'reviewed' (never moves backwards)."""
    p = _must(conn, plan_id)
    status = p["status"] if p["status"] in ("in-progress", "done") else "reviewed"
    conn.execute("UPDATE replication_plans SET origin = 'user', status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), plan_id))


def set_status(conn: sqlite3.Connection, plan_id: str, status: str) -> None:
    p = _must(conn, plan_id)
    if status not in STATUSES:
        raise PlanError(f"状态必须是 {', '.join(STATUSES)} 之一")
    if status != "draft" and p["origin"] == "ai-suggested":
        raise PlanError("AI 建议的方案需要先“标记为已审阅”")
    conn.execute("UPDATE replication_plans SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), plan_id))


def delete(conn: sqlite3.Connection, plan_id: str) -> None:
    from . import coverage
    _must(conn, plan_id)
    conn.execute("DELETE FROM replication_targets WHERE plan_id = ?", (plan_id,))
    coverage.delete_for_plan(conn, plan_id)
    conn.execute("DELETE FROM replication_plans WHERE id = ?", (plan_id,))
