"""LLM provider protocol and the job-spec contract every AI job implements.

A JobSpec builds the exact chat request (so DeepSeek and Claude-Code-as-agent see the
same prompt), declares a pydantic Result schema, and applies a validated result to the
database as `origin='ai-suggested'` rows only.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]   # role/content strings; a prefix-completion message also carries prefix: True
    model: str | None = None          # None = provider default from settings
    max_tokens: int = 4000
    json_mode: bool = True
    thinking: str | None = None       # None/'off' | 'low' | 'high' | 'max'
    temperature: float = 0.2
    params: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"messages": self.messages, "model": self.model, "max_tokens": self.max_tokens,
                "json_mode": self.json_mode, "thinking": self.thinking, "temperature": self.temperature,
                "params": self.params}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "ChatRequest":
        return cls(messages=d["messages"], model=d.get("model"), max_tokens=d.get("max_tokens", 4000),
                   json_mode=d.get("json_mode", True), thinking=d.get("thinking"),
                   temperature=d.get("temperature", 0.2), params=d.get("params", {}))


@dataclass
class ChatResult:
    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)   # prompt_tokens, cache_hit_tokens, completion_tokens, reasoning_tokens
    raw: dict[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    """A provider may also offer `complete_prefix(req)`, where req.messages[-1] is an assistant message marked
    {"prefix": True} and the reply is only its continuation (DeepSeek). The runner continues a cut-off reply that way
    when it can, and by the anchor prompt otherwise."""
    name: str

    def complete(self, req: ChatRequest) -> ChatResult: ...


class JobSpec:
    """Subclass per job kind in research_lab/ai/jobs/<name>.py and call `register()`."""

    kind: ClassVar[str]
    title: ClassVar[str] = ""
    Result: ClassVar[type[BaseModel]]
    target_type: ClassVar[str] = ""
    thinking: ClassVar[str | None] = None

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        """Return the request and a list of input file paths kept outside request_json."""
        raise NotImplementedError

    def apply(self, result: BaseModel, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        """Write ai-suggested rows. Never quotes, never evidence status, never Claim prose."""
        raise NotImplementedError

    def dedupe_key(self, target_id: str) -> str | None:
        return f"{self.kind}:{target_id}"

    def example(self) -> dict[str, Any]:
        """A filled example shown to Claude Code with the schema; override per job."""
        return {}


SYSTEM_RULES = (
    "You are the research assistant inside Leo's Research Lab, a PhD workbench for International "
    "Relations of East Asia. Work only from the material given in this request. Never invent sources, "
    "quotations, dates or facts about cases; when the material does not support an answer, return an "
    "empty list or null. Label every claim you draft with a strength (seed or developed). Write English. "
    "Reply with a single JSON object matching the schema in the request and nothing else. Inside text values "
    "quote with 「」 in Chinese and “” in English, never a bare \" (it breaks the JSON)."
)
