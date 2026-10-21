"""_data/updates.yml: 记录, the comment thread under an output (docs/design/ARCHIVE-ISSUES.md §B, §D, §H).

One entry per thing that happened: who wrote it (`user` is Leo, `assistant` the 助手, `system` the app
noting a change of maturity, version or archive state, or a finished 待办), which output it belongs to,
one sentence of plain text and when, and optionally `link`, an in-app path the line points at (the
assistant's lines link back to the conversation that made the change, docs/design/ISSUE-AGENT.md §4).
Only outputs have a thread here; tasks and 课题 keep theirs in issues.yml. Content
goes where it always went (the body, AI notes, harvests); a line here only says what happened. Leo's own entries are
comments in Markdown, up to 4000 characters, and he can edit or delete them (card 0097, the same as on an issue);
the assistant's and the app's lines are a record and are never edited. Content like tasks.yml: hand-editable, committed with the week,
written through the same ruamel round-trip and sha guard. Ids (`U-YYYY-NNNN`) never appear on a page.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString, SingleQuotedScalarString

from ..config import Paths, get_paths
from .yamlio import StaleFileError, load_yaml, save_yaml, sha256_text

AUTHORS = ("user", "assistant", "system")
FIELDS = ("id", "target", "by", "text", "at", "link", "edited")
ID_RE = re.compile(r"^U-(\d{4})-(\d{4})$")
TARGET_RE = re.compile(r"^ART-\d{4}-\d{4}$")
TEXT_MAX = 4000                     # as an issue comment (files/issues.COMMENT_MAX)
LINK_RE = re.compile(r"^/(?!/)[A-Za-z0-9_\-./?=&%#]*$")    # in-app paths only: the page renders it as href
HEADER = ("记录: one line per thing that happened to an output (ART-…).\n"
          "`by` is user | assistant | system. Leo may edit or delete his own; the others are never edited. Written by the app, safe to hand-edit.")


log = logging.getLogger("research_lab.updates")
BROKEN = "记录文件 _data/updates.yml 格式有误，请先修好这个文件再记录。"


class UpdateError(ValueError):
    """A user-facing validation failure."""


def path(paths: Paths | None = None) -> Path:
    return (paths or get_paths()).data_yml / "updates.yml"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def file_sha(paths: Paths | None = None) -> str:
    return sha256_text(_text(path(paths)))


def _plain(e: Any) -> dict[str, Any]:
    out = {k: (str(e.get(k)) if e.get(k) is not None else "") for k in FIELDS}
    if not LINK_RE.match(out["link"]):
        out["link"] = ""                   # a hand edit never becomes an off-site or javascript: href
    return out


def read(paths: Paths | None = None) -> list[dict[str, Any]]:
    """A file that does not parse reads as no entries (logged) so the pages still open; add refuses it."""
    p = path(paths)
    if not _text(p).strip():
        return []
    try:
        data = load_yaml(p)
    except Exception as e:  # noqa: BLE001 - YAML errors come in many classes
        log.warning("updates.yml does not parse, reading it as empty: %s", str(e).splitlines()[0] if str(e) else type(e).__name__)
        return []
    return [_plain(e) for e in (data if isinstance(data, list) else []) if hasattr(e, "get") and e.get("id")]


def for_target(target: str, paths: Paths | None = None) -> list[dict[str, Any]]:
    """Newest first, the way the thread reads under an output."""
    rows = [u for u in read(paths) if u["target"] == target]
    return sorted(rows, key=lambda u: (u["at"], u["id"]), reverse=True)


def latest_at(paths: Paths | None = None) -> dict[str, str]:
    """target → time of its newest entry, for the archive list's 最近记录 column."""
    out: dict[str, str] = {}
    for u in read(paths):
        if u["at"] > out.get(u["target"], ""):
            out[u["target"]] = u["at"]
    return out


def add(target: str, by: str, text: str, expected_sha: str | None = None, paths: Paths | None = None,
        now: datetime | None = None, *, link: str = "") -> tuple[str, str]:
    """Append one entry. Returns (id, new sha). Raises UpdateError, StaleFileError. Leo's text is Markdown and keeps
    its line breaks; the assistant's and the app's lines are one sentence."""
    target = (target or "").strip()
    text = _clean(text) if by == "user" else " ".join(str(text or "").split())
    if not TARGET_RE.match(target):
        raise UpdateError("记录要挂在一份产出下面。")
    if by not in AUTHORS:
        raise UpdateError("未知的作者。")
    if not text:
        raise UpdateError("写一句再提交。")
    if len(text) > TEXT_MAX:
        raise UpdateError(f"一条评论不要超过 {TEXT_MAX} 个字。")
    if link and not LINK_RE.match(link):
        raise UpdateError("记录只能链接到应用里的页面。")
    p = path(paths)
    if expected_sha is not None and sha256_text(_text(p)) != expected_sha:
        raise StaleFileError(f"{p.name} changed on disk since it was loaded")
    try:
        data = load_yaml(p) if _text(p).strip() else None
    except Exception:  # noqa: BLE001
        raise UpdateError(BROKEN) from None
    if data is not None and not isinstance(data, CommentedSeq):
        raise UpdateError(BROKEN)          # a mapping or a scalar is the user's file, not ours to replace
    if data is None or not len(data):
        data = CommentedSeq()
        data.yaml_set_start_comment(HEADER)
    now = now or datetime.now()
    n = max((int(m.group(2)) for e in data if hasattr(e, "get") and (m := ID_RE.match(str(e.get("id") or "")))
             and int(m.group(1)) == now.year), default=0)
    uid = f"U-{now.year}-{n + 1:04d}"
    e = CommentedMap()
    e["id"] = uid
    e["target"] = target
    e["by"] = by
    e["text"] = LiteralScalarString(text) if "\n" in text else text
    e["at"] = SingleQuotedScalarString(now.strftime("%Y-%m-%d %H:%M"))
    if link:
        e["link"] = link
    data.append(e)
    return uid, save_yaml(p, data, expected_sha)


def _clean(text) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")).strip()


def _own_entry(data, uid: str):
    if not isinstance(data, CommentedSeq):
        raise UpdateError(BROKEN)
    for i, e in enumerate(data):
        if hasattr(e, "get") and str(e.get("id") or "") == uid:
            if str(e.get("by") or "") != "user":
                raise UpdateError("只能修改自己的评论。")
            return i, e
    raise UpdateError("找不到这条评论。")


def edit(uid: str, text: str, expected_sha: str | None = None, paths: Paths | None = None,
         now: datetime | None = None) -> str:
    """Leo rewrites one of his entries. Returns the new sha."""
    text = _clean(text)
    if not text:
        raise UpdateError("写一句再提交。")
    if len(text) > TEXT_MAX:
        raise UpdateError(f"一条评论不要超过 {TEXT_MAX} 个字。")
    p = path(paths)
    if expected_sha is not None and sha256_text(_text(p)) != expected_sha:
        raise StaleFileError(f"{p.name} changed on disk since it was loaded")
    data = load_yaml(p) if _text(p).strip() else None
    _, e = _own_entry(data, uid)
    e["text"] = LiteralScalarString(text) if "\n" in text else text
    e["edited"] = SingleQuotedScalarString((now or datetime.now()).strftime("%Y-%m-%d %H:%M"))
    return save_yaml(p, data, expected_sha)


def delete(uid: str, expected_sha: str | None = None, paths: Paths | None = None) -> str:
    """Leo deletes one of his entries. Returns the new sha."""
    p = path(paths)
    if expected_sha is not None and sha256_text(_text(p)) != expected_sha:
        raise StaleFileError(f"{p.name} changed on disk since it was loaded")
    data = load_yaml(p) if _text(p).strip() else None
    i, _ = _own_entry(data, uid)
    del data[i]
    return save_yaml(p, data, expected_sha)


def record(target: str, text: str, paths: Paths | None = None) -> str | None:
    """A system line for a change the app made. Never fails the change it describes."""
    try:
        return add(target, "system", text, paths=paths)[0]
    except (UpdateError, StaleFileError, OSError):
        return None
