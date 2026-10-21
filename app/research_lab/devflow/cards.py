"""Cards: one Markdown file per piece of work in dev/cards/, front matter + free text.

The cards are the only source of truth; the board is computed from them, so parallel sessions never
fight over one board file. dev/ lives in the content root (the main checkout), like research content:
`rl dev` run from any worktree writes there, and the integrator commits it.
"""
from __future__ import annotations

import fnmatch
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from ..config import get_paths

STATUSES = ("planned", "coding", "testing", "review", "done", "blocked", "dropped")
ACTIVE = ("coding", "testing", "review")
LABEL = {"planned": "待做", "coding": "开发中", "testing": "测试中", "review": "评审中", "done": "已完成",
         "blocked": "受阻", "dropped": "已放弃"}
FIELDS = ("id", "title", "status", "priority", "level", "owner", "branch", "worktree", "files", "tier", "source",
          "test", "review", "created", "updated")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


class CardError(ValueError):
    pass


def dev_dir() -> Path:
    return get_paths().root / "dev"


def cards_dir() -> Path:
    return dev_dir() / "cards"


def _yaml() -> YAML:
    y = YAML()
    y.default_flow_style = None
    y.width = 120
    return y


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "card"


@dataclass
class Card:
    path: Path
    meta: dict[str, Any]
    body: str = ""

    @property
    def id(self) -> str:
        return str(self.meta.get("id"))

    @property
    def status(self) -> str:
        return str(self.meta.get("status") or "planned")

    @property
    def files(self) -> list[str]:
        return [str(f) for f in self.meta.get("files") or []]

    def get(self, key: str, default: Any = None) -> Any:
        return self.meta.get(key, default)

    def view(self) -> dict[str, Any]:
        d = {k: self.meta.get(k) for k in FIELDS}
        d.update(status_label=LABEL.get(self.status, self.status), path=str(self.path.name), body=self.body)
        return d


def parse(path: Path) -> Card:
    text = path.read_text(encoding="utf-8")
    m = _FRONT.match(text)
    if not m:
        raise CardError(f"{path.name}: missing front matter")
    meta = _yaml().load(m.group(1)) or {}
    return Card(path, dict(meta), m.group(2))


def write(card: Card) -> None:
    buf = io.StringIO()
    ordered = {k: card.meta[k] for k in FIELDS if k in card.meta}
    ordered.update({k: v for k, v in card.meta.items() if k not in ordered})
    _yaml().dump(ordered, buf)
    card.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = card.path.with_suffix(".tmp")
    tmp.write_text(f"---\n{buf.getvalue()}---\n{card.body.lstrip(chr(10))}", encoding="utf-8")
    tmp.replace(card.path)


def all_cards() -> list[Card]:
    d = cards_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("[0-9][0-9][0-9][0-9]-*.md")):
        try:
            out.append(parse(p))
        except CardError:
            continue
    return out


def get(card_id: str) -> Card:
    cid = str(card_id).zfill(4)
    for c in all_cards():
        if c.id == cid:
            return c
    raise CardError(f"没有卡片 {card_id}")


def next_id() -> str:
    ids = [int(c.id) for c in all_cards() if c.id.isdigit()]
    return str(max(ids, default=0) + 1).zfill(4)


TEMPLATE = """
## 背景

{why}

## 验收标准

- [ ] {done}

## 说明（coder 填）

## 测试（tester 填）

## 评审（reviewer 填）
"""


def new(title: str, *, files: list[str] | None = None, tier: str = "standard", priority: str = "P2",
        source: str = "", why: str = "", done: str = "", level: str | None = None) -> Card:
    check_level(level)
    cid = next_id()
    card = Card(cards_dir() / f"{cid}-{slugify(title)}.md",
                {"id": cid, "title": title, "status": "planned", "priority": priority, "level": level, "owner": None,
                 "branch": None, "worktree": None, "files": list(files or []), "tier": tier, "source": source,
                 "test": None, "review": None, "created": _now(), "updated": _now()},
                TEMPLATE.format(why=why or "（为什么要做，用户原话或反馈出处）", done=done or "（做完时能检查的结果）"))
    write(card)
    return card


LEVELS = ("L0", "L1", "L2")


def check_level(level: Any) -> None:
    if level not in (None, *LEVELS):
        raise CardError(f"等级只能是 {', '.join(LEVELS)}（L0 小改 / L1 普通 / L2 重要）")


def _fresh(card: Card) -> Card:
    """Re-read the card from disk before writing it, so a write never drops what another session wrote
    since `card` was loaded (a review recorded while a check ran, a note appended meanwhile); only the
    fields this write sets are taken from the caller."""
    try:
        disk = parse(card.path)
    except (OSError, CardError):
        return card
    card.meta, card.body = disk.meta, disk.body
    return card


def update(card: Card, **fields: Any) -> Card:
    if "status" in fields and fields["status"] not in STATUSES:
        raise CardError(f"状态只能是 {', '.join(STATUSES)}")
    if "level" in fields:
        check_level(fields["level"])
    _fresh(card)
    card.meta.update(fields)
    card.meta["updated"] = _now()
    write(card)
    return card


def append_section(card: Card, heading: str, text: str) -> None:
    """Append under `## heading` (creating it at the end when missing)."""
    _fresh(card)
    marker = f"## {heading}"
    stamp = f"\n{text.rstrip()}\n"
    if marker in card.body:
        head, rest = card.body.split(marker, 1)
        nxt = rest.find("\n## ")
        section, tail = (rest, "") if nxt == -1 else (rest[:nxt], rest[nxt:])
        card.body = head + marker + section.rstrip() + "\n" + stamp + tail
    else:
        card.body = card.body.rstrip() + f"\n\n{marker}\n{stamp}"
    write(card)


def _match(x: str, y: str) -> bool:
    if x == y or fnmatch.fnmatch(x, y) or fnmatch.fnmatch(y, x):
        return True
    # "dir/**" claims everything under dir/
    return any(p.endswith("/**") and q.startswith(p[:-2]) for p, q in ((x, y), (y, x)))


def overlaps(a: list[str], b: list[str]) -> list[str]:
    """Declared paths that two cards share (globs allowed: app/research_lab/feeds/**)."""
    return sorted({x if len(x) >= len(y) else y for x in a for y in b if _match(x, y)})


def conflicts(card: Card) -> list[tuple[Card, list[str]]]:
    """Active cards (coding/testing/review) that declared any of this card's files."""
    out = []
    for other in all_cards():
        if other.id == card.id or other.status not in ACTIVE:
            continue
        shared = overlaps(card.files, other.files)
        if shared:
            out.append((other, shared))
    return out


def board() -> dict[str, list[Card]]:
    cols: dict[str, list[Card]] = {s: [] for s in STATUSES}
    for c in all_cards():
        cols.setdefault(c.status, []).append(c)
    for s in cols:
        cols[s].sort(key=lambda c: (str(c.get("priority") or "P9"), c.id))
    return cols
