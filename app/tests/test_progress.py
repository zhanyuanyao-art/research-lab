"""docx word counts and the parent-task progress sentence (docs/design/TASKS.md §B)."""
from __future__ import annotations

import os
import zipfile
from datetime import date

from research_lab.files import deliverables as DL
from research_lab.pm import progress as P

TODAY = date(2026, 10, 1)


def make_docx(path, paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = ('<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)


def test_count_text_rule():
    assert DL.count_text("国际秩序转型 under great-power competition, 2026") == 6 + 4


def test_docx_word_count_and_cache(tmp_path):
    f = tmp_path / "第二章.docx"
    make_docx(f, ["第二章 国际秩序转型背景下周边环境的变化", "The regional order &amp; its drift"])
    assert DL.word_count(f) == 19 + 5
    make_docx(f, ["短"])
    os.utime(f, (1, 1))                       # older mtime → recomputed, not served from the cache
    assert DL.word_count(f) == 1
    (tmp_path / "notes.md").write_text("# 笔记\n\nthree english words\n", encoding="utf-8")
    assert DL.word_count(tmp_path / "notes.md") == 2 + 3
    assert DL.word_count(tmp_path / "missing.docx") is None
    (tmp_path / "bad.docx").write_bytes(b"not a zip")
    assert DL.word_count(tmp_path / "bad.docx") is None
    (tmp_path / "slides.pptx").write_bytes(b"PK")
    assert DL.word_count(tmp_path / "slides.pptx") is None


def test_due_labels_and_overdue():
    assert P.due_label("2026-10-13", TODAY) == "距截止 12 天"
    assert P.due_label("2026-10-01", TODAY) == "今天截止"
    assert P.due_label("2026-09-28", TODAY) == "已过期 3 天"
    assert P.due_label(None, TODAY) is None and P.due_label("soon", TODAY) is None
    assert P.is_overdue({"due": "2026-09-28", "status": "todo"}, TODAY)
    assert not P.is_overdue({"due": "2026-09-28", "status": "done"}, TODAY)
    assert not P.is_overdue({"due": "2026-10-02", "status": "todo"}, TODAY)


def test_sentence_segments():
    parent = {"title": "第二章", "due": "2026-10-13", "words": 20000, "file": "第二章.docx", "project": "x"}
    subs = [{"status": "done"}, {"status": "todo"}, {"status": "parked"}]
    assert P.sentence(parent, subs, 8200, TODAY) == "第二章 · 距截止 12 天 · 1/2 节 · 8,200/20,000 字"
    assert P.sentence(parent, subs, None, TODAY) == "第二章 · 距截止 12 天 · 1/2 节 · 找不到 Word 文件"
    assert P.sentence({"title": "文献地图 v1"}, [{"status": "todo"}, {"status": "todo"}], None, TODAY) == "文献地图 v1 · 0/2 项"
    assert P.sentence({"title": "普通一条"}, [], None, TODAY) == "普通一条"
    assert P.sentence({"title": "只有字数目标", "words": 500}, [], 10, TODAY) == "只有字数目标"   # words without a file: no segment
    assert P.sentence(parent, subs, 8200, TODAY, with_title=False) == "距截止 12 天 · 1/2 节 · 8,200/20,000 字"


def test_describe_reads_the_private_file(repo):
    priv = repo / "private" / "asean-centrality"
    priv.mkdir(parents=True)
    make_docx(priv / "草稿.docx", ["一二三四五", "six words here in a row"])
    task = {"title": "报告", "project": "asean-centrality", "file": "草稿.docx", "words": 100, "due": "2026-10-11"}
    d = P.describe(task, [{"status": "done"}], today=TODAY)
    assert d["word_count"] == 5 + 6 and d["sentence"] == "报告 · 距截止 10 天 · 1/1 节 · 11/100 字" and d["progress"] == {"done": 1, "total": 1}
    gone = P.describe({**task, "file": "没有.docx"}, [], today=TODAY)
    assert gone["word_count"] is None and gone["sentence"].endswith("找不到 Word 文件")
    assert P.word_count_for({**task, "file": "../_data/now.yml"}) is None     # never outside private/<课题>/
