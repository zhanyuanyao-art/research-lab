"""助手 page layout (card 0071): a fixed-height two-pane panel. The page never scrolls; the thread list and the
message stream scroll inside their panes, the composer sits in a dock under the stream, both pane heads match,
and chat.js follows the newest message by scrolling the stream container instead of the window."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "research_lab" / "web" / "static"


def _at(html: str, needle: str) -> int:
    i = html.find(needle)
    assert i >= 0, needle
    return i


def _head(html: str) -> str:
    return html.split("</head>", 1)[0]


def _new_thread(client) -> str:
    r = client.post("/chat/send", data={"text": "/help", "page": "/chat", "thread_id": ""}, follow_redirects=False)
    return r.headers["X-Chat-Thread"]


def _assert_panes(html: str) -> None:
    layout, side, main = _at(html, 'class="chat-layout"'), _at(html, 'class="chat-side"'), _at(html, 'class="chat-main')
    assert layout < side < main
    # left pane: head, own scroller, 后台任务 pinned after it
    assert _at(html, '<div class="chat-pane-head">') < _at(html, 'class="chat-thread-scroll"') < _at(html, 'chat-jobs-link')
    # right pane opens with a pane head of the same class
    assert main < _at(html, 'class="chat-pane-head chat-head"')
    assert "chat.css" in _head(html)


def test_conversation_pane_scrolls_inside_and_docks_the_composer(client):
    html = client.get("/chat").text
    _assert_panes(html)
    head, scroll, stream = _at(html, "chat-head"), _at(html, 'id="chat-scroll"'), _at(html, 'id="chat-stream"')
    dock, composer = _at(html, 'class="chat-dock"'), _at(html, "data-composer-here")
    assert head < scroll < stream < dock < composer
    # the composer is outside the scroller: the stream's closing tags come before the dock
    between = html[stream:dock]
    assert between.count("<div") < between.count("</div>")
    assert "chat-welcome" in html[stream:dock]
    # one stylesheet link (from the head), no second copy from the composer partial
    assert html.count("/static/chat.css") == 1


def test_thread_page_keeps_archive_in_the_head_and_opens_at_the_end(client):
    tid = _new_thread(client)
    html = client.get(f"/chat/{tid}").text
    _assert_panes(html)
    head = html[_at(html, 'class="chat-pane-head chat-head"'):_at(html, 'id="chat-scroll"')]
    assert f'hx-post="/chat/{tid}/archive"' in head and ">Archive<" in head
    assert f'data-thread="{tid}"' in html and f'name="thread_id" value="{tid}"' in html
    # positioned at the newest message before first paint, after the dock is parsed
    script = html[_at(html, 'class="chat-dock"'):]
    assert re.search(r"getElementById\('chat-scroll'\)", script) and "scrollTop = s.scrollHeight" in script
    assert '<li class="active">' in html and f'href="/chat/{tid}" aria-current="page"' in html


def test_chat_page_takes_dropped_files_in_the_composer_only(client):
    body = re.search(r"<body[^>]*>", client.get("/chat").text).group(0)
    assert "data-no-global-drop" in body
    # 后台任务 has no composer: a dropped PDF still imports into the library there
    assert "data-no-global-drop" not in re.search(r"<body[^>]*>", client.get("/chat/jobs").text).group(0)


def test_jobs_page_uses_the_same_panes(client):
    html = client.get("/chat/jobs").text
    _assert_panes(html)
    head = html[_at(html, 'class="chat-pane-head chat-head"'):]
    assert head.index("后台任务") < head.index('class="chat-scroll jobs-scroll"')
    assert 'class="chat-jobs-link active"' in html


def test_layout_css_has_no_page_scroll_or_sticky_offsets():
    css = (STATIC / "chat.css").read_text(encoding="utf-8")
    page = css[css.index("/* ---- conversation page ----"):css.index("/* ---- welcome ---- */")]
    assert ".main:has(> .content > .chat-layout)" in page and "100dvh" in page
    assert re.search(r"\.chat-scroll \{[^}]*overflow-y: auto", page)
    assert re.search(r"\.chat-thread-scroll \{[^}]*overflow-y: auto", page)
    # the sticky offsets that misaligned the heads and let text show under the top bar are gone
    assert "sticky" not in page and "top: 56px" not in page and "top: 64px" not in page
    assert re.search(r"\.chat-dock \{[^}]*background: var\(--card-bg\)", page)
    # colours only from tokens
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", page)


def test_chat_js_scrolls_the_stream_not_the_window():
    js = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert "root.querySelector('.chat-scroll')" in js and "ResizeObserver" in js
    assert "window.scrollY" not in js and "scrollIntoView" not in js
    assert "input.focus()" not in js   # every focus passes preventScroll
    assert "isComposing" in js and "keyCode !== 229" in js
