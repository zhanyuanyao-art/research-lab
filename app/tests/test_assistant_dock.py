"""Floating assistant (card 0074): a glass button bottom-right on every page outside /chat opens a panel with the
current conversation. The shell is in base.html; GET /chat/dock fills it when it opens; chat.js drives the /chat
page and the panel with one controller."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "research_lab" / "web" / "static"


def send(client, text: str, page: str, thread_id: str = "", fresh: bool = False):
    data = {"text": text, "page": page, "thread_id": thread_id}
    if fresh:
        data["fresh"] = "1"
    return client.post("/chat/send", data=data, follow_redirects=False)


def _sidebar_link(html: str) -> str:
    return re.search(r'<a class="nav-item[^"]*" href="/chat"[^>]*>', html).group(0)


def test_every_ordinary_page_has_the_dock_shell(client):
    html = client.get("/papers").text
    head = html.split("</head>", 1)[0]
    assert "/static/chat.css" in head
    dock = html[html.index('id="assistant-dock"'):]
    assert "data-dock" in dock and "data-dock-toggle" in dock and 'id="dock-panel"' in dock
    assert "data-composer-dock" in dock and "/static/chat.js" in dock
    for hook in ("data-dock-title", "data-dock-threads", "data-dock-new", "data-dock-expand", "data-dock-close"):
        assert hook in dock, hook
    # the panel is empty until it opens; the inline script only restores the open state and prefetches
    assert 'class="chat-stream" aria-live="polite"></div>' in dock and "chat-msg" not in dock
    script = dock[dock.index("<script>(function"):]
    assert "localStorage.getItem('rl.dock')" in script and "try {" in script and "/chat/dock?thread=" in script
    assert "(max-width: 640px)" in script
    # the dock sits directly under <body>, after the page
    assert html.index('id="assistant-dock"') > html.index("</main>")
    # the sidebar's Theo goes to the full page (card 0088); the floating button is the small panel
    link = _sidebar_link(html)
    assert "data-dock-open" not in link and ">Theo<" in html[html.index(link):html.index(link) + 400]


def test_no_dock_on_chat_pages(client):
    tid = send(client, "/help", "/chat").headers["X-Chat-Thread"]
    for path in ("/chat", f"/chat/{tid}", "/chat/jobs"):
        html = client.get(path).text
        assert "data-dock" not in html, path
        assert "data-dock-open" not in _sidebar_link(html), path


def test_button_shows_the_pending_count(client, conn):
    assert "dock-count" not in client.get("/papers").text
    for n in (1, 2):
        conn.execute("INSERT INTO ai_jobs(id, kind, status, request_json, schema_json, created_at) "
                     "VALUES (?, 'news.arrange', 'queued', '{}', '{}', '2026-10-08T03:00:00+00:00')", (f"J-20991231-000{n}",))
    html = client.get("/papers").text
    assert '<span class="dock-count">2</span>' in html and "2 个待处理" in html
    assert '<span class="count">2 待处理</span>' in html   # the sidebar keeps its count too


def test_dock_fragment_renders_a_thread_and_the_switcher(client):
    a = send(client, "/help", "/chat").headers["X-Chat-Thread"]
    b = send(client, "/week", "/chat").headers["X-Chat-Thread"]
    r = client.get(f"/chat/dock?thread={a}")
    assert r.status_code == 200 and "<html" not in r.text
    assert f'data-dock-meta data-thread="{a}" data-title="帮助"' in r.text
    assert r.text.count('class="chat-msg ') == 2 and "可用命令如下" in r.text
    assert f'class="dock-thread active" href="/chat/{a}" data-dock-thread="{a}"' in r.text
    assert f'data-dock-thread="{b}"' in r.text and 'href="/chat">' in r.text
    assert "chat-welcome" not in r.text


def test_dock_fragment_new_conversation_for_unknown_or_archived(client):
    tid = send(client, "/help", "/chat").headers["X-Chat-Thread"]
    client.post(f"/chat/{tid}/archive")
    for q in ("", tid, "T-1999-0001"):
        r = client.get(f"/chat/dock?thread={q}")
        assert 'data-dock-meta data-thread="" data-title="新对话"' in r.text, q
        assert "chat-welcome" in r.text and 'class="chat-msg ' not in r.text


def test_dock_shows_only_the_newest_messages(client, conn):
    from research_lab.chat import service
    tid = service.create_thread(conn, "长对话")
    for i in range(34):
        service.add_message(conn, tid, "user" if i % 2 == 0 else "assistant", f"第 {i} 条")
    r = client.get(f"/chat/dock?thread={tid}").text
    assert r.count('class="chat-msg ') == 30
    assert f'<a class="chat-older" href="/chat/{tid}">更早的 4 条消息在完整视图里</a>' in r
    assert "第 33 条" in r and "第 3 条" not in r


def test_fresh_send_from_a_page_starts_a_new_thread(client):
    first = send(client, "/help", "/papers")
    a = first.headers["X-Chat-Thread"]
    # without fresh, a message from an ordinary page continues the recent thread (the old global composer)
    assert send(client, "/status", "/papers").headers["X-Chat-Thread"] == a
    r = send(client, "/week", "/issues", fresh=True)
    b = r.headers["X-Chat-Thread"]
    assert r.status_code == 204 and b != a and r.headers["HX-Redirect"] == f"/chat/{b}"
    # titled by the router's intent label, as a new thread on /chat is
    assert f'data-thread="{b}" data-title="本周进展"' in client.get(f"/chat/dock?thread={b}").text


def test_send_naming_the_thread_appends_from_any_page(client):
    tid = send(client, "/help", "/chat").headers["X-Chat-Thread"]
    r = send(client, "/status", "/papers", thread_id=tid)
    assert r.status_code == 200 and r.headers["X-Chat-Thread"] == tid
    assert 'class="chat-msg chat-msg-user' in r.text


def test_chat_js_one_controller_for_page_and_dock():
    js = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert "function mount(root, hooks)" in js and "RLChat.mount = mount" in js
    assert "mount(root, { redirect: adopt, dropTarget: document })" in js       # the /chat page
    assert "fresh: true" in js and "dropTarget: root" in js                       # the dock
    assert "data.append('page', pageForAssistant())" in js
    assert "'/chat/dock?thread='" in js and "window.RLDockPrefetch" in js
    # localStorage only inside try/catch
    for m in re.finditer(r"localStorage\.(get|set)Item", js):
        assert "try {" in js[max(0, m.start() - 60):m.start()], js[m.start() - 60:m.end()]
    assert "(e.key === 'j' || e.key === 'J')" in js and "e.key !== 'Escape'" in js
    assert "window.scrollY" not in js and "scrollIntoView" not in js and "input.focus()" not in js


def test_dock_css_glass_tokens_and_sheet():
    css = (STATIC / "chat.css").read_text(encoding="utf-8")
    dock = css[css.index("/* ---- floating assistant"):css.index("/* ---- welcome ---- */")]
    assert re.search(r"\.assistant-dock \{[^}]*position: fixed[^}]*z-index: 55", dock)   # above the drop overlay (50)
    assert re.search(r"\.dock-fab \{[^}]*background: var\(--glass\)[^}]*backdrop-filter: blur\(var\(--blur\)\)", dock)
    assert re.search(r"\.dock-panel \{[^}]*background: var\(--glass\)[^}]*backdrop-filter", dock)   # the sidebar's glass
    phone = dock[dock.index("@media (max-width: 640px)"):]
    assert re.search(r"\.dock-panel \{[^}]*position: fixed; inset: 0", phone) and "overflow: hidden" in phone
    assert re.search(r"\.dock-scroll \{[^}]*overflow-x: hidden", dock)
    assert "body:has(.assistant-dock) .content { padding-bottom: 88px; }" in dock                # room for the button
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", dock)


def test_app_js_leaves_dock_drops_to_the_dock():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert re.search(r"function inFileTarget\(e\) \{[^}]*\[data-dock\]", js)
    assert re.search(r"function inOwnTarget\(e\) \{[^}]*\[data-dock\]", js)


def test_issue_sheet_hides_dock_only_on_phone():
    css = (STATIC / "chat.css").read_text(encoding="utf-8")
    assert re.search(
        r"@media \(max-width: 559px\)\s*\{\s*(?:/\*.*?\*/\s*)?"
        r"body:has\(\.issue-popover\[open\]\) > \.assistant-dock\s*\{\s*display: none;\s*\}\s*\}",
        css, re.S,
    )
