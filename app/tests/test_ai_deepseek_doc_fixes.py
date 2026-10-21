"""Card 0197: the DeepSeek calls as the official API docs (read 2026-10-19) describe them. A plan's `model` sent as a
list is merged instead of repaired and the request carries a short example; a failed run is billed; an overload inside
an HTTP 200 is retried and then requeued, with a deadline per request and jittered backoff; a content-filter stop
fails at once; a cut-off reply is continued by chat prefix completion on the beta URL; thinking requests get room for
their reasoning; and the plan prompt opens with the paper's text so the prefix cache covers it.

No network: httpx.MockTransport stands in for DeepSeek everywhere."""
from __future__ import annotations

import json

import httpx
import pytest
from research_lab.ai import claude_code, cost, queue, registry, runner
from research_lab.ai.deepseek import DEADLINE_S, THINKING_MIN_TOKENS, OpenAICompatProvider, ProviderError
from research_lab.ai.provider import SYSTEM_RULES, ChatRequest, system_rules
from research_lab.replication import plans

from .test_ai import VALID, EchoSpec, Script, reply
from .test_data import VALID_PLAN, ingest_quiet
from .test_replication_page_pick import jost_like  # noqa: F401 - fixture
from .test_replication_targets import did_pdf

ASK = ChatRequest(messages=[{"role": "system", "content": SYSTEM_RULES}, {"role": "user", "content": "Echo as json."}])
BUSY = {"error": "The server is unable to start processing your request now, please retry later."}


def make(script, *, sleeps=None, jitter=0.5, **kw):
    return OpenAICompatProvider("https://api.deepseek.com", "sk-test", "deepseek-flash", transport=httpx.MockTransport(script),
                                sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
                                jitter=lambda: jitter, **kw)


def answer(content: str, finish: str = "stop", prompt: int = 1000, completion: int = 100, reasoning: str | None = None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": prompt, "prompt_cache_hit_tokens": 0, "completion_tokens": completion,
                  "completion_tokens_details": {"reasoning_tokens": 0}}})


@pytest.fixture()
def echo(conn, monkeypatch):
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setitem(registry._JOBS, "test.echo", EchoSpec())
    return lambda: queue.enqueue(conn, "test.echo", "EXC-2026-0001", force=True)


def run_of(conn, run_id):
    return conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()


def last_run(conn):
    return conn.execute("SELECT * FROM ai_runs ORDER BY id DESC LIMIT 1").fetchone()


# ---------------------------------------------------------------- overload inside a 200, deadline, jitter

def test_a_200_whose_body_is_an_error_is_retried_like_a_503():
    sleeps: list[float] = []
    script = Script(httpx.Response(200, json=BUSY), reply(VALID))
    res = make(script, sleeps=sleeps).complete(ASK)
    assert json.loads(res.content)["echo"] == "EXC-2026-0001" and len(script.requests) == 2
    assert sleeps == [1.5]                                   # backoff 1.5 × (0.5 + jitter 0.5)


def test_insufficient_system_resource_is_retried_and_the_discarded_reply_is_still_counted():
    script = Script(answer('{"echo": "par', "insufficient_system_resource", 900, 40), reply(VALID, prompt=1200, hit=200, completion=80))
    res = make(script).complete(ASK)
    assert len(script.requests) == 2 and json.loads(res.content)["echo"] == "EXC-2026-0001"
    assert res.usage["prompt_tokens"] == 2100 and res.usage["completion_tokens"] == 120


def test_overload_that_outlasts_the_retries_is_a_retryable_error_with_its_usage():
    script = Script(httpx.Response(200, json=BUSY), answer("", "insufficient_system_resource", 500, 5),
                    httpx.Response(200, json={"error": {"message": "unable to start processing", "type": "server_busy"}}))
    with pytest.raises(ProviderError, match="unable to start processing") as caught:
        make(script).complete(ASK)
    assert len(script.requests) == 3 and caught.value.retryable and caught.value.usage["prompt_tokens"] == 500


def test_keep_alive_lines_before_the_answer_are_fine_and_a_body_of_only_keep_alives_is_retried():
    script = Script(httpx.Response(200, content=b"\n\n\n" + reply(VALID).content), httpx.Response(200, content=b"\n\n\n\n"),
                    reply(VALID))
    p = make(script)
    assert json.loads(p.complete(ASK).content)["echo"] == "EXC-2026-0001" and len(script.requests) == 1
    assert json.loads(p.complete(ASK).content)["echo"] == "EXC-2026-0001" and len(script.requests) == 3


def test_each_request_has_an_overall_deadline_that_keep_alive_lines_cannot_extend():
    now = [0.0]
    pulled = []

    def keep_alive():                                         # an empty line every 60 s, for an hour
        for _ in range(60):
            now[0] += 60
            pulled.append(1)
            yield b"\n"

    script = Script(httpx.Response(200, content=keep_alive()), reply(VALID))
    res = make(script, clock=lambda: now[0]).complete(ASK)
    assert json.loads(res.content)["echo"] == "EXC-2026-0001" and len(script.requests) == 2
    assert len(pulled) == int(DEADLINE_S // 60) + 1 and 600 <= DEADLINE_S <= 720   # given up just past ~11 minutes
    script = Script(*[httpx.Response(200, content=keep_alive()) for _ in range(3)])
    with pytest.raises(ProviderError, match="no answer within 660 s") as caught:
        make(script, clock=lambda: now[0]).complete(ASK)
    assert caught.value.retryable


def test_backoff_is_jittered_and_retry_after_is_honoured():
    for jitter, expected in ((0.0, [0.75, 1.5]), (1.0, [2.25, 4.5])):
        sleeps: list[float] = []
        with pytest.raises(ProviderError) as caught:
            make(Script(*[httpx.Response(503, text="busy")] * 3), sleeps=sleeps, jitter=jitter).complete(ASK)
        assert sleeps == expected and caught.value.retryable and caught.value.status == 503
    sleeps = []
    make(Script(httpx.Response(429, headers={"retry-after": "7"}), reply(VALID)), sleeps=sleeps).complete(ASK)
    assert sleeps == [7.0]


def test_a_client_error_is_still_permanent():
    script = Script(httpx.Response(402, text='{"error": "Insufficient Balance"}'))
    with pytest.raises(ProviderError, match="402") as caught:
        make(script).complete(ASK)
    assert len(script.requests) == 1 and not caught.value.retryable


@pytest.mark.parametrize("error", [
    {"message": "Authentication Fails, Your api key: ****abcd is invalid", "type": "authentication_error"},
    {"message": "Insufficient Balance", "type": "unknown_error"},
    {"message": "Invalid max_tokens value, the valid range of max_tokens is [1, 8192]", "type": "invalid_request_error"},
    {"message": "Model Not Exist", "type": "invalid_request_error", "code": "model_not_found"},
    "Authentication Fails (no such user)",
])
def test_an_error_in_a_200_that_a_retry_cannot_fix_fails_at_once(error):
    script = Script(httpx.Response(200, json={"error": error}), reply(VALID))
    with pytest.raises(ProviderError, match="cannot fix") as caught:
        make(script).complete(ASK)
    assert len(script.requests) == 1 and not caught.value.retryable and caught.value.status == 200


@pytest.mark.parametrize("error", [
    BUSY["error"],
    {"message": "The server is unable to start processing your request now", "type": "server_busy"},
    {"message": "Service temporarily overloaded, please retry", "type": "overloaded_error"},
    # overload text that shares a word with a permanent error: only whole phrases in the message count
    "upstream load balancer error, please retry",
    {"message": "You exceeded quota of concurrent requests, slow down", "type": "server_error"},
    {"message": "Bad gateway: authorization header could not be forwarded", "type": "server_error"},
    {"message": "Inference server not found in pool", "type": "server_error"},
])
def test_an_overload_in_a_200_is_still_retried(error):
    script = Script(httpx.Response(200, json={"error": error}), reply(VALID))
    assert json.loads(make(script).complete(ASK).content)["echo"] == "EXC-2026-0001" and len(script.requests) == 2


def test_errors_name_the_host_they_came_from():
    req = ChatRequest(messages=[*ASK.messages, {"role": "assistant", "content": "{", "prefix": True}])
    with pytest.raises(ProviderError, match=r"HTTP 404 on https://api\.deepseek\.com/beta/chat/completions"):
        make(Script(httpx.Response(404, text="Not Found"))).complete_prefix(req)
    with pytest.raises(ProviderError, match=r"HTTP 400 on https://api\.deepseek\.com/chat/completions"):
        make(Script(httpx.Response(400, text="bad"))).complete(ASK)


def test_a_password_in_the_base_url_never_reaches_the_error_text():
    p = OpenAICompatProvider("https://leo:s3cret@proxy.example/deepseek", "sk-test", transport=httpx.MockTransport(
        Script(httpx.Response(400, text="bad"))), sleep=lambda s: None)
    with pytest.raises(ProviderError) as caught:
        p.complete(ASK)
    assert "s3cret" not in str(caught.value) and "leo" not in str(caught.value) and "s3cret" not in repr(p)
    assert "https://proxy.example/deepseek/chat/completions" in str(caught.value)


# ---------------------------------------------------------------- the runner: requeue, bill, content filter

def test_a_busy_deepseek_puts_the_job_back_in_the_queue_and_bills_what_it_answered(conn, echo):
    jid = echo()
    script = Script(answer("", "insufficient_system_resource", 800, 20), httpx.Response(200, json=BUSY), httpx.Response(503))
    with pytest.raises(claude_code.Deferred) as caught:     # what the worker's sweep sets aside until its next round
        runner.run_job(conn, jid, make(script), cost.cost_usd)
    assert not caught.value.blocks_engine and claude_code.blocked(conn) is None   # Claude Code is not rested for it
    job = queue.get(conn, jid)
    assert job["status"] == "queued" and "放回队列" in job["error"] and job["attempts"] == 1
    run = last_run(conn)
    assert run["valid"] == 0 and run["prompt_tokens"] == 800
    assert run["cost_usd"] == pytest.approx(cost.cost_usd("deepseek-flash", {"prompt_tokens": 800, "completion_tokens": 20}))


def test_a_job_that_stays_busy_fails_after_the_usual_number_of_tries(conn, echo):
    jid = echo()
    for n in range(claude_code.MAX_STALLS):
        with pytest.raises(claude_code.Deferred):
            runner.run_job(conn, jid, make(Script(*[httpx.Response(200, json=BUSY)] * 3)), cost.cost_usd)
        assert queue.get(conn, jid)["status"] == ("failed" if n == claude_code.MAX_STALLS - 1 else "queued")
    assert "不再自动重试" in queue.get(conn, jid)["error"]


def test_a_failed_run_is_billed_and_counts_toward_the_daily_cap(conn, echo):
    jid = echo()
    with pytest.raises(Exception):
        runner.run_job(conn, jid, make(Script(reply("not json", prompt=3000, hit=0, completion=900),
                                              reply('{"wrong": 1}', prompt=4000, hit=0, completion=900))), cost.cost_usd)
    run = last_run(conn)
    assert queue.get(conn, jid)["status"] == "failed" and run["valid"] == 0 and run["repaired"] == 1
    expected = cost.cost_usd("deepseek-flash", {"prompt_tokens": 7000, "completion_tokens": 1800})
    assert run["cost_usd"] == pytest.approx(expected) and expected > 0
    assert cost.spent_today(conn) == pytest.approx(expected)
    assert json.loads(open(run["log_path"]).read())["cost_usd"] == pytest.approx(expected)


def test_a_request_that_fails_midway_still_bills_the_replies_before_it(conn, echo):
    jid = echo()
    with pytest.raises(ProviderError):
        runner.run_job(conn, jid, make(Script(reply("not json", prompt=3000, hit=0, completion=900),
                                              httpx.Response(400, text="bad request"))), cost.cost_usd)
    run = last_run(conn)
    assert run["prompt_tokens"] == 3000 and run["cost_usd"] > 0 and queue.get(conn, jid)["status"] == "failed"


def test_a_content_filter_stop_fails_at_once_without_a_repair(conn, echo):
    jid = echo()
    script = Script(answer('{"echo": "', "content_filter", 1000, 3), reply(VALID))
    with pytest.raises(runner.Refused):
        runner.run_job(conn, jid, make(script), cost.cost_usd)
    job = queue.get(conn, jid)
    assert len(script.requests) == 1 and job["status"] == "failed" and "content_filter" in job["error"]
    assert last_run(conn)["cost_usd"] > 0


# ---------------------------------------------------------------- prefix completion and the thinking floor

FULL = json.dumps({"echo": "alpha " * 40, "ids": ["EXC-2026-0001"]})
HEAD, TAIL = FULL[:150], FULL[150:]


def test_the_beta_url_comes_from_the_configured_base():
    for base in ("https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1",
                 "https://api.deepseek.com/beta"):
        assert OpenAICompatProvider(base).beta_url == "https://api.deepseek.com/beta"
    assert OpenAICompatProvider("https://proxy.example/deepseek").beta_url == "https://proxy.example/deepseek/beta"


def test_a_cut_off_reply_is_continued_by_prefix_completion_on_the_beta_url(conn, echo):
    jid = echo()
    script = Script(answer(HEAD, "length", 1000, 200), answer(TAIL, "stop", 1000, 60))
    run_id = runner.run_job(conn, jid, make(script), cost.cost_usd)
    assert json.loads(queue.get(conn, jid)["result_json"])["echo"] == "alpha " * 40
    first, second = script.bodies()
    assert str(script.requests[0].url) == "https://api.deepseek.com/chat/completions"
    assert str(script.requests[1].url) == "https://api.deepseek.com/beta/chat/completions"
    assert second["messages"] == [*first["messages"], {"role": "assistant", "content": HEAD, "prefix": True}]
    assert "response_format" not in second and second["max_tokens"] == first["max_tokens"]
    run = run_of(conn, run_id)
    assert run["repaired"] == 0 and run["completion_tokens"] == 260


def test_a_repetitive_tail_needs_no_anchor_under_prefix_completion(conn, echo):
    head = '{"echo": "' + "ab" * 200                         # the anchor path has no unique tail here
    assert runner.anchor_of(head) is None
    script = Script(answer(head, "length"), answer('ab", "ids": []}'))
    run_id = runner.run_job(conn, echo(), make(script), cost.cost_usd)
    assert len(script.requests) == 2 and run_of(conn, run_id)["repaired"] == 0


def test_a_join_that_is_still_not_valid_goes_to_the_repair_retry(conn, echo):
    script = Script(answer(HEAD, "length"), answer("garbage", "stop"), reply(VALID))
    run_id = runner.run_job(conn, echo(), make(script), cost.cost_usd)
    assert len(script.requests) == 3 and run_of(conn, run_id)["repaired"] == 1
    repair = script.bodies()[2]
    assert script.requests[2].url.path == "/chat/completions" and repair["messages"][-2]["content"] == HEAD + "garbage"


UNIQUE = json.dumps({"echo": "alpha beta gamma delta epsilon zeta eta theta", "ids": ["EXC-2026-0001", "EXC-2026-0002"]})
U_HEAD = UNIQUE[:UNIQUE.index("EXC-2026-0002") + 6]


@pytest.mark.parametrize("refusal", [httpx.Response(404, text="Not Found"),
                                     httpx.Response(200, json={"error": {"message": "Invalid request: prefix",
                                                                         "type": "invalid_request_error"}})])
def test_a_beta_endpoint_that_refuses_for_good_falls_back_to_the_anchor_continuation(conn, echo, refusal):
    jid = echo()
    anchor = runner.anchor_of(U_HEAD)
    script = Script(answer(U_HEAD, "length", 1000, 300), refusal, answer(anchor + UNIQUE[len(U_HEAD):], "stop", 1100, 20))
    run_id = runner.run_job(conn, jid, make(script), cost.cost_usd)
    assert json.loads(queue.get(conn, jid)["result_json"])["ids"] == ["EXC-2026-0001", "EXC-2026-0002"]
    assert [r.url.path for r in script.requests] == ["/chat/completions", "/beta/chat/completions", "/chat/completions"]
    assert script.bodies()[2]["messages"][-1] == {"role": "user", "content": runner.continue_prompt(anchor)}
    run = run_of(conn, run_id)
    assert run["repaired"] == 0 and run["completion_tokens"] == 320          # the cut-off reply was used, not redone
    exchanges = json.loads(open(run["log_path"]).read())["exchanges"]
    assert "beta/chat/completions" in exchanges[1]["error"]


def test_with_no_anchor_either_the_refused_continuation_ends_in_the_repair(conn, echo):
    head = '{"echo": "' + "ab" * 200                         # no unique tail to anchor on
    script = Script(answer(head, "length"), httpx.Response(404, text="Not Found"), reply(VALID))
    assert runner.anchor_of(head) is None
    run_id = runner.run_job(conn, echo(), make(script), cost.cost_usd)
    assert len(script.requests) == 3 and run_of(conn, run_id)["repaired"] == 1
    assert "not valid for the schema" in script.bodies()[2]["messages"][-1]["content"]


def test_a_busy_beta_endpoint_still_puts_the_job_back_in_the_queue(conn, echo):
    jid = echo()
    script = Script(answer(U_HEAD, "length"), *[httpx.Response(503, text="busy")] * 3)
    with pytest.raises(claude_code.Deferred):
        runner.run_job(conn, jid, make(script), cost.cost_usd)
    assert len(script.requests) == 4 and queue.get(conn, jid)["status"] == "queued"
    assert last_run(conn)["cost_usd"] > 0                     # the cut-off reply is still on the bill


class ThinkingSpec(EchoSpec):
    thinking = "high"


def test_with_thinking_the_prefix_carries_the_reasoning_and_every_request_has_room_for_it(conn, monkeypatch):
    monkeypatch.setattr(registry, "_loaded", True)
    monkeypatch.setitem(registry._JOBS, "test.echo", ThinkingSpec())
    jid = queue.enqueue(conn, "test.echo", "EXC-2026-0001", force=True)
    script = Script(answer(HEAD, "length", reasoning="First the ids, then the echo."), answer(TAIL))
    runner.run_job(conn, jid, make(script), cost.cost_usd)
    first, second = script.bodies()
    assert first["thinking"] == second["thinking"] == {"type": "enabled"} and second["reasoning_effort"] == "high"
    assert first["max_tokens"] == second["max_tokens"] == THINKING_MIN_TOKENS >= 32_000     # the job asked for 200
    assert second["messages"][-1] == {"role": "assistant", "content": HEAD, "prefix": True,
                                      "reasoning_content": "First the ids, then the echo."}
    assert queue.get(conn, jid)["status"] == "done"


def test_the_thinking_floor_only_raises():
    p = make(Script())
    assert p.build_body(ChatRequest(messages=ASK.messages, max_tokens=4000))["max_tokens"] == 4000
    assert p.build_body(ChatRequest(messages=ASK.messages, max_tokens=4000, thinking="high"))["max_tokens"] == 32_000
    assert p.build_body(ChatRequest(messages=ASK.messages, max_tokens=50_000, thinking="max"))["max_tokens"] == 50_000
    prefix = ChatRequest(messages=[*ASK.messages, {"role": "assistant", "content": "{", "prefix": True,
                                                   "reasoning_content": "unused"}])
    script = Script(reply("}"))
    make(script).complete_prefix(prefix)                     # thinking off: no reasoning goes back
    assert script.bodies()[0]["messages"][-1] == {"role": "assistant", "content": "{", "prefix": True}


# ---------------------------------------------------------------- the plan: model as a list, example, prompt order

def test_model_sent_as_a_list_is_merged_into_one_object():
    same = plans.Result.model_validate({**VALID_PLAN, "model": [
        {"family": "ols", "specification": "Conf − Coop ~ mean hawkishness"},
        {"family": "OLS", "specification": "Conf − Coop ~ president's hawkishness"}]})
    assert same.model.family == "OLS"
    assert same.model.specification == "Conf − Coop ~ mean hawkishness；Conf − Coop ~ president's hawkishness"
    mixed = plans.Result.model_validate({**VALID_PLAN, "model": [
        {"family": "Poisson", "specification": "decisions ~ hawkishness（Table 3 模型 1）"},
        {"family": "OLS", "specification": "Conf − Coop ~ hawkishness（Table 3 模型 2）"},
        {"family": "logit"}]})
    assert mixed.model.family == "other"
    assert mixed.model.specification == "decisions ~ hawkishness（Table 3 模型 1）；OLS：Conf − Coop ~ hawkishness（Table 3 模型 2）；logit"
    long = plans.Result.model_validate({**VALID_PLAN, "model": [{"family": "OLS", "specification": f"模型 {i}：" + "y ~ x + " * 40}
                                                                for i in range(30)]})
    assert plans.MODEL_SPEC_CHARS - 5 <= len(long.model.specification) <= plans.MODEL_SPEC_CHARS and long.model.specification.endswith("…")
    assert plans.Result.model_validate({**VALID_PLAN, "model": []}).model.family == "other"


def test_a_plan_whose_model_is_a_list_needs_no_repair(conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    jid = queue.enqueue(conn, "methods.replicate", paper_id)
    plan = {**VALID_PLAN, "model": [{"family": "OLS", "specification": "y ~ visit"}, {"family": "OLS", "specification": "y ~ visit + FE"}]}
    script = Script(reply(json.dumps(plan, ensure_ascii=False)))
    run_id = runner.run_job(conn, jid, make(script), cost.cost_usd)
    assert len(script.requests) == 1 and run_of(conn, run_id)["repaired"] == 0
    assert plans.get(conn, plans.plan_for_job(conn, jid))["plan"]["model"] == {"family": "OLS", "specification": "y ~ visit；y ~ visit + FE"}


def test_the_plan_request_carries_a_short_compact_example_and_room_to_finish(conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    req, _ = plans.build(conn, paper_id)
    user = req.messages[-1]["content"]
    assert user.endswith(plans.EXAMPLE_SHAPE) and len(plans.EXAMPLE_SHAPE) < 1200 and "\n" not in plans.EXAMPLE_SHAPE
    shape = json.loads(plans.EXAMPLE_SHAPE)
    assert isinstance(shape["model"], dict) and shape["links"] == [] and len(shape["variables"]) == 1
    assert plans.Result.model_validate(shape)                # the example itself passes the schema
    assert "one object, not a list" in user
    assert "made-up illustrations" in user and "never reuse any of them" in user    # 东亚峰会 values are not this paper's
    assert user.index("never reuse any of them") < user.index(plans.EXAMPLE_SHAPE)
    assert "fields that state facts about this paper" in user and "every value comes from" not in user   # not the judgments
    assert req.max_tokens == plans.PLAN_MAX_TOKENS == 16_000


def test_the_plan_prompt_opens_with_the_paper_text_and_ends_with_the_task(conn, jost_like):
    req, _ = plans.build(conn, jost_like)
    system, user = req.messages[0]["content"], req.messages[-1]["content"]
    block = plans.paper_block(conn, jost_like)
    assert system == system_rules() and user.startswith(block)
    assert block.index("<paper_text>") < block.index("<results_pages>") < block.index("<availability>")
    assert block.endswith("</availability>\n\n") and "Paper:" not in block and "Task:" not in block
    rest = user[len(block):]
    order = [rest.index(s) for s in ("Paper: Advisers and Aggregation", "Direction", "https://doi.org/10.7910/DVN/GZW94R", "Task:")]
    assert order == sorted(order)


def test_what_changes_between_runs_comes_after_the_cached_part(conn, jost_like):
    before, _ = plans.build(conn, jost_like)
    block = plans.paper_block(conn, jost_like)
    assert plans.paper_block(conn, jost_like) == block       # the same string every time
    conn.execute("UPDATE papers SET ai_summary_json = ? WHERE id = ?",
                 (json.dumps({"research_question": {"text": "顾问如何影响决策", "pages": [2]}}, ensure_ascii=False), jost_like))
    after, _ = plans.build(conn, jost_like)
    assert after.messages[0] == before.messages[0]           # the system message does not move
    assert before.messages[-1]["content"] != after.messages[-1]["content"]
    assert after.messages[-1]["content"].startswith(block) and "顾问如何影响决策" not in block
    with pytest.raises(LookupError):
        plans.paper_block(conn, "no-such-paper")


def test_paper_block_is_what_a_second_job_can_open_with(conn, tmp_path):
    paper_id = ingest_quiet(conn, did_pdf(tmp_path))
    block = plans.paper_block(conn, paper_id)
    req, _ = plans.build(conn, paper_id)
    assert req.messages[-1]["content"][:len(block)] == block and "<paper_text>" in block and "Visit 0.231***" in block


def test_the_shape_example_never_carries_a_link_the_model_could_copy():
    assert "doi.org" not in plans.EXAMPLE_SHAPE and "http" not in plans.EXAMPLE_SHAPE
