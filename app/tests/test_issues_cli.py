"""`rl issues` (card 0176): Markdown an agent reads whole, and writes that go through the assistant's checked paths."""
from __future__ import annotations

from typer.testing import CliRunner

from research_lab.config import get_paths
from research_lab.files import issues as I

SLUG = "us-rok-reassurance"
cli = CliRunner()


def _rl(*args):
    from research_lab.cli import app
    return cli.invoke(app, ["issues", *args])


def test_new_show_comment_attach_and_start(repo):
    r = _rl("new", "英国模式", "--project", SLUG, "--note", "逐次行动编码", "--due", "2026-10-21", "--labels", "结论")
    assert r.exit_code == 0, r.output
    key = r.output.split()[0]
    assert key.startswith(("APUS-", "T-"))        # the test repo has no key file yet, so the ref stands in
    out = _rl("show", key).output
    assert "英国模式" in out and "逐次行动编码" in out and "2026-10-21" in out and "结论" in out
    draft = get_paths().projects / SLUG / "drafts" / "07-uk-pattern.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# uk\n", encoding="utf-8")
    assert _rl("comment", key, "--text", "写完了", "--attach", "drafts/07-uk-pattern.md").exit_code == 0
    assert _rl("attach", key, "drafts/07-uk-pattern.md").exit_code == 0          # idempotent
    out = _rl("show", key).output
    assert "## 评论" in out and "写完了" in out and "附件：drafts/07-uk-pattern.md" in out
    assert out.count("drafts/07-uk-pattern.md") == 2                            # once under 附件, once on the comment
    ref = I.resolve(key)
    assert [a["rel"] for a in I.attachments(ref)] == ["drafts/07-uk-pattern.md"]
    assert _rl("comment", key, "--attach", "drafts/missing.md").exit_code == 1
    assert _rl("start", key).exit_code == 0 and I.get(ref)["status"] == "doing"
    assert _rl("set", key, "status", "done").exit_code == 1 and I.get(ref)["status"] == "doing"
    assert "进行中" in _rl("list", "--project", SLUG).output
    assert key in _rl("show", SLUG, "--project").output


def test_unknown_key_fails_cleanly(repo):
    r = _rl("show", "APUS-9999")
    assert r.exit_code == 1 and "找不到" in r.output
