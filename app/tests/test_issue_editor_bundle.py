"""The vendored TipTap bundle and the issue editor wrapper (card 0096).

The bundle is built by scripts/vendor/editor/build.mjs and committed, so the app runs offline and never
needs node. These tests check the committed files, not a rebuild."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import research_lab
from research_lab.files.markdown import render

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(research_lab.__file__).resolve().parent / "web" / "static"
VENDOR_SRC = ROOT / "scripts" / "vendor" / "editor"
BUNDLE = STATIC / "vendor" / "tiptap-editor.min.js"
LICENCES = STATIC / "vendor" / "tiptap-editor.LICENSES.txt"
WRAPPER = STATIC / "issue-editor.js"
STYLES = STATIC / "vendor" / "issue-editor.css"


def manifest():
    return json.loads((VENDOR_SRC / "package.json").read_text())


def test_versions_are_pinned_exactly():
    pkg = manifest()
    for name, version in {**pkg["dependencies"], **pkg["devDependencies"]}.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{name} must be pinned exactly, not {version!r}"
    assert pkg["dependencies"]["@tiptap/core"].startswith("3.")


def test_lockfile_comes_from_the_npm_registry_with_integrity():
    lock = json.loads((VENDOR_SRC / "package-lock.json").read_text())
    pinned = manifest()["dependencies"]
    packages = {k: v for k, v in lock["packages"].items() if k}
    assert packages, "package-lock.json lists no packages"
    for path, meta in packages.items():
        assert meta.get("resolved", "").startswith("https://registry.npmjs.org/"), path
        assert meta.get("integrity", "").startswith("sha512-"), path
    for name, version in pinned.items():
        assert packages[f"node_modules/{name}"]["version"] == version, name


def test_bundle_is_committed_with_versions_and_licence_banner():
    assert BUNDLE.is_file()
    head = BUNDLE.read_text(encoding="utf-8")[:400]
    pinned = manifest()["dependencies"]
    assert f"TipTap {pinned['@tiptap/core']}" in head
    assert f"markdown-it {pinned['markdown-it']}" in head
    assert f"prosemirror-markdown {pinned['prosemirror-markdown']}" in head
    assert "tiptap-editor.LICENSES.txt" in head
    assert "var RLEditor=" in BUNDLE.read_text(encoding="utf-8")[:2000]
    size = BUNDLE.stat().st_size
    assert 200_000 < size < 900_000, f"unexpected bundle size {size}"


def test_licence_file_covers_every_bundled_package():
    text = LICENCES.read_text(encoding="utf-8")
    pinned = manifest()["dependencies"]
    for name in ("@tiptap/core", "@tiptap/pm", "markdown-it", "prosemirror-markdown"):
        assert f"{name} {pinned[name]} (MIT)" in text
    for name in ("prosemirror-model", "prosemirror-view", "prosemirror-state", "linkifyjs", "entities"):
        assert re.search(rf"^{re.escape(name)} \d+\.\d+\.\d+ \(", text, re.M), name
    assert text.count("Permission is hereby granted") >= 20


def test_wrapper_uses_only_what_the_bundle_exports():
    entry = (VENDOR_SRC / "entry.mjs").read_text()
    exported = set(re.findall(r"export \* as (\w+)", entry))
    for group in re.findall(r"export \{([^}]*)\}", entry):
        exported |= {part.split(" as ")[-1].strip() for part in group.split(",")}
    exported |= set(re.findall(r"export const (\w+)", entry))
    used = set(re.findall(r"\bR\.([A-Za-z]+)", WRAPPER.read_text()))
    assert used, "issue-editor.js no longer reads window.RLEditor"
    assert used <= exported, f"not exported by the bundle: {sorted(used - exported)}"


def test_wrapper_references_no_external_address():
    src = WRAPPER.read_text()
    assert not re.search(r"https?://[A-Za-z0-9]", src), "external URL in issue-editor.js"
    assert not re.search(r"[\"'(=]\s*//[A-Za-z0-9]", src), "protocol-relative URL in issue-editor.js"
    for banned in ("fetch(", "XMLHttpRequest", "import(", "importScripts", "<script", "WebSocket", "sendBeacon"):
        assert banned not in src, banned


def test_editor_styles_load_nothing_from_outside():
    css = STYLES.read_text()
    assert "@import" not in css
    for url in re.findall(r"url\(([^)]*)\)", css):
        assert url.strip("'\" ").startswith("data:"), url
    assert "@media (prefers-color-scheme" not in css, "colours come from app.css tokens, not a second theme"
    assert "var(--surface)" in css and "var(--fg)" in css


def test_wrapper_is_plain_script_with_one_global():
    src = WRAPPER.read_text()
    assert src.lstrip().startswith("//") and "window.RLIssueEditor = {" in src
    assert "console." not in src


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_browser_and_server_render_markdown_the_same(tmp_path):
    """The editor reads Markdown with markdown-it (JS) and the page with markdown-it-py; same rules."""
    corpus = [
        "a **b** *c* ~~d~~ ++e++ `f`\n第二行", "按**「承诺可信度」**归档", "的*（注）*后", "😀**粗**😀",
        "- [ ] a\n- [x] b\n  - [ ] c", "- [ ] a\n- b", "1. [ ] a",
        "+++ 检索式\n内容 **粗**\n\n- 列表\n+++\n后面", "+++\n空标题\n+++", "+++ 未闭合\n内容", "- a\n  +++ x\n  +++",
        "> 引用\n\n```py\n**x**\n```", "**a `b` c**", "[`x` ++u++](https://e.com)", "~~a ++b++ c~~", "++a ~~b~~ c++",
        "- ++u++ 和 **b**", "[x](javascript:alert(1)) [y](/papers/1) [z](//evil.com) <https://ok.com>",
        "<script>alert(1)</script>", "1. a\n2. b", "++++", "a+b++c", "++a+++", "\\+\\+x\\+\\+",
        "| a | b |\n|---|---|\n| 1 | 2 |", "![img](https://e.com/x.png)", "***bi***", "_it_ and __b__",
        "x [l](https://a.com \"t\") y", "~~++x++~~", "++[l](https://a.com)++", "[++u++ **b**](https://a.com)",
    ]
    script = tmp_path / "render.js"
    script.write_text(
        "const fs = require('fs'), vm = require('vm');\n"
        "const ctx = { console, navigator: { platform: 'MacIntel', userAgent: '' } };\n"
        "ctx.window = ctx; ctx.globalThis = ctx; vm.createContext(ctx);\n"
        "vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + ';window.RLEditor = RLEditor;', ctx);\n"
        "vm.runInContext(fs.readFileSync(process.argv[3], 'utf8'), ctx);\n"
        "const corpus = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        "process.stdout.write(JSON.stringify(corpus.map(s => ctx.RLIssueEditor.render(s))));\n")
    out = subprocess.run(["node", str(script), str(BUNDLE), str(WRAPPER)], input=json.dumps(corpus),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    browser = json.loads(out.stdout)
    for source, js in zip(corpus, browser):
        assert js == str(render(source)), source


def test_the_slash_menu_offers_the_blocks_and_the_page_commands():
    """card 0118: / at the start of a line or after a space; the table's rules decide what is greyed out."""
    src = WRAPPER.read_text()
    items = src.split("var SLASH_ITEMS = [", 1)[1].split("];", 1)[0]
    for label in ("标题 1", "列表", "编号列表", "待办清单", "引用", "折叠", "代码块", "附件", "关联文献"):
        assert f"label: '{label}'" in items, label
    assert "command: 'attach'" in items and "command: 'link-material'" in items
    assert "'rl:editor-command'" in src                                   # the page opens its own pickers
    assert r"/(^|\s)\/([^\s/]{0,24})$/" in src                            # line start or after whitespace
    assert "parent.type.spec.code || parent.type.name === 'detailsSummary'" in src   # never in code or a title
    assert "'列表里只放段落'" in src and "'引用里只放段落'" in src
    css = STYLES.read_text()
    assert ".rl-slash {" in css and '.rl-slash-item[aria-disabled="true"]' in css
