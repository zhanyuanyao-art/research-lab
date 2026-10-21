"""Card 0210: what the models read names the workspace's own user and directions, never Leo or a discipline;
Leo's own lab (no profile.yml) still reads as Leo with his directions. No model or Claude Code is called."""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research_lab.agent import skills, tasks
from research_lab.ai.provider import system_rules
from research_lab.files import issues as I

PKG = Path(I.__file__).resolve().parents[1]
ROOT = PKG.parents[1]
FOREIGN = ("Leo", "East Asia", "International Relations", "东亚国际关系", "PhD")
# Text of the maintainer's own dev cards, which only Leo's lab files (report_bug refuses elsewhere).
DEV_CARD_TEXT = ("Leo 报的 bug", "Leo 提的需求", "Leo 在对话里说：", "Leo 说的问题不再出现", "集成会话和 Leo 定下")
PROMPT_MODULES = ["agent/tools.py", "agent/tasks.py", "agent/drivers.py", "agent/skills.py", "agent/replication_tools.py",
                  "agent/feed_tools.py",
                  "ai/provider.py",
                  "feeds/enrich.py", "replication/rrun.py", "replication/plans.py",
                  *sorted(f"ai/jobs/{p.name}" for p in (PKG / "ai/jobs").glob("*.py")),
                  *sorted(f"research/{p.name}" for p in (PKG / "research").glob("*.py"))]


def _workspace(repo: Path, owner: str = "我", directions: str = "[]\n", **profile: str) -> None:
    lines = [f"owner_name: {owner}", *(f"{k}: {v}" for k, v in profile.items())]
    (repo / "_data/profile.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (repo / "_data/directions.yml").write_text(directions, encoding="utf-8")


def _foreign(text: str) -> list[str]:
    return [w for w in FOREIGN if w in text]


def test_call_name_comes_from_the_profile(repo):
    assert I.call_name() == "Leo" and I.owner_name() == "Zhanyuan Yao"       # legacy lab: no profile.yml
    _workspace(repo)
    assert I.call_name() == "" and I.owner_name() == "我"
    _workspace(repo, "Alex Chen")
    assert I.call_name() == "Alex Chen"
    _workspace(repo, "Alex Chen", call_name="Alex")
    assert I.call_name() == "Alex" and I.owner_name() == "Alex Chen"
    _workspace(repo, "Alex Chen", call_name='"<b>"')
    assert I.call_name() == "Alex Chen"
    (repo / "_data/profile.yml").unlink()
    (repo / "_data/profile.yml").mkdir()
    assert I.call_name() == ""


def test_research_field_lists_the_workspace_directions(repo):
    assert "联盟政治与美国研究" in I.research_field()                           # the fixture's directions
    _workspace(repo, directions="- {slug: health, key: PH, name: 公共卫生, name_en: Public Health}\n"
                                "- {slug: urban, key: UR, name: Urban Studies}\n- not a direction\n")
    assert I.research_field() == "公共卫生 (Public Health); Urban Studies"
    _workspace(repo)
    assert I.research_field() == ""
    assert I.user_facts() == "The user: unnamed. Research directions: not set yet."


@pytest.mark.parametrize("owner, who", [("我", "unnamed"), ("Alex Chen", "Alex Chen")])
def test_a_fresh_workspace_names_no_one_else(repo, conn, owner, who):
    _workspace(repo, owner)
    facts = f"The user: {who}. Research directions: not set yet."
    assert {"task", "issues", "methods", "replication-control"} <= {s.name for s in skills.load_all().values() if s.runs_tasks}
    for sk in skills.load_all().values():
        if not sk.runs_tasks:
            continue
        prompt = tasks.system_prompt(sk)
        assert facts in prompt and prompt.index(facts) < prompt.index(sk.instructions), sk.name
        assert not _foreign(prompt), (sk.name, _foreign(prompt))
    assert system_rules().endswith(facts) and not _foreign(system_rules())

    from research_lab.ai.jobs import chat_reply, excerpt_associate, news_brief, paper_translate
    from research_lab.replication import rrun, workspace
    ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO feed_news(id, source, title, link, summary, published, first_seen, lane) "
                 "VALUES ('w1', 'BBC World', 'Ceasefire talks resume', 'https://example.org/w1', 'Summary.', ?, ?, 'world')",
                 (ts, ts))
    req, _ = news_brief.SPEC.build("2026-10-02", conn)
    brief = "\n".join(m["content"] for m in req.messages)
    assert facts in brief and not _foreign(brief)
    for text in (paper_translate.SYSTEM, chat_reply.CHAT_RULE, excerpt_associate.TASK, rrun.REPAIR_PROMPT,
                 workspace.skill_prompt(), workspace.skill_prompt(translate=True)):
        assert not _foreign(text), _foreign(text)


def test_leos_own_lab_still_reads_as_leo(repo):
    prompt = tasks.system_prompt(skills.get("task"))
    assert "The user: Leo. Research directions: 联盟政治与美国研究" in prompt
    assert "The user: Leo." in system_rules() and "周边安全与非传统安全" in system_rules()


def _strings(path: Path) -> list[str]:
    """String literals of a module, docstrings left out (they record design history, not prompts)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


@pytest.mark.parametrize("rel", PROMPT_MODULES)
def test_no_prompt_text_names_leo_or_a_field(rel):
    bad = [s for s in _strings(PKG / rel) if _foreign(s) and not any(d in s for d in DEV_CARD_TEXT)]
    assert not bad, bad


def test_skills_and_the_app_guide_name_no_one():
    for f in sorted((PKG / "agent/skills").glob("*.md")):
        assert not _foreign(f.read_text(encoding="utf-8")), f.name
    guide = (ROOT / "docs/APP-GUIDE.md").read_text(encoding="utf-8")
    assert not _foreign(guide) and "Zhanyuan" not in guide
    assert "建方向时选定" in guide
