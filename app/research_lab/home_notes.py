"""One quiet line at the foot of 首页, a different one each day. Written for this app; no quotations."""
from __future__ import annotations

from datetime import date

NOTES = (
    "慢慢读，认真写。今天多想清楚一个问题，就是进展。",
    "好的研究很少一蹴而就，它是很多个普通的上午叠起来的。",
    "先把问题问对，答案会慢慢露出来。",
    "读不完的文献永远读不完，写下来的想法才属于你。",
    "今天的一条划线，可能是下个月一段论证的起点。",
    "不确定是研究的常态。把它记下来，它就成了下一个问题。",
    "一份粗糙的初稿，胜过脑子里一篇完美的文章。",
    "证据不够时承认它，证据够了就相信它。",
    "走得慢没关系，只要还在往前走。",
    "把今天能做完的一小步做完，就很好。",
    "问题越具体，答案越近。",
    "休息也是研究的一部分。明天再来。",
)


def for_day(day: date | None = None) -> str:
    d = day or date.today()
    return NOTES[d.toordinal() % len(NOTES)]
