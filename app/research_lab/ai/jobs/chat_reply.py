"""`chat.reply`: answer a free-text chat message from the lab's own records.

The target is the placeholder assistant message (chat_messages.id). apply() only fills that
message: it never writes artifacts, excerpts, claims or any status. Display only.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ...db.connection import now_iso
from ...files import datafiles
from .. import registry
from ..provider import ChatRequest, JobSpec, system_rules

MAX_WORDS = 250
HISTORY = 10
RELATED = 8
SNIPPET = 400
CHAT_RULE = ("You answer inside the user's research workbench. Ground every statement in the context blocks; when the "
             "context does not contain it, say so and suggest the command that would fetch it. Never invent sources, "
             "quotes or case facts.")
CardKind = Literal["paper_summary", "association", "hunt", "week", "search", "excerpt", "artifacts", "milestones"]


class Card(BaseModel):
    kind: CardKind
    ref: str


class ChatReplyResult(BaseModel):
    reply: str
    card: Card | None = None
    suggested_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _no_quotes(cls, data: Any) -> Any:
        # method-layer rule 2: a quote can only come from the user
        if isinstance(data, dict) and ("quote" in data or "quotes" in data):
            raise ValueError("an AI result may not carry a quote field")
        return data

    @field_validator("reply")
    @classmethod
    def _word_cap(cls, v: str) -> str:
        tokens = re.findall(r"\S+\s*", v.strip())
        return v.strip() if len(tokens) <= MAX_WORDS else "".join(tokens[:MAX_WORDS]).rstrip() + " …"

    @field_validator("suggested_commands")
    @classmethod
    def _commands(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c.strip().startswith("/")][:3]


def _message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (int(message_id) if str(message_id).isdigit() else -1,)).fetchone()
    if row is None:
        raise LookupError(f"no chat message {message_id}")
    return row


def _now_block() -> list[str]:
    try:
        now = datafiles.read("now") or {}
    except Exception:  # noqa: BLE001
        now = {}
    lines = ["## now.yml"]
    for key in ("question", "puzzle", "project", "this_week", "next_week"):
        if now.get(key):
            lines.append(f"- {key}: {now[key]}")
    for item in (now.get("need_to_find_out") or [])[:5]:
        lines.append(f"- need_to_find_out: {item}")
    return lines if len(lines) > 1 else ["## now.yml", "(empty)"]


def _today_block(conn: sqlite3.Connection) -> list[str]:
    from ... import dashboard
    try:
        d = dashboard.build(conn)
    except Exception as e:  # noqa: BLE001 - context is best effort
        return ["## Today", f"(unavailable: {e})"]
    lines = ["## Today", f"- week: {d['current_week']}"]
    a = d.get("week_artifact")
    lines.append(f"- this week's artifact: {a['ref']} · {a['title']} · {a['type']} · {a['level']}"
                 + ("" if a["counts"] else " · claim empty") if a else "- this week's artifact: none yet")
    lines += [f"- action: {x['text']}" for x in d.get("actions") or []]
    return lines


def _related_block(conn: sqlite3.Connection, text: str) -> list[str]:
    from ...search.related import related
    try:
        cands = related(conn, text=text, limit=RELATED) if text.strip() else []
    except Exception as e:  # noqa: BLE001
        return ["## Related records", f"(unavailable: {e})"]
    if not cands:
        return ["## Related records", "(none matched)"]
    out = ["## Related records (excerpts and artifacts; cite by id)"]
    for c in cands:
        out.append(f"- id: {c.id} · type: {c.type} · {c.title}\n  {' '.join(c.snippet.split())[:SNIPPET]}")
    return out


def _history_block(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[list[str], str]:
    rows = list(reversed(conn.execute("SELECT role, content, card_kind, card_ref FROM chat_messages WHERE thread_id = ? AND id < ? "
                                      "ORDER BY id DESC LIMIT ?", (row["thread_id"], row["id"], HISTORY)).fetchall()))
    question = next((r["content"] for r in reversed(rows) if r["role"] == "user"), "")
    out = ["## Conversation (oldest first; the last user message is the one to answer)"]
    for r in rows:
        body = " ".join((r["content"] or "").split())[:1200] or "(empty)"
        # 'page' marks where Leo sent from, for the issue tools (chat/router.py): not part of the talk
        card = f" [card {r['card_kind']}: {r['card_ref']}]" if r["card_kind"] and r["card_kind"] != "page" else ""
        out.append(f"- {r['role']}: {body}{card}")
    return out, question


class ChatReply(JobSpec):
    kind = "chat.reply"
    title = "Answer a chat message from the lab's records"
    target_type = "message"
    Result = ChatReplyResult

    def build(self, target_id: str, conn: sqlite3.Connection) -> tuple[ChatRequest, list[str]]:
        from ...chat.router import COMMANDS
        row = _message(conn, target_id)
        history, question = _history_block(conn, row)
        parts = [
            *_now_block(), "",
            *_today_block(conn), "",
            *_related_block(conn, question), "",
            *history, "",
            "## Commands the user can run in the composer",
            *[f"- {u} — {d}" for u, d in COMMANDS], "",
            "## Task", TASK,
        ]
        req = ChatRequest(messages=[{"role": "system", "content": system_rules() + " " + CHAT_RULE},
                                    {"role": "user", "content": "\n".join(parts)}],
                          max_tokens=1200, temperature=0.3)
        return req, []

    def apply(self, result: ChatReplyResult, run_id: int, conn: sqlite3.Connection, target_id: str) -> None:
        row = _message(conn, target_id)
        card_kind, card_ref = (result.card.kind, result.card.ref.strip()) if result.card else (None, None)
        if card_kind and not _card_exists(conn, card_kind, card_ref or ""):
            card_kind, card_ref = None, None
        conn.execute("UPDATE chat_messages SET content = ?, card_kind = ?, card_ref = ? WHERE id = ?",
                     (result.reply, card_kind, card_ref, row["id"]))
        conn.execute("UPDATE chat_threads SET updated_at = ? WHERE id = ?", (now_iso(), row["thread_id"]))

    def example(self) -> dict[str, Any]:
        return {
            "reply": "Your W40 landscape still has an empty great-power acceptance row, and none of the related "
                     "excerpts speaks to it. The closest record is EXC-2026-0014 on EAS attendance, which bears on "
                     "the chair rule rather than acceptance. Run `/search great-power acceptance` to check the page text.",
            "card": {"kind": "excerpt", "ref": "EXC-2026-0014"},
            "suggested_commands": ["/search great-power acceptance", "/hunt"],
        }


def _card_exists(conn: sqlite3.Connection, kind: str, ref: str) -> bool:
    if not ref:
        return False
    if kind == "paper_summary":
        return bool(conn.execute("SELECT 1 FROM papers WHERE id = ?", (ref,)).fetchone())
    if kind in ("excerpt", "association"):
        return bool(conn.execute("SELECT 1 FROM excerpts WHERE id = ?", (ref,)).fetchone())
    return True


TASK = f"""Answer the last user message in the conversation above.
- reply: markdown, at most {MAX_WORDS} words. Use only the context blocks; cite records by their ids (EXC-…, ART-…, paper citekeys). When the context does not hold the answer, say so plainly and name the command that would fetch it.
- card: optionally one record to show under the reply: {{"kind": one of paper_summary (ref paper id), association or excerpt (ref EXC id), hunt or milestones (ref project slug), week (ref ISO week), search (ref query), artifacts (ref project slug, ART ref or "all"), "ref": "..."}}. Only ids that appear above. null when none helps.
- suggested_commands: up to 3 commands from the list above, filled in (e.g. "/search chair rule").
Do not include a quote field, do not quote the user's excerpts at length, and do not propose edits to artifacts or statuses: this reply is display only."""


registry.register(ChatReply())
