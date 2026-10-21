"""Card 0161: the non-blocking notes of past reviews (0129, 0130, 0131, 0138, 0156), one test each where a page or a
file can show it; the scripts' behaviour was checked in the browser (see the card)."""
from pathlib import Path

import research_lab
from research_lab import weekly
from research_lab.feeds import importer

WEB = Path(research_lab.__file__).parent / "web"


def test_the_import_sheet_offers_no_direction(client):
    html = client.get("/imports/sheet", params={"page": "/papers?direction=strategy-signaling"}).text
    box = html.split('name="direction"', 1)[1].split("</select>", 1)[0]
    assert '<option value="">不指定方向</option>' in box and '<option value="strategy-signaling" selected>' in box


def test_the_sheet_says_which_files_it_skipped_and_offers_to_attach_on_a_bare_reader():
    js = (WEB / "static/import.js").read_text()
    assert "跳过 ' + skipped.length" in js and "只导入 PDF：" in js                    # not silently any more
    assert "!reader.dataset.pdfUrl && pdfs.length === 1" in js and "'/files'" in js     # attach_file, its duplicate check kept
    assert ">Attach to this paper</button>" in js and ">Import as new</button>" in js


def test_the_drop_overlay_is_only_the_highlight():
    base = (WEB / "templates/base.html").read_text()
    overlay = base.split('<div class="drop-overlay"', 1)[1].split("</div></div>", 1)[0]
    assert "松开以导入文献" not in overlay and "正在导入" not in overlay and "file-up" in overlay


def test_the_issue_picker_names_its_active_option_and_keeps_its_styles_in_css(client, conn):
    from datetime import datetime
    from research_lab.files import issues as I
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 ("P-1", "Reassurance after the Washington Declaration", "P-1", '["Lee, Min"]', 2024, now, now))
    conn.commit()
    I.create("梳理延伸威慑文献", "us-rok-reassurance", "", I.revision())
    page = client.get("/papers/P-1").text
    assert '<style>' not in page.split('id="paper-props"', 1)[0].rsplit("<header", 1)[-1]   # no inline block before the props
    assert 'role="combobox"' in page and 'aria-controls="paper-issue-results"' in page and 'id="paper-issue-results" role="listbox"' in page
    hits = client.get("/papers/P-1/issues/search").text
    assert 'id="paper-issue-opt-1"' in hits and 'aria-selected="false"' in hits
    assert "[data-paper-issue-picker] .issue-pick-panel" in (WEB / "static/paper-reader.css").read_text()
    assert "<style>" not in (WEB / "templates/partials/_paper_props.html").read_text()
    js = (WEB / "static/paper-reader.js").read_text()
    assert "setAttribute('aria-activedescendant', next.id)" in js and "setAttribute('aria-selected', 'true')" in js


def test_the_reader_breadcrumb_truncates_and_keeps_the_whole_title(client, conn):
    from datetime import datetime
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO papers(id, title, title_hash, authors_json, year, direction, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 ("P-2", "A Very Long Title About Reassurance", "P-2", '["Lee, Min"]', 2024, "alliance-us", now, now))
    conn.commit()
    crumbs = client.get("/papers/P-2").text.split('class="topbar-crumbs"', 1)[1].split("</nav>", 1)[0]
    assert 'title="联盟政治与美国研究"' in crumbs and "A Very Long Title About Reassurance" in crumbs.split('class="current"', 1)[1]
    css = (WEB / "static/paper-reader.css").read_text()
    assert ".topbar-crumbs > .current { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis" in css


def test_stale_words_and_the_empty_hook_are_gone(client):
    assert "alerts" not in weekly.__doc__
    assert not hasattr(importer, "autoimport_on_view")
    assert "autoimport_on_view" not in (WEB / "routes/today.py").read_text()
    assert client.get("/").status_code == 200
