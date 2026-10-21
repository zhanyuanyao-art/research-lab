"""Small-bug autofix pipeline (card 0089): the whole run on fakes, plus the pieces that are cheap to exercise for
real (git in a throwaway repo, sandbox-exec on a probe). No test here starts the real Claude Code CLI, creates a
branch or worktree in the real repository, merges, releases or installs anything."""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from research_lab.devflow import autofix, cards, flow
from research_lab.devflow.autofix import ClaudeRun

GOOD_CHANGES = [("app/research_lab/web/routes/feed.py", 4, 1), ("app/tests/test_feed_ui.py", 12, 0)]
GOOD_DIFF = ("diff --git a/app/research_lab/web/routes/feed.py b/app/research_lab/web/routes/feed.py\n"
             "--- a/app/research_lab/web/routes/feed.py\n+++ b/app/research_lab/web/routes/feed.py\n"
             "@@ -1 +1 @@\n-meaning = None\n+meaning = item.get('meaning')\n")
SUMMARY = "简报里的意义字段没传到模板。改成从条目里读出来，并加了一条测试。"
PASS = '{"verdict": "pass", "notes": "清单 1–7 都没问题；这个 bug 修好了"}'
CHANGES = '{"verdict": "changes", "notes": "回归测试没有断言灰色说明的文字"}'
GREEN = {"ok": True, "summary": "301 passed", "failed": [], "pages": [("/", 200)]}
FAILED_CHECK = {"ok": False, "summary": "1 failed, 300 passed", "failed": ["FAILED app/tests/test_feed_ui.py::test_meaning"],
                "tracebacks": {"app/tests/test_feed_ui.py::test_meaning": "app/tests/test_feed_ui.py:40: in test_meaning\n"
                                                                         "E   AssertionError: 灰色说明不见了"},
                "pages": [("/", 200), ("/feed", 500)]}
ENV_SECRET = "deepseek-live-0123456789abcdef"     # stands in for a value read from app/.env


@pytest.fixture(autouse=True)
def autofix_allowed(request, monkeypatch):
    """conftest's repo fixture turns RL_AUTOFIX off for every other test; these run the pipeline on fakes."""
    if "repo" in request.fixturenames:
        request.getfixturevalue("repo")
    monkeypatch.delenv("RL_AUTOFIX", raising=False)


def diff_of(path: str, *added: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n" + "".join(f"+{l}\n" for l in added)


class World:
    """Fake outside world. Cards, claim and review are the real ones (claim reuses a pre-made folder, so
    no script and no git run); everything that would start a process or touch a repository is faked."""

    def __init__(self, tmp_path: Path):
        self.fixes: list[ClaudeRun] = []
        self.reviews: list[str] = []
        self.checks: list[dict] = []
        self.base = {"ok": True, "summary": "300 passed", "passed": 300}
        self.changes = list(GOOD_CHANGES)
        self.diff = GOOD_DIFF
        self.links: dict[str, str | None] = {}
        self.leftovers: list[str] = []
        self.catch: str | Exception = "current"
        self.calls: list[tuple[str, list[str], str, Path, int, dict]] = []
        self.jails: list = []
        self.commits: list[str] = []
        self.integrated: list[tuple[str, str]] = []
        self.reviewed_at_merge: list[dict] = []
        self.released: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.lock_owner: str | None = None
        self.lock_log: list[str] = []
        self.dirty: list[str] = []
        self.now = 0.0
        self.tags: set[str] = set()
        self.lock_file = tmp_path / "locks" / "rl-autofix.lock"

    def claude(self, cmd, prompt, cwd, timeout, env):
        kind = "fix" if "Edit" in cmd[cmd.index("--tools") + 1].split(",") else "review"
        assert self.lock_owner is None, "the integration lock is only taken for merge and release"
        self.calls.append((kind, cmd, prompt, cwd, timeout, env))
        if kind == "fix":
            return self.fixes.pop(0) if self.fixes else ClaudeRun(True, SUMMARY, 3)
        return ClaudeRun(True, self.reviews.pop(0) if self.reviews else PASS, 2)

    def fix_prompts(self) -> list[str]:
        return [c[2] for c in self.calls if c[0] == "fix"]

    def baseline(self, wt, jail):
        self.jails.append(("baseline", jail))
        return dict(self.base)

    def check(self, card_id, jail):
        self.jails.append(("check", jail))
        r = self.checks.pop(0) if self.checks else dict(GREEN)
        cards.update(cards.get(card_id), status="review" if r["ok"] else "coding",
                     test={"result": "pass" if r["ok"] else "fail", "level": "L1", "commit": "abc"})
        return r

    def catch_up(self, card):
        assert self.lock_owner == autofix.HOLDER
        if isinstance(self.catch, Exception):
            raise self.catch
        return self.catch

    def integrate(self, card_id, holder):
        # flow.integrate since Leo's 10-10 rule: L0/L1 merge on green tests, only a "changes" verdict stops them.
        # The review gate for autofix is therefore the pipeline's own; record what it was at merge time.
        assert self.lock_owner == holder == autofix.HOLDER                  # held by the caller (lock_held)
        c = cards.get(card_id)
        if c.get("test")["result"] != "pass" or (c.get("review") or {}).get("verdict") == "changes":
            raise flow.FlowError("integrate refused")
        cards.update(c, status="done")
        self.integrated.append((card_id, holder))
        self.reviewed_at_merge.append(dict(c.get("review") or {}))
        return "pushed main"

    def release(self, version, holder):
        assert self.lock_owner == holder == autofix.HOLDER
        self.released.append((version, holder))
        return [f"commit + tag v{version}", "installed"]

    def acquire(self, holder, purpose):
        if self.lock_owner and self.lock_owner != holder:
            raise flow.FlowError(f"集成锁在 {self.lock_owner} 手里")
        self.lock_owner = holder
        self.lock_log.append(f"take {holder}")

    def unlock(self, holder):
        if self.lock_owner == holder:
            self.lock_owner = None
            self.lock_log.append(f"drop {holder}")

    def sleep(self, seconds):
        self.now += seconds

    def steps(self, **over) -> autofix.Steps:
        s = autofix.Steps(
            claude=self.claude, claim=flow.claim, baseline=self.baseline, check=self.check, review=flow.review,
            catch_up=self.catch_up, integrate=self.integrate, release=self.release, stage=lambda wt: None,
            changes=lambda wt: list(self.changes), diff=lambda wt: self.diff, links=lambda wt, paths: dict(self.links),
            worktree_dirty=lambda wt: list(self.leftovers),
            commit=lambda wt, msg: self.commits.append(msg) or f"c{len(self.commits)}", main_dirty=lambda: list(self.dirty),
            acquire_lock=self.acquire, release_lock=self.unlock, version=lambda: "1.2.0", tag_exists=lambda v: v in self.tags,
            remove_worktree=lambda c: None, notify=lambda thread, text: self.messages.append(text),
            cli=lambda: "/fake/claude", model=lambda: "opus", enabled=lambda: True, secrets=lambda: {ENV_SECRET},
            sleep=self.sleep, clock=lambda: self.now)
        return dataclasses.replace(s, **over)

    def run(self, card, thread="T-2026-0001", over=None, **kw):
        kw.setdefault("queue_wait", 0)
        kw.setdefault("dirty_wait", 0)
        return autofix.run(card.id, thread=thread, steps=self.steps(**(over or {})), lock_file=self.lock_file, **kw)


@pytest.fixture()
def world(repo, tmp_path):
    return World(tmp_path)


def bug_card(repo, title="推送页：新闻的灰色说明不显示", **kw) -> cards.Card:
    c = cards.new(title, source="助手 · Leo 报的 bug", why="Leo：世界要闻下面那行灰色的字不见了", **kw)
    (repo.parent / f"research-lab.{flow.worktree_name(c)}").mkdir(exist_ok=True)
    return c


def assert_handed_off(out, card, *words):
    c = cards.get(card.id)
    assert out.status == "handed_off" and c.status == "blocked" and c.get("owner") == autofix.INTEGRATOR
    for w in words:
        assert w in out.reason
    assert "转交集成会话" in c.body


# ---------------------------------------------------------------- the whole way through

def test_happy_path_fixes_reviews_merges_and_installs_the_next_patch(world, repo, conn):
    from research_lab.chat import service
    thread = service.create_thread(conn, "推送页的灰色字")
    card = bug_card(repo)
    out = world.run(card, thread=thread, over={"notify": autofix.post_to_thread})

    assert out.status == "installed" and out.version == "1.2.1"
    assert world.released == [("1.2.1", autofix.HOLDER)] and world.integrated == [(card.id, autofix.HOLDER)]
    assert [(r["verdict"], r["by"]) for r in world.reviewed_at_merge] == [("pass", autofix.REVIEWER)]
    # the integration lock: taken once, after the review, held through integrate and release, then let go
    assert world.lock_log == [f"take {autofix.HOLDER}", f"drop {autofix.HOLDER}"] and world.lock_owner is None
    final = cards.get(card.id)
    assert final.status == "done" and final.get("owner") == autofix.HOLDER
    assert final.get("review")["by"] == autofix.REVIEWER != autofix.HOLDER
    assert final.files == sorted(p for p, _, _ in GOOD_CHANGES)          # the placeholder gave way to real files
    assert "已合并并装 v1.2.1" in final.body and SUMMARY in final.body
    assert len(world.commits) == 1 and "Co-Authored-By" in world.commits[0]

    # tests ran in the jail, both main's baseline and the fix's check
    assert [k for k, j in world.jails] == ["baseline", "check"] and all(callable(j) for _, j in world.jails)
    jailed, env = world.jails[1][1](["uv", "run", "pytest"], {"RL_AUTOFIX": "0"})
    assert jailed[:2] == [autofix.SANDBOX_EXEC, "-p"] and jailed[-3:] == ["uv", "run", "pytest"]
    assert env["RL_AUTOFIX"] == "0" and env["UV_NO_SYNC"] == "1" and Path(env["TMPDIR"]).name.startswith("rl-autofix-")

    fix = [c for c in world.calls if c[0] == "fix"]
    review = [c for c in world.calls if c[0] == "review"]
    assert len(fix) == 1 and len(review) == 1
    _, cmd, prompt, cwd, timeout, env = fix[0]
    wt = (repo.parent / f"research-lab.{flow.worktree_name(card)}").resolve()
    assert Path(cwd) == wt and timeout == 20 * 60 and cmd[cmd.index("--model") + 1] == "opus"
    assert "--restricted" in cmd and f"Edit(/{wt}/**)" in cmd
    assert "推送页：新闻的灰色说明不显示" in prompt and "regression test" in prompt and "AGENTS.md" in prompt
    assert env["RL_AUTOFIX"] == "0" and not any("DEEPSEEK" in k for k in env)
    assert "```diff" in review[0][2] and "item.get('meaning')" in review[0][2] and SUMMARY in review[0][2]
    assert "UNTRUSTED" in review[0][2] and review[0][4] == autofix.REVIEW_TIMEOUT_S

    said = [m.content for m in service.messages(conn, thread)]
    assert all(m.role == "assistant" for m in service.messages(conn, thread))
    assert said[0].startswith("开始修了（卡片 0001）")
    assert "改好了，全套测试通过，正在请另一个会话评审。" in said
    assert "修好了，正在装 v1.2.1，应用会重启一次。" in said
    assert said[-1].startswith("装好了 v1.2.1。") and SUMMARY in said[-1]

    reports = list((repo / "dev/reports").glob("0001-autofix-*.md"))
    assert len(reports) == 1 and out.report == str(reports[0])
    text = reports[0].read_text(encoding="utf-8")
    assert "硬性检查：通过" in text and "评审：pass" in text and "发版" in text


def test_next_patch_skips_tags_that_exist(world, repo):
    world.tags = {"1.2.1"}
    out = world.run(bug_card(repo))
    assert out.version == "1.2.2" and world.released[0][0] == "1.2.2"
    assert autofix.next_patch("0.9.9") == "0.9.10"
    with pytest.raises(flow.FlowError):
        autofix.next_patch("1.2")


def test_style_only_fix_needs_no_test_but_still_the_review(world, repo):
    world.changes = [("app/research_lab/web/static/app.css", 4, 2)]
    assert world.run(bug_card(repo)).status == "installed"
    assert [c[0] for c in world.calls] == ["fix", "review"] and world.reviewed_at_merge[0]["verdict"] == "pass"


def test_merging_waits_for_the_pipelines_own_review_even_when_integrate_would_not(world, repo):
    """flow.integrate merges an L1 card on green tests alone; the pipeline must not lean on it."""
    card = bug_card(repo)
    world.reviews = [CHANGES, CHANGES]
    lenient = []

    def integrate_anything(card_id, holder):        # an integrate with no review gate at all
        lenient.append(card_id)
        return "pushed main"
    out = world.run(card, over={"integrate": integrate_anything})
    assert_handed_off(out, card, "评审要求修改")
    assert not lenient and not world.released and world.lock_log == []


# ---------------------------------------------------------------- catching up with main under the lock

def test_main_moving_in_code_means_rebase_and_test_again_before_merging(world, repo):
    world.catch = "rebased"
    out = world.run(bug_card(repo))
    assert out.status == "installed" and [k for k, _ in world.jails] == ["baseline", "check", "check"]
    assert any("挪到最新的 main" in m for m in world.messages)


def test_red_tests_after_the_rebase_hand_off(world, repo):
    card = bug_card(repo)
    world.catch = "rebased"
    world.checks = [dict(GREEN), FAILED_CHECK]
    out = world.run(card)
    assert_handed_off(out, card, "挪到最新的 main 后", "测试没通过")
    assert not world.integrated and world.lock_owner is None


def test_a_rebase_conflict_hands_off(world, repo):
    card = bug_card(repo)
    world.catch = flow.FlowError("挪到最新的 main 时有冲突：feed.py")
    assert_handed_off(world.run(card), card, "冲突")
    assert not world.integrated and world.lock_owner is None


# ---------------------------------------------------------------- hard checks hand off

@pytest.mark.parametrize("path", ["_data/now.yml", "app/.env", "app/.env.local", "private/notes.md", "dev/cards/0001-x.md",
                                  ".github/workflows/ci.yml", "notes/today.md", "_artifacts/2026-W40-x.md",
                                  "projects/asean/README.md", ".claude/settings.json", "app/.claude/settings.json",
                                  "CLAUDE.md", "app/research_lab/CLAUDE.md", "AGENTS.md", ".mcp.json",
                                  "_data/新.yml", "notes/会议.md"])
def test_touching_a_forbidden_path_hands_off(world, repo, path):
    card = bug_card(repo)
    world.changes = [*GOOD_CHANGES, (path, 1, 0)]
    out = world.run(card)
    assert_handed_off(out, card, "不许自动修改", path)
    assert not world.commits and not world.released and not [c for c in world.calls if c[0] == "review"]
    assert world.messages[-1].startswith("这个问题没能自动修好，已转交集成会话")


def test_a_rename_out_of_a_forbidden_folder_counts(world, repo):
    card = bug_card(repo)
    world.diff = GOOD_DIFF + "--- a/dev/cards/0001-x.md\n+++ b/app/research_lab/x.md\n"
    assert_handed_off(world.run(card), card, "dev/cards/0001-x.md")


def test_a_binary_file_hands_off(world, repo):
    card = bug_card(repo)
    world.changes = [*GOOD_CHANGES, ("app/research_lab/web/static/x.png", -1, -1)]
    assert_handed_off(world.run(card), card, "二进制", "x.png")


@pytest.mark.parametrize("target,word", [(None, "仓库以外"), ("_data/now.yml", "不许碰")])
def test_a_symlink_out_of_bounds_hands_off(world, repo, target, word):
    card = bug_card(repo)
    world.changes = [*GOOD_CHANGES, ("app/research_lab/link.py", 1, 0)]
    world.links = {"app/research_lab/link.py": target}
    assert_handed_off(world.run(card), card, "符号链接", word)


def test_a_secret_in_the_diff_hands_off_without_repeating_it(world, repo):
    card = bug_card(repo)
    secret = "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx0123"
    world.diff = GOOD_DIFF + f"+API_KEY = '{secret}'\n"
    out = world.run(card)
    assert_handed_off(out, card, "密钥")
    assert secret not in out.reason and secret not in cards.get(card.id).body
    assert all(secret not in m for m in world.messages) and secret not in Path(out.report).read_text(encoding="utf-8")
    assert not world.commits


def test_a_value_from_app_env_in_the_diff_hands_off(world, repo):
    card = bug_card(repo)
    world.diff = GOOD_DIFF + diff_of("app/tests/test_feed_ui.py", f"KEY = '{ENV_SECRET}'")
    out = world.run(card)
    assert_handed_off(out, card, "app/.env 里的值")
    assert ENV_SECRET not in Path(out.report).read_text(encoding="utf-8")


@pytest.mark.parametrize("line,kind", [
    ("+KEY = 'sk-ant-api03-abcdefghijklmnopqrstuv'", "sk-"),
    ("+aws = 'AKIAIOSFODNN7EXAMPLE'", "AWS"),
    ("+-----BEGIN OPENSSH PRIVATE KEY-----", "PEM"),
    ("+token = '3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6d7e8f901'", "十六进制"),
    ("+gh = 'ghp_abcdefghijklmnopqrstuvwxyz0123456789'", "GitHub"),
])
def test_secret_shapes(line, kind):
    assert any(kind in k for k in autofix.secrets_in(f"--- a/app/x.py\n+++ b/app/x.py\n{line}\n"))


def test_ordinary_text_is_not_a_secret():
    assert autofix.secrets_in("+++ b/app/x.py\n+risk-assessment-matrix-for-the-quarter = 1\n"
                              "+n = 123456789012345678901234567890123456\n"
                              "-old = 'sk-abcdefghijklmnopqrstuvwxyz0123'\n") == []    # removed lines do not count


def test_redact_knows_the_env_values_and_the_shapes():
    text = f"key {ENV_SECRET} and sk-ant-api03-ZyXwVuTsRqPoNmLkJiHg"
    assert autofix.redact(text, {ENV_SECRET}) == "key 〔已隐去〕 and 〔已隐去〕"


def test_changing_conftest_hands_off(world, repo):
    card = bug_card(repo)
    world.changes = [*GOOD_CHANGES, ("app/tests/conftest.py", 2, 0)]
    assert_handed_off(world.run(card), card, "conftest.py")


@pytest.mark.parametrize("line", ["@pytest.mark.skip(reason='flaky')", "pytest.skip('later')", "@pytest.mark.xfail",
                                  "np = pytest.importorskip('numpy')"])
def test_a_new_skip_hands_off(world, repo, line):
    card = bug_card(repo)
    world.diff = GOOD_DIFF + diff_of("app/tests/test_feed_ui.py", line, "def test_x(): pass")
    assert_handed_off(world.run(card), card, "跳过测试")


def test_fewer_passing_tests_than_main_counts_as_a_failure(world, repo):
    card = bug_card(repo)
    world.checks = [dict(GREEN, summary="299 passed"), dict(GREEN, summary="299 passed")]
    out = world.run(card)
    assert_handed_off(out, card, "比 main 少", "299 < 300")
    assert "299 < 300" in world.fix_prompts()[1]


def test_main_red_before_the_fix_hands_off(world, repo):
    card = bug_card(repo)
    world.base = {"ok": False, "summary": "2 failed, 298 passed", "passed": 298}
    assert_handed_off(world.run(card), card, "main 的测试", "2 failed")
    assert not world.calls


def test_files_left_behind_by_the_tests_stop_the_review(world, repo):
    card = bug_card(repo)
    world.leftovers = ["app/research_lab/CLAUDE.md"]
    assert_handed_off(world.run(card), card, "没提交的文件", "CLAUDE.md")
    assert [c[0] for c in world.calls] == ["fix"]


def test_an_important_change_hands_off(world, repo):
    card = bug_card(repo)
    world.changes = [("app/research_lab/agent/tools.py", 3, 1), ("app/tests/test_agent.py", 6, 0)]
    out = world.run(card)
    assert_handed_off(out, card, "重要改动")
    assert not world.commits and not world.released


def test_a_card_marked_important_hands_off_even_for_a_small_diff(world, repo):
    card = bug_card(repo, level="L2")
    assert_handed_off(world.run(card), card, "卡片标为重要改动")


def test_a_code_fix_without_a_test_hands_off(world, repo):
    card = bug_card(repo)
    world.changes = [("app/research_lab/web/routes/feed.py", 3, 1)]
    out = world.run(card)
    assert_handed_off(out, card, "测试")
    assert not world.commits


def test_no_change_at_all_hands_off(world, repo):
    card = bug_card(repo)
    world.changes = []
    assert_handed_off(world.run(card), card, "没有改任何文件")


def test_a_clash_with_another_active_card_hands_off(world, repo):
    other = cards.new("别的卡", files=["app/research_lab/web/routes/feed.py"])
    cards.update(other, status="coding", owner="别的会话")
    card = bug_card(repo)
    assert_handed_off(world.run(card), card, "正在开发的卡片", other.id)


def test_a_fixer_writing_into_the_main_checkout_hands_off(world, repo):
    card = bug_card(repo)
    real = world.claude

    def sneaky(cmd, prompt, cwd, timeout, env):
        world.dirty = ["app/research_lab/web/routes/feed.py"]           # an edit that missed the worktree
        return real(cmd, prompt, cwd, timeout, env)
    out = world.run(card, over={"claude": sneaky})
    assert_handed_off(out, card, "主仓库", "feed.py")
    assert not world.commits


def test_secret_shaped_text_from_the_model_is_never_repeated(world, repo):
    secret = "sk-ant-api03-ZyXwVuTsRqPoNmLkJiHg"
    world.fixes = [ClaudeRun(True, f"改好了。顺便看到 .env 里的 {secret} 和 {ENV_SECRET}")]
    out = world.run(bug_card(repo))
    assert out.status == "installed"
    assert all(secret not in m and ENV_SECRET not in m for m in world.messages) and "〔已隐去〕" in world.messages[-1]
    report = Path(out.report).read_text(encoding="utf-8")
    assert secret not in report and ENV_SECRET not in report and ENV_SECRET not in cards.get("0001").body


def test_the_default_check_runs_the_whole_suite_in_the_jail(monkeypatch):
    seen = {}
    monkeypatch.setattr(flow, "check", lambda card_id, **kw: seen.update(kw, card=card_id) or {"ok": True})
    jail = object()
    autofix.jailed_check("0007", jail)
    assert seen == {"card": "0007", "full": True, "jail": jail} and autofix.Steps().check is autofix.jailed_check
    assert autofix.Steps().integrate is autofix.integrate_held and autofix.Steps().release is autofix.release_held


# ---------------------------------------------------------------- retry once, then hand off

def test_failed_tests_are_fed_back_once_and_the_retry_passes(world, repo):
    world.checks = [FAILED_CHECK]
    out = world.run(bug_card(repo))
    assert out.status == "installed" and len(world.commits) == 2
    prompts = world.fix_prompts()
    assert len(prompts) == 2 and "previous attempt did not pass" not in prompts[0]
    assert "test_feed_ui.py::test_meaning" in prompts[1] and "/feed 500" in prompts[1]
    assert "E   AssertionError: 灰色说明不见了" in prompts[1]                     # flow.check's short traceback (card 0053)
    assert any("再改一次" in m for m in world.messages)


def test_two_failures_hand_off(world, repo):
    card = bug_card(repo)
    world.checks = [FAILED_CHECK, FAILED_CHECK]
    out = world.run(card)
    assert_handed_off(out, card, "测试没通过", "1 failed")
    assert len(world.fix_prompts()) == 2 and not world.integrated and not world.released


def test_a_fixer_that_crashes_twice_hands_off(world, repo):
    card = bug_card(repo)
    world.fixes = [ClaudeRun(False, "超过 20 分钟没有结束，已中止"), ClaudeRun(False, "超过 20 分钟没有结束，已中止")]
    assert_handed_off(world.run(card), card, "修复会话没有正常结束")
    assert not world.commits


def test_review_asking_for_changes_goes_back_to_the_fixer(world, repo):
    card = bug_card(repo)
    world.reviews = [CHANGES, PASS]
    out = world.run(card)
    assert out.status == "installed"
    assert "回归测试没有断言灰色说明的文字" in world.fix_prompts()[1]
    body = cards.get(card.id).body
    assert f"{autofix.REVIEWER}：需要修改" in body and f"{autofix.REVIEWER}：通过" in body


def test_review_asking_for_changes_twice_hands_off(world, repo):
    card = bug_card(repo)
    world.reviews = [CHANGES, CHANGES]
    assert_handed_off(world.run(card), card, "评审要求修改")
    assert not world.integrated


@pytest.mark.parametrize("reply", ["看起来不错", f"好的。\n```json\n{PASS}\n```", f"结论：{PASS}"])
def test_a_review_that_is_not_exactly_one_json_object_hands_off(world, repo, reply):
    card = bug_card(repo)
    world.reviews = [reply]
    assert_handed_off(world.run(card), card, "没有给出结论")
    assert not world.integrated


def test_a_failed_install_after_merge_says_so(world, repo):
    card = bug_card(repo)

    def broken(version, holder):
        raise flow.FlowError("打包失败：codesign")
    out = world.run(card, over={"release": broken})
    assert_handed_off(out, card, "v1.2.1", "codesign")
    assert world.integrated and world.messages[-1].startswith("代码已经合并，但新版没装上")
    assert world.lock_owner is None


# ---------------------------------------------------------------- single flight, daily cap, switches

def test_another_run_holding_the_process_lock_hands_off(world, repo):
    card = bug_card(repo)
    world.lock_file.parent.mkdir(parents=True)
    other = open(world.lock_file, "a+")
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)                  # a second open file: a different holder
    try:
        out = world.run(card)
    finally:
        other.close()
    assert_handed_off(out, card, "排队太久")
    assert not world.calls and cards.get(card.id).get("worktree") is None


def test_the_integrator_holding_the_lock_only_matters_at_merge_time(world, repo):
    card = bug_card(repo)
    world.lock_owner = "集成会话"
    real = world.claude

    def claude(cmd, prompt, cwd, timeout, env):           # the integrator's lock does not stop fixing or review
        world.lock_owner, saved = None, world.lock_owner
        try:
            return real(cmd, prompt, cwd, timeout, env)
        finally:
            world.lock_owner = saved
    out = world.run(card, over={"claude": claude})
    assert_handed_off(out, card, "集成锁在 集成会话 手里")
    assert [c[0] for c in world.calls] == ["fix", "review"] and world.lock_owner == "集成会话"   # not taken from them
    assert not world.integrated


def test_a_run_queues_at_merge_time_until_the_lock_frees(world, repo):
    world.lock_owner = None
    calls = []

    def acquire(holder, purpose):
        calls.append(purpose)
        if len(calls) == 1:
            raise flow.FlowError("集成锁在 集成会话 手里")
        world.acquire(holder, purpose)
    out = world.run(bug_card(repo), queue_wait=3600, over={"acquire_lock": acquire})
    assert out.status == "installed" and len(calls) == 2 and any("等它完成再合并" in m for m in world.messages)


def test_uncommitted_code_in_main_waits_then_hands_off(world, repo):
    card = bug_card(repo)
    world.dirty = ["app/research_lab/web/routes/feed.py"]
    out = world.run(card, dirty_wait=600)
    assert_handed_off(out, card, "没提交的代码", "feed.py")
    assert world.now >= 600 and not world.calls and any("最多等 10 分钟" in m for m in world.messages)


def test_main_getting_clean_while_waiting_lets_the_run_go_on(world, repo):
    world.dirty = ["app/x.py"]

    def sleep(seconds):
        world.now += seconds
        world.dirty = []
    assert world.run(bug_card(repo), dirty_wait=600, over={"sleep": sleep}).status == "installed"


def test_daily_cap(world, repo):
    reports = repo / "dev/reports"
    reports.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    for i in range(autofix.DAILY_RUNS):
        (reports / f"{i:04d}-autofix-{today}-10000{i}.md").write_text("x")
    (reports / f"0009-autofix-{today}-100000.log").write_text("spawn output does not count")
    card = bug_card(repo)
    out = world.run(card)
    assert_handed_off(out, card, "上限")
    assert not world.calls


@pytest.mark.parametrize("ending", ["installed", "handed_off", "crashed"])
def test_the_run_tmp_and_its_database_copy_go_whatever_happens(world, repo, ending):
    import tempfile
    seen = []

    def check(card_id, jail):
        copy = Path(tempfile.gettempdir()) / "rl-check-x/lab.sqlite"     # where flow._smoke copies Leo's database
        copy.parent.mkdir(parents=True, exist_ok=True)
        for name in ("lab.sqlite", "lab.sqlite-wal", "lab.sqlite-shm"):
            (copy.parent / name).write_text("db")
        seen.append(copy)
        if ending == "crashed":
            raise RuntimeError("page server died")
        return world.check(card_id, jail) if ending == "installed" else dict(FAILED_CHECK)
    out = world.run(bug_card(repo), over={"check": check})
    assert out.status == ("installed" if ending == "installed" else "handed_off")
    assert seen and all(not c.exists() and not c.parents[1].exists() for c in seen)
    assert seen[0].parents[1].name.startswith(autofix.TMP_PREFIX) and tempfile.tempdir != str(seen[0].parents[1])


def test_stale_run_folders_are_swept_databases_first(tmp_path):
    dead = subprocess.Popen(["true"])
    dead.wait()
    gone, live, anon = (tmp_path / f"{autofix.TMP_PREFIX}{n}" for n in ("gone", "live", "anon"))
    for folder in (gone, live, anon):
        (folder / "data").mkdir(parents=True)
        (folder / "data/lab.sqlite").write_text("db")
    (gone / autofix.OWNER_FILE).write_text(str(dead.pid))
    (live / autofix.OWNER_FILE).write_text(str(os.getpid()))
    assert autofix.sweep_stale_tmp(tmp_path) == [gone]
    assert not gone.exists() and (live / "data/lab.sqlite").exists() and (anon / "data/lab.sqlite").exists()


def test_purge_databases_keeps_other_files_and_never_follows_links(tmp_path):
    outside = tmp_path / "outside.sqlite"
    outside.write_text("not ours")
    run = tmp_path / "run"
    (run / "data").mkdir(parents=True)
    for name in ("a.sqlite", "a.sqlite-wal", "a.sqlite-shm", "b.db", "notes.md"):
        (run / "data" / name).write_text("x")
    (run / "link.sqlite").symlink_to(outside)
    autofix.purge_databases(run)
    assert sorted(p.name for p in run.rglob("*")) == ["data", "link.sqlite", "notes.md"] and outside.exists()


def test_rl_autofix_0_refuses_to_run(world, repo, monkeypatch):
    from typer.testing import CliRunner
    from research_lab.cli import app as rl
    card = bug_card(repo)
    monkeypatch.setenv("RL_AUTOFIX", "0")
    with pytest.raises(autofix.Disabled):
        world.run(card)
    with pytest.raises(autofix.Disabled):
        autofix.spawn(card.id, "T-2026-0001")
    res = CliRunner().invoke(rl, ["dev", "autofix", card.id])
    assert res.exit_code == 1 and "RL_AUTOFIX" in res.output
    assert not world.calls and not world.messages and cards.get(card.id).status == "planned"


def test_switched_off_in_settings_does_not_run(world, repo):
    card = bug_card(repo)
    out = world.run(card, over={"enabled": lambda: False})
    c = cards.get(card.id)
    assert out.status == "refused" and c.status == "planned" and "没有运行" in c.body
    assert not world.calls and "关掉" in world.messages[-1]


def test_the_setting_reads_false_as_off(conn):
    from research_lab.db.connection import set_setting
    assert autofix.setting_enabled() is True
    set_setting(conn, "dev.autofix", False)
    assert autofix.setting_enabled() is False


def test_a_card_someone_else_owns_is_left_alone(world, repo):
    card = bug_card(repo)
    cards.update(card, status="coding", owner="别的会话")
    out = world.run(card)
    c = cards.get(card.id)
    assert out.status == "refused" and c.status == "coding" and c.get("owner") == "别的会话" and not world.calls


# ---------------------------------------------------------------- the fences

WT, MAIN, TMP = Path("/Users/x/Projects/research-lab.c0099-card"), Path("/Users/x/Projects/research-lab"), Path("/private/tmp/rl-autofix-1")


def _list_after(cmd: list[str], flag: str) -> list[str]:
    i = cmd.index(flag) + 1
    out = []
    while i < len(cmd) and not cmd[i].startswith("--"):
        out.append(cmd[i])
        i += 1
    return out


def test_fix_command_fences_the_model():
    cmd = autofix.fix_command("/bin/claude", "opus", WT, MAIN, TMP)
    assert "--restricted" in cmd and "--strict-mcp-config" in cmd and "--setting-sources" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk" and cmd[cmd.index("--tools") + 1] == "Read,Edit,Write,Grep,Glob,Bash"
    allowed = _list_after(cmd, "--allowedTools")
    assert cmd[-len(allowed):] == allowed                                # the variadic list ends the command
    assert [a for a in allowed if a.startswith("Bash")] == [
        "Bash(uv run --project app pytest:*)", "Bash(git diff)", "Bash(git diff main...HEAD)", "Bash(git status)"]
    assert not any("git diff:*" in a for a in cmd)
    assert {f"Read(/{WT}/**)", f"Edit(/{WT}/**)", f"Write(/{WT}/**)"} <= set(allowed)
    assert all(a.startswith("Bash(") or a in ("Grep", "Glob") or f"/{WT}/" in a for a in allowed)   # no bare Read/Edit/Write
    denied = _list_after(cmd, "--disallowedTools")
    assert {"WebFetch", "WebSearch", f"Edit(/{MAIN}/**)", f"Write(/{MAIN}/**)", f"Read(/{MAIN}/app/.env)",
            f"Edit(/{WT}/.claude/**)", f"Edit(/{WT}/**/CLAUDE.md)", f"Edit(/{WT}/AGENTS.md)"} <= set(denied)
    sandbox = json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]
    assert sandbox["enabled"] and sandbox["failIfUnavailable"] and sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["network"]["allowedDomains"] == [] and sandbox["network"]["strictAllowlist"] is True
    fs = sandbox["filesystem"]
    assert set(fs["allowWrite"]) == {str(WT), str(TMP), *(str(p) for p in autofix.cocoa_save_dirs())}
    assert str(MAIN) in fs["denyWrite"]
    assert str(WT / ".claude") in fs["denyWrite"] and str(MAIN / "app/.env") in fs["denyRead"]
    assert {str(r) for r in autofix.private_roots()} <= set(fs["denyRead"])  # $HOME, /private/tmp, /var/folders/<me>
    assert fs["allowRead"] == ([str(p) for p, _ in autofix.home_readable(WT, MAIN)] + [str(TMP)]
                               + [str(p) for p in autofix.cocoa_save_dirs()])
    assert str(MAIN) not in fs["allowRead"]                                # allowRead beats denyRead in Claude Code


def test_review_command_is_read_only():
    cmd = autofix.review_command("/bin/claude", "opus", WT, MAIN, TMP)
    assert "--restricted" in cmd and cmd[cmd.index("--tools") + 1].split(",") == ["Read", "Grep", "Glob", "Bash"]
    allowed = _list_after(cmd, "--allowedTools")
    assert allowed == [f"Read(/{WT}/**)", "Grep", "Glob", "Bash(git diff)", "Bash(git diff main...HEAD)"]
    assert {"Edit", "Write", "WebFetch", "WebSearch"} <= set(_list_after(cmd, "--disallowedTools"))
    fs = json.loads(cmd[cmd.index("--settings") + 1])["sandbox"]["filesystem"]
    assert fs["allowWrite"] == [str(TMP)] and str(WT) in fs["denyWrite"]
    assert fs["allowRead"] == [str(WT), str(MAIN / ".git"), str(TMP)] and "/private/tmp" in fs["denyRead"]


def test_parse_verdict_takes_only_a_whole_json_reply():
    assert autofix.parse_verdict(PASS) == ("pass", "清单 1–7 都没问题；这个 bug 修好了")
    assert autofix.parse_verdict(f"  {CHANGES}\n") == ("changes", "回归测试没有断言灰色说明的文字")
    for reply in (f"```json\n{PASS}\n```", f"结论：{PASS}", '{"verdict": "maybe"}', "看起来不错", "[]"):
        assert autofix.parse_verdict(reply) is None


def test_the_child_environment_is_a_whitelist(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", ENV_SECRET)
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-0123456789")
    monkeypatch.setenv("FEISHU_APP_SECRET", "feishu-0123456789")
    monkeypatch.setenv("LC_ALL", ENV_SECRET)                     # a whitelisted name holding a secret value
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/x/.claude")
    env = autofix.clean_env(tmp_path, secrets={ENV_SECRET})
    assert not {"DEEPSEEK_API_KEY", "BRAVE_SEARCH_API_KEY", "FEISHU_APP_SECRET", "LC_ALL", "CLAUDE_CONFIG_DIR"} & set(env)
    assert ENV_SECRET not in env.values()
    assert env["RL_AUTOFIX"] == "0" and env["RL_CLAUDE_BIN"] == "none" and env["TMPDIR"] == str(tmp_path)
    assert env["UV_CACHE_DIR"] == str(tmp_path / "uv-cache") and env["UV_OFFLINE"] == "1" and "PATH" in env and "HOME" in env
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["UV_NO_CONFIG"] == "1"     # $HOME is unreadable in the jail
    claude_env = autofix.clean_env(tmp_path, claude=True)
    assert claude_env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude"                             # the CLI's login only
    assert claude_env["CLAUDE_TMPDIR"] == str(tmp_path / "claude") and "CLAUDE_TMPDIR" not in env


def test_run_claude_reads_the_result_and_stops_on_timeout(tmp_path):
    env = autofix.clean_env(tmp_path)
    script = "import json, os, sys; sys.stdin.read(); print(json.dumps({'result': os.environ['RL_AUTOFIX'], 'is_error': False}))"
    done = autofix.run_claude([sys.executable, "-c", script], "提示", tmp_path, 30, env)
    assert done.ok and done.text == "0"
    err = autofix.run_claude([sys.executable, "-c", "import json; print(json.dumps({'result': 'x', 'is_error': True}))"],
                             "", tmp_path, 30, env)
    assert not err.ok
    slow = autofix.run_claude([sys.executable, "-c", "import time; time.sleep(30)"], "", tmp_path, 1, env)
    assert not slow.ok and "中止" in slow.text and slow.seconds < 10


def _sandbox_usable() -> bool:
    if sys.platform != "darwin" or not Path(autofix.SANDBOX_EXEC).exists():
        return False
    # nested sandboxes are refused: inside the jailed check or Claude Code's sandbox this test cannot run
    return subprocess.run([autofix.SANDBOX_EXEC, "-p", "(version 1)(allow default)", "/usr/bin/true"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _sandbox_usable(), reason="needs macOS sandbox-exec, not already sandboxed")
def test_the_jail_really_confines_writes_reads_and_network(tmp_path):
    wt, main, run_tmp = tmp_path / "wt", tmp_path / "main", tmp_path / "run"
    for d in (wt / "app", main / "app/data/papers", main / "_data", main / ".git", main / "dev/cards",
              main / "_site/app/data", run_tmp):
        d.mkdir(parents=True)
    (main / "_site/app/data/lab.sqlite").write_text("db")              # the Jekyll build copies app/data
    (main / "_data/stray.sqlite").write_text("db")                     # a database inside an allowed folder
    (main / "dev/cards/0001-x.md").write_text("card")
    (run_tmp / "own.sqlite").write_text("db")                          # the run's own copy stays usable
    shared = Path("/private/tmp") / f"rl-jail-probe-{os.getpid()}.sqlite"   # another session's scratch copy
    shared.write_text("db")
    stray = tmp_path / "preview.sqlite"                                # this user's /var/folders, outside the run
    (main / "app/.env").write_text("KEY=x\n")
    (main / "_data/.env.local").write_text("KEY=x\n")                   # a stray .env anywhere in the checkout
    (main / "_data/now.yml").write_text("week: 1\n")
    (main / "app/data/lab.sqlite").write_text("db")
    (main / "app/data/papers/a.pdf").write_text("pdf")
    (main / ".git/HEAD").write_text("ref: refs/heads/main\n")
    outside = Path.home() / f".rl-jail-probe-{os.getpid()}"            # outside every writable place
    secret = Path.home() / f".rl-jail-secret-{os.getpid()}"            # a fake key under $HOME
    secret.write_text("FAKE_KEY=not-a-real-key\n")
    probe = f"""
import json, os, socket
def t(fn):
    try:
        fn(); return "ok"
    except Exception as e:
        return type(e).__name__
def w(p): open(p, "w").write("x")
def r(p): open(p).read()
def local():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
    socket.create_connection(s.getsockname(), timeout=3).close()
print(json.dumps({{
  "wt": t(lambda: w({str(wt / 'app/ok.py')!r})), "tmp": t(lambda: w({str(run_tmp / 'x')!r})),
  "outside": t(lambda: w({str(outside)!r})), "claude_md": t(lambda: w({str(wt / 'app/CLAUDE.md')!r})),
  "dot_claude": t(lambda: os.makedirs({str(wt / '.claude')!r})), "env": t(lambda: r({str(main / 'app/.env')!r})),
  "stray_env": t(lambda: r({str(main / '_data/.env.local')!r})), "db": t(lambda: r({str(main / 'app/data/lab.sqlite')!r})),
  "home_secret": t(lambda: r({str(secret)!r})), "home_list": t(lambda: os.listdir({str(Path.home())!r})),
  "content": t(lambda: r({str(main / '_data/now.yml')!r})), "pdf": t(lambda: r({str(main / 'app/data/papers/a.pdf')!r})),
  "git": t(lambda: r({str(main / '.git/HEAD')!r})), "cards": t(lambda: r({str(main / 'dev/cards/0001-x.md')!r})),
  "site_db": t(lambda: r({str(main / '_site/app/data/lab.sqlite')!r})), "content_db": t(lambda: r({str(main / '_data/stray.sqlite')!r})),
  "own_db": t(lambda: r({str(run_tmp / 'own.sqlite')!r})), "shared_tmp_db": t(lambda: r({str(shared)!r})),
  "user_tmp_db": t(lambda: r({str(stray)!r})), "list_checkout": t(lambda: os.listdir({str(main)!r})),
  "net": t(lambda: socket.create_connection(("1.1.1.1", 443), timeout=5)), "localhost": t(local),
}}))
"""
    # the base interpreter (under ~/.local/share/uv/python, which the jail lets through), isolated from the
    # test runner's own .venv, which sits in a worktree this jail does not know about
    python = os.path.realpath(sys.executable)
    stray.write_text("db")
    try:
        cmd, env = autofix.make_jail(wt.resolve(), run_tmp.resolve(), main.resolve())([python, "-I", "-c", probe], {})
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        got = json.loads(r.stdout)
    finally:
        outside.unlink(missing_ok=True)
        secret.unlink(missing_ok=True)
        shared.unlink(missing_ok=True)
    denied = "PermissionError"
    in_user_folders = any(r in tmp_path.resolve().parents for r in autofix.private_roots()[2:])
    assert got == {"wt": "ok", "tmp": "ok", "outside": denied, "claude_md": denied, "dot_claude": denied, "env": denied,
                   "stray_env": denied, "db": denied, "home_secret": denied, "home_list": denied, "content": "ok",
                   "pdf": "ok", "git": "ok", "net": denied, "localhost": "ok", "cards": "ok", "site_db": denied,
                   "content_db": denied, "own_db": "ok", "shared_tmp_db": denied,
                   "user_tmp_db": denied if in_user_folders else got["user_tmp_db"], "list_checkout": denied}


def test_reads_are_a_whitelist_and_databases_stay_shut():
    profile = autofix.seatbelt_profile(WT, TMP, MAIN)
    lines = profile.splitlines()
    roots = autofix.private_roots()
    assert str(Path.home().resolve()) == str(roots[0]) and Path("/private/tmp") in roots
    deny_roots = next(i for i, l in enumerate(lines) if l.startswith("(deny file-read*") and "/private/tmp" in l)
    allow = next(i for i, l in enumerate(lines) if l.startswith(f'(allow file-read* (subpath "{WT}")'))
    assert deny_roots < allow                                            # later rules win in SBPL
    assert all(f'(subpath "{MAIN / rel}")' in lines[allow] for rel, _ in autofix.MAIN_CONTENT)
    assert f'(subpath "{MAIN}")' not in profile and "_site" not in profile   # never the checkout as a whole
    scoped = "|".join(autofix._sb_regex(r) for r in roots)                # system tables (iconv's *.db) stay readable
    assert lines[-2] == f'(deny file-read* (regex #"^({scoped})/.*\\.(sqlite|sqlite\\-wal|sqlite\\-shm|db)$"))'
    assert lines[-1] == f'(allow file-read* (subpath "{TMP}"))'           # the run's own databases only
    readable = autofix.home_readable(WT, MAIN)
    assert [p for p, _ in readable][:2] == [WT, MAIN / ".git"] and all(why for _, why in readable)
    assert all(why for _, why in autofix.MAIN_CONTENT)
    assert autofix.home_ancestors([Path.home() / "Projects/research-lab/x"]) == [Path.home().resolve(),
                                                                                 Path.home().resolve() / "Projects",
                                                                                 Path.home().resolve() / "Projects/research-lab"]
    assert all(p.name == "TemporaryItems" for p in autofix.cocoa_save_dirs())
    path = autofix.sandbox_path("/usr/bin:/bin").split(os.pathsep)
    dev = autofix.developer_bin()
    assert path[-2:] == ["/usr/bin", "/bin"] and (dev is None or path[0] == str(dev))   # real git before the xcrun shim


# ---------------------------------------------------------------- real git, in a throwaway repository

def _git_repo(tmp_path: Path) -> tuple[Path, callable]:
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                  check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("x = 1\n")
    g("add", "."); g("commit", "-qm", "base")
    g("checkout", "-qb", "agent/x")
    return tmp_path, g


def test_non_ascii_binary_and_symlink_paths_are_seen_as_they_are(tmp_path):
    wt, _ = _git_repo(tmp_path / "wt")
    (wt / "_data").mkdir()
    (wt / "_data/新.yml").write_text("a: 1\n")
    (wt / "notes").mkdir()
    (wt / "notes/会议.md").write_text("x\n")
    (wt / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    (wt / "out").symlink_to(tmp_path)                                  # leaves the worktree
    (wt / "in").symlink_to(wt / "_data/新.yml")                        # into a forbidden folder
    autofix.stage_all(wt)
    changes = dict((p, (a, d)) for p, a, d in autofix.staged_changes(wt))
    assert {"_data/新.yml", "notes/会议.md"} <= set(changes) and changes["logo.png"] == (-1, -1)
    assert set(autofix.forbidden(changes)) >= {"_data/新.yml", "notes/会议.md"}
    assert autofix.symlinks(wt, list(changes)) == {"out": None, "in": "_data/新.yml"}
    assert "+++ b/_data/新.yml" in autofix.branch_diff(wt)
    assert [p for p, _, _ in flow.changes_of(wt) if not p.isascii()] == ["_data/新.yml", "notes/会议.md"]


def test_commit_all_skips_repository_hooks(tmp_path):
    wt, _ = _git_repo(tmp_path / "wt")
    hook = wt / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'hook-ran'}\nexit 1\n")
    hook.chmod(0o755)
    (wt / "app.py").write_text("x = 2\n")
    autofix.stage_all(wt)
    assert autofix.commit_all(wt, "fix") and not (tmp_path / "hook-ran").exists()
    assert autofix.commit_all(wt, "again") == "nothing new to commit"
    assert autofix.worktree_dirty(wt) == []


# ---------------------------------------------------------------- flow: the jail hook, locks

def test_flow_runs_commands_through_a_jail(tmp_path):
    seen = {}

    def jail(cmd, extra):
        seen["extra"] = extra
        return ["/bin/echo", "jailed", *cmd], {"PATH": "/bin:/usr/bin"}
    r = flow._run(["echo", "hi"], tmp_path, env={"RL_AUTOFIX": "0"}, jail=jail)
    assert r.stdout.strip() == "jailed echo hi" and seen["extra"] == {"RL_AUTOFIX": "0"}


def test_rl_dev_check_runs_tests_and_pages_with_autofix_off_and_through_the_jail(repo, monkeypatch):
    card = cards.new("X", files=["app/research_lab/web/routes/feed.py"])
    wt = repo.parent / "research-lab.c0001-x"
    wt.mkdir()
    cards.update(card, status="coding", owner="s", worktree=str(wt))
    seen = {}

    def fake_run(cmd, cwd, env=None, timeout=900, jail=None):
        seen["tests"], seen["tests_jail"] = env or {}, jail
        return subprocess.CompletedProcess(cmd, 0, "1 passed in 0.1s\n", "")

    def fake_pages(wt, port, env, jail=None):
        seen["pages"], seen["pages_jail"] = env, jail
        return [("/", 200)]
    monkeypatch.setattr(flow, "_run", fake_run)
    monkeypatch.setattr(flow, "_smoke_on", fake_pages)
    monkeypatch.setattr(flow, "changes_of", lambda wt: [("app/research_lab/web/routes/feed.py", 1, 1)])
    assert flow.check(card.id)["ok"]
    assert seen["tests"].get("RL_AUTOFIX") == "0" and seen["pages"].get("RL_AUTOFIX") == "0"
    assert seen["tests_jail"] is None and seen["pages_jail"] is None
    jail = lambda cmd, extra: (cmd, extra)
    flow.check(card.id, jail=jail)
    assert seen["tests_jail"] is jail and seen["pages_jail"] is jail


@pytest.fixture()
def lock_file(tmp_path, monkeypatch):
    from research_lab.devflow import status
    path = tmp_path / "rl-integration.lock"
    monkeypatch.setattr(status, "lock_path", lambda: path)
    monkeypatch.setattr(flow, "lock_path", lambda: path)
    return path


def test_a_lock_left_by_a_dead_process_counts_as_free(lock_file):
    dead = subprocess.Popen(["true"])
    dead.wait()
    lock_file.write_text(json.dumps({"holder": "集成会话", "purpose": "integrate 0042", "pid": dead.pid}))
    flow.acquire_lock(autofix.HOLDER, "autofix 0001")
    assert json.loads(lock_file.read_text())["holder"] == autofix.HOLDER
    lock_file.write_text(json.dumps({"holder": "集成会话", "purpose": "integrate 0042", "pid": os.getpid()}))
    with pytest.raises(flow.FlowError, match="集成会话"):
        flow.acquire_lock(autofix.HOLDER, "autofix 0001")
    lock_file.write_text(json.dumps({"holder": "集成会话", "purpose": "old lock without a pid"}))
    with pytest.raises(flow.FlowError):
        flow.acquire_lock(autofix.HOLDER, "autofix 0001")


def test_integrate_and_release_can_run_under_the_callers_lock(repo, lock_file, monkeypatch):
    # the repo fixture is not a git checkout; release now refuses an unreadable main checkout first (card 0167)
    monkeypatch.setattr(flow, "dirty", lambda path, timeout=None: {"code": [], "content": []})
    with pytest.raises(flow.FlowError, match="集成锁不在"):
        flow._hold(autofix.HOLDER, "integrate 0001", lock_held=True)
    with pytest.raises(flow.FlowError, match="集成锁不在"):
        flow.release("9.9.9", autofix.HOLDER, lock_held=True)              # refused before touching anything
    flow.acquire_lock(autofix.HOLDER, "autofix 0001")
    flow._hold(autofix.HOLDER, "integrate 0001", lock_held=True)
    assert json.loads(lock_file.read_text())["holder"] == autofix.HOLDER   # still held: the caller lets go


# ---------------------------------------------------------------- starting it from the app

def test_spawn_starts_a_detached_process_from_the_main_checkout(repo, monkeypatch):
    card = cards.new("推送页的 bug")
    seen = {}

    class Proc:
        pid = 4242

    def fake_popen(cmd, **kw):
        seen.update(kw, cmd=cmd)
        return Proc()
    monkeypatch.setattr(autofix.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("PYTHONHOME", "/Applications/Research Lab.app/Contents/Resources")
    monkeypatch.setenv("RL_DESKTOP", "1")
    pid, log = autofix.spawn(card.id, "T-2026-0001")
    assert pid == 4242 and seen["start_new_session"] is True and seen["stdin"] == subprocess.DEVNULL
    assert Path(seen["cwd"]).resolve() == repo.resolve()
    cmd = seen["cmd"]
    assert cmd[cmd.index("--project") + 1] == str(Path(seen["cwd"]) / "app")
    assert cmd[-6:] == ["rl", "dev", "autofix", card.id, "--thread", "T-2026-0001"]
    assert not {"PYTHONHOME", "RL_DESKTOP", "RL_REPO_ROOT"} & set(seen["env"])
    assert log.parent.resolve() == (repo / "dev/reports").resolve() and log.name.endswith(".log")
