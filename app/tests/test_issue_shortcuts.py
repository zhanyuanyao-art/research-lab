"""Card 0114 (ISSUES-LINEAR.md §2.7): ? opens the 快捷键速查 on issue pages and 研究档案, and the sheet lists the
keys the page really binds: every property menu's key, which is also the list issues.js listens for."""
import re
from pathlib import Path

from research_lab.files import issues as I

JS = Path(__file__).resolve().parents[1] / 'research_lab/web/static/issues.js'


def sheet(html):
    assert html.count('data-issue-shortcuts') == 1
    return html.split('<dialog class="issue-shortcuts" data-issue-shortcuts', 1)[1].split('</dialog>', 1)[0]


def test_the_sheet_lists_every_property_key_the_page_and_script_bind(client, repo):
    ref = I.create('快捷键测试', 'us-rok-reassurance', '', I.revision())
    html = client.get(f'/issues/{ref}').text
    combos = set(re.findall(r'data-combo="([^"]+)"', sheet(html)))
    menu_keys = set(re.findall(r'<details class="issue-popover"[^>]*data-key="([^"]+)"', html))
    assert menu_keys == {'S', 'P', 'A', 'L', '⇧D'}
    assert menu_keys <= combos and {'⌘⇧O', 'C', '?', '↑↓', '↩', 'Esc'} <= combos
    bound = re.search(r"\[((?:'[^']+',\s*)+'[^']+')\]\.indexOf\(key\)", JS.read_text()).group(1)
    assert set(re.findall(r"'([^']+)'", bound)) == menu_keys
    # Outside the workspace, so refreshing #issue-workspace never morphs an open sheet.
    assert html.index('data-issue-shortcuts') > html.rindex('id="issue-workspace"')
    workspace = html.split('id="issue-workspace"', 1)[1].split('<dialog class="issue-shortcuts"', 1)[0]
    assert 'data-issue-shortcuts' not in workspace


def test_the_archive_has_the_sheet_and_the_home_links_to_all_open_issues(client, repo):
    for path in ('/issues', '/issues?view=todo'):
        assert '快捷键速查' in sheet(client.get(path).text)
    home = client.get('/').text
    assert 'href="/issues?view=todo">全部待办' in home and 'href="/tasks?view=week">全部待办' not in home
