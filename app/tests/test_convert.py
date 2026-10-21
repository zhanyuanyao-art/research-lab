"""Leo's Word / Markdown / text files → Markdown (files/convert.py), standard library only."""
from __future__ import annotations

import zipfile

import pytest

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _p(text: str, style: str | None = None, num: tuple[int, int] | None = None) -> str:
    ppr = ""
    if style or num:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "")
        ppr += (f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>' if num else "") + "</w:pPr>"
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def make_docx(path, body: str, *, numbering: bool = False, footnotes: dict[str, str] | None = None) -> None:
    styles = (f'<w:styles xmlns:w="{NS}">'
              '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="heading 1"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="标题 2"/></w:style>'
              '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/></w:style></w:styles>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>')
        z.writestr("word/styles.xml", styles)
        if numbering:
            z.writestr("word/numbering.xml", f'<w:numbering xmlns:w="{NS}">'
                       '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
                       '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
                       '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
                       '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
        if footnotes:
            notes = "".join(f'<w:footnote w:id="{k}"><w:p><w:r><w:t>{v}</w:t></w:r></w:p></w:footnote>' for k, v in footnotes.items())
            z.writestr("word/footnotes.xml", f'<w:footnotes xmlns:w="{NS}"><w:footnote w:type="separator" w:id="-1"/>{notes}</w:footnotes>')


def test_docx_keeps_headings_lists_tables_and_footnotes(tmp_path):
    from research_lab.files.convert import to_markdown
    body = (_p("第二章 国际秩序转型背景下周边环境的变化", "1")
            + _p("一、地区秩序变化", "2")
            + '<w:p><w:r><w:t>转型期的地区秩序</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r>'
              '<w:del><w:r><w:delText>删掉的话</w:delText></w:r></w:del><w:ins><w:r><w:t>，补上的话</w:t></w:r></w:ins></w:p>'
            + _p("要点甲", num=(1, 0)) + _p("要点乙", num=(1, 0))
            + _p("第一步", num=(2, 0)) + _p("第二步", num=(2, 0))
            + '<w:tbl><w:tr><w:tc>' + _p("年份") + '</w:tc><w:tc>' + _p("事件") + '</w:tc></w:tr>'
              '<w:tr><w:tc>' + _p("2017") + '</w:tc><w:tc>' + _p("印太 | 战略") + '</w:tc></w:tr></w:tbl>'
            + _p("   ")
            + _p("引用的一段", "Quote"))
    f = tmp_path / "ch2.docx"
    make_docx(f, body, numbering=True, footnotes={"1": "见某书第 3 页。"})
    md = to_markdown(f)
    assert md.startswith("# 第二章 国际秩序转型背景下周边环境的变化\n\n## 一、地区秩序变化\n")
    assert "转型期的地区秩序[^1]，补上的话" in md and "删掉的话" not in md
    assert "- 要点甲\n- 要点乙" in md and "1. 第一步\n2. 第二步" in md
    assert "| 年份 | 事件 |\n| --- | --- |\n| 2017 | 印太 \\| 战略 |" in md
    assert "引用的一段" in md and md.rstrip().endswith("[^1]: 见某书第 3 页。")


def test_text_files_and_encodings(tmp_path):
    from research_lab.files.convert import to_markdown
    a = tmp_path / "notes.md"
    a.write_bytes("﻿# 笔记\r\n\r\n\r\n\r\n第一段".encode("utf-8"))
    assert to_markdown(a) == "# 笔记\n\n第一段\n"
    b = tmp_path / "旧笔记.txt"
    b.write_bytes("周边环境的演变态势".encode("gb18030"))
    assert to_markdown(b) == "周边环境的演变态势\n"


def test_unsupported_and_broken_files_say_why(tmp_path):
    from research_lab.files.convert import ConvertError, supported, to_markdown
    assert supported("第二章.DOCX") and supported("x.md") and not supported("x.pages") and not supported("x.pdf")
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ConvertError, match="损坏"):
        to_markdown(broken)
    with pytest.raises(ConvertError, match="不支持"):
        to_markdown(tmp_path / "x.pages")
