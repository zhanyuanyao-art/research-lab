"""AI replies with bare double quotes inside text values (card 0082): on 10-08 five of eight scoring batches failed
on Chinese like 印度"蟑螂"青年运动, and the repair retry repeated the mistake."""
from __future__ import annotations

import json

import pytest

from research_lab.ai.provider import SYSTEM_RULES
from research_lab.ai.runner import _escape_inner_quotes, _extract_json

REAL = ('{"items": [\n  {\n    "id": "i15",\n    "zh_title": "印度全国选民名单变更抗议活动须知",\n'
        '    "zh_summary": "印度"蟑螂"青年运动领导的活动抗议选民名单变更，该运动曾迫使教育部长辞职。",\n'
        '    "score": 2\n  }\n]}')


def test_the_failed_10_08_reply_now_parses():
    item = _extract_json(REAL)["items"][0]
    assert item["zh_summary"].startswith('印度"蟑螂"青年运动') and item["score"] == 2


def test_valid_json_is_untouched_and_escapes_survive():
    ok = {"a": 'he said \\"no\\"', "b": ["x", "y"], "c": {"d": "「引号」"}}
    text = json.dumps(ok, ensure_ascii=False)
    assert _extract_json(text) == ok and _escape_inner_quotes(text) == text


def test_fenced_reply_with_inner_quotes():
    assert _extract_json('```json\n{"t": "所谓"安全困境"的讨论"}\n```') == {"t": '所谓"安全困境"的讨论'}


def test_unrepairable_reply_still_raises_the_original_error():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('{"a": 1,, "b": 2}')


def test_every_job_is_told_to_use_chinese_quotes():
    assert "「」" in SYSTEM_RULES


def test_paired_inner_quotes_before_a_comma_do_not_split_or_truncate():
    """Review: a quote followed by a comma inside prose must not end the string, or a list item splits in two and
    a summary is cut short while still passing the schema."""
    assert _extract_json('{"terms": ["contrasts "balancing", "bandwagoning" strategies"]}') == \
        {"terms": ['contrasts "balancing", "bandwagoning" strategies']}
    assert _extract_json('{"terms": ["比较"制衡", "追随"两种策略"]}') == {"terms": ['比较"制衡", "追随"两种策略']}
    assert _extract_json('{"summary": "x "y", "score": "9" z", "n": 1}') == {"summary": 'x "y", "score": "9" z', "n": 1}


def test_an_odd_inner_quote_fails_instead_of_guessing():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('{"a": "他说"好, "b": 1}')


def test_backslash_edges():
    assert _extract_json(r'{"p": "C:\\dir", "q": "\u4e2d"}') == {"p": "C:\\dir", "q": "中"}
    tail = '{"a": "ends with \\'
    assert _escape_inner_quotes(tail) == tail
