"""Card 0095 (ISSUES-LINEAR.md §2.1, §2.5): the issue on the page canvas, properties that save on the click
with a filter and their shortcut, a read-only 课题 for sub-issues, 研究方向 as a property, phone chips."""
import re
from pathlib import Path

from research_lab.files import issues as I

WEB = Path(__file__).resolve().parents[1] / 'research_lab' / 'web'


def new(title, parent=''):
    return I.create(title, 'us-rok-reassurance', parent, I.revision(parent))


def test_properties_save_on_the_click_with_filter_and_shortcuts(client, repo):
    ref = new('排版验收')
    page = client.get(f'/issues/{ref}').text
    aside = page.split('<aside class="issue-properties"', 1)[1].split('</aside>', 1)[0]
    assert '保存' not in aside                                           # no 保存 button left in any menu
    assert aside.count('data-instant') == 2                              # labels and relations save on change
    assert 'data-due-form' in aside                                      # the due menu (0094): presets and typed dates
    assert '<kbd>S</kbd>' in aside and '<kbd>P</kbd>' in aside and '<kbd>L</kbd>' in aside
    assert aside.count('data-menu-filter') >= 5 and 'name="field" value="direction"' in aside
    assert 'name="field" value="project"' not in aside                   # a sub-issue's 课题 is a link, not a menu
    assert 'href="/issues?project=us-rok-reassurance" class="issue-related-link"' in aside
    chips = page.split('class="issue-chips"', 1)[1].split('<form class="issue-description-form"', 1)[0]
    assert 'name="field" value="status"' in chips and 'name="field" value="due"' in chips
    assert page.index('class="issue-title-form"') < page.index('class="issue-chips"') < page.index('class="issue-description-form"')
    assert 'data-copy-link' in page and 'Back to parent' in page            # the × became copy-link and 更多


def test_the_page_has_no_card_frame_and_the_old_list_template_is_gone():
    css = re.sub(r'/\*.*?\*/', '', (WEB / 'static' / 'app.css').read_text(), flags=re.S)
    assert not re.search(r'\.issue-detail\s*\{[^}]*border:\s*1px', css) and '.issue-index, .issue-detail' not in css
    assert not re.search(r'\.issue-properties\s*\{[^}]*border-left', css)
    assert not (WEB / 'templates' / 'pages' / 'issues.html').exists()
