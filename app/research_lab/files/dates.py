"""Due dates in words (docs/design/ISSUES-LINEAR.md §4): the menu's presets, the Fridays the assistant
moves things to, and a rule parser shared by the page's due menu and the assistant. No model call: what
the rules cannot read returns None and is reported as unreadable, never guessed. Dates only, no times.

    read(text, today=None) -> (date | None, note) parse plus a note when it read a passed day forward
    parse(text, today=None) -> date | None      今天 明天 后天 · 周三 下周三 本周五 这周末 · 3天 2周 1个月 24h 7d
                                                · 10月20日 10-20 10/20 2026-10-20 Oct 20 · 月底 下月初
    presets(today=None) -> [Preset]             明天 · 周五 · 一周后 · 一个月后, each with its date and label
    preset(key, today=None) -> date | None      one preset's date, computed when it is pressed
    upcoming_friday(today) · this_friday(today) · next_friday(today) · in_months(today, n)
    label(d, today=None) -> '10月20日 周二'      ('2027年1月5日 周二' outside this year)
    tone(due, status, today=None) -> 'overdue' | 'soon' | ''   the list colour (§4)

A date with no year (10月20日, 10-20, Oct 20) is its next occurrence, today included: a due date is set
forward. 周三 alone is the coming Wednesday (today when it is Wednesday); 本周三 is this ISO week's, and
once that day has passed it reads as next week's, with a note saying so (本周五 said on a Sunday is next
Friday); 下周三 is next week's. 24h reads as tomorrow. "Today" is Beijing time, as on 首页 (feeds.brief).
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = "一二三四五六日"
_DAY = {c: i for i, c in enumerate(WEEKDAYS)} | {"天": 6}
_EN_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_EN_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}
SOON_DAYS = 7
BJ = ZoneInfo("Asia/Shanghai")      # feeds.brief.TZ: Leo's day, whatever the Mac's clock zone


@dataclass(frozen=True)
class Preset:
    key: str          # tomorrow | friday | week | month
    name: str         # 明天 · 周五 · 一周后 · 一个月后
    date: date

    @property
    def iso(self) -> str:
        return self.date.isoformat()


# ---------------------------------------------------------------- named days

def _today(today: date | None) -> date:
    return today or datetime.now(BJ).date()


def upcoming_friday(today: date | None = None) -> date:
    """「周五」 in the menu: the next Friday, today when it is Friday (Linear's 本周末)."""
    t = _today(today)
    return t + timedelta(days=(4 - t.weekday()) % 7)


def this_friday(today: date | None = None) -> date:
    """「这周做完」: this ISO week's Friday, or today once Friday has passed."""
    t = _today(today)
    return max(t, t + timedelta(days=4 - t.weekday()))


def next_friday(today: date | None = None) -> date:
    """「挪到下周」: the Friday of next ISO week (weekends stay for writing)."""
    t = _today(today)
    return t + timedelta(days=11 - t.weekday())


def in_months(today: date | None, n: int) -> date:
    """The same day n months on, the month's last day when it is shorter (1月31日 + 1 → 2月28日)."""
    t = _today(today)
    m = t.month - 1 + n
    y, m = t.year + m // 12, m % 12 + 1
    return date(y, m, min(t.day, calendar.monthrange(y, m)[1]))


def presets(today: date | None = None) -> list[Preset]:
    t = _today(today)
    return [Preset("tomorrow", "明天", t + timedelta(days=1)), Preset("friday", "周五", upcoming_friday(t)),
            Preset("week", "一周后", t + timedelta(days=7)), Preset("month", "一个月后", in_months(t, 1))]


def preset(key: str, today: date | None = None) -> date | None:
    """A preset's date worked out now, when its button is pressed (a page left open overnight still
    gives tomorrow's 明天); None for an unknown key."""
    return next((p.date for p in presets(today) if p.key == key), None)


def label(d: date | str, today: date | None = None) -> str:
    """'10月20日 周二'; the year is added outside the current one. An ISO string is accepted too."""
    t = _today(today)
    d = date.fromisoformat(d) if isinstance(d, str) else d
    day = f"{d.month}月{d.day}日 周{WEEKDAYS[d.weekday()]}"
    return day if d.year == t.year else f"{d.year}年{day}"


def tone(due: str | date | None, status: str = "todo", today: date | None = None) -> str:
    """The list colour (§4): due today or overdue 'overdue' (red), within SOON_DAYS 'soon' (orange), else
    '' (grey). Done and parked issues are always grey."""
    if not due or status in ("done", "parked"):
        return ""
    try:
        d = due if isinstance(due, date) else date.fromisoformat(str(due))
    except ValueError:
        return ""
    days = (d - _today(today)).days
    return "overdue" if days <= 0 else "soon" if days <= SOON_DAYS else ""


# ---------------------------------------------------------------- parsing

def _num(text: str) -> float | None:
    """3 · 三 · 十二 · 两 · 半 as a number; None when it is not one."""
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    if all(c in _CN_NUM for c in text):
        if text == "十":
            return 10
        if "十" in text:                                     # 十二, 二十, 二十三
            tens, _, ones = text.partition("十")
            return (_CN_NUM[tens] if tens else 1) * 10 + (_CN_NUM[ones] if ones else 0)
        return _CN_NUM[text] if len(text) == 1 else None
    return None


def _on_or_after(t: date, month: int, day: int) -> date | None:
    """The next occurrence of month/day, today included."""
    for year in (t.year, t.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if d >= t:
            return d
    return None


def _weekday_in(t: date, weekday: int, weeks_ahead: int) -> date:
    """That weekday in the ISO week `weeks_ahead` from this one."""
    return t - timedelta(days=t.weekday()) + timedelta(days=7 * weeks_ahead + weekday)


def parse(text: str, today: date | None = None) -> date | None:
    """A due date from what Leo types (see the module notes); None when the rules cannot read it."""
    return read(text, today)[0]


# 到 / 在 / 于 / 截止 and a verb before them (挪到, 改到, 推到…) carry no date; neither does closing punctuation.
_LEAD = re.compile(r"^(?:(?:挪|改|推|移|放|定|设|延)(?:到|在|成|为|至)|截止(?:到|在|于|日期)?|到|在|于|至|due|by)\s*")


def _clean(text: str) -> str | None:
    """What the rules read: lower case, single spaces, no lead-in words or closing punctuation, no 前 at
    the end (下周三前). None for a duration with 前, which means ago (3天前)."""
    s = re.sub(r"\s+", " ", str(text or "")).strip().lower().rstrip("。.!！?？,，;；、")
    s = _LEAD.sub("", s).strip()
    if re.search(r"(天|日|周|星期|礼拜|月|小时|h|d|w)\s*前$", s):
        return None
    return s[:-1].strip() if len(s) > 1 and s.endswith("前") else s


def read(text: str, today: date | None = None) -> tuple[date | None, str]:
    """parse() and a note for the menu: '' or why the date is not the literal one (本周五已过，按下周五算).
    The check runs on the cleaned text, so 到本周五 · 在本周五 · 挪到本周五 · 本周五。 are read forward too."""
    t = _today(today)
    s = _clean(text)
    if not s:
        return None, ""
    m = re.fullmatch(r"(这|本)(?:个)?(?:周|星期|礼拜)([一二三四五六日天])", s)
    if m:
        wd = _DAY[m.group(2)]
        d = _weekday_in(t, wd, 0)
        if d < t:
            return _weekday_in(t, wd, 1), f"本周{m.group(2)}已过，按下周{m.group(2)}算"
        return d, ""
    return _parse(s, t), ""


def _parse(s: str, t: date) -> date | None:
    """The rules over text already cleaned by _clean."""
    fixed = {"今天": 0, "今日": 0, "today": 0, "明天": 1, "明日": 1, "tomorrow": 1, "后天": 2, "大后天": 3}
    if s in fixed:
        return t + timedelta(days=fixed[s])
    # weekend: this / next week's Sunday
    m = re.fullmatch(r"(这|本|下|下下)?(周末|礼拜天?末)", s)
    if m:
        return _weekday_in(t, 6, {"下": 1, "下下": 2}.get(m.group(1) or "", 0))
    # 周三 · 本周三 · 这周三 · 下周三 · 下下周三 · 星期三 · 礼拜三
    m = re.fullmatch(r"(这|本|下|下下)?(?:个)?(?:周|星期|礼拜)([一二三四五六日天])", s)
    if m:
        wd = _DAY[m.group(2)]
        if not m.group(1):
            return t + timedelta(days=(wd - t.weekday()) % 7)
        return _weekday_in(t, wd, {"这": 0, "本": 0, "下": 1, "下下": 2}[m.group(1)])
    # next wed · wednesday · wed
    m = re.fullmatch(r"(next )?(mon|tue|wed|thu|fri|sat|sun)[a-z]*", s)
    if m:
        wd = _EN_DAYS[m.group(2)]
        return _weekday_in(t, wd, 1) if m.group(1) else t + timedelta(days=(wd - t.weekday()) % 7)
    # 月底 · 下月底 · 月初 · 下月初 · 下个月初
    m = re.fullmatch(r"(这个?|本|下个?)?月(底|末|初)", s)
    if m:
        ahead = 1 if (m.group(1) or "").startswith("下") else 0
        if m.group(2) == "初":
            first = date(t.year + (t.month - 1 + ahead) // 12, (t.month - 1 + ahead) % 12 + 1, 1)
            return first if first >= t else in_months(first, 1)
        end = in_months(t.replace(day=1), ahead)
        return end.replace(day=calendar.monthrange(end.year, end.month)[1])
    if s in ("下个月", "下月", "next month"):
        return in_months(t, 1)
    if s in ("下周", "next week"):
        return next_friday(t)
    # durations: 3天 · 3天后 · 三天 · 2周 · 两周 · 1个月 · 半个月 · 24h · 7d · 2w · 3 days · 1 week
    m = re.fullmatch(r"([\d.]+|[零一二两三四五六七八九十半]+)\s*(个)?\s*(天|日|周|星期|礼拜|月|小时|h|d|w|days?|weeks?|months?|hours?)(后|之后|以后|内)?", s)
    if m:
        n, unit = _num(m.group(1)), m.group(3)
        if n is None or n <= 0:
            return None
        if unit in ("小时", "h", "hour", "hours"):
            return t + timedelta(days=max(1, -(-int(n) // 24)))          # 24h → tomorrow; dates only
        if unit in ("周", "星期", "礼拜", "w", "week", "weeks"):
            return t + timedelta(days=round(n * 7))
        if unit in ("月", "month", "months"):
            return t + timedelta(days=15) if n == 0.5 else (in_months(t, int(n)) if n == int(n) else None)
        return t + timedelta(days=round(n)) if n == int(n) else None
    # 2026-10-20 · 2026/10/20 · 2026年10月20日
    m = re.fullmatch(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*[日号]?", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    # 10月20日 · 10月20号 · 10月20 · 10-20 · 10/20 · 10.20
    m = re.fullmatch(r"(\d{1,2})\s*(?:月\s*(\d{1,2})\s*[日号]?|[-/.]\s*(\d{1,2}))", s)
    if m:
        return _on_or_after(t, int(m.group(1)), int(m.group(2) or m.group(3)))
    # oct 20 · october 20 · 20 oct
    m = re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?|(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3})[a-z]*\.?", s)
    if m:
        mon = _EN_MONTHS.get(m.group(1) or m.group(4))
        day = int(m.group(2) or m.group(3))
        return _on_or_after(t, mon, day) if mon else None
    return None
