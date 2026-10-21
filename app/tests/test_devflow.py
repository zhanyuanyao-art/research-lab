"""Development workflow: cards, file ownership, the step rules, release notes, and the 开发 page."""
from __future__ import annotations

import subprocess

import pytest


def test_card_round_trip_and_board(repo):
    from research_lab.devflow import cards
    c = cards.new("推送：一键导入时下载开放获取 PDF", files=["app/research_lab/feeds/**"], priority="P1",
                  why="Leo：导入后还要手动上传 PDF", done="有开放获取链接时自动下载并走完整导入")
    assert c.id == "0001" and c.path.name.startswith("0001-") and (repo / "dev/cards").is_dir()
    again = cards.get("1")
    assert again.meta["title"] == "推送：一键导入时下载开放获取 PDF" and again.files == ["app/research_lab/feeds/**"]
    assert "Leo：导入后还要手动上传 PDF" in again.body and "## 验收标准" in again.body
    cards.update(again, status="coding", owner="ui-session")
    cards.append_section(cards.get("1"), "说明（coder 填）", "- 改了 importer.py")
    reread = cards.get("0001")
    assert reread.status == "coding" and "- 改了 importer.py" in reread.body
    assert [x.id for x in cards.board()["coding"]] == ["0001"]
    with pytest.raises(cards.CardError):
        cards.update(reread, status="whatever")
    assert cards.next_id() == "0002"


def test_file_overlap_rules():
    from research_lab.devflow.cards import overlaps
    assert overlaps(["app/research_lab/feeds/**"], ["app/research_lab/feeds/importer.py"]) == ["app/research_lab/feeds/importer.py"]
    assert overlaps(["app/research_lab/web/templates/base.html"], ["app/research_lab/web/templates/*.html"])
    assert overlaps(["app/research_lab/agent/**"], ["app/research_lab/feeds/**"]) == []


def test_claim_refuses_without_files_and_on_clash(repo, monkeypatch):
    from research_lab.devflow import cards, flow
    monkeypatch.setattr(flow, "main_root", lambda: repo)
    a = cards.new("A", files=["app/research_lab/feeds/**"])
    b = cards.new("B", files=["app/research_lab/feeds/news.py"])
    none = cards.new("C")
    with pytest.raises(flow.FlowError, match="声明"):
        flow.claim(none.id, "s1")
    (repo.parent / f"research-lab.{flow.worktree_name(a)}").mkdir()  # an existing worktree: no script run
    claimed = flow.claim(a.id, "s1")
    assert claimed.status == "coding" and claimed.get("owner") == "s1" and claimed.get("branch").startswith("agent/c0001")
    with pytest.raises(flow.FlowError, match="占用"):
        flow.claim(b.id, "s2")
    with pytest.raises(flow.FlowError, match="已由 s1 领取"):
        flow.claim(a.id, "s2")


def test_review_and_integrate_rules(repo):
    from research_lab.devflow import cards, flow
    c = cards.new("D", files=["x.py"])
    cards.update(c, status="coding", owner="writer", branch="agent/d")
    with pytest.raises(flow.FlowError, match="还没通过测试"):
        flow.review(c.id, "pass", "other")
    cards.update(cards.get(c.id), status="review", test={"result": "pass"})
    with pytest.raises(flow.FlowError, match="另一个会话"):
        flow.review(c.id, "pass", "writer")
    back = flow.review(c.id, "changes", "other", "缺测试")
    assert back.status == "coding" and back.get("review")["verdict"] == "changes" and "缺测试" in back.body
    with pytest.raises(flow.FlowError, match="评审"):
        flow.integrate(c.id, "integrator", do_push=False)


def test_release_notes_newest_first(repo):
    from research_lab.devflow import cards, flow
    flow._note_unreleased("第一项")
    flow._note_unreleased("第二项")
    text = (cards.dev_dir() / "RELEASES.md").read_text(encoding="utf-8")
    assert text.index("第二项") < text.index("第一项") and text.count("## 未发布") == 1


def test_port_matches_worktree_script():
    from pathlib import Path
    from research_lab.devflow.status import port_of
    crc = int(subprocess.run(["cksum"], input="devops", capture_output=True, text=True).stdout.split()[0])
    assert port_of("devops") == 4101 + crc % 80
    # c0020-tasks-md-3 lands on 4180, the installed Mac app's port: both sides move it to 4181
    script = Path(__file__).resolve().parents[2] / "scripts" / "agent-worktree.sh"
    fn = subprocess.run(["sed", "-n", "/^port_of()/,/}$/p", str(script)], capture_output=True, text=True).stdout
    for name in ("devops", "c0020-tasks-md-3", "c0011-rl-dev-check"):
        sh = subprocess.run(["bash", "-c", fn + f'\nport_of "{name}"'], capture_output=True, text=True).stdout.strip()
        assert int(sh) == port_of(name)
    assert port_of("c0020-tasks-md-3") == 4181


def test_smoke_takes_a_free_port_and_stops_safely():
    import socket
    from research_lab.devflow import flow
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert flow.free_port(busy) != busy
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    flow._stop(proc)                                               # already exited: no PermissionError


def test_card_writes_keep_what_another_session_wrote(repo):
    from research_lab.devflow import cards
    c = cards.new("K", files=["k.py"])
    tester, reviewer = cards.get(c.id), cards.get(c.id)            # two sessions holding the same card
    cards.append_section(reviewer, "评审（reviewer 填）", "- 通过")
    cards.update(reviewer, review={"verdict": "pass", "by": "other"})
    cards.update(tester, status="review", test={"result": "pass"})
    cards.append_section(tester, "测试（tester 填）", "- 通过 · 260 passed")
    final = cards.get(c.id)
    assert final.get("review")["verdict"] == "pass" and final.get("test")["result"] == "pass"
    assert "- 通过\n" in final.body and "260 passed" in final.body


def test_records_only_behind(tmp_path):
    from research_lab.devflow.flow import records_only_behind
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                  check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    g("add", "."); g("commit", "-qm", "base")
    g("branch", "agent/x")
    assert records_only_behind("agent/x", tmp_path) == []
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev/c.md").write_text("card\n")
    g("add", "."); g("commit", "-qm", "dev only")
    assert records_only_behind("agent/x", tmp_path) == ["dev/c.md"]
    (tmp_path / "app.py").write_text("x = 2\n")
    g("commit", "-qam", "code")
    assert records_only_behind("agent/x", tmp_path) is None


def test_dev_page_renders(client, repo):
    from research_lab.devflow import cards
    cards.new("看板上的卡", files=["a.py"], priority="P1")
    page = client.get("/dev")
    assert page.status_code == 200 and "看板上的卡" in page.text and "接口用量" in page.text


def test_dirty_splits_code_and_content(tmp_path):
    from research_lab.devflow.status import dirty
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "_artifacts").mkdir()
    (tmp_path / "_artifacts/a.md").write_text("x")
    (tmp_path / "app.py").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"], check=True)
    (tmp_path / "_artifacts/a.md").write_text("y")  # " M _artifacts/a.md": the line starts with a space
    (tmp_path / "app.py").write_text("y")
    d = dirty(tmp_path)
    assert d == {"code": ["app.py"], "content": ["_artifacts/a.md"]}


def test_change_levels():
    from research_lab.devflow.flow import classify, higher
    assert classify([("app/research_lab/web/static/app.css", 10, 6)])[0] == "L0"
    assert classify([("dev/README.md", 30, 0), ("app/research_lab/web/templates/pages/feed.html", 4, 2)])[0] == "L0"
    assert classify([("app/research_lab/web/static/app.css", 70, 10)])[0] == "L1"          # small-only, but too big
    assert classify([("app/research_lab/web/routes/feed.py", 3, 1)])[0] == "L1"            # any Python
    level, why = classify([("app/research_lab/db/migrations/0015_pm.sql", 12, 0)])
    assert level == "L2" and "高风险" in why[0]
    assert classify([("app/research_lab/agent/tools.py", 1, 1)])[0] == "L2"               # AI guardrails live here
    assert classify([("app/research_lab/web/routes/feed.py", 300, 150)])[0] == "L2"       # large
    assert classify([("app/tests/test_feeds.py", 0, 40)])[0] == "L2"                      # deleting tests
    assert higher("L2", "L0") == "L2" and higher(None, "L0") == "L0" and higher(None, None) == "L1"


def test_review_gates_follow_the_level(repo):
    from research_lab.devflow import cards, flow
    big = cards.new("E", files=["x.py"])
    cards.update(big, status="review", owner="writer", branch="agent/e", test={"result": "pass", "level": "L2"})
    with pytest.raises(flow.FlowError, match="清单"):
        flow.review(big.id, "pass", "other")                                  # 重要: notes required
    assert flow.review(big.id, "pass", "other", "1–7 逐条看过").get("review")["verdict"] == "pass"
    unreviewed = cards.new("E2", files=["z.py"])
    cards.update(unreviewed, status="review", owner="writer", branch="agent/e2", test={"result": "pass", "level": "L2"})
    with pytest.raises(flow.FlowError, match="评审"):
        flow.integrate(unreviewed.id, "integrator", do_push=False)            # 重要: review required
    for name, path, level in (("F", "y.py", "L1"), ("G", "a.css", "L0")):     # 普通 and 小改: pass the review gate,
        card = cards.new(name, files=[path])                                  # then fail on git in a tmp dir
        cards.update(card, status="review", owner="writer", branch=f"agent/{name.lower()}",
                     test={"result": "pass", "level": level})
        with pytest.raises(Exception) as err:
            flow.integrate(card.id, "integrator", do_push=False)
        assert "评审" not in str(err.value)
    held = cards.new("I", files=["w.py"])
    cards.update(held, status="review", owner="writer", branch="agent/i", test={"result": "pass", "level": "L1"},
                 review={"verdict": "changes", "by": "other"})
    with pytest.raises(flow.FlowError, match="要求修改"):
        flow.integrate(held.id, "integrator", do_push=False)                 # a changes verdict holds any level
    cards.update(cards.get(held.id), review={"verdict": "pass", "by": "other"})
    with pytest.raises(Exception) as err:
        flow.integrate(held.id, "integrator", do_push=False)
    assert "要求修改" not in str(err.value)
    raised = cards.new("H", files=["b.css"], level="L2")
    cards.update(raised, test={"result": "pass", "level": "L0"})
    assert flow.level_of(cards.get(raised.id)) == "L2"                        # the planner's level wins upward


def test_review_follow_ups(repo, tmp_path):
    from research_lab.devflow import cards
    from research_lab.devflow.flow import changes_of, classify
    # (b) a mistyped level is refused, not silently treated as 普通
    with pytest.raises(cards.CardError, match="等级"):
        cards.new("I", files=["a.css"], level="L3")
    with pytest.raises(cards.CardError, match="等级"):
        cards.update(cards.new("J", files=["b.css"]), level="high")
    # (c) cutting assertions while touching one line still counts as removing tests
    assert classify([("app/tests/test_feeds.py", 1, 12)])[0] == "L2"
    assert classify([("app/tests/test_feeds.py", 10, 12)])[0] == "L1"
    # (d) the whole db/ package is 重要
    assert classify([("app/research_lab/db/connection.py", 2, 1)])[0] == "L2"
    # (a) changes are measured from the fork point, so main moving on adds nothing
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                  check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "base.css").write_text("a\n")
    g("add", "."); g("commit", "-qm", "base")
    g("checkout", "-qb", "agent/x")
    (tmp_path / "base.css").write_text("a\nb\n"); g("commit", "-qam", "branch")
    g("checkout", "-q", "main")
    (tmp_path / "app").mkdir()
    (tmp_path / "app/new.py").write_text("x = 1\n" * 50); g("add", "."); g("commit", "-qm", "main moves on")
    g("checkout", "-q", "agent/x")
    assert changes_of(tmp_path) == [("base.css", 1, 0)]


def test_page_check_runs_on_a_copy_of_the_database(repo, monkeypatch):
    """Card 0037: the smoke server gets RL_DATA_DIR pointing at a snapshot, so a branch's migrations never
    land in Leo's database."""
    import sqlite3
    from research_lab.config import get_paths
    from research_lab.db.connection import connect, migrate
    from research_lab.devflow import flow
    real = get_paths().db
    c = connect()
    migrate(c)
    before = c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    c.close()
    seen = {}

    class Dead:
        pid = 0
        def poll(self):
            return 1
        def wait(self, timeout=None):
            return 1

    def fake_popen(cmd, cwd=None, env=None, **kw):
        seen["data"] = env["RL_DATA_DIR"]
        copy = sqlite3.connect(f"{env['RL_DATA_DIR']}/lab.sqlite")
        seen["migrations"] = copy.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        copy.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (999, 'branch')")   # a branch migration
        copy.commit()
        copy.close()
        return Dead()
    monkeypatch.setattr(flow.subprocess, "Popen", fake_popen)
    assert flow._smoke(repo, 4199) == [("/", "服务没有起来")]
    assert seen["data"] != str(real.parent) and seen["migrations"] == before
    after = sqlite3.connect(real)
    assert after.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 999").fetchone()[0] == 0
    after.close()
    from pathlib import Path
    assert not Path(seen["data"]).exists()                                       # the copy is gone afterwards


def test_integrate_refuses_code_other_than_the_tested_commit(repo, monkeypatch):
    from research_lab.devflow import cards, flow
    monkeypatch.setattr(flow, "git", lambda *a, **k: "head1" if a[:1] == ("rev-parse",) else "")
    card = cards.new("J", files=["v.py"])
    for tested in (None, "head0"):                                            # untested commit, or edits uncommitted
        cards.update(cards.get(card.id), status="review", owner="writer", branch="agent/j",
                     test={"result": "pass", "level": "L1", "commit": tested})
        with pytest.raises(flow.FlowError, match="重跑"):
            flow.integrate(card.id, "integrator", do_push=False)
    cards.update(cards.get(card.id), test={"result": "pass", "level": "L1", "commit": "head1"})
    with pytest.raises(Exception) as err:
        flow.integrate(card.id, "integrator", do_push=False)
    assert "重跑" not in str(err.value)


def test_check_runs_the_suite_in_parallel_only_when_xdist_is_there(monkeypatch, tmp_path):
    import subprocess
    from research_lab.devflow import flow
    for code, expected in ((0, ["-n", "auto"]), (1, [])):
        monkeypatch.setattr(flow, "_run", lambda cmd, cwd, env=None, timeout=900, code=code:
                            subprocess.CompletedProcess(cmd, code, "", ""))
        assert flow._parallel(tmp_path) == expected
