"""Shared Markdown for issue descriptions and comments (files/markdown.py, card 0096).

Covers the eleven formats as the editor writes them, the compatibility table (Leo 10-10) as the server
resolves old Markdown that breaks it, XSS filtering, and old plain text. The browser editor parses with
the same rules; test_issue_editor_bundle.py checks the two renderers agree."""
import re
from html.parser import HTMLParser

import pytest
from markupsafe import Markup

from research_lab.files.markdown import is_safe_link, render


def html(text):
    return str(render(text)).strip()


# ------------------------------------------------------------------------- the eleven formats

# Markdown exactly as static/issue-editor.js serialises each format, and what the page shows for it.
FORMATS = [
    ("正文", "一段正文", "<p>一段正文</p>"),
    ("标题", "## 小标题", "<h2>小标题</h2>"),
    ("粗体", "这是**重点**。", "<p>这是<strong>重点</strong>。</p>"),
    ("斜体", "这是*术语*。", "<p>这是<em>术语</em>。</p>"),
    ("删除线", "这是~~旧想法~~。", "<p>这是<s>旧想法</s>。</p>"),
    ("下划线", "这是++一句话的贡献++。", "<p>这是<u>一句话的贡献</u>。</p>"),
    ("链接", "见 [Lee 2024](https://example.com/lee)。",
     '<p>见 <a href="https://example.com/lee" target="_blank" rel="noopener noreferrer">Lee 2024</a>。</p>'),
    ("站内链接", "[文献](/papers/12)", '<p><a href="/papers/12">文献</a></p>'),
    ("引用", "> 导师 10 月 8 日：先别追求全。", "<blockquote>\n<p>导师 10 月 8 日：先别追求全。</p>\n</blockquote>"),
    ("折叠", "+++ 检索式\n内容\n+++",
     '<details><summary>检索式</summary><div data-type="detailsContent">\n<p>内容</p>\n</div></details>'),
    ("行内代码", "先用 `JSTOR` 检索", "<p>先用 <code>JSTOR</code> 检索</p>"),
    ("代码块", "```\nPY=(2023-2026)\n```", "<pre><code>PY=(2023-2026)\n</code></pre>"),
    ("无序列表", "- 一\n- 二", "<ul>\n<li>一</li>\n<li>二</li>\n</ul>"),
    ("有序列表", "1. 一\n2. 二", "<ol>\n<li>一</li>\n<li>二</li>\n</ol>"),
    ("待办清单", "- [x] 已归档\n- [ ] 写贡献",
     '<ul data-type="taskList">\n'
     '<li data-type="taskItem" data-checked="true"><label><input type="checkbox" disabled checked></label><div>已归档</div></li>\n'
     '<li data-type="taskItem" data-checked="false"><label><input type="checkbox" disabled></label><div>写贡献</div></li>\n'
     '</ul>'),
]


@pytest.mark.parametrize("name,source,expected", FORMATS, ids=[f[0] for f in FORMATS])
def test_each_format_renders(name, source, expected):
    assert html(source) == expected


@pytest.mark.parametrize("name,source,expected", FORMATS, ids=[f[0] for f in FORMATS])
def test_rendering_is_stable_and_leaves_source_alone(name, source, expected):
    """Rendering never rewrites the stored text and gives the same HTML every time."""
    kept = str(source)
    assert html(source) == html(source)
    assert source == kept


def test_render_returns_markup_and_empty_is_empty():
    assert isinstance(render("**x**"), Markup)
    assert render("") == "" and render(None) == "" and render("  \n ") == ""


def test_single_newline_is_a_line_break():
    assert html("第一行\n第二行") == "<p>第一行<br />\n第二行</p>"


def test_nested_lists_and_ordered_start():
    out = html("3. 三\n4. 四\n   1. 四点一")
    assert out.startswith('<ol start="3">') and "<li>四\n<ol>" in out


def test_nested_task_list():
    out = html("- [ ] 父\n  - [x] 子")
    assert out.count('data-type="taskList"') == 2 and 'data-checked="true"' in out


def test_task_item_with_no_text():
    assert 'data-type="taskList"' in html("- [ ] ")


def test_collapse_without_title_and_unclosed():
    assert html("+++\n内容\n+++").startswith("<details><summary></summary>")
    assert "<p>后面</p>" in html("+++ 标题\n后面")   # runs to the end, like an unclosed fence


def test_collapse_holds_paragraphs_and_lists():
    out = html("+++ 检索式\n一段\n\n- 列表\n+++\n之后")
    assert "<p>一段</p>" in out and "<li>列表</li>" in out and out.endswith("<p>之后</p>")


def test_underline_needs_two_plus_signs():
    assert html("a+b") == "<p>a+b</p>"
    assert html("C++ 和 1+1") == "<p>C++ 和 1+1</p>"
    assert html(r"\+\+不是下划线\+\+") == "<p>++不是下划线++</p>"
    assert html("++a+++") == "<p><u>a</u>+</p>"


def test_cjk_emphasis_next_to_cjk_punctuation():
    assert html("按**「承诺可信度」**归档") == "<p>按<strong>「承诺可信度」</strong>归档</p>"
    assert html("的*（注）*后") == "<p>的<em>（注）</em>后</p>"
    assert html("中~~「旧」~~文") == "<p>中<s>「旧」</s>文</p>"
    assert html("中++「下」++文") == "<p>中<u>「下」</u>文</p>"
    # Latin text keeps CommonMark's rule: a delimiter between a letter and punctuation does not open.
    assert html('a**"b"**c') == '<p>a**&quot;b&quot;**c</p>'


# ------------------------------------------------------------------ the compatibility table

def test_code_block_shows_formatting_literally():
    assert html("```\n**粗** ++下++ [l](https://e.com)\n```") == \
        "<pre><code>**粗** ++下++ [l](https://e.com)\n</code></pre>"


def test_inline_code_shows_formatting_literally():
    assert html("`**粗** ~~删~~`") == "<p><code>**粗** ~~删~~</code></p>"


def test_inline_code_drops_bold_italic_strike_underline_around_it():
    assert html("**粗 `码` 体**") == "<p><strong>粗 </strong><code>码</code><strong> 体</strong></p>"
    assert html("*斜 `码`*") == "<p><em>斜 </em><code>码</code></p>"
    assert html("~~删 `码`~~") == "<p><s>删 </s><code>码</code></p>"
    assert html("++下 `码`++") == "<p><u>下 </u><code>码</code></p>"


def test_link_drops_inline_code_inside_it():
    assert html("[`代码`](https://e.com)") == \
        '<p><a href="https://e.com" target="_blank" rel="noopener noreferrer">代码</a></p>'


def test_link_never_carries_underline():
    inner = html("[++下划++](https://e.com)")
    outer = html("++[链接](https://e.com)++")
    assert "<u>" not in inner and ">下划</a>" in inner
    assert "<u>" not in outer and ">链接</a>" in outer


def test_underline_and_strike_never_overlap():
    assert html("~~删 ++下++ 删~~") == "<p><s>删 下 删</s></p>"
    assert html("++下 ~~删~~ 下++") == "<p><u>下 </u><s>删</s><u> 下</u></p>"


def test_no_underline_in_list_items():
    assert "<u>" not in html("- ++不行++")
    assert "<u>" not in html("1. ++不行++")
    assert "<u>" not in html("- [ ] ++不行++")
    assert "<u>" not in html("- 外\n  - ++里层也不行++")
    assert "<u>可以</u>" in html("++可以++\n\n- 列表")


def test_bold_italic_and_link_stack():
    out = html("[***重点***](https://e.com)")
    assert "<strong>" in out and "<em>" in out and "<a " in out


def test_strike_and_bold_and_link_stack():
    assert html("~~**粗删**~~") == "<p><s><strong>粗删</strong></s></p>"


def test_mixed_task_list_keeps_brackets_as_text():
    out = html("- [ ] 待办\n- 普通")
    assert "taskList" not in out and "<li>[ ] 待办</li>" in out


def test_ordered_list_items_are_never_tasks():
    assert "taskList" not in html("1. [ ] 一")


def test_collapse_only_at_top_level():
    assert "<details>" not in html("- 项\n  +++ 不是折叠\n  +++")
    assert "<details>" not in html("> +++ 不是折叠\n> +++")
    nested = html("+++ 外\n+++ 内\n+++")
    assert nested.count("<details>") == 1 and "+++ 内" in nested


def test_collapse_title_is_plain_text():
    assert "<summary>**不加粗**</summary>" in html("+++ **不加粗**\n内容\n+++")


# ---------------------------------------------------------------------------------- XSS

XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '<a href="javascript:alert(1)" onclick="alert(1)">x</a>',
    "[x](javascript:alert(1))",
    "[x](JAVASCRIPT:alert(1))",
    "[x](&#106;avascript:alert(1))",
    "[x](java%0ascript:alert(1))",
    "[x](vbscript:msgbox(1))",
    "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "[x](file:///etc/passwd)",
    "<javascript:alert(1)>",
    "![x](javascript:alert(1))",
    "![x](data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+)",
    '[x](https://e.com "a\\" onmouseover=\\"alert(1)")',
    '```js" onmouseover="alert(1)\ncode\n```',
    "+++ <img src=x onerror=alert(1)>\n内容\n+++",
    "- [ ] <script>alert(1)</script>",
    "<details open ontoggle=alert(1)>",
]


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags_of(markup):
    p = _Tags()
    p.feed(markup)
    return p.tags


@pytest.mark.parametrize("source", XSS)
def test_no_script_event_handler_or_dangerous_url(source):
    for tag, attrs in tags_of(html(source)):
        assert tag not in ("script", "iframe", "object", "embed", "style"), tag
        assert not any(name.startswith("on") for name in attrs), attrs
        for name in ("href", "src"):
            if name in attrs:
                assert is_safe_link(attrs[name]), attrs[name]


def test_raw_html_is_shown_as_text():
    assert html("<b>粗</b>") == "<p>&lt;b&gt;粗&lt;/b&gt;</p>"


def test_protocol_relative_and_backslash_links_never_leave_the_app():
    assert "<a " not in html("[x](//evil.com)")
    assert "<a " not in html("<//evil.com>")
    for src in ("[x](/\\evil.com)", "[x](\\\\evil.com)"):
        for tag, attrs in tags_of(html(src)):
            href = attrs.get("href")
            if href is not None:   # the backslash arrives percent-encoded, so the browser keeps it a path
                assert not href.startswith(("//", "/\\", "\\")) and "\\" not in href, href


def test_external_links_open_safely():
    out = html("<https://example.com>")
    assert 'target="_blank"' in out and 'rel="noopener noreferrer"' in out
    assert "target" not in html("[站内](/issues/1)")


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a?b=1#c", True), ("http://example.com", True), ("HTTPS://EXAMPLE.COM", True),
    ("/papers/12", True), ("#top", True), ("?tab=files", True), ("notes/a.md", True),
    ("javascript:alert(1)", False), (" javascript:alert(1)", False), ("mailto:a@b.c", False),
    ("data:text/html,x", False), ("vbscript:x", False), ("file:///etc/passwd", False),
    ("//evil.com", False), ("/\\evil.com", False), ("\\\\evil.com", False), ("java%20script:x", False), ("", False),
])
def test_is_safe_link(url, ok):
    assert is_safe_link(url) is ok


# ------------------------------------------------------------------------ old content

def test_old_plain_text_displays_as_written():
    old = "周三和导师见面\n带上文献清单\n\n会后整理纪要"
    assert html(old) == "<p>周三和导师见面<br />\n带上文献清单</p>\n<p>会后整理纪要</p>"


def test_old_text_with_angle_brackets_and_ampersands():
    assert html("比较 a<b & c>d") == "<p>比较 a&lt;b &amp; c&gt;d</p>"


def test_old_markdown_from_the_assistant_still_renders():
    out = html("**要点**\n\n1. 读 Lee 2024\n2. 写综述\n\n| 文献 | 年 |\n|---|---|\n| Lee | 2024 |")
    assert "<strong>要点</strong>" in out and "<ol>" in out and "<table>" in out


def test_unclosed_and_odd_markup_does_not_raise():
    for src in ("**没闭合", "++没闭合", "~~没闭合", "+++", "[链接](", "```\n没闭合", "- [", "> ", "\x00", "\r\n\r\n"):
        render(src)
