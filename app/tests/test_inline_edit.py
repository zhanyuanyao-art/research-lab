"""Card 0092 (ISSUES-LINEAR.md §6): one elevation scale, sub-issue rows on one grid, and text that edits in
place without a frame, so typing never moves the page."""
import re
from pathlib import Path

import pytest

from research_lab.files import issues as I

WEB = Path(__file__).resolve().parents[1] / 'research_lab' / 'web'
STATIC, TEMPLATES = WEB / 'static', WEB / 'templates'
SHEETS = ('app.css', 'chat.css', 'data.css', 'method.css', 'excerpts.css', 'pm.css', 'papers.css')


def css(name):
    return re.sub(r'/\*.*?\*/', '', (STATIC / name).read_text(), flags=re.S)


def rules(text):
    """(selector, body) for every innermost rule; at-rule wrappers are dropped."""
    out, depth, start, sel = [], 0, 0, []
    for i, ch in enumerate(text):
        if ch == '{':
            sel.append(text[start:i].strip())
            depth += 1
            start = i + 1
        elif ch == '}':
            if sel:
                body = text[start:i]
                if '{' not in body:
                    out.append((sel[-1], body))
                sel.pop()
            depth -= 1
            start = i + 1
        elif ch == ';' and depth == 0:
            start = i + 1
    return out


def decls(body):
    return {k.strip(): v.strip() for k, _, v in (d.partition(':') for d in body.split(';')) if v.strip()}


def layers(value):
    parts, depth, cur = [], 0, ''
    for ch in value:
        depth += ch == '('
        depth -= ch == ')'
        if ch == ',' and depth == 0:
            parts.append(cur.strip())
            cur = ''
        else:
            cur += ch
    return parts + [cur.strip()]


def block(text, opener):
    start = text.index(opener) + len(opener)
    return text[start:text.index('}', start)]


def test_three_shadow_tiers_in_every_theme_block():
    text = css('app.css')
    light = block(text, ':root {')
    system_dark = block(text, '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {')
    forced_dark = block(text, ':root[data-theme="dark"] {')
    for theme in (light, system_dark, forced_dark):
        for tier in ('--shadow-1:', '--shadow-2:', '--shadow-3:'):
            assert tier in theme
    # The old names follow the new tiers, so every existing var(--shadow-md) moves with them.
    for old, new in (('sm', '1'), ('md', '2'), ('lg', '3')):
        assert f'--shadow-{old}: var(--shadow-{new});' in light


def test_no_hand_written_drop_shadows():
    """Drop shadows come from the three tiers. What remains literal is geometry without blur (inset edges,
    1–5px halos) drawn in theme colours, never a hard-coded rgba."""
    for name in SHEETS:
        for selector, body in rules(css(name)):
            if selector.startswith(':root'):
                continue
            value = decls(body).get('box-shadow')
            if not value or value == 'none':
                continue
            for layer in layers(value):
                assert not re.search(r'rgba?\(|#[0-9a-f]{3}', layer, re.I), (name, selector, layer)
                if layer.startswith('var(--'):
                    continue
                spread_only = re.match(r'(inset )?-?\d+(px)? -?\d+(px)? 0 ', layer) or re.match(r'(inset )?0 0 0 \d+px ', layer)
                assert spread_only and 'var(--' in layer, (name, selector, layer)


def test_module_stylesheets_carry_no_elevation():
    for name in ('data.css', 'method.css', 'excerpts.css', 'pm.css'):
        for selector, body in rules(css(name)):
            assert '--shadow' not in decls(body).get('box-shadow', ''), (name, selector)


def test_cards_and_options_do_not_lift_on_hover():
    for name in SHEETS:
        for selector, body in rules(css(name)):
            hovered = [s for s in layers(selector) if re.search(r':hover[^ >+~]*$', s.strip())]
            if not hovered:
                continue
            d = decls(body)
            assert '--shadow' not in d.get('box-shadow', ''), (name, selector)
            assert 'translateY(-' not in d.get('transform', ''), (name, selector)


def test_surfaces_with_a_shadow_have_no_second_outline():
    text = css('app.css')
    for selector in ('.menu-panel', '.issue-popover-panel', '.issue-compose-dialog', '.toast', '.props'):
        found = [decls(b) for s, b in rules(text) if s == selector]
        assert found, selector
        for d in found:
            border = d.get('border', '') + d.get('border-color', '')
            assert not border or border in ('0', '1px solid transparent', 'transparent'), (selector, border)


def test_issue_empty_is_defined_once():
    text = css('app.css')
    assert sum(1 for s, _ in rules(text) if s == '.issue-empty') == 1


INLINE_FIELDS = {
    'pages/issue.html': ['aria-label="议题标题"', 'aria-label="议题描述"', 'aria-label="子议题标题"', 'aria-label="发表评论"'],
    'partials/_issue_composer.html': ['name="title"', 'name="description"'],
    'pages/artifact.html': ['aria-label="标题"', 'aria-label="添加子任务"'],
    'partials/_updates.html': ['aria-label="写评论"'],
    'pages/papers.html': ['aria-label="集合名称"'],
    'pages/collections.html': ['aria-label="新集合名称"', 'aria-label="集合名称：'],
    'partials/ui.html': ['aria-label="新文件夹名称"', 'aria-label="文件夹名称：'],
    'partials/_excerpt_form.html': ['id="xf-quote"', 'id="xf-note"'],
    'pages/excerpt.html': ['id="ed-quote"', 'id="ed-note"'],
    'partials/_home_tasks.html': ['id="plan-this-week"'],
    'pages/now.html': ['<textarea', '<input type="text"'],
    'pages/data_run.html': ['name="reading"'],
    'partials/_data_step.html': ['name="note"'],
}


@pytest.mark.parametrize('template,marks', INLINE_FIELDS.items())
def test_in_place_text_fields_use_inline_edit(template, marks):
    tags = re.findall(r'<(?:input|textarea)\b[^>]*>', (TEMPLATES / template).read_text())
    for mark in marks:
        found = [t for t in tags if mark in t]
        assert found, (template, mark)
        for tag in found:
            assert re.search(r'class="[^"]*\binline-edit\b', tag), (template, tag)
            if tag.startswith('<textarea'):
                assert 'data-autogrow' in tag, (template, tag)


def test_every_inline_textarea_grows():
    for path in TEMPLATES.rglob('*.html'):
        for tag in re.findall(r'<textarea\b[^>]*>', path.read_text()):
            if re.search(r'class="[^"]*\binline-edit\b', tag):
                assert 'data-autogrow' in tag, (path.name, tag)


def test_inline_edit_keeps_its_box_on_hover_and_focus():
    """The description used to gain 10px of side padding and an outline on focus: every line moved right
    and re-wrapped. In-place fields may only change colours when hovered or focused."""
    contexts = ('inline-edit', 'issue-title-form', 'issue-description-form', 'issue-comment-form', 'tl-compose', 'issue-add-sub')
    allowed = {'background', 'background-color', 'box-shadow', 'outline', 'color', 'caret-color', 'border-color'}
    text = css('app.css')
    for selector, body in rules(text):
        if any(c in selector for c in contexts) and re.search(r':(hover|focus)', selector):
            assert set(decls(body)) <= allowed, (selector, body)
    base = [decls(b) for s, b in rules(text) if s == ':is(input, textarea).inline-edit']
    assert base and base[0]['padding'] == '0' and base[0]['border'] == '0'


def test_grow_holds_the_page_height_while_measuring():
    js = (STATIC / 'issues.js').read_text()
    body = js[js.index('function grow(el)'):js.index('function growAll')]
    assert body.index("holder.style.minHeight = holder.offsetHeight") < body.index("el.style.height = 'auto'")
    assert 'window.scrollTo' in body
    # Any page's autogrow textarea grows, not only issue forms.
    assert "document.addEventListener('input', function (e) { if (e.target.matches && e.target.matches('textarea[data-autogrow]')) grow(e.target); });" in js


def test_issue_page_fields_and_subissue_rows(client, repo):
    parent = I.create('对齐验收', 'us-rok-reassurance', '', I.revision())
    child = I.create('有标签的子议题', 'us-rok-reassurance', parent, I.revision(parent))
    I.update(child, 'labels', ['文献地图'], I.revision(child))
    I.create('没有标签的子议题', 'us-rok-reassurance', parent, I.revision(parent))
    page = client.get(f'/issues/{parent}').text
    assert '支持 Markdown' not in page
    actions = page.split('class="description-actions"')[1].split('</div>', 1)[0]
    assert 'data-save-status' in actions and '<noscript>' not in actions
    for label in ('议题标题', '议题描述', '发表评论', '子议题标题'):
        tag = re.search(r'<(?:input|textarea)\b[^>]*aria-label="' + label + '"[^>]*>', page).group(0)
        assert 'inline-edit' in tag, label
    # Labels sit in one cell, so a row with two labels and a row with none keep the same columns.
    rows = page.split('class="issue-subtree"')[1].split('class="issue-add-sub"')[0]
    assert rows.count('class="issue-row-labels"') == 1
