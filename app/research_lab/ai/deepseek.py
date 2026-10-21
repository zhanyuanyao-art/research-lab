"""OpenAI-compatible chat provider for DeepSeek (https://api-docs.deepseek.com, checked 2026-09-29 and 2026-10-19).

Request facts relied on: POST {base}/chat/completions; `thinking: {"type": "enabled"|"disabled"}`
with a top-level `reasoning_effort` (low|high|max); thinking is on by default server-side, so it is
always sent explicitly; temperature is ignored in thinking mode; JSON mode needs
`response_format: {"type": "json_object"}` and the word "json" somewhere in the prompt. Reasoning shares
`max_tokens` with the answer and can use all of it, leaving the content empty, so a thinking request asks
for at least THINKING_MIN_TOKENS (only generated tokens are billed, so the floor costs nothing by itself).

Overload (10-19): a request the server has not started on is held open with empty keep-alive lines for up
to about 10 minutes and can then end as HTTP 200 with a body like {"error": "...unable to start processing..."};
a reply can also stop with finish_reason `insufficient_system_resource`. Both are retried like a 503 and,
once the retries are spent, raised as retryable so the runner puts the job back in the queue. An error in a 200
that names the key, the balance or the request itself (_permanent) is not an overload and fails at once. The
keep-alive lines reset httpx's read timeout, so every attempt also has an overall deadline (DEADLINE_S).

Chat prefix completion (beta): POST {base}/beta/chat/completions whose last message is
{"role": "assistant", "content": <text so far>, "prefix": true} (plus `reasoning_content` in thinking mode)
returns only what follows that text. The runner uses it for a reply cut off at max_tokens.
"""
from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import httpx

from .pricing import DEFAULT_BASE_URL, DEFAULT_MODEL
from .provider import ChatRequest, ChatResult

RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_FINISH = frozenset({"insufficient_system_resource"})
DEADLINE_S = 660.0              # the server gives up on an unstarted request after ~10 minutes; one more for the answer
THINKING_MIN_TOKENS = 32_000
# An error inside a 200 that a retry cannot fix: a bad or missing key, no balance, a request the API rejects. Without
# this an authentication error ran 3 requests × 3 queue tries (review of card 0197). Everything else in a 200 error body
# is taken for an overload, so the patterns stay narrow: words like quota, not found or permission show up in overload
# text too ("exceeded quota of concurrent requests", "server not found in pool"), so they count only in the error's
# machine-readable type or code; the free-text message counts only for whole phrases.
PERMANENT_KIND = re.compile(r"authenticat|unauthori[sz]ed|permission|forbidden|api[_ -]?key|insufficient[_ -]?balance|"
                            r"quota|billing|payment[_ -]?required|invalid[_ -]?(?:request|param|argument|model)|"
                            r"not[_ -]?found|not[_ -]?exist", re.I)
PERMANENT_MESSAGE = re.compile(r"\bapi[ _-]?key\b|\bauthentication fail|\binsufficient balance\b|\binvalid request\b",
                               re.I)
JSON_HINT = "Reply with a single json object."


class ProviderError(RuntimeError):
    """`retryable`: the server was busy or out of reach, nothing was wrong with the request; the runner puts the job
    back in the queue. `usage`: tokens of replies that came back and were thrown away for a retry (a reply stopped
    for lack of resources), so the bill still counts them."""

    def __init__(self, message: str, status: int | None = None, *, retryable: bool = False,
                 usage: dict[str, int] | None = None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.usage = usage or {}


class _Late(Exception):
    """One attempt ran past the overall deadline."""


# OpenAI-compatible request fields a caller may set through ChatRequest.params; everything else in params is job metadata.
API_PARAMS = frozenset({"tools", "tool_choice", "stop", "top_p", "presence_penalty", "frequency_penalty",
                        "logprobs", "top_logprobs", "seed"})


class OpenAICompatProvider:
    name = "deepseek"

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str = "", default_model: str = DEFAULT_MODEL,
                 transport: httpx.BaseTransport | None = None, *, timeout: float = 120.0, retries: int = 2,
                 backoff: float = 1.5, sleep: Callable[[float], None] = time.sleep, deadline: float = DEADLINE_S,
                 clock: Callable[[], float] = time.monotonic, jitter: Callable[[], float] = random.random):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key
        self.default_model = default_model or DEFAULT_MODEL
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self.deadline = deadline
        self.clock = clock
        self.jitter = jitter

    def __repr__(self) -> str:
        return f"OpenAICompatProvider({_shown(self.base_url)!r}, model={self.default_model!r})"

    @property
    def beta_url(self) -> str:
        """The beta base for prefix completion, from the configured base: https://api.deepseek.com(/v1) → …/beta."""
        return re.sub(r"/(?:v1|beta)\Z", "", self.base_url) + "/beta"

    def _client(self, base_url: str | None = None) -> httpx.Client:
        return httpx.Client(base_url=base_url or self.base_url, transport=self.transport, timeout=self.timeout,
                            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"})

    def build_body(self, req: ChatRequest) -> dict[str, Any]:
        messages = [dict(m) for m in req.messages]
        if req.json_mode and not any("json" in (m.get("content") or "").lower() for m in messages):
            last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if last_user is None:
                messages.append({"role": "user", "content": JSON_HINT})
            else:
                last_user["content"] = f"{last_user.get('content') or ''}\n\n{JSON_HINT}".lstrip()
        body: dict[str, Any] = {"model": req.model or self.default_model, "messages": messages,
                                "max_tokens": req.max_tokens, "stream": False}
        if req.thinking in (None, "off"):
            body["thinking"] = {"type": "disabled"}
            body["temperature"] = req.temperature
        else:
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = req.thinking
            body["max_tokens"] = max(req.max_tokens, THINKING_MIN_TOKENS)
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}
        # req.params carries both the task driver's API fields (tools, tool_choice) and the jobs' own metadata
        # (schema_name, ids, stream, page_range, …). Only the API fields may reach the body: feeds.enrich's
        # `stream: "news"` once replaced the boolean `stream` and DeepSeek answered 422 (card 0174).
        body.update({k: v for k, v in (req.params or {}).items() if k in API_PARAMS})
        return body

    def _send(self, client: httpx.Client, method: str, path: str, json_body: dict | None) -> tuple[httpx.Response, bytes]:
        """One attempt, read chunk by chunk so the deadline holds while keep-alive lines keep the read timeout from firing."""
        stop = self.clock() + self.deadline
        with client.stream(method, path, json=json_body) as resp:
            chunks: list[bytes] = []
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                if self.clock() > stop:
                    raise _Late()
            return resp, b"".join(chunks)

    def _call(self, method: str, path: str, json_body: dict | None = None, *,
              base_url: str | None = None) -> tuple[dict[str, Any], dict[str, int]]:
        """The parsed reply and the usage of replies discarded on the way (see ProviderError.usage). Errors name the
        full URL, so a failure on the beta host (prefix completion) reads as such."""
        last: ProviderError | None = None
        spent: dict[str, int] = {}
        url = _shown((base_url or self.base_url).rstrip("/") + path)
        with self._client(base_url) as client:
            for attempt in range(self.retries + 1):
                resp: httpx.Response | None = None
                try:
                    resp, raw = self._send(client, method, path, json_body)
                except _Late:
                    last = ProviderError(f"DeepSeek {method} {url}: no answer within {self.deadline:.0f} s", retryable=True)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = ProviderError(f"DeepSeek {method} {url} failed: {type(e).__name__}: {e}", retryable=True)
                else:
                    text = raw.decode("utf-8", "replace")
                    if resp.status_code >= 300:
                        last = ProviderError(f"DeepSeek HTTP {resp.status_code} on {url}: {text.strip()[:800]}",
                                             status=resp.status_code, retryable=resp.status_code in RETRY_STATUS)
                        if not last.retryable:
                            last.usage = spent
                            raise last
                    elif not text.strip():
                        last = ProviderError(f"DeepSeek kept {url} open and closed it without an answer",
                                             status=resp.status_code, retryable=True)
                    else:
                        try:
                            data = json.loads(text)
                        except ValueError as e:
                            raise ProviderError(f"DeepSeek {url} returned a non-JSON body: {text[:800]}", usage=spent) from e
                        problem = _problem(data)
                        if problem is None:
                            return data, spent
                        why, retryable = problem
                        _add(spent, map_usage(data.get("usage") or {}))
                        last = ProviderError(f"DeepSeek {url}: {why}", status=resp.status_code, retryable=retryable)
                        if not retryable:
                            last.usage = spent
                            raise last
                if attempt < self.retries:
                    self.sleep(self._delay(attempt, resp))
        assert last is not None
        last.usage = spent
        raise last

    def _delay(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None:
            after = resp.headers.get("retry-after")
            if after and after.replace(".", "", 1).isdigit():
                return min(float(after), 30.0)
        # 50–150 % of the exponential step, so jobs hit by one overload do not all come back at the same moment
        return self.backoff * (2 ** attempt) * (0.5 + self.jitter())

    def _result(self, body: dict[str, Any], base_url: str | None = None) -> ChatResult:
        data, spent = self._call("POST", "/chat/completions", body, base_url=base_url)
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            usage = map_usage(data.get("usage") or {}) if isinstance(data, dict) else {}
            raise ProviderError(f"DeepSeek returned no choices: {json.dumps(data, ensure_ascii=False)[:800]}",
                                usage=_add(dict(spent), usage))
        message = choices[0].get("message") or {}
        return ChatResult(content=message.get("content") or "", model=data.get("model") or body["model"],
                          usage=_add(map_usage(data.get("usage") or {}), spent), raw=data)

    def complete(self, req: ChatRequest) -> ChatResult:
        return self._result(self.build_body(req))

    def complete_prefix(self, req: ChatRequest) -> ChatResult:
        """Chat prefix completion: `req.messages[-1]` is the assistant prefix ({"prefix": True}) and the reply is only
        what follows it. Everything before it goes out exactly as build_body() sent the original request, so the prompt
        is a cache hit; JSON mode stays off (it would ask for a new object, not the rest of this one)."""
        prefix = dict(req.messages[-1])
        body = self.build_body(replace(req, messages=req.messages[:-1]))
        body.pop("response_format", None)
        if body["thinking"]["type"] != "enabled":
            prefix.pop("reasoning_content", None)
        body["messages"].append(prefix)
        return self._result(body, base_url=self.beta_url)

    def list_models(self) -> list[str]:
        data, _ = self._call("GET", "/models")
        return [m.get("id") for m in data.get("data") or [] if m.get("id")]


def _problem(data: Any) -> tuple[str, bool] | None:
    """What is wrong with a 200 and whether a retry can help, or None for a real answer."""
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if error:
        shown = json.dumps(error, ensure_ascii=False)[:300]
        if _permanent(error):
            return f"answered 200 with an error a retry cannot fix: {shown}", False
        return f"answered 200 with an error: {shown}", True
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict) \
            and choices[0].get("finish_reason") in RETRY_FINISH:
        return f"stopped with finish_reason {choices[0]['finish_reason']}", True
    return None


def _permanent(error: Any) -> bool:
    if isinstance(error, dict):
        kind = " ".join(str(error.get(k) or "") for k in ("type", "code"))[:300]
        message = str(error.get("message") or "")[:600]
    else:
        kind, message = "", str(error)[:600]
    return bool(PERMANENT_KIND.search(kind) or PERMANENT_MESSAGE.search(message))


def _shown(url: str) -> str:
    """The URL for error text, without any user:password@ part a configured base URL may carry."""
    return re.sub(r"(?<=://)[^/@]*@", "", url)


def _add(into: dict[str, int], more: dict[str, int]) -> dict[str, int]:
    for k, v in more.items():
        into[k] = into.get(k, 0) + (v or 0)
    return into


def map_usage(u: dict[str, Any]) -> dict[str, int]:
    details = u.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "cache_hit_tokens": int(u.get("prompt_cache_hit_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
    }
