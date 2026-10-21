"""Card 0147 (Leo 10-12: 「增加拖动排序」): sub-issues reorder by drag or Alt+↑/↓ on the issue page. The order is tasks.yml's
file order among siblings; a move is checked against the page's revision (409 when old) and against the page's own
list; keys never change; the list shows 手动 order when Leo picks it (a cookie), 按截止 otherwise."""
from datetime import date, timedelta

from research_lab.files import issues as I, tasks as T

SLUG = "us-rok-reassurance"
JSON = {"Accept": "application/json", "HX-Request": "true"}


def new(title, parent=""):
    return I.create(title, SLUG, parent, I.revision(parent))


def kids(parent):
    return [t["id"] for t in T.children(parent)]


def move(client, page, ref, **fields):
    return client.post(f"/issues/{ref}/move", data={"page": page, "revision": I.revision(page), **fields}, headers=JSON)


def test_a_drag_puts_a_sub_issue_above_another_or_last_without_touching_keys(client, repo):
    I.sync_keys(create=True)
    parent = new("第二章")
    a, b, c = (new(t, parent) for t in ("第一节", "第二节", "第三节"))
    later = new("后面的待办")
    keys = {r: I.key_of(r) for r in (a, b, c)}
    r = move(client, parent, c, before=a)
    assert r.status_code == 200 and r.json()["revision"] == I.revision(parent) and kids(parent) == [c, a, b]
    assert move(client, parent, c, before="").status_code == 200 and kids(parent) == [a, b, c]
    ids = [t["id"] for t in T.read()]
    assert ids.index(c) < ids.index(later)                     # last among its siblings, not at the end of the file
    assert {x: I.key_of(x) for x in (a, b, c)} == keys          # order is not numbering


def test_alt_arrows_step_one_place_and_stop_at_the_ends(client, repo):
    parent = new("父议题")
    a, b, c = (new(t, parent) for t in ("甲", "乙", "丙"))
    assert move(client, parent, c, step="up").status_code == 200 and kids(parent) == [a, c, b]
    assert move(client, parent, a, step="down").status_code == 200 and kids(parent) == [c, a, b]
    assert move(client, parent, c, step="up").status_code == 200 and kids(parent) == [c, a, b]


def test_a_stale_page_or_a_row_from_elsewhere_moves_nothing(client, repo):
    parent, other = new("父议题"), new("另一个父议题")
    a, b = new("甲", parent), new("乙", parent)
    stranger = new("别处的子议题", other)
    old = I.revision(parent)
    I.update(a, "status", "doing", I.revision(a))
    stale = client.post(f"/issues/{b}/move", data={"page": parent, "revision": old, "before": a}, headers=JSON)
    assert stale.status_code == 409 and kids(parent) == [a, b]
    assert move(client, parent, stranger, before=a).status_code == 400
    assert move(client, parent, b, before=stranger).status_code == 400 and kids(parent) == [a, b]
    for output in [r["id"] for r in I.read() if r["artifact"]][:1]:     # outputs keep their place
        assert move(client, I.project_ref(I.get(output)["project"]), output, before="").status_code == 400


def test_the_list_shows_due_order_until_leo_picks_manual(client, repo):
    parent = new("父议题")
    late, soon = new("晚的", parent), new("早的", parent)
    T.update(late, {"due": (date.today() + timedelta(days=20)).isoformat()})
    T.update(soon, {"due": (date.today() + timedelta(days=2)).isoformat()})
    due = client.get(f"/issues/{parent}").text
    assert due.index('data-movable="' + soon) < due.index('data-movable="' + late) and ">By due</button>" in due
    client.cookies.set("rl_suborder", "manual")
    manual = client.get(f"/issues/{parent}").text
    assert manual.index('data-movable="' + late) < manual.index('data-movable="' + soon) and ">Manual</button>" in manual
    assert 'class="issue-row-grip" draggable="true" data-issue-grip' in manual
    client.cookies.clear()


def test_the_row_styles_live_in_app_css():
    from pathlib import Path
    import research_lab
    web = Path(research_lab.__file__).parent / "web"
    assert ".issue-row-menu" not in (web / "templates/pages/issue.html").read_text()
    css = (web / "static/app.css").read_text()
    assert ".issue-context-menu {" in css and ".issue-row-grip {" in css


def test_nested_siblings_can_move_from_the_project_page(client, repo):
    parent, other = new("父议题"), new("其他父议题")
    a, b = new("甲", parent), new("乙", parent)
    stranger = new("其他子议题", other)
    page = I.project_ref(SLUG)
    assert move(client, page, b, before=a).status_code == 200
    assert kids(parent) == [b, a]
    assert move(client, page, b, before=stranger).status_code == 400
    assert kids(parent) == [b, a]


def test_move_retains_checked_version_until_write(client, repo, monkeypatch):
    parent = new("父议题")
    a, b = new("甲", parent), new("乙", parent)
    original = T.move
    def concurrent(task, before, expected):
        T.update(a, {"title": "已修改"})
        return original(task, before, expected)
    monkeypatch.setattr(T, "move", concurrent)
    assert move(client, parent, b, before=a).status_code == 409
    assert kids(parent) == [a, b] and T.get(a)["title"] == "已修改"
