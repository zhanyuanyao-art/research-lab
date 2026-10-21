"""Skills: one budget profile per kind of work, shared by every engine.

A skill file (`agent/skills/<name>.md`) holds front matter and, for task skills, the agent's
instructions. The budget fields are what keep token use predictable whichever engine runs:

- `tier` light | standard | deep → a concrete model per engine (`engine_model`)
- for tasks: `max_output_tokens` (the reply), `max_tool_calls`, `tool_output_chars`; background jobs
  keep their own output size (a summary or translation chunk is sized from the paper)
- `tools`: the only lab tools the agent sees for this skill
- `jobs`: structured job kinds (ai/jobs) that run under this skill's tier and output cap
- `triggers`: words that route a chat request to this skill without asking a model

Per-skill tier overrides live in settings (`skill.<name>.tier`), so tuning needs no code change.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ruamel.yaml import YAML


SKILL_DIR = Path(__file__).parent / "skills"
TIERS = ("light", "standard", "deep")
TIER_LABEL = {"light": "轻量", "standard": "标准", "deep": "深度"}

# Tier → model per engine. Overridable in settings as `engine.<engine>.<tier>`.
ENGINE_TIERS = {
    "claude": {"light": "haiku", "standard": "sonnet", "deep": "opus"},
    "deepseek": {"light": "deepseek-flash", "standard": "deepseek-flash", "deep": "deepseek-v4-pro"},
}
# DeepSeek reasons only when asked; the deep tier asks for it.
DEEPSEEK_THINKING = {"light": None, "standard": None, "deep": "high"}


@dataclass(frozen=True)
class Assistant:
    """Who answers in the 助手 conversation (ASSISTANT-UPGRADE.md §3 0088). One today; writing, translation or data
    assistants are meant to join as more entries, each with its own default skill."""
    key: str
    name: str
    intro: str            # one Chinese line, used in the system prompt and the page
    default_skill: str    # where a request goes when nothing routes it elsewhere


ASSISTANTS = {"theo": Assistant("theo", "Theo", "研究台的任务管理助手", "task")}
CURRENT_ASSISTANT = "theo"


def assistant(key: str | None = None) -> Assistant:
    return ASSISTANTS[key or CURRENT_ASSISTANT]


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    summary: str = ""
    mode: str = "task"                       # task | job | both
    tier: str = "standard"
    task_tier: str | None = None
    max_output_tokens: int = 3000
    max_tool_calls: int = 10
    tool_output_chars: int = 6000
    tools: tuple[str, ...] = ()
    jobs: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    instructions: str = ""
    path: Path | None = field(default=None, compare=False)

    @property
    def runs_tasks(self) -> bool:
        return self.mode in ("task", "both")


_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def parse(text: str, path: Path | None = None) -> Skill:
    m = _FRONT.match(text)
    if not m:
        raise ValueError(f"{path or 'skill'}: missing front matter")
    meta = YAML(typ="safe").load(m.group(1)) or {}
    tier = str(meta.get("tier") or "standard")
    if tier not in TIERS or (meta.get("task_tier") and meta["task_tier"] not in TIERS):
        raise ValueError(f"{path or meta.get('name')}: tier must be one of {TIERS}")
    return Skill(
        name=str(meta["name"]), title=str(meta.get("title") or meta["name"]), summary=str(meta.get("summary") or ""),
        mode=str(meta.get("mode") or "task"), tier=tier, task_tier=meta.get("task_tier"),
        max_output_tokens=int(meta.get("max_output_tokens") or 3000), max_tool_calls=int(meta.get("max_tool_calls") or 10),
        tool_output_chars=int(meta.get("tool_output_chars") or 6000), tools=tuple(meta.get("tools") or ()),
        jobs=tuple(meta.get("jobs") or ()), triggers=tuple(str(t).lower() for t in meta.get("triggers") or ()),
        instructions=m.group(2).strip(), path=path)


@lru_cache(maxsize=1)
def load_all() -> dict[str, Skill]:
    out = {}
    for p in sorted(SKILL_DIR.glob("*.md")):
        s = parse(p.read_text(encoding="utf-8"), p)
        out[s.name] = s
    return out


def get(name: str) -> Skill:
    return load_all()[name]


def for_job(kind: str) -> Skill | None:
    return next((s for s in load_all().values() if kind in s.jobs), None)


# Routing to the issues skill (docs/design/ISSUE-AGENT.md §7) takes only explicit task words, never
# guesses from chapters, short titles or 课题 names: research questions use the same words
# (「Katzenstein 的第四章为什么推迟出版」「东盟十国里哪些落后了」). Everything else Leo says about his
# work lands on the default task skill, which has the issue tools too; the write tools decide what may
# change (named issue, per-request limits, cards), so a misroute ends in a question or a card.
ISSUE_ACTION = re.compile(
    r"进行中|已完成|完成|搁置|(?:改|标|设)(?:成|为)|完了|开始|挪到|排到|移到|推到|改到|顺延|推迟|提前|优先级"
    r"|截止|进度|做到哪|卡住|卡在|落后|逾期|没动|拆成|拆分|关联|新建|建一个|加一个|添加|删|记一笔|记一条|评论|标签")
ISSUE_ANCHOR = re.compile(
    r"待办|子议题|子任务"
    # a due date said the way Leo says it (no plan week since card 0093): 改到周五 / 明天 / 月底
    r"|改到(?:这周|本周|下周|下下周)?(?:周|星期)[一二三四五六日]|改到(?:明天|后天|月底|下月初)"
    r"|(?:改|标|设)(?:成|为)(?:进行中|已完成|完成|搁置|待办)"
    r"|(?:挪|排|移|推|推迟|顺延)到(?:这周|本周|下周|下下周|第\s*\d+\s*周)"
    r"|(?:本周|这周)的?进度"
    r"|(?:撤销|撤回)(?:刚才|刚刚|上次|上一次)?的?(?:修改|改动)")
ISSUE_NOUN = re.compile(r"(?:这|那|哪|几|每|各)(?:个|条|些)?议题|议题(?:页|列表|状态)")
# The research skills start a new, long workflow, so their trigger words count only with a verb that
# asks for one: 「东盟课题的文献地图做到哪了」 asks after an issue, not for a new map.
CREATE = re.compile(r"做(?!到|完|了|得)|写(?!完|到|得)|整理|梳理|生成|建立|新建|建一|画|列(?:一|个|出)|出一|来一|弄一"
                    r"|编|制作|准备|检索|搜索|查找|找(?!到)|汇总|补充|更新|修订")
PROGRESS = re.compile(r"做到哪|写到哪|到哪了|进度|进展如何|怎么样了|还差多少|(?:完成|做完|写完|读完)了(?:吗|没)")
# 「东盟文献地图那条下周再做」 refers to an existing item, it does not ask for a new one.
LATER = re.compile(r"那条|这条|再做|再写|下周再|之后再|以后再|回头再")
# Commands about Leo's own work (not questions): the general task skill holds the issue tools, so
# these must not drift to analysis or summary skills on a word like 数据 (「数据清洗那条先搁置」).
COMMAND = re.compile(r"搁置|放一放|先放|再说|搞定|完了(?!吗|没)|删了|删掉|删除|挪到|推到|改到|排到|顺延|推迟|往后推"
                     r"|(?:改|标)(?:成|为)(?:进行中|已完成|完成|搁置|待办)|开始(?:写|做|读)了|新建一个|加一个"
                     r"|调高|调低|优先级(?:调|改|设|提)|(?:打上|加上|去掉).{0,8}标签"
                     r"|截止.{0,6}(?:改|调|挪|推|设|定|往后|提前)|关联起来|取消关联|解除关联"
                     # a due date said as a deadline: 「引言那条这周做完」「第一节3天后交」 (card 0110)
                     r"|(?:这周|本周|下周|下下周)(?:内)?(?:做完|写完|交|完成)"
                     r"|(?:\d+|[一二两三四五六七八九十]+)(?:天|周)(?:后|之后|以后)?(?:交|做完|写完|完成)"
                     r"|(?:今天|明天|后天|月底|(?:下)?(?:周|星期)[一二三四五六日天])(?:前)?(?:交|做完|写完|完成)")
NOTE = re.compile(r"记一笔|记一条|记下来|记下|备注一下|加一条评论|写一条评论")
# Someone else's words (「郑永年第二章说东盟一体化已经搁置了」): a report about research, not an order.
REPORTED = re.compile(r"(?<![再别])说|认为|指出|提到|写道|论证|主张|讨论|分析")
_END = r"[。.!！～~，,]*$"
# 吧 asks only as a confirmation (「写完了吧」「是吧」「该写完了吧」); after an order it softens the order
# (「删掉第四节吧」「把引言挪到下周吧」). 这几 / 前几 are not a count. X不X and X没X are yes-no questions.
QUESTION = re.compile(
    r"[?？]|为什么|为何|怎么|怎样|如何|是否|可否|([\u4e00-\u9fff])[不没]\1"
    r"|吗(?:[，,。.!！～~呀啊呢哈]|谢谢|$)|(?:么|嘛|呢|没|没有|不|否)" + _END
    + r"|(?:完了|好了|是|对|的)吧" + _END + r"|该.{0,12}吧" + _END
    + r"|多少|哪|啥|咋|(?<![这那前好])几|谁|什么")


# A request to tune the app itself (docs/design/SETTINGS-AGENT.md §3). Research shares almost every word a setting
# uses (「这篇论文的 key argument」「120 比 5 通过」「导师提醒」「债务上限」「议程设置」), so routing needs the
# app's own words: a few phrases name a setting by themselves; anything else needs an app word (推送, 深色, 档位,
# 助手, 设置页, a push source's or direction's name) together with a change. A sentence about an issue (a status,
# 优先级, 那条, 第X章) is left to the issue tools: 「把第二章设置成进行中」.
_NUM = r"(?:\d+|[一二两三四五六七八九十]+)"
SETTING_PHRASE = re.compile(
    # appearance words only with a switch (「外观上看是联盟」「外观设计专利」 are research)
    r"(?:换成|换到|切到|切换到?|改成|改为|调成|用)(?:深色|浅色|暗色|夜间)|(?:深色|浅色|暗色|夜间)(?:模式|主题|外观)"
    r"|外观(?:换|改|切|调)|(?:换|改|切换).{0,4}外观"
    r"|恢复默认|自动入库|自动下载|设置页|设置里|设置项|(?:助手|应用|ai)的?运行方式"
    r"|(?:助手|技能|对话|聊天|模型)的?档位|档位(?:调|改|换|提|降)"
    rf"|只看最近\s*{_NUM}\s*天|只看最近(?:一|两|三)天|(?:多少天|几天)内?不重复|首页每栏"
    rf"|(?:新闻|论文|推送)[^。，,]{{0,6}}每天\s*{_NUM}\s*(?:篇|条)|每天\s*{_NUM}\s*(?:篇|条)(?:新闻|论文)"
    rf"|(?:新闻|论文|要闻)[^。，,？?]{{0,8}}{_NUM}\s*比\s*{_NUM}"
    r"|每天花费|今天花费|花费上限|周五.{0,6}进度.{0,4}(?:别|不要|停|关)|(?:卡住|没动).{0,10}提醒"
    r"|(?:重要媒体|重要来源|顶刊).{0,6}(?:靠前|往前|靠后|往后|优先)|(?:助手|对话|聊天).{0,6}(?:模型|档位)"
    r"|(?:这个|那个)(?:推送)?源.{0,3}(?:关掉|关了|停掉|打开)|(?:deepseek|claude|api).{0,6}(?:key|密钥)|\.env")
# papers.summary_mode (card 0122): 「概要读全文」「以后概要只读首页」 tune every summary; 「这篇概要读全文重新生成」 is
# one paper's summary, left to the summarize skill.
SUMMARY_DEPTH = re.compile(r"概要.{0,4}(?:读|看)(?:全文|首页|第一页)|概要读多少")
ONE_PAPER = re.compile(r"这篇|这本|该文|本文|此文|重新生成|重跑|再生成|再跑")
# 简报 alone is Leo's own writing (政策简报, 研究简报, 「起草一份简报」); the app's is the daily one.
APP_WORD = re.compile(r"推送|今日精选|每日简报|今日简报|推送的简报|世界要闻|首页|助手")
# change words only: a noun next to an app word asks about the app (「首页那篇论文的模型是什么」「最近推送的论文质量
# 怎么样」); the general skill answers those with app_help and read_push
SETTING_CHANGE = re.compile(
    r"(?<![a-z])key(?![a-z])|密钥|优先|提醒|上限|开放获取|关掉|打开|关了|开了|停掉|往前排|往后排"
    r"|靠前|靠后|权重|关键词|比例|别发|别超过|不超过|最多|花费|调成|设置成|改成|换成|调高|调低|多一点|少一点|多推|少推|严格|宽松")
ISSUE_OBJECT = re.compile(r"进行中|已完成|完成|搁置|待办|优先级|那条|这条|第\s*[一二三四五六七八九十百\d]+\s*[章节]")
# Push subscriptions (card 0211): the feed tools are on the settings skill; 「加一个期刊」 would read as a task command.
FEED_SOURCE = re.compile(r"订阅|退订|停订|新闻源|(?<![a-z])rss(?![a-z])|(?:加|添加|增加)(?:一个|一本|个|本)?(?:期刊|杂志)")


def _app_names(text: str) -> bool:
    """A push source's or a direction's name in the message (「把 Foreign Policy 这个源关掉」「东盟方向加个关键词」).
    Read only when a change word has no other app word; any failure reads as no."""
    try:
        from ..files import datafiles
        feeds = datafiles.read("feeds") or {}
        names = [str(f.get("name") or "") for f in (feeds.get("news") or []) + (feeds.get("journals") or [])
                 if isinstance(f, dict)]
        if any(len(n) >= 4 and n.casefold() in text for n in names):
            return True
        if "方向" in text:
            for d in datafiles.read("directions") or []:
                parts = re.split(r"[与和·、\s]+", re.sub(r"研究$", "", str(d.get("name") or "")))
                if any(len(w) >= 2 and w.replace("研究", "") in text for w in parts if w.replace("研究", "")):
                    return True
    except Exception:  # noqa: BLE001 - routing must never fail on a broken data file
        return False
    return False


def is_settings_request(text: str) -> bool:
    t = (text or "").lower()
    if ISSUE_OBJECT.search(t):
        return False
    if FEED_SOURCE.search(t):
        return True
    if SETTING_PHRASE.search(t) or (SUMMARY_DEPTH.search(t) and not ONE_PAPER.search(t)):
        return True
    if not SETTING_CHANGE.search(t):
        return False
    return bool(APP_WORD.search(t)) or _app_names(t)


def asks(text: str) -> bool:
    """A question rather than an instruction: the issue tools never change anything directly for one.
    「少推几篇，5 篇」 gives the number it asks for, so 几 there is no question."""
    t = (text or "").strip()
    if re.search(rf"{_NUM}\s*(?:篇|条)", re.sub(r"(?:少|多)推?几(?:篇|条)", "", t)):
        t = re.sub(r"(?:少|多)推?几(?:篇|条)", "", t)
    return bool(QUESTION.search(t))


def commands(text: str) -> bool:
    """An order to change Leo's own work: an explicit task word, a note to record, or a change verb
    that is not someone else's claim. The task skill writes directly only for these (tools.py)."""
    t = (text or "").lower()
    if is_issue_request(t) or NOTE.search(t):
        return True
    return bool(COMMAND.search(t)) and not REPORTED.search(t)


def is_issue_request(text: str) -> bool:
    """An explicit task word: 待办, 子议题, 本周进度, 改成/标成 + a status, 挪到下周 or 改到周五 (a due date), 撤销刚才的
    修改, or 「这个议题」 with an action. `/议题` is handled by the router."""
    t = (text or "").lower()
    return bool(ISSUE_ANCHOR.search(t) or (ISSUE_NOUN.search(t) and ISSUE_ACTION.search(t)))


# Steering a replication (card 0212) goes to its tools ahead of the progress and command words below, which would send
# 「复现进度怎么样了」 to the task skill. A new plan or a question about the design stays with methods, and an issue
# about a replication (「复现那条这周做完」) with the issue tools.
REPLICATION = re.compile(r"复现|一键跑完|run\s*all|展示稿|待补数据|replicat|作者的?\s*r\s*代码|(?<![a-z])r\s*包"
                         r"|停(?:掉|下|止)?\s*r(?![a-z])")
NOT_REPLICATION = re.compile(r"(?:做|生成|拟|写|出|建)(?:一个|一份|个|份)?复现方案|研究设计|识别假设|那条|这条|待办|议题"
                             r"|优先级|截止|搁置|进行中|已完成|(?:这周|本周|下周)内?(?:做完|写完|交|完成)")


def pick(text: str) -> Skill:
    """Route a chat request by trigger words; the general task skill otherwise. No model call."""
    t = (text or "").lower()
    if is_issue_request(t):
        return get("issues")
    if is_settings_request(t):
        return get("settings")
    # Explicit deliverables take priority over generic method/summary words. 建课题 comes first: an
    # outline Leo pastes often has 文献 or 时间线 in its section titles.
    setup = get("project-setup")
    if any(w in t for w in setup.triggers):
        return setup
    wants_new = bool(CREATE.search(t)) and not PROGRESS.search(t) and not LATER.search(t)
    for name in ("case-timeline", "literature-map"):
        skill = get(name)
        if wants_new and any(w in t for w in skill.triggers):
            return skill
    if re.search(r"(?:检索|查找|搜索|找).{0,40}(?:文献|论文)", t) and not PROGRESS.search(t):
        return get("literature-map")
    if REPLICATION.search(t) and not NOT_REPLICATION.search(t):
        return get("replication-control")
    # Progress questions and commands about Leo's work go where the issue tools are, before a word like
    # 数据 sends 「数据清洗做到哪了」 to the analysis skill.
    if PROGRESS.search(t) or (COMMAND.search(t) and not asks(t)):
        return get("task")
    best, hits = get(assistant().default_skill), 0
    for s in load_all().values():
        if not s.runs_tasks or s.name in ("task", "case-timeline", "literature-map", "project-setup"):
            continue
        n = sum(1 for w in s.triggers if w in t)
        if n > hits:
            best, hits = s, n
    return best


def tier_of(conn: sqlite3.Connection | None, skill: Skill, *, task: bool = False) -> str:
    default = (skill.task_tier or skill.tier) if task else skill.tier
    if conn is None:
        return default
    # through the registry, so a stored value the settings page calls invalid is the one the runtime ignores
    from .. import tunables
    try:
        v = tunables.tunable(conn, f"skill.{skill.name}.{'task_tier' if task else 'tier'}")
    except (KeyError, LookupError):
        return default
    return v if v in TIERS else default


def engine_model(conn: sqlite3.Connection | None, engine: str, tier: str) -> str:
    default = ENGINE_TIERS.get(engine, {}).get(tier, "")
    if conn is None:
        return default
    from .. import tunables
    try:
        return tunables.tunable(conn, f"engine.{engine}.{tier}") or default
    except (KeyError, LookupError):
        return default
