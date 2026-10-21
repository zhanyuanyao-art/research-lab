"""Read/write contract for _artifacts/*.md.

Rules: front matter is edited with ruamel so diffs stay minimal; the body is an opaque
string; the app writes into the body only inside `<!-- rl:begin excerpts -->…<!-- rl:end -->`
blocks and a `## AI notes` section; every write is guarded by the sha the caller saw.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..config import Paths, get_paths
from .weeks import week_of
from .yamlio import atomic_write, dump_yaml, load_yaml, sha256_text, yaml_rt

log = logging.getLogger("research_lab.artifacts")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
PLACEHOLDER_RE = re.compile(r"^\s*(\.\.\.|…|>\s*\.\.\.|\*\*I currently think:\*\*)?\s*$")
BLOCK_BEGIN = "<!-- rl:begin excerpts -->"
BLOCK_END = "<!-- rl:end -->"
EARNED_SECTIONS = ("What did I figure out", "Claim")


@dataclass
class Artifact:
    path: Path
    front: CommentedMap
    body: str
    sha: str

    @property
    def ref(self) -> str: return str(self.front.get("ref", ""))

    def get(self, key: str, default: Any = None) -> Any:
        v = self.front.get(key, default)
        return v if v is not None else default

    def text(self) -> str:
        return "---\n" + dump_yaml(self.front) + "---\n" + self.body


def parse_text(text: str, path: Path | None = None) -> Artifact:
    m = FRONT_RE.match(text)
    if not m:
        raise ValueError(f"{path}: no front matter")
    front = yaml_rt().load(m.group(1)) or CommentedMap()
    body = text[m.end():]
    return Artifact(path or Path("<memory>"), front, body, sha256_text(text))


def load(path: Path) -> Artifact:
    return parse_text(path.read_text(encoding="utf-8"), path)


def save(a: Artifact, expected_sha: str | None = "same") -> str:
    """Write back; by default refuse if the file changed since `a` was loaded."""
    sha = a.sha if expected_sha == "same" else expected_sha
    new = atomic_write(a.path, a.text(), sha)
    a.sha = new
    return new


def sections(body: str) -> list[tuple[str, int, int]]:
    """(heading text, start offset, end offset) for every `## ` section."""
    heads = [(m.group(1).strip(), m.start()) for m in re.finditer(r"^## +(.+)$", body, re.M)]
    out = []
    for i, (h, start) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(body)
        out.append((h, start, end))
    return out


def section_body(body: str, heading_prefix: str) -> str | None:
    for h, s, e in sections(body):
        if h.lower().startswith(heading_prefix.lower()):
            return body[s:e].split("\n", 1)[1] if "\n" in body[s:e] else ""
    return None


def _strip_markup(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = text.replace("**I currently think:**", "")
    return text


def missing_earned(body: str) -> list[str]:
    """Earned sections ('What did I figure out', 'Claim') that are absent or still placeholders."""
    out = []
    for prefix in EARNED_SECTIONS:
        sec = section_body(body, prefix)
        content = _strip_markup(sec).strip() if sec is not None else ""
        if not content or all(PLACEHOLDER_RE.match(line) for line in content.splitlines()):
            out.append(prefix)
    return out


def counts(body: str) -> bool:
    """An artifact counts when 'What did I figure out' and 'Claim' hold real text."""
    return not missing_earned(body)


def append_to_section(a: Artifact, heading_prefix: str, line: str) -> None:
    """Append `line` inside the managed block at the end of a section, creating both if needed."""
    if section_body(a.body, heading_prefix) is None:
        a.body = a.body.rstrip("\n") + f"\n\n## {heading_prefix}\n\n"
    for h, s, e in sections(a.body):
        if h.lower().startswith(heading_prefix.lower()):
            sec = a.body[s:e]
            if BLOCK_BEGIN in sec:
                head, rest = sec.split(BLOCK_END, 1)
                sec = head.rstrip("\n") + "\n" + line + "\n" + BLOCK_END + rest
            else:
                sec = sec.rstrip("\n") + f"\n\n{BLOCK_BEGIN}\n{line}\n{BLOCK_END}\n\n"
            a.body = a.body[:s] + sec + a.body[e:]
            return


def set_ai_notes(a: Artifact, markdown: str) -> None:
    """Replace the `## AI notes` section (ignored by the level checker)."""
    marker = "## AI notes"
    if marker in a.body:
        for h, s, e in sections(a.body):
            if h == "AI notes":
                a.body = a.body[:s] + f"{marker}\n\n{markdown.strip()}\n" + a.body[e:]
                return
    a.body = a.body.rstrip("\n") + f"\n\n{marker}\n\n{markdown.strip()}\n"


def add_related(a: Artifact, ref: str) -> bool:
    rel = a.front.get("related")
    if rel is None:
        rel = CommentedSeq()
        a.front["related"] = rel
    if ref in rel:
        return False
    rel.append(ref)
    rel.fa.set_flow_style()
    return True


def trash_dir(paths: Paths | None = None) -> Path:
    """Deleted outputs go here (app/data/trash, outside git and outside the index), never to /dev/null."""
    return (paths or get_paths()).data / "trash"


def trash(a: Artifact, expected_sha: str, paths: Paths | None = None) -> Path:
    """Move an output's file into the trash folder; the next index() drops its row. Refuses a stale sha."""
    from datetime import datetime
    from .yamlio import StaleFileError
    current = sha256_text(a.path.read_text(encoding="utf-8"))
    if current != expected_sha:
        raise StaleFileError(str(a.path))
    folder = trash_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{a.path.name}"
    a.path.replace(target)
    return target


# path -> (mtime_ns, size, parses): each file is parsed once per change, not once per page.
_readable: dict[str, tuple[int, int, bool]] = {}


def _parses(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    key = str(path)
    hit = _readable.get(key)
    if hit and hit[:2] == (st.st_mtime_ns, st.st_size):
        return hit[2]
    try:
        load(path)
        ok = True
    except Exception as e:  # noqa: BLE001 - YAML and decoding errors come in many classes
        log.warning("skipping unreadable output %s: %s", path.name, str(e).splitlines()[0] if str(e) else type(e).__name__)
        ok = False
    _readable[key] = (st.st_mtime_ns, st.st_size, ok)
    return ok


def list_paths(paths: Paths | None = None) -> list[Path]:
    """Output files that parse. A stray note or a broken front matter in _artifacts/ is logged and
    left out, so one bad file cannot take down every page that lists outputs."""
    p = paths or get_paths()
    return [f for f in sorted(p.artifacts.glob("*.md")) if _parses(f)]


def unreadable_paths(paths: Paths | None = None) -> list[Path]:
    """The files list_paths leaves out, for checks that must not treat "unknown" as "unused"."""
    p = paths or get_paths()
    return [f for f in sorted(p.artifacts.glob("*.md")) if not _parses(f)]


def _retired_refs(p: Paths) -> set[str]:
    """Refs that once belonged to an output: in the trash, or still named by the sidecar files
    (issue fields and comments, 记录, 待办 sources). A new output must not inherit what they hold."""
    refs: set[str] = set()
    trash = trash_dir(p)
    for path in sorted(trash.glob("*.md")) if trash.is_dir() else []:
        try:
            refs.add(load(path).ref)
        except Exception:  # noqa: BLE001
            continue
    for name, field in (("issues.yml", None), ("updates.yml", "target"), ("tasks.yml", "source")):
        f = p.data_yml / name
        try:
            data = load_yaml(f) if f.exists() else None
        except Exception:  # noqa: BLE001 - a broken sidecar is reported where it is read
            continue
        if field is None:
            refs.update(str(k) for k in (data.keys() if hasattr(data, "keys") else []))
        elif isinstance(data, list):
            refs.update(str(e.get(field)) for e in data if hasattr(e, "get") and e.get(field))
    return refs


def next_ref(paths: Paths | None = None, week: str | None = None) -> str:
    """ART-YYYY-WWNN: max sequence for the week + 1 over live files and retired refs, so a deleted
    output's number is never handed out again."""
    p = paths or get_paths()
    week = week or week_of()
    y, w = week.split("-W")
    prefix = f"ART-{y}-{w}"
    refs = _retired_refs(p)
    for path in list_paths(p):
        try:
            refs.add(load(path).ref)
        except Exception:  # noqa: BLE001
            continue
    seq = max((int(r[-2:]) for r in refs if r.startswith(prefix) and len(r) == len(prefix) + 2 and r[-2:].isdigit()),
              default=0)
    return f"{prefix}{seq + 1:02d}"


def project_directions(paths: Paths | None = None) -> dict[str, str]:
    """project slug → direction slug, from _data/projects.yml."""
    from . import datafiles
    return {str(x.get("slug")): str(x.get("direction") or "") for x in (datafiles.read("projects", paths) or [])}


def set_front(a: Artifact, key: str, value: Any, after: str | None = None) -> bool:
    """Set one front-matter key in place (comments and order kept); a new key goes after `after`.
    None removes the key, so a cleared optional field (direction, archived_from) leaves no `null`."""
    front = a.front
    if value is None:
        if key not in front:
            return False
        del front[key]
        return True
    if key in front:
        if front.get(key) == value:
            return False
        front[key] = value
        return True
    keys = list(front.keys())
    pos = keys.index(after) + 1 if after in keys else len(keys)
    front.insert(pos, key, value)
    return True


def create(type_: str, slug: str, project: str, title: str | None = None,
           week: str | None = None, level: str = "seed", paths: Paths | None = None,
           direction: str | None = None) -> Path:
    p = paths or get_paths()
    week = week or week_of()
    path = p.artifacts / f"{week}-{slug}.md"
    if path.exists():
        raise FileExistsError(path)
    tpl = load(p.templates / "artifact.md")
    tpl.front["ref"] = next_ref(p, week)
    tpl.front["title"] = title or slug.replace("-", " ").capitalize()
    tpl.front["week"] = week
    tpl.front["type"] = type_
    tpl.front["level"] = level
    tpl.front["project"] = project
    set_front(tpl, "direction", direction or project_directions(p).get(project, "") or None, after="project")
    tpl.front["created"] = date.today().isoformat()
    tpl.path = path
    atomic_write(path, tpl.text())
    from . import issues       # outputs are issues too: they take the next key of their direction
    issues.assign_key(str(tpl.front["ref"]), str(tpl.front.get("direction") or ""), paths)
    return path


def index(conn: sqlite3.Connection, paths: Paths | None = None) -> int:
    """Refresh the derived `artifacts` table from disk. Returns number of rows (re)written."""
    p = paths or get_paths()
    seen, written = set(), 0
    known = {r["path"]: (r["mtime"], r["sha256"]) for r in conn.execute("SELECT path, mtime, sha256 FROM artifacts")}
    proj_dir = project_directions(p)
    for path in list_paths(p):
        rel = str(path.relative_to(p.root))
        mtime = path.stat().st_mtime
        seen.add(rel)
        if rel in known and known[rel][0] == mtime:
            continue
        try:
            a = load(path)
        except Exception:
            continue
        f = a.front
        related = [str(x) for x in (f.get("related") or [])]
        direction = str(f.get("direction") or "").strip()
        source = "front" if direction else "project"
        if not direction:
            direction = proj_dir.get(str(f.get("project", "")), "")
        conn.execute(
            "INSERT INTO artifacts(ref, path, title, week, type, level, project, status, version, created, related_json, body, counts, sha256, mtime, direction, direction_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET path=excluded.path, title=excluded.title, week=excluded.week, "
            "type=excluded.type, level=excluded.level, project=excluded.project, status=excluded.status, version=excluded.version, created=excluded.created, "
            "related_json=excluded.related_json, body=excluded.body, counts=excluded.counts, sha256=excluded.sha256, mtime=excluded.mtime, "
            "direction=excluded.direction, direction_source=excluded.direction_source",
            (a.ref or rel, rel, str(f.get("title", "")), str(f.get("week", "")), str(f.get("type", "")), str(f.get("level", "")),
             str(f.get("project", "")), str(f.get("status", "")), str(f.get("version", "")), str(f.get("created", "")),
             json.dumps(related), a.body, int(counts(a.body)), a.sha, mtime, direction or None, source),
        )
        written += 1
    for rel in set(known) - seen:
        conn.execute("DELETE FROM artifacts WHERE path = ?", (rel,))
    # Artifacts without their own `direction` follow their project's, which may have changed since.
    for slug, d in proj_dir.items():
        conn.execute("UPDATE artifacts SET direction = ? WHERE direction_source = 'project' AND project = ? "
                     "AND COALESCE(direction, '') != ?", (d or None, slug, d))
    return written


def backlinks(conn: sqlite3.Connection, ref: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT ref, title, week, type FROM artifacts WHERE related_json LIKE ? AND ref != ? ORDER BY ref",
        (f'%"{ref}"%', ref)).fetchall()


# ---------------------------------------------------------------- in-app editor: protected regions
#
# The editor edits the body the user owns. Two regions belong to the app and must survive any editor
# round trip byte for byte: the `## AI notes` section (never sent to the editor, re-attached on save)
# and every `<!-- rl:begin excerpts -->…<!-- rl:end -->` block (sent inline so the user sees where it
# sits, then restored from disk on save, because a WYSIWYG round trip can reflow or drop comments).

AI_NOTES = "AI notes"
BLOCK_RE = re.compile(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END), re.S)


def split_ai_notes(body: str) -> tuple[str, str, str | None]:
    """(body without the AI notes section, the section text or '', heading that followed it or None)."""
    secs = sections(body)
    for i, (h, s, e) in enumerate(secs):
        if h == AI_NOTES:
            before, after = body[:s].rstrip("\n"), body[e:]
            nxt = secs[i + 1][0] if i + 1 < len(secs) else None
            rest = (before + "\n\n" + after) if after else (before + "\n" if before else "")
            return rest, body[s:e], nxt
    return body, "", None


def editable_body(body: str) -> str:
    return split_ai_notes(body)[0]


def ai_notes(body: str) -> str:
    return split_ai_notes(body)[1]


def managed_blocks(body: str) -> list[tuple[str | None, str]]:
    """(heading of the enclosing `## ` section or None, block text) for every managed block."""
    out = []
    secs = sections(body)
    for m in BLOCK_RE.finditer(body):
        head = None
        for h, s, e in secs:
            if s <= m.start() < e:
                head = h
        out.append((head, m.group(0)))
    return out


def _loose(line: str) -> str:
    line = re.sub(r"<!--.*?-->", "", line)
    line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
    line = line.replace("\\", "")
    return " ".join(line.split())


def merge_protected(disk_body: str, edited: str) -> str:
    """The body to write: the user's edit with the app-owned regions of `disk_body` restored."""
    edited = edited.replace("\r\n", "\n")
    edited = split_ai_notes(edited)[0]
    _, notes, notes_next = split_ai_notes(disk_body)
    disk_blocks = managed_blocks(disk_body)
    # 1. blocks still present (markers intact) get their disk text back, matched by position
    i = 0

    def restore(m: re.Match) -> str:
        nonlocal i
        i += 1
        return disk_blocks[i - 1][1] if i <= len(disk_blocks) else m.group(0)

    edited = BLOCK_RE.sub(restore, edited)
    # 2. blocks whose markers the editor dropped: remove their loose lines, re-insert the block at the
    #    end of the section it lived in (or at the end of the body)
    for head, text in disk_blocks[i:]:
        inner = {_loose(l) for l in text.split("\n")[1:-1] if _loose(l)}
        if inner:
            edited = "\n".join(l for l in edited.split("\n") if _loose(l) not in inner or not _loose(l))
        target = None
        for h, s, e in sections(edited):
            if head is not None and h == head:
                target = (s, e)
        if target:
            s, e = target
            sec = edited[s:e].rstrip("\n")
            edited = edited[:s] + sec + "\n\n" + text + "\n\n" + edited[e:].lstrip("\n")
        else:
            edited = edited.rstrip("\n") + "\n\n" + text + "\n"
    if notes:
        anchor = next((s for h, s, _e in sections(edited) if notes_next is not None and h == notes_next), None)
        if anchor is not None:
            edited = edited[:anchor].rstrip("\n") + "\n\n" + notes.rstrip("\n") + "\n\n" + edited[anchor:]
        else:
            edited = edited.rstrip("\n") + "\n\n" + notes.rstrip("\n") + "\n"
    elif not edited.endswith("\n"):
        edited += "\n"
    return edited


def save_body(a: Artifact, edited: str, expected_sha: str) -> str:
    """Replace the body from the editor, keeping protected regions. StaleFileError if the file moved."""
    if a.sha != expected_sha:
        from .yamlio import StaleFileError
        raise StaleFileError(f"{a.path.name} changed on disk since it was loaded")
    a.body = merge_protected(a.body, edited)
    return save(a, expected_sha)
