"""Run a queued job through a ChatProvider with strict JSON handling: a reply cut off at the output limit is
continued (one request for the missing tail: DeepSeek's chat prefix completion, else a prompt that has the model repeat
an anchor), a reply that is complete but wrong gets one repair retry. A content-filter stop fails at once; a provider
that was busy or out of reach (a retryable error) puts the job back in the queue. Every request actually made is on the
run's bill, a failed run's too, so the daily cap counts it."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from ..config import get_paths
from ..db.connection import now_iso
from ..tunables import tunable
from ..agent import skills
from . import claude_code, queue, registry
from .provider import ChatProvider, ChatRequest, ChatResult


_CLOSES = re.compile(r"\s*(?:[,:}\]]|$)")


def _escape_inner_quotes(text: str) -> str:
    """Escape bare double quotes inside JSON strings (models write 印度"蟑螂"青年运动 inside a value). Quotes in prose
    come in pairs, so a quote followed by , : } ] ends the string only when the quotes escaped so far in it are
    even; otherwise `"比较"制衡", "追随"两种策略"` would split one list item into two that still pass the schema.
    An odd count left at the end fails the parse, which is the safe outcome (the repair retry runs)."""
    out, in_str, inner, i = [], False, 0, 0
    while i < len(text):
        ch = text[i]
        if in_str and ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == '"':
            if not in_str:
                in_str, inner = True, 0
            elif inner % 2 == 0 and _CLOSES.match(text, i + 1):
                in_str = False
            else:
                out.append('\\"')
                inner += 1
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    body = text[start:end + 1]
    try:
        return json.loads(body)
    except json.JSONDecodeError as first:
        try:
            return json.loads(_escape_inner_quotes(body))
        except json.JSONDecodeError:
            raise first from None


# Why a reply stopped, as the two drivers leave it in ChatResult.raw: DeepSeek's `choices[0].finish_reason`
# ("length" at max_tokens) and the Claude Code result event's `stop_reason` ("max_tokens").
CUT_REASONS = frozenset({"length", "max_tokens"})
ANCHOR_MIN, ANCHOR_MAX = 30, 160   # characters of the cut-off reply the continuation must repeat to prove where it joins
# DeepSeek's finish_reason when its content filter stopped the reply: asking again or repairing meets the same filter.
REFUSED = "content_filter"


class Refused(RuntimeError):
    """The provider's content filter stopped the reply; the job fails without a continuation or a repair."""


def job_tier(conn: sqlite3.Connection, kind: str, tier: str) -> str:
    """The tier a job runs at. The skill's tier is the rule; a setting that names a tier for one kind of job
    can only make it cheaper. replication.extract_tier = light sends methods.replicate (the plan and the printed
    numbers) to the light model; 'standard' leaves the 方法提取 skill's own tier in charge."""
    if kind == "methods.replicate" and tunable(conn, "replication.extract_tier") == "light":
        return "light"
    return tier


def finish_reason(res: ChatResult) -> str:
    raw = res.raw or {}
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return str(raw.get("stop_reason") or "")


def _open_ended(text: str) -> bool:
    """True when the first JSON object in `text` is still open at its end: inside a string, or with an unclosed
    brace or bracket. A reply that ends abruptly looks like this whatever the driver says about why it stopped."""
    start = text.find("{")
    if start == -1:
        return False
    depth, in_str, esc = 0, False, False
    for ch in text[start:]:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth <= 0:
                return False
    return True


def cut_off(res: ChatResult) -> bool:
    """The reply has a partial JSON object that was cut short (driver says so, or the text ends mid-object)."""
    text = res.content or ""
    return "{" in text and (finish_reason(res) in CUT_REASONS or _open_ended(text))


def _reasoning(res: ChatResult) -> str:
    choices = (res.raw or {}).get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str((choices[0].get("message") or {}).get("reasoning_content") or "")
    return ""


def prefix_request(req: ChatRequest, res: ChatResult) -> ChatRequest:
    """The request that continues `res`, a reply of `req` cut off at the output limit, by chat prefix completion: the
    same request with the reply so far as a last assistant message marked prefix, plus its reasoning_content when
    thinking was on (the API wants a thinking turn's reasoning back with it)."""
    prefix: dict = {"role": "assistant", "content": res.content, "prefix": True}
    reasoning = _reasoning(res) if req.thinking not in (None, "off") else ""
    if reasoning:
        prefix["reasoning_content"] = reasoning
    return replace(req, messages=[*req.messages, prefix])


def _deferred(e: Exception) -> claude_code.Deferred | None:
    """The job goes back to the queue: the engine said so (claude_code.Deferred), or the provider was busy or out of
    reach after its own retries (an error marked retryable: DeepSeek's 429/5xx, overload inside a 200, a deadline)."""
    if isinstance(e, claude_code.Deferred):
        return e
    if getattr(e, "retryable", False):
        return claude_code.Deferred(datetime.now(timezone.utc), f"DeepSeek 繁忙或连不上，已放回队列，下一轮自动再试（{str(e)[:160]}）",
                                    blocks_engine=False)
    return None


def anchor_of(partial: str) -> str | None:
    """The tail of a cut-off reply that its continuation has to open by repeating: ANCHOR_MIN characters, grown until
    the tail occurs only once in the reply, so the seam has exactly one place. No leading whitespace (a driver may
    trim the continuation's start). None when no tail up to ANCHOR_MAX is unique, a run of near-identical rows."""
    for n in range(min(ANCHOR_MIN, len(partial)), min(ANCHOR_MAX, len(partial)) + 1, 10):
        tail = partial[-n:].lstrip()
        if tail and partial.find(tail) == len(partial) - len(tail):
            return tail
    return None


def continue_prompt(anchor: str) -> str:
    return ("Your reply stopped at the output limit, in the middle of the JSON object. Continue it. Begin by repeating, "
            "character for character, the last characters of your reply, given here as a JSON string literal (write the "
            "characters it spells, not the quotes or the escapes):\n" + json.dumps(anchor, ensure_ascii=False)
            + "\nThen go on directly after them and write only the missing rest. Do not start over, and do not add a "
            "code fence or any comment.")


def stitch(partial: str, more: str) -> str | None:
    """The cut-off reply plus the rest of its continuation, joined right after the repeated anchor. None when the
    continuation does not open with the anchor: a literal join of two halves can double a word, a digit or a space
    and still be valid JSON (`12` + `12.5` is 1212.5), so nothing is guessed and the repair retry takes over."""
    anchor = anchor_of(partial)
    more = re.sub(r"\s*```\s*\Z", "", re.sub(r"\A\s*```(?:json)?[ \t]*\r?\n?", "", more)).lstrip()
    if anchor is None or not more.startswith(anchor):
        return None
    return partial + more[len(anchor):]


def _log(job_id: str, run_id: int, payload: dict) -> str:
    d = get_paths().ai_log
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{job_id}-{run_id}.json"
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return str(p)


def run_job(conn: sqlite3.Connection, job_id: str, provider: ChatProvider, cost_fn=None) -> int:
    """Returns the ai_runs id. On a second invalid answer the job is marked failed and ValueError raised; a
    content-filter stop marks it failed and raises Refused; a busy provider puts it back in the queue and raises
    claude_code.Deferred (failed after MAX_STALLS counted tries). The run is billed in every case."""
    job = queue.claim(conn, job_id)
    spec = registry.get(job["kind"])
    req = queue.request_of(job)
    if spec.thinking and not req.thinking:
        req.thinking = spec.thinking
    engine = "claude" if provider.name == "claude-code" else "deepseek"
    skill = skills.for_job(job["kind"])
    tier = None
    if skill is not None:
        # The skill's tier picks the model on whichever engine runs. Output size stays the job's own
        # (summaries and translation chunks size it from the paper); skill caps bound task replies only.
        tier = job_tier(conn, job["kind"], skills.tier_of(conn, skill))
        req.model = skills.engine_model(conn, engine, tier) or req.model
        if engine == "deepseek" and not req.thinking:
            req.thinking = skills.DEEPSEEK_THINKING.get(tier)
    if engine == "claude":
        if tier is not None and not req.thinking:   # thinking only where the tier asks for it (deep), as on DeepSeek
            req.thinking = skills.DEEPSEEK_THINKING.get(tier)
        model = req.model or getattr(provider, "default_model", "") or "claude-code"
    else:
        model = req.model or tunable(conn, "ai.model")
    run_id = queue.start_run(conn, job_id, provider.name, model)
    exchanges: list[dict] = []
    total_usage: dict[str, int] = {}
    cost = 0.0
    repaired = continued = False

    def add_usage(usage: dict | None) -> None:
        for k, v in (usage or {}).items():
            total_usage[k] = total_usage.get(k, 0) + (v or 0)

    def ask(request: ChatRequest, send=None) -> ChatResult:
        try:
            res = (send or provider.complete)(request)
        except Exception as e:
            add_usage(getattr(e, "usage", None))     # replies the provider received and threw away before giving up
            exchanges.append({"request": request.to_json(), "error": str(e)})
            raise
        exchanges.append({"request": request.to_json(), "response": res.raw or res.content})
        add_usage(res.usage)
        if finish_reason(res) == REFUSED:
            raise Refused("内容过滤拦下了这次回复（finish_reason=content_filter），不续写也不修复；请检查送去的材料")
        return res

    def bill() -> float:
        return cost_fn(model, total_usage) if cost_fn else 0.0

    def parse(text: str):
        return spec.Result.model_validate(_extract_json(text))

    try:
        res = ask(req)
        text = res.content
        try:
            parsed = parse(text)
        except (ValueError, ValidationError) as first:
            parsed = None
            send_prefix = getattr(provider, "complete_prefix", None)
            cut = cut_off(res)
            if cut and callable(send_prefix):
                # DeepSeek goes on from the cut-off reply itself (chat prefix completion): only the missing tail is
                # generated, the prompt goes out unchanged (a cache hit), and the two parts join as they are. The JSON
                # check below still decides; a join that fails it goes to the repair retry.
                try:
                    more = ask(prefix_request(req, res), send_prefix)
                except Exception as e:  # noqa: BLE001
                    # The beta endpoint refusing for good (a 404 once it moves, a request it rejects) must not cost the
                    # paid cut-off reply: the anchor prompt below continues it on the main endpoint instead. A busy
                    # server (retryable) still puts the job back in the queue.
                    if getattr(e, "retryable", True):
                        raise
                else:
                    continued = True
                    text = text + more.content
                    try:
                        parsed = parse(text)
                    except (ValueError, ValidationError) as e:
                        first = e
            if parsed is None and cut and not continued and (anchor := anchor_of(text)) is not None:
                # Other providers, or a prefix completion that failed: ask only for the missing tail instead of paying
                # for the whole reply again. Every job answers in JSON, and the seam is proved by the repeated anchor,
                # not by a page check, so this holds for translation, chat and summaries too; with no anchor, or a
                # continuation that skips it, repair runs.
                continued = True
                more = ask(ChatRequest(messages=req.messages + [
                    {"role": "assistant", "content": text}, {"role": "user", "content": continue_prompt(anchor)}],
                    model=req.model, max_tokens=req.max_tokens, json_mode=False, thinking=None, temperature=0.0))
                joined = stitch(text, more.content)
                if joined is not None:
                    try:
                        parsed = parse(joined)
                    except (ValueError, ValidationError) as e:
                        first, text = e, joined
            if parsed is None:
                repaired = True
                parsed = parse(ask(ChatRequest(messages=req.messages + [
                    {"role": "assistant", "content": text or "(empty)"},
                    {"role": "user", "content": "That reply was not valid for the schema. Errors: %s\n"
                                                "Return only the corrected JSON object." % str(first)[:1500]}],
                    model=req.model, max_tokens=req.max_tokens, json_mode=True, thinking=None, temperature=0.0)).content)
        cost = bill()
        conn.execute("BEGIN")
        spec.apply(parsed, run_id, conn, job["target_id"])
        conn.execute("UPDATE ai_jobs SET status='done', result_json=?, completed_at=?, error=NULL WHERE id=?",
                     (parsed.model_dump_json(), now_iso(), job_id))
        conn.execute("COMMIT")
        log_path = _log(job_id, run_id, {"exchanges": exchanges, "usage": total_usage, "cost_usd": cost, "continued": continued})
        queue.finish_run(conn, run_id, valid=True, usage=total_usage, cost_usd=cost, repaired=repaired, log_path=log_path)
        return run_id
    except Exception as e:  # noqa: BLE001
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        cost = bill()   # a failed run paid for every reply it got, and the daily cap has to see that
        log_path = _log(job_id, run_id, {"exchanges": exchanges, "usage": total_usage, "cost_usd": cost, "error": str(e)})
        queue.finish_run(conn, run_id, valid=False, usage=total_usage, cost_usd=cost, repaired=repaired, log_path=log_path, error=str(e)[:2000])
        deferred = _deferred(e)
        if deferred is None:
            queue.fail(conn, job_id, str(e))
            raise
        if deferred.blocks_engine:
            claude_code.block(conn, deferred.until, deferred.why)
        if deferred.counts_attempt and (job["attempts"] or 0) >= claude_code.MAX_STALLS:
            queue.fail(conn, job_id, f"{deferred}（已尝试 {job['attempts']} 次，不再自动重试）")
        else:
            queue.defer(conn, job_id, str(deferred), count_attempt=deferred.counts_attempt)
        if deferred is e:
            raise
        # the worker's sweep sets a Deferred job aside until its next round; any other error would be taken again at once
        raise deferred from e
