"""Card 0127 (Leo 10-11: 「cta 可以用英文，比如 read more，别用深度阅读这种」): buttons and action entries say what
they do in short English (Save, Cancel, Undo, Read more, New project …); headings, hints, descriptions, values and
tooltips stay Chinese. This walks every template the card covers and every button they draw, so a new Chinese
button label fails here instead of slipping in."""
import re
from pathlib import Path

import research_lab

TEMPLATES = Path(research_lab.__file__).parent / "web" / "templates"
STATIC = TEMPLATES.parent / "static"
# Rewritten by the literature cards (0128 and after) and the coach card (0124) with their own English buttons.
LATER = {"pages/papers.html", "pages/paper.html", "pages/search.html", "partials/_summary_card.html", "partials/_paper_card.html",
         "partials/_translation.html", "partials/_paper_line.html", "partials/_paper_peek.html", "pages/today.html",
         "pages/settings.html", "pages/guide.html", "partials/_coach_home.html"}
# Buttons whose words are data or a hint, not an action: a matrix cell's state, a picked material, a reject reason
# beside the other reasons, the chat box's hint, and counts that carry their Chinese unit.
NOT_ACTIONS = {
    ("partials/_cell.html", "{{ c.glyph }}"),
    ("partials/_issue_materials.html", "{{ h.label }}"),
    ("partials/_association_panel.html", "不说明理由"),
    ("partials/_composer.html", "输入 / 使用命令"),
    ("pages/artifact.html", "Read more · {{ '{:,}'.format(desc_chars) }} 字"),
    ("pages/feed.html", "View all{% if unread_count %}"),
}
ELEMENTS = [re.compile(r"<button\b[^>]*>(.*?)</button>", re.S),
            re.compile(r'<a\b[^>]*class="[^"]*\bbtn\b[^"]*"[^>]*>(.*?)</a>', re.S),
            re.compile(r'<summary\b[^>]*class="[^"]*\b(?:btn|kicker)\b[^"]*"[^>]*>(.*?)</summary>', re.S)]
MENU = re.compile(r'<div class="menu-panel[^"]*"[^>]*>(.*?)</div>', re.S)
CJK = re.compile(r"[一-鿿]")


def labels(text: str):
    """Each button's visible words: icons and tags out, Jinja kept (its literals are what shows)."""
    inners = [m.group(1) for pat in ELEMENTS for m in pat.finditer(text)]
    inners += [a.group(1) for m in MENU.finditer(text) for a in re.finditer(r"<a\b[^>]*>(.*?)</a>", m.group(1), re.S)]
    for inner in inners:
        words = re.sub(r"\{\{\s*(?:ui\.)?icon\([^)]*\)\s*\}\}", "", inner)
        yield re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", words)).strip()


def test_buttons_and_action_entries_are_short_english():
    chinese = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = str(path.relative_to(TEMPLATES))
        if rel in LATER:
            continue
        for label in labels(path.read_text(encoding="utf-8")):
            if CJK.search(label) and not any(rel == f and label.startswith(start) for f, start in NOT_ACTIONS):
                chinese.append(f"{rel}: {label}")
    assert not chinese, "Chinese button labels (card 0127 wants short English):\n" + "\n".join(chinese)


def empty_state_buttons(text: str):
    """The label argument (5th) of each ui.empty(...) call: the empty state's one button."""
    for m in re.finditer(r"ui\.empty\(", text):
        i, depth, quote, args, start = m.end(), 1, None, [], m.end()
        while depth:
            c = text[i]
            if quote:
                quote = None if c == quote else quote
            elif c in "'\"":
                quote = c
            elif c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif c == "," and depth == 1:
                args.append(text[start:i])
                start = i + 1
            i += 1
        args.append(text[start:i - 1])
        if len(args) >= 5:
            yield args[4].strip()


def test_empty_state_buttons_are_english_too():
    chinese = [f"{path.relative_to(TEMPLATES)}: {label}" for path in sorted(TEMPLATES.rglob("*.html"))
               if str(path.relative_to(TEMPLATES)) not in LATER
               for label in empty_state_buttons(path.read_text(encoding="utf-8")) if CJK.search(label)]
    assert not chinese, "\n".join(chinese)


def test_the_common_actions_share_one_word_each():
    """Save, Cancel and Undo everywhere: 保存 / 取消 / 撤销 are gone from the buttons, including the ones scripts draw."""
    seen = {label for path in TEMPLATES.rglob("*.html") if str(path.relative_to(TEMPLATES)) not in LATER
            for label in labels(path.read_text(encoding="utf-8"))}
    assert {"Save", "Cancel", "Edit", "Delete"} <= seen
    issues_js = (STATIC / "issues.js").read_text(encoding="utf-8")
    assert "textContent = 'Undo'" in issues_js and ">Cancel</button>" in issues_js and ">Save</button>" in issues_js
    assert "textContent = 'Confirm'" in issues_js and "textContent = 'Delete'" in issues_js
    for name in ("data.js", "weekly.js"):
        assert "'Copied'" in (STATIC / name).read_text(encoding="utf-8")


def test_headings_hints_and_values_stay_chinese(client, repo):
    """Only the buttons changed: the 研究档案 page still reads in Chinese around its English New project button."""
    page = client.get("/issues").text
    assert "New project" in page and "研究档案" in page and "全部待办" in page
    assert "新建课题" not in page
