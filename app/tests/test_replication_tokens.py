"""Card 0185 (docs/design/REPLICATION-v3.md §2D): a replication spends fewer tokens and says how many it spent.
A reply cut off at the output limit is continued, not asked for again; every plan has a ledger of what it used;
reading the paper into a plan can run on the cheaper tier; and the page material chosen for a file is kept.
Card 0197: DeepSeek continues by chat prefix completion; the anchor prompt stays for the other providers (Plain)."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from research_lab import tunables
from research_lab.ai import cost, queue, registry, runner
from research_lab.ai.provider import ChatResult
from research_lab.config import get_paths
from research_lab.db.connection import now_iso
from research_lab.replication import plans

from .test_ai import EchoSpec, Script, provider
from .test_data import VALID_PLAN, data_client, ingest_quiet  # noqa: F401 - data_client is a fixture
from .test_replication_targets import did_pdf, suggest, table_page

FULL = json.dumps({"echo": "alpha beta gamma", "ids": ["EXC-2026-0001", "EXC-2026-0002"]})
CUT = FULL.index("EXC-2026-0001") + 5            # inside the first id
HEAD, TAIL = FULL[:CUT], FULL[CUT:]
ANCHOR = runner.anchor_of(HEAD)
GOOD = ANCHOR + TAIL                              # what a continuation looks like: the anchor first, then the rest


def deepseek(content: str, finish: str = "stop", prompt: int = 1000, completion: int = 100) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "prompt_cache_hit_tokens": 0, "completion_tokens": completion,
                  "completion_tokens_details": {"reasoning_tokens": 0}}})


class Fake:
    """A provider that answers from a list: the engine's name and the raw result decide how a stop is reported."""

    def __init__(self, name: str, *replies: tuple[str, dict]):
        self.name, self.replies, self.requests = name, list(replies), []

    def complete(self, req):
        self.requests.append(req)
        content, raw = self.replies.pop(0)
        return ChatResult(content=content, model="m", usage={"prompt_tokens": 10, "completion_tokens": 5}, raw=raw)


class Plain:
    """The DeepSeek provider without chat prefix completion: how any other provider continues a cut-off reply, by the
    anchor prompt."""
    name = "openai-compat"

    def __init__(self, script):
        self.inner = provider(script)

    def complete(self, req):
        return self.inner.complete(req)


@pytest.fixture()
def echo(conn, monkeypatch):
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setitem(registry._JOBS, "test.echo", EchoSpec())
    return lambda: queue.enqueue(conn, "test.echo", "EXC-2026-0001", force=True)


def run_of(conn, run_id):
    return conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()


# ---------------------------------------------------------------- continue, don't redo

def test_a_reply_cut_off_at_the_limit_is_continued_not_asked_again(conn, echo):
    jid = echo()
    script = Script(deepseek(HEAD, "length", 1000, 50), deepseek(TAIL, "stop", 1060, 20))
    run_id = runner.run_job(conn, jid, provider(script), cost.cost_usd)
    job = queue.get(conn, jid)
    assert job["status"] == "done" and json.loads(job["result_json"])["ids"] == ["EXC-2026-0001", "EXC-2026-0002"]
    first, body = script.bodies()
    assert len(script.requests) == 2                                       # no repair request after it
    assert script.requests[1].url.path == "/beta/chat/completions"         # DeepSeek's chat prefix completion
    assert body["messages"][:-1] == first["messages"]                      # the prompt again, unchanged: a cache hit
    assert body["messages"][-1] == {"role": "assistant", "content": HEAD, "prefix": True}
    assert "response_format" not in body                                   # JSON mode would force a whole new object
    run = run_of(conn, run_id)
    assert run["valid"] == 1 and run["repaired"] == 0
    assert (run["prompt_tokens"], run["completion_tokens"]) == (2060, 70)    # both calls are on the bill
    assert json.loads(open(run["log_path"]).read())["continued"] is True


@pytest.mark.parametrize("name, raw", [
    ("deepseek", {"choices": [{"finish_reason": "length", "message": {"content": HEAD}}]}),
    ("claude-code", {"type": "result", "stop_reason": "max_tokens"}),
    ("deepseek", {}),                                                       # a driver that says nothing: the text ends mid-object
    ("claude-code", {"type": "result", "stop_reason": "end_turn"}),         # or says it finished: the text still ends mid-object
])
def test_both_engines_continue_and_so_does_a_reply_whose_stop_reason_is_missing(conn, echo, name, raw):
    fake = Fake(name, (HEAD, raw), (GOOD, {}))
    runner.run_job(conn, echo(), fake)
    assert len(fake.requests) == 2 and fake.requests[1].messages[-1]["content"] == runner.continue_prompt(ANCHOR)
    assert fake.requests[1].json_mode is False
    assert json.loads(queue.list_jobs(conn)[0]["result_json"])["echo"] == "alpha beta gamma"


def test_a_continuation_in_a_fence_is_unwrapped(conn, echo):
    script = Script(deepseek(HEAD, "length"), deepseek("```json\n" + GOOD + "\n```"))
    runner.run_job(conn, echo(), Plain(script), cost.cost_usd)
    assert len(script.requests) == 2 and queue.list_jobs(conn)[0]["status"] == "done"


QUICK = "the quick brown fox jumps over the lazy dog"


class NumResult(BaseModel):
    alpha: str
    beta: float


class NumSpec(EchoSpec):
    kind = "test.num"
    Result = NumResult

    def apply(self, result, run_id, conn, target_id):
        pass


@pytest.fixture()
def seam(conn, monkeypatch):
    """Runs one job on a reply cut off at `cut`, then a continuation, then (if it is asked for) a repair answer."""
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setitem(registry._JOBS, "test.echo", EchoSpec())
    monkeypatch.setitem(registry._JOBS, "test.num", NumSpec())

    def go(kind, full, cut_after, continuation, repair):
        head = full[:full.index(cut_after) + len(cut_after)]
        script = Script(deepseek(head, "length"), deepseek(continuation), deepseek(repair))
        jid = queue.enqueue(conn, kind, "EXC-2026-0001", force=True)
        run_id = runner.run_job(conn, jid, Plain(script), cost.cost_usd)
        return json.loads(queue.get(conn, jid)["result_json"]), script, run_of(conn, run_id)
    return go


@pytest.mark.parametrize("cut_after, bad", [
    ("fox jumps", 'fox jumps over the lazy dog", "ids": []}'),      # repeats a few words: a literal join says jumpsfox jumps
    ("the lazy d", 'd dog", "ids": []}'),                              # repeats one character, far under any overlap rule
    ("the quick", 'brown fox jumps over the lazy dog", "ids": []}'),   # drops the space at the seam: quickbrown
])
def test_a_continuation_that_does_not_open_with_the_anchor_is_never_joined_by_guessing(seam, cut_after, bad):
    full = json.dumps({"echo": QUICK, "ids": []})
    result, script, run = seam("test.echo", full, cut_after, bad, full)
    assert result["echo"] == QUICK                                       # the repair answer, not a join of the two halves
    assert len(script.requests) == 3 and run["repaired"] == 1 and run["valid"] == 1


def test_a_number_that_the_continuation_starts_again_is_not_glued_on(seam):
    full = json.dumps({"alpha": "x", "beta": 12.5})
    result, script, run = seam("test.num", full, '"beta": 12', "12.5}", full)       # 12 + 12.5 would be 1212.5
    assert result["beta"] == 12.5 and len(script.requests) == 3 and run["repaired"] == 1


def test_a_clean_continuation_of_a_number_is_joined_at_the_anchor(seam):
    full = json.dumps({"alpha": "x", "beta": 12.5})
    head = full[:full.index("12") + 2]
    result, script, run = seam("test.num", full, '"beta": 12', runner.anchor_of(head) + ".5}", "{}")
    assert result["beta"] == 12.5 and len(script.requests) == 2 and run["repaired"] == 0


def test_a_tail_that_repeats_itself_has_no_anchor_so_there_is_no_continuation(conn, echo):
    head = '{"echo": "' + "ab" * 200                                  # the last 160 characters occur earlier too
    assert runner.anchor_of(head) is None
    script = Script(deepseek(head, "length"), deepseek(json.dumps({"echo": "ok", "ids": []})))
    run_id = runner.run_job(conn, echo(), Plain(script), cost.cost_usd)
    assert len(script.requests) == 2 and run_of(conn, run_id)["repaired"] == 1
    assert "not valid for the schema" in script.bodies()[1]["messages"][-1]["content"]


def test_the_anchor_and_the_splice():
    text = '{"rows": [{"sig": "", "n": "", "page": "1"}, {"sig": "", "n": "", "page": "2"}, {"term": "Carter'
    anchor = runner.anchor_of(text)
    assert text.endswith(anchor) and text.count(anchor) == 1 and len(anchor) >= runner.ANCHOR_MIN
    assert runner.anchor_of('{"a": "   short') == '{"a": "   short'          # a short reply: all of it
    assert runner.anchor_of('{"a": 1,   \n        "b": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx') == '"b": "' + "x" * 41  # no leading space
    assert runner.anchor_of("") is None
    rest = ' Heinrich", "n": "3"}]}'
    assert runner.stitch(text, anchor + rest) == text + rest                     # clean
    assert runner.stitch(text, "```json\n" + anchor + rest + "\n```") == text + rest
    assert runner.stitch(text, "  \n" + anchor + rest) == text + rest            # a trimmed or padded start is fine
    assert runner.stitch(text, rest) is None                                       # no anchor
    assert runner.stitch(text, "Sure, here is the rest: " + anchor + rest) is None   # anchor somewhere else: not guessed
    assert runner.stitch(text, anchor[:-1] + rest) is None                           # one character short at the seam
    assert runner.stitch(text, anchor[:5] + "X" + anchor[6:] + rest) is None


def test_when_the_continuation_does_not_help_the_repair_retry_still_runs(conn, echo):
    valid = json.dumps({"echo": "ok", "ids": []})
    script = Script(deepseek(HEAD, "length"), deepseek("I am sorry, I lost my place."), deepseek(valid))
    run_id = runner.run_job(conn, echo(), provider(script), cost.cost_usd)
    run = run_of(conn, run_id)
    assert len(script.requests) == 3 and run["repaired"] == 1 and run["valid"] == 1
    assert run["prompt_tokens"] == 3000                                     # all three calls summed
    repair = script.bodies()[2]["messages"]
    assert "not valid for the schema" in repair[-1]["content"] and repair[-2]["role"] == "assistant"


def test_a_finished_but_wrong_reply_goes_straight_to_the_repair_retry(conn, echo):
    script = Script(deepseek('{"wrong": 1}', "stop"), deepseek(json.dumps({"echo": "ok"})))
    run_id = runner.run_job(conn, echo(), provider(script), cost.cost_usd)
    assert len(script.requests) == 2 and run_of(conn, run_id)["repaired"] == 1
    assert "not valid for the schema" in script.bodies()[1]["messages"][-1]["content"]
    # an empty reply that hit the limit (thinking used it all) has nothing to continue from either
    script = Script(deepseek("", "length"), deepseek(json.dumps({"echo": "ok"})))
    run_id = runner.run_job(conn, echo(), provider(script), cost.cost_usd)
    assert len(script.requests) == 2 and run_of(conn, run_id)["repaired"] == 1


def test_a_continuation_asks_for_at_most_one_more_reply(conn, echo):
    script = Script(deepseek(HEAD, "length"), deepseek(TAIL[:4], "length"), deepseek("{not json", "length"))
    with pytest.raises(Exception):
        runner.run_job(conn, echo(), provider(script), cost.cost_usd)
    assert len(script.requests) == 3                                        # reply, one continuation, one repair
    assert queue.list_jobs(conn)[0]["status"] == "failed"


def test_how_a_stop_is_read_from_each_driver():
    ds = ChatResult("", "m", raw={"choices": [{"finish_reason": "length"}]})
    cc = ChatResult("", "m", raw={"type": "result", "stop_reason": "max_tokens"})
    assert runner.finish_reason(ds) == "length" and runner.finish_reason(cc) == "max_tokens"
    assert runner.finish_reason(ChatResult("", "m")) == ""
    for text, open_ in [('{"a": 1}', False), ('{"a": [1, 2', True), ('{"a": "x', True), ('{"a": "}"', True),
                        ('{"a": "x\\"}"}', False), ('```json\n{"a": {"b": 1}', True), ("no object here", False),
                        ('{"a": [1]}\n', False)]:
        assert runner._open_ended(text) is open_, text


def test_the_real_case_a_replication_plan_cut_off_in_its_targets(conn, tmp_path):
    """methods.replicate was truncated at about 9,100 characters, inside the targets, and ran again from the start."""
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    page = table_page(conn, paper_id)
    plan = {**VALID_PLAN, "steps": [f"步骤 {i}：" + "整理变量并核对来源，" * 14 for i in range(60)],
            "targets": [{"table": "Table 2", "model": "(1)", "term": "Visit", "value": "0.231", "se": "(0.054)", "page": page},
                        {"table": "Table 2", "model": "(2)", "term": "Visit", "value": "0.198", "se": "(0.071)", "page": page}]}
    full = json.dumps(plan, ensure_ascii=False)
    assert len(full) > 9100
    cut = full.index('"value": "0.198"') - 12
    jid = queue.enqueue(conn, "methods.replicate", paper_id)
    script = Script(deepseek(full[:cut], "length", 30000, 3500), deepseek(full[cut:], "stop", 30500, 120))
    run_id = runner.run_job(conn, jid, provider(script), cost.cost_usd)
    assert len(script.requests) == 2 and run_of(conn, run_id)["repaired"] == 0
    assert script.bodies()[1]["messages"][-1] == {"role": "assistant", "content": full[:cut], "prefix": True}
    plan_id = plans.plan_for_job(conn, jid)
    assert [(t["value"], t["se"]) for t in plans.targets(conn, plan_id)] == [("0.231", "(0.054)"), ("0.198", "(0.071)")]
    assert len(plans.get(conn, plan_id)["plan"]["steps"]) == 60


# ---------------------------------------------------------------- a cheaper tier for reading the paper

def test_extract_tier_is_a_setting_that_says_a_cheap_model_cannot_invent_numbers():
    d = tunables.describe(None, "replication.extract_tier")
    assert d["default"] == "standard" and [c["value"] for c in d["choices"]] == ["light", "standard"]
    assert "不会编" in d["help"] and "逐个核对" in d["help"] and "舍弃" in d["help"]
    with pytest.raises(tunables.TunableError):
        tunables.validate("replication.extract_tier", "deep")
    # lowering costs less (direct), raising costs more (a confirm card)
    assert tunables.tier_for("replication.extract_tier", "light", "standard") == tunables.DIRECT
    assert tunables.tier_for("replication.extract_tier", "standard", "light") == tunables.CONFIRM


def test_job_tier_only_ever_makes_methods_replicate_cheaper(conn):
    assert runner.job_tier(conn, "methods.replicate", "standard") == "standard"
    assert runner.job_tier(conn, "methods.replicate", "deep") == "deep"          # the skill's own tier stays in charge
    tunables.set_value(conn, "replication.extract_tier", "light")
    assert runner.job_tier(conn, "methods.replicate", "deep") == "light"
    assert runner.job_tier(conn, "paper.summarize", "standard") == "standard"    # other jobs are not touched


def test_methods_replicate_runs_on_the_chosen_tiers_model(conn, tmp_path):
    tunables.set_value(conn, "engine.deepseek.standard", "deepseek-v4-pro")
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    models = []
    for tier in ("standard", "light"):
        tunables.set_value(conn, "replication.extract_tier", tier)
        script = Script(deepseek(json.dumps(VALID_PLAN)))
        runner.run_job(conn, queue.enqueue(conn, "methods.replicate", paper_id, force=True), provider(script), cost.cost_usd)
        models.append(script.bodies()[0]["model"])
    assert models == ["deepseek-v4-pro", "deepseek-flash"]


# ---------------------------------------------------------------- the ledger

def add_job(conn, kind, target):
    jid = queue.new_job_id(conn)
    conn.execute("INSERT INTO ai_jobs(id, kind, status, target_type, target_id, request_json, schema_json, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (jid, kind, "done", "x", target, "{}", "{}", now_iso()))
    return jid


def spend(conn, job_id, prompt, completion, *, hit=0, usd=0.0, valid=True, model="deepseek-flash"):
    rid = queue.start_run(conn, job_id, "deepseek", model)
    queue.finish_run(conn, rid, valid=valid, cost_usd=usd, usage={
        "prompt_tokens": prompt, "cache_hit_tokens": hit, "completion_tokens": completion, "reasoning_tokens": 0})
    return rid


def analysis_run(conn, run_id, plan_id, method="ols", tokens=None):
    conn.execute("INSERT INTO analysis_runs(id, plan_id, method, status, tokens, created_at) VALUES (?,?,?,?,?,?)",
                 (run_id, plan_id, method, "done", tokens, now_iso()))


def workspace_job(conn, plan_id, mode, tokens, run_id=None):
    conn.execute("INSERT INTO replication_jobs(plan_id, mode, state, run_id, tokens, started_at) VALUES (?,?,?,?,?,?)",
                 (plan_id, mode, "done", run_id, tokens, now_iso()))


@pytest.fixture()
def spent(conn, tmp_path):
    """A plan with every step spent on, a second plan of the same paper with one step, a third that spent nothing, and
    noise that belongs to none of them."""
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    plan_id = suggest(conn, paper_id)
    run_id = conn.execute("SELECT ai_run_id FROM replication_plans WHERE id = ?", (plan_id,)).fetchone()[0]
    job_id = conn.execute("SELECT job_id FROM ai_runs WHERE id = ?", (run_id,)).fetchone()[0]
    conn.execute("UPDATE ai_runs SET prompt_tokens = 20000, cache_hit_tokens = 15000, completion_tokens = 3000, "
                 "cost_usd = 0.002 WHERE id = ?", (run_id,))
    spend(conn, job_id, 12000, 8000, usd=0.001, valid=False)                 # an earlier attempt that failed: still paid for
    other, idle = plans.create_blank(conn, paper_id), plans.create_blank(conn, paper_id)
    analysis_run(conn, "RUN-2026-0001", plan_id)
    spend(conn, add_job(conn, "analysis.read", "RUN-2026-0001"), 4000, 500, usd=0.0005, model="deepseek-v4-pro")
    analysis_run(conn, "RUN-2026-0009", other)
    spend(conn, add_job(conn, "analysis.read", "RUN-2026-0009"), 9000, 999)  # the second plan's, not the first's
    spend(conn, add_job(conn, "paper.summarize", paper_id), 7777, 7777)      # not about replication
    analysis_run(conn, "RUN-2026-0002", plan_id, "workspace", 5000)
    workspace_job(conn, plan_id, "ai", 5000, "RUN-2026-0002")
    workspace_job(conn, plan_id, "rerun", 0)
    analysis_run(conn, "RUN-2026-0003", plan_id, "workspace", 700)           # a stored session no job points at
    return paper_id, plan_id, other, idle


def test_the_ledger_sums_the_actual_tokens_of_each_step(conn, spent):
    _, plan_id, _, _ = spent
    led = plans.ledger(conn, plan_id)
    assert [s["key"] for s in led["steps"]] == ["extract", "read", "workspace"]
    extract, read, workspace = led["steps"]
    assert (extract["runs"], extract["input"], extract["output"], extract["cached"], extract["tokens"]) == (2, 32000, 11000, 15000, 43000)
    assert (read["runs"], read["input"], read["output"], read["tokens"], read["models"]) == (1, 4000, 500, 4500, ["deepseek-v4-pro"])
    assert (workspace["runs"], workspace["tokens"], workspace["input"]) == (2, 5700, None)   # 5000 once, not twice, plus 700
    assert led["tokens"] == 43000 + 4500 + 5700
    assert led["cost_usd"] == pytest.approx(0.0035) and led["cost_cny"] > led["cost_usd"]
    assert "deepseek-v4-pro" in led["models"] and extract["models"] == ["deepseek-flash", "test"]


def test_each_plan_has_its_own_ledger_and_an_idle_one_is_empty(conn, spent):
    _, _, other, idle = spent
    led = plans.ledger(conn, other)
    assert led["tokens"] == 9999 and [s["key"] for s in led["steps"]] == ["read"]
    assert plans.ledger(conn, idle) == {"tokens": 0, "cost_usd": 0.0, "cost_cny": 0.0, "steps": [], "models": []}
    assert plans.ledger(conn, "RP-NOPE")["tokens"] == 0


def test_the_paper_ledger_adds_jobs_that_made_no_plan(conn, spent):
    paper_id, plan_id, other, _ = spent
    spend(conn, add_job(conn, "methods.replicate", paper_id), 6000, 1000)    # a run that failed before any plan existed
    assert plans.paper_ledger(conn, paper_id)["tokens"] == plans.ledger(conn, plan_id)["tokens"] + 9999 + 7000
    assert plans.paper_ledger(conn, "PAP-NONE")["tokens"] == 0


def test_the_plan_page_says_what_the_replication_has_cost(conn, spent, data_client):
    _, plan_id, other, idle = spent
    html = data_client.get(f"/data/plans/{plan_id}/detail").text
    assert "这份复现已花 53,200 tokens" in html
    assert "读论文、拟方案" in html and "解读分析结果" in html and "工作区里的 AI 运行" in html and "43,000" in html
    assert "这份复现已花 9,999 tokens" in data_client.get(f"/data/plans/{other}/detail").text
    assert "这份复现已花" not in data_client.get(f"/data/plans/{idle}/detail").text       # nothing spent: no line


# ---------------------------------------------------------------- reuse the page material

def cache_parts():
    return sorted(p.name.split(".")[-2] for p in (get_paths().cache / "replication").glob("*.json"))


def test_page_material_is_chosen_once_per_file_and_reused(conn, tmp_path, monkeypatch):
    from research_lab.intake import extract
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    calls = []
    real = extract.sideways_texts
    monkeypatch.setattr(extract, "sideways_texts", lambda path, also=(): calls.append(path) or real(path, also))

    first, _ = plans.build(conn, paper_id)
    assert len(calls) == 1                                                  # miss: the PDF is read once for all three pickers
    assert cache_parts() == ["avail", "methods", "tables", "upright"]
    again, _ = plans.build(conn, paper_id)
    assert len(calls) == 1 and again.messages == first.messages             # hit: nothing recomputed
    assert plans.availability_links(conn, paper_id) == plans.availability_links(conn, paper_id) and len(calls) == 1
    assert all(isinstance(piece, tuple) for piece in plans.methods_material(conn, paper_id)["pieces"])
    assert all(isinstance(piece, tuple) for piece in plans.results_material(conn, paper_id, {}))
    assert len(plans.page_texts(conn, paper_id)) == 4 and len(calls) == 1   # apply() checks numbers against the same pages


def test_a_sideways_page_is_read_again_only_on_a_miss(conn, tmp_path, monkeypatch):
    from research_lab.intake import extract
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    calls = []
    monkeypatch.setattr(extract, "sideways_texts", lambda path, also=(): calls.append(1) or {1: "TABLE 3 turned upright"})
    for _ in range(3):
        assert plans.page_texts(conn, paper_id)["2"] == "TABLE 3 turned upright"
    assert len(calls) == 1


def test_a_new_selection_version_new_page_text_or_new_file_is_a_miss(conn, tmp_path, monkeypatch):
    from research_lab.intake import extract
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    calls = []
    real = extract.sideways_texts
    monkeypatch.setattr(extract, "sideways_texts", lambda path, also=(): calls.append(path) or real(path, also))
    plans.build(conn, paper_id)
    assert len(calls) == 1
    monkeypatch.setattr(plans, "SELECTION_VERSION", plans.SELECTION_VERSION + 1)
    plans.build(conn, paper_id)
    assert len(calls) == 2 and cache_parts() == ["avail", "methods", "tables", "upright"]   # the old version's files are gone
    conn.execute("UPDATE paper_pages SET text = text || ' (corrected)' WHERE page_index = 2")
    plans.build(conn, paper_id)
    assert len(calls) == 3
    plans.build(conn, paper_id)
    assert len(calls) == 3
    # the same page text on a different file (another sha) shares nothing
    conn.execute("UPDATE paper_files SET sha256 = ? WHERE paper_id = ?", ("0" * 64, paper_id))
    plans.build(conn, paper_id)
    assert len(calls) == 4


def test_a_damaged_or_unwritable_cache_is_only_a_slower_run(conn, tmp_path, monkeypatch):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    first, _ = plans.build(conn, paper_id)
    for p in (get_paths().cache / "replication").glob("*.json"):
        p.write_text("{not json", encoding="utf-8")
    again, _ = plans.build(conn, paper_id)
    assert again.messages == first.messages
    monkeypatch.setattr(get_paths().__class__, "cache", property(lambda self: tmp_path / "file-not-dir"))
    (tmp_path / "file-not-dir").write_text("x")
    assert plans.build(conn, paper_id)[0].messages == first.messages


@pytest.fixture()
def folder():
    d = get_paths().cache / "replication"
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.txt").write_text("not ours")
    return d


def shas(folder):
    return {p.name.split(".")[0] for p in folder.glob("*.json")}


def test_the_entries_of_a_replaced_file_go_on_the_next_write(conn, tmp_path, folder):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    plans.build(conn, paper_id)
    [old] = shas(folder)
    conn.execute("UPDATE paper_files SET sha256 = ? WHERE paper_id = ?", ("1" * 64, paper_id))
    plans.build(conn, paper_id)
    assert shas(folder) == {"1" * 24} != {old} and cache_parts() == ["avail", "methods", "tables", "upright"]
    assert (folder / "notes.txt").exists()                                   # only files this cache wrote are touched


def test_the_entries_of_a_deleted_file_go_when_another_paper_writes(conn, tmp_path, folder):
    from .test_data import methods_pdf
    first = ingest_quiet(conn, did_pdf(tmp_path))
    plans.build(conn, first)
    [gone] = shas(folder)
    second = ingest_quiet(conn, methods_pdf(tmp_path))
    conn.execute("DELETE FROM paper_files WHERE paper_id = ?", (first,))
    plans.build(conn, second)
    assert gone not in shas(folder) and len(shas(folder)) == 1


def test_an_entry_unread_for_ninety_days_goes_and_a_read_keeps_it(conn, tmp_path, folder):
    import os
    import time
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    plans.build(conn, paper_id)
    stale = next(folder.glob("*.avail.json"))
    long_ago = time.time() - (plans.CACHE_MAX_AGE_DAYS + 1) * 86400
    os.utime(stale, (long_ago, long_ago))
    plans.availability_pages(conn, paper_id)                                 # a hit refreshes the entry
    assert stale.stat().st_mtime > time.time() - 60
    os.utime(stale, (long_ago, long_ago))
    next(folder.glob("*.methods.json")).unlink()
    plans.methods_material(conn, paper_id)                                   # a miss writes, and prunes
    assert not stale.exists() and (folder / "notes.txt").exists()


def test_over_the_size_cap_the_oldest_entries_go_first(conn, tmp_path, folder, monkeypatch):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    plans.build(conn, paper_id)
    monkeypatch.setattr(plans, "CACHE_MAX_BYTES", 1)
    next(folder.glob("*.methods.json")).unlink()
    plans.methods_material(conn, paper_id)
    assert cache_parts() == ["methods"] and (folder / "notes.txt").exists()   # the entry just written stays
