"""The issue description in the rich editor (card 0109): the page loads the editor, shows the description rendered
by files/markdown.py until it mounts, keeps the textarea as the save path, and tells Leo once when an old
description gets tidied on its first edit."""
from pathlib import Path

import research_lab
from research_lab.files import issues as I

STATIC = Path(research_lab.__file__).parent / "web" / "static"


def _issue(description=""):
    ref = I.create("编辑器验收", "us-rok-reassurance", "", I.revision())
    if description:
        I.update(ref, "description", description, I.revision(ref))
    return ref


def _form(page):
    return page.split('<form class="issue-description-form"', 1)[1].split("</form>", 1)[0]


def test_the_page_loads_the_editor_before_issues_js(client, repo):
    page = client.get(f"/issues/{_issue()}").text
    assert "/static/vendor/issue-editor.css?v=" in page
    bundle, wrapper, issues = (page.index(s) for s in ("vendor/tiptap-editor.min.js", "/static/issue-editor.js", "/static/issues.js"))
    assert bundle < wrapper and 'src="/static/issues.js' in page and "defer" in page[issues - 40:issues + 60]


def test_the_description_shows_rendered_until_the_editor_mounts(client, repo):
    ref = _issue("目标：**一页文献地图**。\n\n- [x] 归档\n- [ ] 写贡献\n\n+++ 检索式\n内容\n+++")
    form = _form(client.get(f"/issues/{ref}").text)
    view = form.split('class="rl-md issue-desc-view"', 1)[1].split("</div>\n      <textarea", 1)[0]
    assert "<strong>一页文献地图</strong>" in view and 'data-type="taskList"' in view and "<summary>检索式</summary>" in view
    assert '<textarea name="value"' in form and "data-rich hidden" in form          # still the save path
    assert "+++ 检索式" in form.split("<textarea", 1)[1]                              # the textarea keeps Markdown
    assert "<noscript><style>" in form                                              # without scripts: the textarea


def test_old_plain_descriptions_display_as_written(client, repo):
    ref = _issue("周三和导师见面\n带上文献清单")
    view = _form(client.get(f"/issues/{ref}").text).split('issue-desc-view"', 1)[1]
    assert "周三和导师见面<br />\n带上文献清单" in view


def test_an_empty_description_shows_the_placeholder(client, repo):
    view = _form(client.get(f"/issues/{_issue()}").text).split('issue-desc-view"', 1)[1]
    assert '<p class="faint">添加描述…</p>' in view


def test_description_markup_stays_inert(client, repo):
    ref = _issue('<img src=x onerror=alert(1)> [x](javascript:alert(1))')
    form = _form(client.get(f"/issues/{ref}").text)
    assert "<img src=x" not in form and 'href="javascript' not in form and "&lt;img" in form


def test_the_markdown_filter_is_the_shared_renderer():
    from research_lab.web.context import templates
    out = templates().env.filters["markdown"]("++下划++ 和 `代码`")
    assert str(out) == "<p><u>下划</u> 和 <code>代码</code></p>\n"


def test_issues_js_mounts_the_editor_on_the_textarea():
    js = (STATIC / "issues.js").read_text()
    assert "RLIssueEditor.mount(ta" in js and "textarea[data-rich]" in js
    assert "new Event('input', {bubbles: true})" in js          # the existing autosave hears each change
    assert "c.rlEditor.setMarkdown" in js                         # another window's change reaches the editor
    assert "已按新格式整理" in js and "mountDescription();" in js
    css = (STATIC / "app.css").read_text()
    note = css.split(".issue-tidy-note {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in note                            # the note never moves the text


def test_saving_the_description_still_goes_through_the_property_form(client, repo):
    ref = _issue("旧描述")
    response = client.post(f"/issues/{ref}/property", data={"field": "description", "value": "- 新描述", "revision": I.revision(ref)},
                           headers={"Accept": "application/json", "HX-Request": "true"})
    assert response.status_code == 200 and response.json()["value"] == "- 新描述"


def test_the_slash_menus_commands_open_the_pages_entries():
    """card 0119: the editor's 附件 / 关联文献 (rl:editor-command) reach the paperclip and the picker."""
    js = (STATIC / "issues.js").read_text()
    hook = js.split("addEventListener('rl:editor-command'", 1)[1].split("});", 1)[0]
    assert "'attach'" in hook and "[data-attach]" in hook and ".click()" in hook
    assert "'link-material'" in hook and "details[data-materials]" in hook and ".open = true" in hook
