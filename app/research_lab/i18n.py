"""Simplified-Chinese labels for the keys the UI shows (stages, artifact types, levels, statuses, job
kinds, milestone titles, matrix vocabulary). Keys stay English everywhere on disk and in the database;
only the display changes. A missing key falls back to the English label from `_data/*.yml` when there
is one, else to the key itself, so a new type or stage never renders blank."""
from __future__ import annotations

from typing import Any, Iterable

STAGE = {
    "idea": "想法", "exploring": "探索", "puzzle": "谜题", "evidence": "证据", "argument": "论证",
    "draft": "成稿", "submitted": "已投稿", "published": "已发表",
}
TYPE = {
    "puzzle": "谜题", "question": "研究问题", "literature-map": "文献地图", "concept": "概念",
    "case-map": "案例图", "evidence": "证据", "counterargument": "反驳论证", "mechanism": "机制",
    "paragraph": "段落", "memo": "备忘录", "analysis": "分析", "research-design": "研究设计",
    "draft": "草稿", "advisor-question": "导师问题", "landscape": "研究版图",
}
LEVEL = {"seed": "初稿", "developed": "成形", "paper-ready": "可入文"}
LEVEL_RANK = {"seed": 1, "developed": 2, "paper-ready": 3}


def due_zh(due: str | None) -> str:
    """'2026-11-15' → '11 月 15 日'; unreadable values come back as they are."""
    try:
        y, m, d = str(due)[:10].split("-")
        return f"{int(m)} 月 {int(d)} 日"
    except (ValueError, AttributeError):
        return str(due or "")
# Four visible kinds of research output (docs/design/PRODUCT.md); the fifteen stored types map onto them.
KIND = {"question": "问题", "literature": "文献地图", "data": "数据结果", "conclusion": "结论"}
KIND_ORDER = ["question", "literature", "data", "conclusion"]
ISSUE_STATUS = {"todo": "待办", "doing": "进行中", "done": "已完成", "parked": "搁置"}
ISSUE_PRIORITY = {"urgent": "紧急", "high": "高", "medium": "中", "low": "低", "none": "无优先级"}
# A 课题's output type (Leo 10-10, ISSUES-LINEAR.md §5): one of three, set on the 课题 and inherited by its
# sub-issues. Shown as 「类型：课题」 so it is not read as the 课题 level itself. The six labels below are
# the second group, 内容: what a step does, several or none.
ISSUE_TYPES = ("论文", "课题", "手稿")
ISSUE_LABELS = ("问题", "文献地图", "数据结果", "结论", "案例时间轴", "写作")
ISSUE_FIELD = {"title": "标题", "description": "描述", "status": "状态", "priority": "优先级", "assignee": "负责人",
               "week": "计划周", "due": "截止", "labels": "标签", "project": "课题", "related": "关联",
               "direction": "研究方向", "type": "类型"}
ISSUE_LABEL_NAMES = {"问题": "问题", "文献地图": "文献", "数据结果": "数据", "结论": "结论", "案例时间轴": "案例", "写作": "写作"}
ISSUE_LABEL_COLORS = {"问题": "violet", "文献地图": "blue", "数据结果": "teal", "结论": "green", "案例时间轴": "amber", "写作": "rose"}
TYPE_KIND = {
    "puzzle": "question", "question": "question", "advisor-question": "question",
    "literature-map": "literature", "landscape": "literature", "concept": "literature",
    "analysis": "data", "evidence": "data", "case-map": "data", "research-design": "data",
    "mechanism": "conclusion", "counterargument": "conclusion", "paragraph": "conclusion", "memo": "conclusion", "draft": "conclusion",
}
# Kept for templates written against the five-group scheme; groups now equal kinds.
GROUP, TYPE_GROUP, GROUP_ORDER = KIND, TYPE_KIND, KIND_ORDER
# Artifact front-matter status.
STATUS = {"draft": "草稿", "active": "进行中", "superseded": "已被取代", "absorbed-into-paper": "已并入论文"}
JOB_STATUS = {"queued": "排队中", "claimed": "处理中", "done": "已完成", "failed": "失败", "cancelled": "已取消"}
JOB_KIND = {
    "paper.summarize": "中文概要", "paper.translate": "论文翻译", "excerpt.associate": "划线关联",
    "artifact.draft": "产出草稿笔记", "harvest.review": "收获复盘", "checks.derive": "推导检验",
    "chat.reply": "助手回复", "methods.replicate": "复现方案", "analysis.read": "结果解读",
    "news.arrange": "新闻整理", "feeds.enrich": "推送整理", "artifact.split": "拆子任务", "test.echo": "测试回显",
}
MILESTONE = {
    "M1": "摸清研究版图", "M2": "提出谜题", "M3": "文献地图 v1", "M4": "概念定型（v0）",
    "M5": "问题与竞争解释", "M6": "观察框架", "M7": "证据覆盖", "M8": "首个实证检验",
    "M9": "机制与最强对手", "M10": "设计经得起追问", "M11": "可入稿成果包",
}
CELL_STATUS = {
    "missing": "缺失", "weak": "弱", "strong": "强", "contradicting": "相矛盾",
    "searched-none": "已查无果", "na": "不适用",
}
TEST_TYPE = {"straw": "风中稻草检验", "hoop": "环形检验", "smoking-gun": "确凿证据检验", "doubly-decisive": "双重决定检验"}
CHECK_STATUS = {"planned": "已计划", "data-ready": "数据就绪", "run": "已运行", "recorded": "已记录结果"}
EXCERPT_STATUS = {"inbox": "待整理", "linked": "已关联", "used": "已用于产出", "discarded": "已丢弃"}
EXCERPT_KIND = {"quote": "引文", "data": "数据", "paraphrase": "转述", "idea": "想法"}
CAMP = {"institutional-constructivist": "制度建构主义", "small-state-strategy": "小国战略", "great-power-hierarchy": "大国等级",
        "sceptic": "怀疑论", "relational-network": "关系网络", "other": "其他"}

# Core validation messages stay English (CLI, tests, agents); the web form shows these instead.
_ERRORS = (
    ("my_note is required", "请写一句自己的笔记，说明这段为什么重要。"),
    ("quote is required", "请填入原文。"),
    ("locator is required", "请填写页码或位置。"),
    ("unknown paper", "找不到这篇文献。"),
    ("stance must be one of", "立场只能是支持、反驳或中立。"),
    ("unknown claim", "这个假设已经不存在，请刷新页面。"),
    ("use must be among", "用途不在可选范围内。"),
    ("private excerpts stay out of artifacts", "私密划线不会写进产出。"),
    ("relation_to_puzzle must be one of", "与谜题的关系只能从列表里选。"),
    ("read_depth must be one of", "阅读深度只能从列表里选。"),
)


def error_zh(message: str) -> str:
    for prefix, zh in _ERRORS:
        if message.startswith(prefix):
            return zh
    return message
CLAIM_KIND = {"explanation": "解释", "mechanism": "机制", "claim": "主张", "definition": "定义"}
CLAIM_STATUS = {"accepted": "已接受", "proposed": "待确认", "rejected": "已拒绝"}
ASSOC_STATUS = {"proposed": "建议", "accepted": "已接受", "rejected": "已拒绝"}
AI_MODE = {"auto": "自动", "claude": "Claude Code", "deepseek": "DeepSeek", "agent": "手动队列"}
TASK_STATUS = {"todo": "待办", "doing": "进行中", "done": "已完成", "parked": "搁置"}
TASK_ORIGIN = {"user": "自己添加", "assistant": "助手建议"}


class LabelMap(dict):
    """dict whose missing keys return the fallback label (or the key), so `L.type[k]` never fails."""

    def __init__(self, labels: dict[str, str], fallback: dict[str, str] | None = None):
        super().__init__({**(fallback or {}), **labels})

    def __missing__(self, key: Any) -> str:
        return "" if key is None else str(key)


class Labels:
    """Exposed to templates as `L`: `L.stage[key]`, `L.type[key]`, `L.milestone['M3']`, …"""

    def __init__(self, data: dict[str, Any] | None = None):
        data = data or {}
        self.stage = LabelMap(STAGE, _yml_labels(data.get("stages")))
        self.type = LabelMap(TYPE, _yml_labels(data.get("types")))
        self.level = LabelMap(LEVEL, _yml_labels(data.get("levels")))
        self.status = LabelMap(STATUS)
        self.job_status = LabelMap(JOB_STATUS)
        self.job_kind = LabelMap(JOB_KIND)
        self.milestone = LabelMap(MILESTONE)
        self.cell_status = LabelMap(CELL_STATUS)
        self.test_type = LabelMap(TEST_TYPE)
        self.check_status = LabelMap(CHECK_STATUS)
        self.excerpt_status = LabelMap(EXCERPT_STATUS)
        self.excerpt_kind = LabelMap(EXCERPT_KIND)
        self.camp = LabelMap(CAMP)
        self.claim_kind = LabelMap(CLAIM_KIND)
        self.claim_status = LabelMap(CLAIM_STATUS)
        self.assoc_status = LabelMap(ASSOC_STATUS)
        self.ai_mode = LabelMap(AI_MODE)
        self.task_status = LabelMap(TASK_STATUS)
        self.task_origin = LabelMap(TASK_ORIGIN)
        self.task_source = task_source   # callable: L.task_source(value)
        self.kind = LabelMap(KIND)
        self.issue_label = LabelMap(ISSUE_LABEL_NAMES)
        self.issue_label_color = LabelMap(ISSUE_LABEL_COLORS)
        self.type_kind = LabelMap(TYPE_KIND)
        self.group = self.kind
        self.type_group = self.type_kind

    def direction(self, slug: str | None, directions: Iterable[dict] | None) -> str:
        for d in directions or []:
            if str(d.get("slug")) == slug:
                return str(d.get("name") or slug)
        return slug or ""


def _yml_labels(items: list[dict] | None) -> dict[str, str]:
    return {str(i["key"]): str(i.get("label") or i["key"]) for i in (items or []) if "key" in i}


def week_label(week: str | None) -> str:
    """'2026-W40' → '2026年 第40周'."""
    if not week or "-W" not in week:
        return week or ""
    y, w = week.split("-W", 1)
    try:
        return f"{y}年 第{int(w)}周"
    except ValueError:
        return week


def short_week(week: str | None) -> str:
    """'2026-W40' → '第40周'."""
    if not week or "-W" not in week:
        return week or ""
    try:
        return f"第{int(week.split('-W', 1)[1])}周"
    except ValueError:
        return week


_SOURCE_WEEK = __import__("re").compile(r"^\d{4}-W\d{2}$")


def task_source(value: str | None, title: str | None = None) -> str:
    """Where a 待办 came from, in words: a milestone check, an output (with its title when the caller
    has it), an assistant task or a weekly review. Internal ids never reach the page; an unknown value
    shows nothing."""
    v = (value or "").strip()
    if not v:
        return ""
    if v in MILESTONE:
        return f"来自检查：{MILESTONE[v]}"
    if v.startswith("ART-"):
        return f"来自产出《{title}》" if title else "来自产出"
    if v.startswith("AT-"):
        return "助手建立"
    if _SOURCE_WEEK.match(v):
        return f"来自{week_label(v)}的回顾"
    return ""
