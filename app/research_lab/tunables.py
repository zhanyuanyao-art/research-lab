"""Settings the assistant and the settings page may tune (docs/design/SETTINGS-AGENT.md §3, §5).

One declarative registry. Each entry: key, 中文名, type, range or choices, default, where it is stored
(the settings table, _data/feeds.yml or _data/directions.yml) and its 档: direct (change it, offer undo),
confirm (a confirm card first) or never (the assistant only says where Leo can change it). A `spend` entry is
direct when the new value costs less than the old one and confirm when it costs more.

Code reads a settings-table value through tunable(conn, key): a stored value of the wrong type or out of range
reads as the default, so a hand-broken row never breaks a page. Keys with {placeholders} are families (one per
skill, per feeds.yml source, per direction). The two YAML files have no default; their values are read from the
file and written through files/datafiles.py, guarded by the file's fingerprint (set_file_value).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable

SETTINGS, FEEDS, DIRECTIONS = "settings", "feeds.yml", "directions.yml"
DIRECT, CONFIRM, NEVER = "direct", "confirm", "never"
TIER_LABEL = {DIRECT: "直接改", CONFIRM: "先确认", NEVER: "不做"}
INT, FLOAT, BOOL, CHOICE, STR, LIST = "int", "float", "bool", "choice", "str", "list"
MAX_DIRECT = 3   # more changes than this in one request go through a confirm card (§3)

SKILL_TIERS = (("light", "轻量"), ("standard", "标准"), ("deep", "深度"))   # agent.skills.TIERS, cheapest first
WEEK_RE = r"\d{4}-W(0[1-9]|[1-4]\d|5[0-3])"            # web/routes/settings.py
MODEL_RE = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}"
PATH_RE = r"/\S*"                                      # feeds.yml keep / drop are link-path prefixes


class TunableError(ValueError):
    """A value the entry does not take. The message is Chinese and can be shown as is."""


@dataclass(frozen=True)
class Tunable:
    key: str                     # exact key, or a family pattern with {placeholders}
    name: str                    # 中文名; placeholders are filled with readable names
    type: str
    default: Any = None          # a value, or callable(params) for a family
    lo: float | None = None
    hi: float | None = None
    step: float | None = None    # floats only: values must sit on this grid
    choices: Any = ()            # ((stored value, 中文标签), ...) cheapest first, or callable(params)
    pattern: str | None = None   # str values, and each item of a list, must match this in full
    store: str = SETTINGS
    tier: str = DIRECT
    spend: bool = False          # raising the value costs more: raising needs a confirm card, lowering does not
    unit: str = ""
    none_label: str = "未设"
    help: str = ""               # one or two Chinese sentences on what the entry changes, for the assistant and the page
    params: tuple[tuple[str, str], ...] = ()   # placeholder values once a family key is resolved

    @property
    def family(self) -> bool:
        return "{" in self.key

    def param(self, name: str) -> str:
        return dict(self.params)[name]


def _engine_models(p: dict[str, str]) -> tuple:
    if p["engine"] == "claude":
        return (("haiku", "Haiku"), ("sonnet", "Sonnet"), ("opus", "Opus"))
    if p["engine"] == "deepseek":
        from .ai.pricing import KNOWN_MODELS
        return tuple((m, m) for m in KNOWN_MODELS)
    raise KeyError(p["engine"])


def _engine_default(p: dict[str, str]) -> str:
    from .agent.skills import ENGINE_TIERS
    return ENGINE_TIERS[p["engine"]][p["tier"]]


def _skill_default(task: bool) -> Callable[[dict[str, str]], str]:
    def default(p: dict[str, str]) -> str:
        from .agent.skills import get
        sk = get(p["skill"])   # KeyError for a skill that does not exist: the key is unknown
        return (sk.task_tier or sk.tier) if task else sk.tier
    return default


_E = Tunable
REGISTRY: tuple[Tunable, ...] = (
    # 推送 (§5): constants that used to live in feeds/*.py
    _E("feeds.digest.news_n", "每天新闻条数", INT, 10, 5, 20, unit="条"),
    _E("feeds.digest.papers_n", "每天论文篇数", INT, 10, 5, 20, unit="篇"),
    _E("feeds.digest.world_ratio", "世界要闻占新闻的比例", FLOAT, 0.6, 0, 1, 0.1),
    _E("feeds.digest.news_window_days", "新闻看几天内的", INT, 2, 1, 3, unit="天"),
    _E("feeds.digest.papers_window_days", "论文看几天内的", INT, 10, 3, 30, unit="天"),
    _E("feeds.digest.repeat_days", "多少天内不重复推", INT, 30, 7, 60, unit="天"),
    _E("feeds.digest.source_boost", "重要来源优先程度", CHOICE, "normal",
       choices=(("none", "不加分"), ("normal", "普通"), ("strong", "强"))),
    _E("feeds.home_limit", "首页每栏条数", INT, 3, 1, 5, unit="条"),
    # 0–10 is the total cap's own range; 0069 can bound it by ai.daily_cap_cny once that key exists
    _E("feeds.daily_cny", "推送每天最多花", FLOAT, 0.8, 0, 10, 0.01, unit="元", spend=True),
    _E("feeds.refresh_hours", "多久更新一次论文和新闻", INT, 24, 6, 48, unit="小时"),
    _E("feeds.news_fresh_minutes", "打开页面时新闻多旧就重抓", INT, 30, 10, 120, unit="分钟"),
    _E("feeds.brief_hour", "每日简报几点后生成", INT, 7, 5, 12, unit="点"),
    # card 0156: after Leo's own refresh, that day's downloadable articles; no summary until the paper is opened
    _E("feeds.autoimport_oa", "刷新时当天能下载的论文自动入库", BOOL, True, spend=True),
    # 文献 (card 0122): light reads the abstract and first page (~1,500 tokens) for 研究问题 and 核心论点 only
    _E("papers.summary_mode", "文献概要读多少", CHOICE, "light", choices=(("light", "只读首页"), ("full", "读全文")),
       spend=True),
    # 待办与提醒 (ISSUE-AGENT §6, §10b)
    _E("pm.stalled_days", "进行中几天没动算卡住", INT, 14, 7, 30, unit="天"),
    _E("pm.friday_digest", "周五发本周进度", BOOL, True),
    _E("pm.rule.overdue", "提醒：已逾期", BOOL, True),
    _E("pm.rule.stalled", "提醒：进行中太久没动", BOOL, True),
    # 助手与界面
    _E("chat.reuse_minutes", "多久内的消息接着上个对话", INT, 30, 5, 240, unit="分钟"),
    _E("ui.theme", "外观", CHOICE, "system", choices=(("system", "跟随系统"), ("light", "浅色"), ("dark", "深色"))),
    _E("map.target_works", "文献地图目标篇数", INT, 20, 1, 200, unit="篇"),
    # card 0185 (REPLICATION-v3 §2D): the model tier that reads a paper into a replication plan and its printed numbers
    _E("replication.extract_tier", "复现方案读论文用的档位", CHOICE, "standard",
       choices=(("light", "便宜档"), ("standard", "标准档")), spend=True,
       help="便宜档用轻量模型读论文、抽数字，省 token。每个数值都会逐个核对它所注的那一页，页面上没有印出来的一律舍弃，"
            "所以便宜的模型不会编数字，只可能漏掉几个。标准档照「方法提取」技能的档位运行。"),
    _E("year.start_week", "学年起始周", STR, None, pattern=WEEK_RE, none_label="第一份产出所在的周"),
    # Card 0069 (on hold) brings the total cap; when it lands, add:
    # _E("ai.daily_cap_cny", "每天花费上限", FLOAT, 2.0, 0, 10, 0.1, unit="元", spend=True),
    _E("ai.mode", "运行方式", CHOICE, "auto", tier=CONFIRM,
       choices=(("auto", "自动"), ("claude", "Claude Code"), ("deepseek", "DeepSeek"), ("agent", "手动"))),
    _E("ai.model", "DeepSeek 模型", STR, "deepseek-flash", pattern=MODEL_RE, tier=CONFIRM),
    _E("ai.claude_daily_runs", "Claude Code 每天最多运行次数", INT, 200, 0, 2000, unit="次", spend=True),
    _E("engine.{engine}.{tier}", "{engine} 的{tier}档模型", CHOICE, _engine_default, choices=_engine_models, tier=CONFIRM),
    _E("skill.{skill}.tier", "「{skill}」的档位", CHOICE, _skill_default(False), choices=SKILL_TIERS, spend=True),
    _E("skill.{skill}.task_tier", "「{skill}」对话任务的档位", CHOICE, _skill_default(True), choices=SKILL_TIERS, spend=True),
    # _data/feeds.yml: no defaults, every change behind a confirm card
    _E("feeds.news[{name}].enabled", "来源「{name}」开关", BOOL, store=FEEDS, tier=CONFIRM),
    _E("feeds.news[{name}].weight", "来源「{name}」的权重", INT, None, 0, 100, store=FEEDS, tier=CONFIRM),
    _E("feeds.news[{name}].keep", "来源「{name}」只收的栏目", LIST, pattern=PATH_RE, store=FEEDS, tier=CONFIRM),
    _E("feeds.news[{name}].drop", "来源「{name}」不收的栏目", LIST, pattern=PATH_RE, store=FEEDS, tier=CONFIRM),
    _E("feeds.journal[{abbr}].enabled", "期刊「{abbr}」开关", BOOL, store=FEEDS, tier=CONFIRM),
    _E("feeds.journal[{abbr}].weight", "期刊「{abbr}」的权重", INT, None, 0, 100, store=FEEDS, tier=CONFIRM),
    _E("feeds.journal[{abbr}].gp_filter", "期刊「{abbr}」只收全球政治相关的", BOOL, store=FEEDS, tier=CONFIRM),
    _E("feeds.radar_terms", "雷达词根", LIST, store=FEEDS, tier=CONFIRM),
    _E("feeds.gp_terms", "全球政治词根", LIST, store=FEEDS, tier=CONFIRM),
    # _data/directions.yml (written by datafiles.update_direction)
    _E("directions[{slug}].keywords", "方向「{slug}」的关键词", LIST, store=DIRECTIONS, tier=CONFIRM),
    # Never through the assistant: the pause belongs to the settings page (the assistant is paused with it),
    # paths are machine setup, related.* are developer knobs. Keys and app/.env are not settings at all.
    _E("ai.paused", "暂停全部 AI", BOOL, False, tier=NEVER),
    _E("ai.claude_bin", "Claude Code 程序位置", STR, None, tier=NEVER),
    _E("digest.repo_path", "旧摘要仓库位置", STR, None, tier=NEVER),
    _E("related.{item}", "划线关联的内部参数", STR, None, tier=NEVER),
)

_EXACT = {t.key: t for t in REGISTRY if not t.family}
_WIDE = {"name", "abbr"}   # feeds.yml names and abbreviations may contain dots and spaces
_SEGMENT = r"[^.\[\]]+"


def _family_re(key: str) -> re.Pattern[str]:
    parts = re.split(r"\{(\w+)\}", key)
    rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>{'.+' if p in _WIDE else _SEGMENT})"
                 for i, p in enumerate(parts))
    return re.compile(f"^{rx}$")


_FAMILIES = [(t, _family_re(t.key)) for t in REGISTRY if t.family]


def _readable(name: str, value: str) -> str:
    if name == "tier":
        return dict(SKILL_TIERS).get(value, value)
    if name == "engine":
        return {"claude": "Claude Code", "deepseek": "DeepSeek"}.get(value, value)
    if name == "skill":
        try:
            from .agent.skills import get
            return get(value).title
        except KeyError:
            return value
    if name == "slug":
        from .files import datafiles
        try:
            d = datafiles.direction(value)
        except Exception:  # noqa: BLE001 - a broken directions.yml must not break a name
            d = None
        return str(d.get("name") or value) if d else value
    return value


def entries() -> list[Tunable]:
    """Every entry in registry order; families keep their {placeholder} key."""
    return list(REGISTRY)


def entry(key: str) -> Tunable:
    """The entry for a key, a family resolved to the concrete key. KeyError for anything off the list."""
    t = _EXACT.get(key)
    if t is not None:
        return t
    for fam, rx in _FAMILIES:
        m = rx.match(key)
        if not m:
            continue
        p = m.groupdict()
        default = fam.default(p) if callable(fam.default) else fam.default
        choices = fam.choices(p) if callable(fam.choices) else fam.choices
        name = fam.name.format(**{k: _readable(k, v) for k, v in p.items()})
        return replace(fam, key=key, name=name, default=default, choices=choices, params=tuple(p.items()))
    raise KeyError(key)


def default(key: str) -> Any:
    return entry(key).default


# ---------------------------------------------------------------- validation

_TRUE = {"true", "on", "yes", "1", "开", "打开", "是"}
_FALSE = {"false", "off", "no", "0", "关", "关闭", "否"}


def _num(v: float) -> str:
    return f"{v:g}"


def range_label(t: Tunable) -> str:
    """'5–20 条', '0–1，步长 0.1', '不加分、普通、强'."""
    if t.type == BOOL:
        return "开、关"
    if t.type == CHOICE:
        return "、".join(label for _, label in t.choices)
    if t.type in (INT, FLOAT) and t.lo is not None and t.hi is not None:
        step = f"，步长 {_num(t.step)}" if t.step and t.step >= 0.1 else ""
        return f"{_num(t.lo)}–{_num(t.hi)}{' ' + t.unit if t.unit else ''}{step}"
    return ""


def _coerce(t: Tunable, value: Any) -> Any:
    if t.type == BOOL:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise TunableError(f"{t.name}只能是开或关")
    if t.type in (INT, FLOAT):
        if isinstance(value, bool) or value is None:
            raise TunableError(f"{t.name}要填一个数")
        try:
            v = float(value.strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError, OverflowError):   # a 309-digit JSON integer overflows float()
            raise TunableError(f"{t.name}要填一个数") from None
        if not math.isfinite(v):
            raise TunableError(f"{t.name}要填一个数")
        if t.type == INT:
            if v != int(v):
                raise TunableError(f"{t.name}要填整数")
            v = int(v)
        elif t.step:
            places = max(0, -math.floor(math.log10(t.step)))
            snapped = round(round(v / t.step) * t.step, places)
            if abs(snapped - v) > 1e-9:
                raise TunableError(f"{t.name}按 {_num(t.step)} 一档调")
            v = snapped
        if (t.lo is not None and v < t.lo) or (t.hi is not None and v > t.hi):
            raise TunableError(f"{t.name}的范围是 {range_label(t)}")
        return v
    if t.type == CHOICE:
        for val, label in t.choices:
            if value == val and type(value) is not bool:
                return val
            if isinstance(value, str) and value.strip() in (str(val), label):
                return val
        raise TunableError(f"{t.name}只能是：{range_label(t)}")
    if t.type == STR:
        if value is None or (isinstance(value, str) and not value.strip() and t.default is None):
            if t.default is None:
                return None
            raise TunableError(f"{t.name}不能为空")
        s = str(value).strip()
        if t.pattern and not re.fullmatch(t.pattern, s):
            raise TunableError(f"{t.name}的格式不对")
        return s
    if t.type == LIST:
        # A list is taken item by item and terms keep their spaces (" war" is not "war"); typed text is split
        # and trimmed, since the space after a comma is never part of a term.
        items = value if isinstance(value, (list, tuple)) else [x.strip() for x in re.split(r"[,，;；\n]", str(value or ""))]
        out: list[str] = []
        for x in items:
            s = str(x).strip() if t.pattern else str(x)
            if not s.strip() or s in out:
                continue
            if t.pattern and not re.fullmatch(t.pattern, s):
                raise TunableError(f"{t.name}里的「{s}」格式不对")
            out.append(s)
        return out
    raise TunableError(f"{t.name}不能修改")


def validate(key: str, value: Any) -> Any:
    """The value as it would be stored (labels such as 强 or 开 become the stored value). Raises KeyError for
    a key off the list, TunableError for a value the entry does not take."""
    return _coerce(entry(key), value)


def label(key_or_entry: str | Tunable, value: Any) -> str:
    """A value as Leo reads it: 开 / 关, the choice's label, the number with its unit."""
    t = entry(key_or_entry) if isinstance(key_or_entry, str) else key_or_entry
    if value is None:
        return t.none_label
    if t.type == BOOL:
        return "开" if value else "关"
    if t.type == CHOICE:
        return dict(t.choices).get(value, str(value))
    if t.type in (INT, FLOAT) and isinstance(value, (int, float)) and not isinstance(value, bool):
        return _num(value) + (f" {t.unit}" if t.unit else "")
    if t.type == LIST and isinstance(value, (list, tuple)):
        return "、".join(str(v).strip() for v in value) or "（空）"
    return str(value)


# ---------------------------------------------------------------- settings table

_MISSING = object()


def _row(conn: sqlite3.Connection, key: str) -> tuple[Any, str | None]:
    """(stored value or _MISSING, updated_at). A row that is not JSON reads as a broken value."""
    row = conn.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return _MISSING, None
    try:
        return json.loads(row[0]), row[1]
    except (TypeError, ValueError):
        return row[0], row[1]


def _settings_entry(key: str) -> Tunable:
    t = entry(key)
    if t.store != SETTINGS:
        raise LookupError(f"{key} is kept in {t.store}, not the settings table")
    return t


def tunable(conn: sqlite3.Connection, key: str) -> Any:
    """The stored value when it is valid, the default otherwise (missing, wrong type, out of range)."""
    t = _settings_entry(key)
    raw, _ = _row(conn, key)
    if raw is _MISSING:
        return t.default
    try:
        return _coerce(t, raw)
    except TunableError:
        return t.default


def set_value(conn: sqlite3.Connection, key: str, value: Any) -> Any:
    """Validate and store; returns the stored value. A value equal to the default removes the row instead, so a
    later change of the default reaches it. Never-entries are refused: their own pages write them."""
    t = _settings_entry(key)
    if t.tier == NEVER:
        raise TunableError(f"{t.name}只能在设置页改")
    v = _coerce(t, value)
    if v == t.default:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        from .db.connection import set_setting
        set_setting(conn, key, v)
    return v


def reset(conn: sqlite3.Connection, key: str) -> bool:
    """恢复默认: delete the stored value. Returns whether there was one."""
    t = _settings_entry(key)
    if t.tier == NEVER:
        raise TunableError(f"{t.name}只能在设置页改")
    return conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount > 0


def describe(conn: sqlite3.Connection | None, key: str) -> dict[str, Any]:
    """Everything a tool or page shows about one entry: current value and default (raw and as labels), range,
    store, 档. Settings-table entries add `stored` (a row exists), `broken` (it does not validate) and
    `updated_at`; feeds.yml / directions.yml entries add the file's `sha`, which a later write must present."""
    t = entry(key)
    out: dict[str, Any] = {"key": key, "name": t.name, "type": t.type, "store": t.store, "tier": t.tier,
                           "tier_label": TIER_LABEL[t.tier], "spend": t.spend, "unit": t.unit,
                           "default": t.default, "default_label": label(t, t.default) if t.store == SETTINGS else None,
                           "help": t.help, "range_label": range_label(t), "choices": [{"value": v, "label": lab} for v, lab in t.choices],
                           "stored": False, "broken": False, "updated_at": None, "sha": None}
    if t.store == SETTINGS:
        raw, updated = _row(conn, key) if conn is not None else (_MISSING, None)
        value = t.default
        if raw is not _MISSING:
            out.update(stored=True, updated_at=updated)
            try:
                value = _coerce(t, raw)
            except TunableError:
                out["broken"] = True
        out.update(value=value, value_label=label(t, value), is_default=value == t.default)
        return out
    value, sha = _file_value(t)
    out.update(value=value, value_label=label(t, value), is_default=None, sha=sha)
    return out


def changed(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """当前微调: settings-table entries whose stored value differs from the default, plus broken rows (they read as
    the default, and 恢复默认 clears them). Never-entries are left out: the page shows them in their own block."""
    keys = [t.key for t in REGISTRY if not t.family and t.store == SETTINGS and t.tier != NEVER]
    for fam, rx in _FAMILIES:
        if fam.store != SETTINGS or fam.tier == NEVER:
            continue
        prefix = fam.key.split("{", 1)[0]
        keys += [k for (k,) in conn.execute("SELECT key FROM settings WHERE substr(key, 1, ?) = ? ORDER BY key",
                                            (len(prefix), prefix)) if rx.match(k)]
    out = []
    for key in dict.fromkeys(keys):
        try:
            d = describe(conn, key)
        except KeyError:   # a skill or engine that no longer exists
            continue
        if d["stored"] and (d["broken"] or not d["is_default"]):
            out.append(d)
    return out


# ---------------------------------------------------------------- 档

def _rank(t: Tunable, v: Any) -> float:
    if t.type == CHOICE:
        return float([val for val, _ in t.choices].index(v))
    return float(v)


def tier_for(key: str, new: Any, old: Any = _MISSING, conn: sqlite3.Connection | None = None) -> str:
    """direct | confirm | never for changing `key` from `old` to `new`. Spend entries confirm only when the new
    value costs more than the current one: pass `old`, or `conn` to read it; with neither the change confirms,
    since comparing against the default would let 0.3 → 0.7 pass as a saving. Raises TunableError for a value
    the entry does not take."""
    t = entry(key)
    if t.tier == NEVER:
        return NEVER
    new_v = _coerce(t, new)
    if t.tier == CONFIRM or not t.spend:
        return t.tier
    if old is _MISSING or old is None:
        if conn is None:
            return CONFIRM
        old = tunable(conn, key)
    try:
        old_v = _coerce(t, old)
    except TunableError:
        return CONFIRM
    return CONFIRM if _rank(t, new_v) > _rank(t, old_v) else DIRECT


def batch_tier(changes: Iterable[tuple[str, Any, Any]]) -> str:
    """The 档 of one request's (key, new, old) changes together: never wins, then confirm; more than MAX_DIRECT
    changes confirm even when each alone would be direct."""
    changes = list(changes)
    tiers = {tier_for(k, new, old) for k, new, old in changes}
    if NEVER in tiers:
        return NEVER
    return CONFIRM if CONFIRM in tiers or len(changes) > MAX_DIRECT else DIRECT


# ---------------------------------------------------------------- feeds.yml / directions.yml

def _file_value(t: Tunable) -> tuple[Any, str | None]:
    from .files import datafiles
    if t.store == DIRECTIONS:
        d = datafiles.direction(t.param("slug"))
        if d is None:
            raise KeyError(t.key)
        return [str(k) for k in d.get("keywords") or []], datafiles.file_sha("directions")
    field = t.key.rsplit(".", 1)[1]
    if field.endswith("_terms"):
        data = datafiles.read("feeds") or {}
        return [str(x) for x in data.get(field) or []], datafiles.file_sha("feeds")
    kind = "news" if t.key.startswith("feeds.news[") else "journal"
    src = datafiles.feed_source(kind, t.param("name" if kind == "news" else "abbr"))
    if src is None:
        raise KeyError(t.key)
    v = src.get(field)
    if field == "enabled":
        v = v is not False
    elif field == "gp_filter":
        v = bool(v)
    elif field in ("keep", "drop"):
        v = [str(x) for x in ([v] if isinstance(v, str) else v or [])]
    elif field == "weight" and v is None:
        from .feeds.sources import DEFAULT_WEIGHT
        v = 60 if kind == "news" else DEFAULT_WEIGHT   # sources.NewsFeed.weight / Journal.weight
    return v, datafiles.file_sha("feeds")


def set_file_value(key: str, value: Any, expected_sha: str) -> str:
    """Write one feeds.yml or directions.yml entry, refusing (StaleFileError) when the file is no longer the one
    whose sha the caller read. Returns the file's new sha."""
    from .files import datafiles
    t = entry(key)
    if t.store == SETTINGS:
        raise LookupError(f"{key} is kept in the settings table")
    if not expected_sha:
        raise ValueError("expected_sha is required: read the file (describe) before writing it")
    v = _coerce(t, value)
    if t.store == DIRECTIONS:
        return datafiles.update_direction(t.param("slug"), {"keywords": v}, expected_sha)
    field = t.key.rsplit(".", 1)[1]
    if field.endswith("_terms"):
        return datafiles.set_feed_terms(field, v, expected_sha)
    kind = "news" if t.key.startswith("feeds.news[") else "journal"
    return datafiles.update_feed_source(kind, t.param("name" if kind == "news" else "abbr"), {field: v}, expected_sha)
