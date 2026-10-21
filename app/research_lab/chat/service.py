"""Threads and messages: the only module that writes chat_threads / chat_messages."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..db.connection import next_sequence_id, now_iso

ROLES = ("user", "assistant", "system")
TITLE_MAX = 60
CITEKEY_RE = re.compile(r"\b[a-z][a-z'-]*\d{4}[a-z]?-[a-z0-9-]+\b")


@dataclass
class Message:
    id: int
    thread_id: str
    role: str
    content: str
    card_kind: str | None
    card_ref: str | None
    job_id: str | None
    created_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Message":
        return cls(r["id"], r["thread_id"], r["role"], r["content"] or "", r["card_kind"], r["card_ref"],
                   r["job_id"], r["created_at"])


def infer_title(text: str | None, label: str | None = None) -> str:
    """First user line up to 60 characters; a slash command or an empty message takes the intent label."""
    t = " ".join((text or "").split())
    if label and (not t or t.startswith("/")):
        return label[:TITLE_MAX]
    if not t:
        return label[:TITLE_MAX] if label else "新对话"
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


def create_thread(conn: sqlite3.Connection, title: str = "新对话", project_slug: str | None = None) -> str:
    ts = now_iso()
    for _ in range(3):
        tid = next_sequence_id(conn, "chat_threads", "T")
        try:
            conn.execute("INSERT INTO chat_threads(id, title, project_slug, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (tid, title[:TITLE_MAX] or "新对话", project_slug, ts, ts))
            return tid
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a thread id")


def get_thread(conn: sqlite3.Connection, thread_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chat_threads WHERE id = ?", (thread_id,)).fetchone()


def set_title(conn: sqlite3.Connection, thread_id: str, title: str) -> None:
    conn.execute("UPDATE chat_threads SET title = ? WHERE id = ?", (title[:TITLE_MAX], thread_id))


def touch(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute("UPDATE chat_threads SET updated_at = ? WHERE id = ?", (now_iso(), thread_id))


def archive(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute("UPDATE chat_threads SET archived = 1 WHERE id = ?", (thread_id,))


def list_threads(conn: sqlite3.Connection, limit: int = 50, include_archived: bool = False) -> list[sqlite3.Row]:
    where = "" if include_archived else "WHERE archived = 0"
    # updated_at has second precision; the latest message id breaks ties between threads touched in the same second
    return conn.execute(f"SELECT t.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.thread_id = t.id) AS n_messages, "
                        f"(SELECT MAX(m.id) FROM chat_messages m WHERE m.thread_id = t.id) AS last_message_id "
                        f"FROM chat_threads t {where} ORDER BY updated_at DESC, COALESCE(last_message_id, 0) DESC, id DESC "
                        f"LIMIT ?", (limit,)).fetchall()


def add_message(conn: sqlite3.Connection, thread_id: str, role: str, content: str = "", *,
                card_kind: str | None = None, card_ref: str | None = None, job_id: str | None = None) -> Message:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    ts = now_iso()
    cur = conn.execute("INSERT INTO chat_messages(thread_id, role, content, card_kind, card_ref, job_id, created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (thread_id, role, content or "", card_kind, card_ref, job_id, ts))
    touch(conn, thread_id)
    return get_message(conn, int(cur.lastrowid))  # type: ignore[return-value]


def update_message(conn: sqlite3.Connection, message_id: int, **fields: Any) -> None:
    allowed = {"content", "card_kind", "card_ref", "job_id"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn.execute(f"UPDATE chat_messages SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), message_id))


def get_message(conn: sqlite3.Connection, message_id: int) -> Message | None:
    r = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    return Message.from_row(r) if r else None


def messages(conn: sqlite3.Connection, thread_id: str, *, limit: int | None = None, before_id: int | None = None) -> list[Message]:
    """Oldest → newest; with `limit`, the newest `limit` messages (still oldest first)."""
    sql, args = "SELECT * FROM chat_messages WHERE thread_id = ?", [thread_id]
    if before_id is not None:
        sql += " AND id < ?"
        args.append(before_id)
    if limit is None:
        rows = conn.execute(sql + " ORDER BY id", args).fetchall()
    else:
        rows = list(reversed(conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()))
    return [Message.from_row(r) for r in rows]


def latest_paper_in_thread(conn: sqlite3.Connection, thread_id: str) -> str | None:
    """The paper most recently shown as a card or named by citekey in this thread."""
    for m in reversed(messages(conn, thread_id)):
        if m.card_kind == "paper_summary" and m.card_ref:
            return m.card_ref
        if m.card_kind == "job_wait" and m.card_ref:
            job = conn.execute("SELECT kind, target_id FROM ai_jobs WHERE id = ?", (m.card_ref,)).fetchone()
            if job and job["kind"] == "paper.summarize":
                return job["target_id"]
        for key in reversed(CITEKEY_RE.findall(m.content or "")):
            if conn.execute("SELECT 1 FROM papers WHERE id = ?", (key,)).fetchone():
                return key
    return None
