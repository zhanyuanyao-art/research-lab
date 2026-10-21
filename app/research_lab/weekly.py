"""This-week service: the week's artifact file, the excerpt basket, the draft job hand-off,
user-initiated inserts into Evidence, front-matter edits, and the explicit local git commit.

Writes to artifact bodies happen only through `artifacts.append_to_section` (Evidence, on a user
click) and full-text saves the user typed; both carry the sha the user saw.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Paths, get_paths
from .db.connection import now_iso, set_setting
from .files import artifacts, datafiles
from .files.artifacts import Artifact
from .files.weeks import week_of, week_start
from .files.yamlio import StaleFileError, sha256_text

MARKER_RE = re.compile(r"<!--\s*rl:(EXC-\d{4}-\d{4})\s*-->")
EXC_REF_RE = re.compile(r"EXC-\d{4}-\d{4}")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STATUSES = ("draft", "active", "superseded", "absorbed-into-paper")
ARCHIVED = ("superseded", "absorbed-into-paper")   # what 归档 writes; the archive list hides these
VERSION_RE = re.compile(r"^v(\d+)\.(\d+)$")
LEVEL_ORDER = {"seed": 1, "developed": 2, "paper-ready": 3}
CONTENT_DIRS = ("_artifacts", "_data", "_harvests", "projects")
SELECTION_KEY = "artifact.draft.selection:{ref}"


class WeeklyError(ValueError):
    """A user-facing validation failure (shown on the page, status 400)."""


# ---------------------------------------------------------------- artifact lookup

def row_for(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM artifacts WHERE ref = ?", (ref,)).fetchone()


def load_ref(conn: sqlite3.Connection, ref: str, paths: Paths | None = None) -> Artifact | None:
    p = paths or get_paths()
    artifacts.index(conn, p)
    row = row_for(conn, ref)
    if row is None:
        return None
    path = p.root / row["path"]
    return artifacts.load(path) if path.exists() else None


def week_row(conn: sqlite3.Connection, week: str | None = None, paths: Paths | None = None) -> sqlite3.Row | None:
    """Latest ref for the ISO week, from a fresh index."""
    artifacts.index(conn, paths or get_paths())
    return conn.execute("SELECT * FROM artifacts WHERE week = ? ORDER BY ref DESC LIMIT 1",
                        (week or week_of(),)).fetchone()


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].strip("-")


def create_artifact(type_: str, slug: str, title: str, level: str, project: str,
                    paths: Paths | None = None, direction: str | None = None) -> Path:
    p = paths or get_paths()
    types = [str(t["key"]) for t in datafiles.read("types", p) or []]
    levels = [str(l["key"]) for l in datafiles.read("levels", p) or []]
    projects = [str(x["slug"]) for x in datafiles.read("projects", p) or []]
    slug = (slug or "").strip() or slugify(title)
    if types and type_ not in types:
        raise WeeklyError(f"未知类型：{type_}")
    if levels and level not in levels:
        raise WeeklyError(f"未知成熟度：{level}")
    if projects and project not in projects:
        raise WeeklyError(f"未知项目：{project}")
    if not SLUG_RE.match(slug):
        raise WeeklyError("Slug 只能用小写字母、数字和连字符，例如 research-landscape。")
    directions = [str(d["slug"]) for d in datafiles.read("directions", p) or []]
    if direction and directions and direction not in directions:
        raise WeeklyError(f"未知研究方向：{direction}")
    try:
        return artifacts.create(type_, slug, project, title=(title or "").strip() or None, level=level, paths=p,
                                direction=direction or None)
    except FileExistsError as e:
        raise WeeklyError("同名的产出已存在，换一个短名。") from e


# ---------------------------------------------------------------- saving with the sha guard

def diff_text(current: str, proposed: str, current_label: str = "on disk", proposed_label: str = "your edit") -> str:
    return "".join(difflib.unified_diff(current.splitlines(keepends=True), proposed.splitlines(keepends=True),
                                        fromfile=current_label, tofile=proposed_label))


def conflict(path: Path, proposed: str) -> dict[str, Any]:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"current": current, "proposed": proposed, "diff": diff_text(current, proposed),
            "current_sha": sha256_text(current)}


def save_text(path: Path, text: str, expected_sha: str) -> str:
    """Save a full artifact file typed by the user. StaleFileError if the file moved on."""
    text = text.replace("\r\n", "\n")
    try:
        a = artifacts.parse_text(text, path)
    except Exception as e:  # noqa: BLE001 - YAML errors come in many classes
        raise WeeklyError(f"Front matter 无法解析：{e}") from e
    return artifacts.save(a, expected_sha)


# ---------------------------------------------------------------- basket

def _first_author(authors_json: str | None) -> str:
    try:
        authors = json.loads(authors_json or "[]")
    except ValueError:
        return ""
    if not authors:
        return ""
    a = authors[0]
    if isinstance(a, dict):
        return str(a.get("family") or a.get("last") or a.get("name") or "").strip()
    name = str(a).strip()
    return name.split(",")[0].strip() if "," in name else (name.split()[-1] if name.split() else "")


def citation(row: sqlite3.Row | dict) -> str:
    r = dict(row)
    author = _first_author(r.get("authors_json")) or (r.get("paper_id") or "")
    year = r.get("year") or "n.d."
    loc = str(r.get("locator") or "").strip()
    if loc and loc[0].isdigit():
        loc = f"p. {loc}"
    return f"{author} ({year}{', ' + loc if loc else ''})"


def markers(body: str) -> list[str]:
    return list(dict.fromkeys(MARKER_RE.findall(body or "")))


def week_excerpts(conn: sqlite3.Connection, week: str, body: str | None = None,
                  ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Excerpts created this ISO week, plus any referenced by rl markers in the file (or by id)."""
    start = week_start(week)
    end = start + timedelta(days=7)
    marked = markers(body or "")
    wanted = list(dict.fromkeys([*marked, *(ids or [])]))
    sql = ("SELECT e.*, p.title AS paper_title, p.year, p.authors_json, p.metadata_status "
           "FROM excerpts e JOIN papers p ON p.id = e.paper_id "
           "WHERE e.status != 'discarded' AND ((e.created_at >= ? AND e.created_at < ?)")
    # Stored timestamps are UTC; convert each local boundary separately for DST weeks.
    args: list[Any] = [datetime.combine(day, time.min).astimezone(timezone.utc).isoformat()
                       for day in (start, end)]
    if wanted:
        sql += f" OR e.id IN ({','.join('?' * len(wanted))})"
        args += wanted
    sql += ") ORDER BY e.created_at, e.id"
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = dict(r)
        d["citation"] = citation(r)
        d["in_file"] = d["id"] in marked
        out.append(d)
    return out


def basket(conn: sqlite3.Connection, week: str, body: str | None = None) -> list[dict[str, Any]]:
    """Groups of the week's excerpts: one per accepted claim that cites them, then 'unassigned'."""
    items = week_excerpts(conn, week, body)
    claims = conn.execute("SELECT id, code, statement, strength, source_excerpts_json FROM claims "
                          "WHERE status = 'accepted' ORDER BY id").fetchall()
    groups: dict[int, dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for ex in items:
        home = None
        for c in claims:
            try:
                srcs = json.loads(c["source_excerpts_json"] or "[]")
            except ValueError:
                srcs = []
            if ex["id"] in srcs:
                home = c
                break
        if home is None:
            unassigned.append(ex)
        else:
            groups.setdefault(home["id"], {"claim": dict(home), "excerpts": []})["excerpts"].append(ex)
    out = list(groups.values())
    if unassigned:
        out.append({"claim": None, "excerpts": unassigned})
    return out


# ---------------------------------------------------------------- draft job hand-off

def start_draft(conn: sqlite3.Connection, ref: str, excerpt_ids: list[str]) -> str:
    """Store the selection, enqueue `artifact.draft`, hand it to the dispatcher when present."""
    from .ai import queue
    ids = [i for i in dict.fromkeys(excerpt_ids) if EXC_REF_RE.fullmatch(i)]
    if not ids:
        raise WeeklyError("请先至少勾选一条划线。")
    known = {r[0] for r in conn.execute(f"SELECT id FROM excerpts WHERE id IN ({','.join('?' * len(ids))})", ids)}
    ids = [i for i in ids if i in known]
    if not ids:
        raise WeeklyError("所选划线都不存在。")
    artifacts.index(conn, get_paths())
    if row_for(conn, ref) is None:
        raise WeeklyError(f"没有 artifact {ref}。")
    # A new selection replaces a draft nobody has picked up yet; a claimed one keeps running.
    for j in conn.execute("SELECT id, status FROM ai_jobs WHERE kind = 'artifact.draft' AND target_id = ? "
                          "AND status IN ('queued','claimed')", (ref,)).fetchall():
        if j["status"] == "claimed":
            return j["id"]
        queue.cancel(conn, j["id"])
    set_setting(conn, SELECTION_KEY.format(ref=ref), ids)
    job_id = queue.enqueue(conn, "artifact.draft", ref)
    if job_id is None:
        raise WeeklyError("这个 artifact 已有一个起草任务在排队。")
    from .ai import dispatch as ai_dispatch
    ai_dispatch.dispatch(job_id)
    return job_id


def job_params(job: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(job["request_json"]).get("params") or {}
    except (ValueError, TypeError):
        return {}


def draft_view(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    from .ai import queue
    job = queue.get(conn, job_id)
    if job is None:
        return None
    params = job_params(job)
    result = json.loads(job["result_json"]) if job["result_json"] else None
    allowed = set(params.get("excerpt_ids") or [])
    bullets = []
    if result and job["kind"] == "artifact.draft":
        for i, b in enumerate(result.get("evidence_bullets") or []):
            if b.get("excerpt_id") in allowed:
                bullets.append({**b, "index": i})
    return {"job": dict(job), "status": job["status"], "result": result, "bullets": bullets,
            "params": params, "pending": job["status"] in ("queued", "claimed"), "mode": queue.mode(conn)}


def bullet_line(excerpt_id: str, text: str) -> str:
    text = " ".join((text or "").split())[:300]
    if f"[{excerpt_id}]" not in text:
        text = f"{text} [{excerpt_id}]"
    return f"- {text} <!-- rl:{excerpt_id} -->"


def insert_evidence(conn: sqlite3.Connection, job_id: str, indices: list[int], expected_sha: str,
                    paths: Paths | None = None) -> tuple[str, int]:
    """User action: append the chosen evidence bullets of a finished draft to § Evidence."""
    view = draft_view(conn, job_id)
    if view is None or view["status"] != "done":
        raise WeeklyError("这个草稿任务还没有完成。")
    ref = view["job"]["target_id"]
    a = load_ref(conn, ref, paths)
    if a is None:
        raise WeeklyError(f"没有 artifact {ref}。")
    if a.sha != expected_sha:
        raise StaleFileError(f"{a.path.name} changed on disk since it was loaded")
    chosen = [b for b in view["bullets"] if b["index"] in set(indices)]
    if not chosen:
        raise WeeklyError("请至少勾选一条证据要点。")
    added = []
    for b in chosen:
        line = bullet_line(b["excerpt_id"], b["text"])
        if line in a.body:
            continue
        artifacts.append_to_section(a, "Evidence", line)
        added.append(b["excerpt_id"])
    new_sha = artifacts.save(a, expected_sha)
    if added:
        conn.execute(f"UPDATE excerpts SET status = 'used', updated_at = ? WHERE id IN ({','.join('?' * len(added))}) "
                     "AND status IN ('inbox','linked')", [now_iso(), *added])
    artifacts.index(conn, paths or get_paths())
    return new_sha, len(added)


# ---------------------------------------------------------------- front matter

def bump_minor(version: str) -> str:
    m = VERSION_RE.match(str(version or "").strip())
    if not m:
        raise WeeklyError(f"版本号 {version} 不是 v0.1 这样的格式。")
    return f"v{m.group(1)}.{int(m.group(2)) + 1}"


def update_front(conn: sqlite3.Connection, ref: str, expected_sha: str, *, level: str | None = None,
                 status: str | None = None, version: str | None = None, related_add: str | None = None,
                 bump: bool = False, confirmed: bool = False, paths: Paths | None = None) -> list[str]:
    """Edit level/status/version/related. Promotions and version bumps need `confirmed`."""
    p = paths or get_paths()
    a = load_ref(conn, ref, p)
    if a is None:
        raise WeeklyError(f"没有 artifact {ref}。")
    changed: list[str] = []
    levels = [str(l["key"]) for l in datafiles.read("levels", p) or []] or list(LEVEL_ORDER)
    if level and level != a.get("level"):
        if level not in levels:
            raise WeeklyError(f"未知成熟度：{level}")
        if LEVEL_ORDER.get(level, 0) > LEVEL_ORDER.get(str(a.get("level")), 0):
            if not confirmed:
                raise WeeklyError(f"提升到「{level}」需要你确认。")
            if not artifacts.counts(a.body):
                raise WeeklyError(f"提升到「{level}」之前，需要先写好「想清楚了什么」和「主张」两节。")
        a.front["level"] = level
        changed.append("level")
    if status and status != a.get("status"):
        if status not in STATUSES:
            raise WeeklyError(f"未知状态：{status}")
        a.front["status"] = status
        changed.append("status")
    if bump:
        if not confirmed:
            raise WeeklyError("升级版本需要你确认。")
        version = bump_minor(str(a.get("version", "v0.1")))
    if version and version != str(a.get("version")):
        if not VERSION_RE.match(version):
            raise WeeklyError(f"版本号 {version} 不是 v0.1 这样的格式。")
        if not confirmed:
            raise WeeklyError("修改版本号需要你确认。")
        a.front["version"] = version
        changed.append("version")
    if related_add:
        related_add = related_add.strip()
        if related_add == ref:
            raise WeeklyError("artifact 不能关联自己。")
        if row_for(conn, related_add) is None:
            raise WeeklyError(f"没有 artifact {related_add}。")
        if artifacts.add_related(a, related_add):
            changed.append("related")
        conn.execute("UPDATE associations SET status = 'accepted' WHERE src_type = 'artifact' AND src_id = ? AND rel = 'related' "
                     "AND dst_type = 'artifact' AND dst_id = ? AND status = 'proposed'", (ref, related_add))
    if changed:
        artifacts.save(a, expected_sha)
        artifacts.index(conn, p)
    return changed


def related_view(conn: sqlite3.Connection, a: Artifact) -> dict[str, list[dict[str, Any]]]:
    forward = []
    for r in a.get("related", []) or []:
        hit = row_for(conn, str(r))
        forward.append({"ref": str(r), "title": hit["title"] if hit else None, "type": hit["type"] if hit else None})
    back = [dict(r) for r in artifacts.backlinks(conn, a.ref)]
    proposed = [dict(r) for r in conn.execute(
        "SELECT s.dst_id AS ref, s.reason, s.origin, a.title FROM associations s LEFT JOIN artifacts a ON a.ref = s.dst_id "
        "WHERE s.src_type = 'artifact' AND s.src_id = ? AND s.rel = 'related' AND s.status = 'proposed' ORDER BY s.id", (a.ref,))]
    return {"forward": forward, "back": back, "proposed": proposed}


# ---------------------------------------------------------------- git

def _git(paths: Paths, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=paths.root, capture_output=True, text=True, timeout=timeout)


def is_repo(paths: Paths | None = None) -> bool:
    p = paths or get_paths()
    try:
        r = _git(p, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and Path(r.stdout.strip()).resolve() == p.root.resolve()


def git_status(paths: Paths | None = None) -> list[str] | None:
    """`git status --porcelain` lines for the content dirs; None when the root is not a repo."""
    p = paths or get_paths()
    if not is_repo(p):
        return None
    r = _git(p, "status", "--porcelain", "--", *CONTENT_DIRS)
    return [l for l in r.stdout.splitlines() if l.strip()]


def versions(rel_path: str, paths: Paths | None = None) -> list[dict[str, str]]:
    p = paths or get_paths()
    if not is_repo(p):
        return []
    r = _git(p, "log", "--follow", "--format=%h%x09%ad%x09%s", "--date=short", "--", rel_path)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})
    return out


def commit_message(row: sqlite3.Row | dict | None) -> str:
    if not row:
        return ""
    r = dict(row)
    return f"W{str(r.get('week', '')).split('-W')[-1]}: {r.get('type', '')} — {r.get('title', '')}"


def commit(conn: sqlite3.Connection, ref: str, message: str, paths: Paths | None = None) -> dict[str, Any]:
    """The explicit Commit button: stage and commit only the artifact file and _data/now.yml. Never pushes."""
    p = paths or get_paths()
    message = " ".join((message or "").split())
    if not message:
        raise WeeklyError("请写一条提交说明。")
    if not is_repo(p):
        raise WeeklyError("这个文件夹不是 git 仓库。")
    artifacts.index(conn, p)
    row = row_for(conn, ref)
    if row is None:
        raise WeeklyError(f"没有 artifact {ref}。")
    files = [f for f in (row["path"], "_data/now.yml") if (p.root / f).exists()]
    add = _git(p, "add", "--", *files)
    if add.returncode != 0:
        raise WeeklyError(add.stderr.strip() or "git add failed")
    if _git(p, "diff", "--cached", "--quiet", "--", *files).returncode == 0:
        raise WeeklyError("没有可提交的内容：artifact 和 now.yml 与上次提交一致。")
    # Pathspec form commits these files only, leaving anything else the user staged untouched.
    r = _git(p, "commit", "-m", message, "--", *files, timeout=30)
    if r.returncode != 0:
        raise WeeklyError((r.stdout + r.stderr).strip() or "git commit failed")
    head = _git(p, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"hash": head, "files": files, "output": r.stdout.strip()}


# ---------------------------------------------------------------- now.yml

def now_form(paths: Paths | None = None) -> tuple[dict[str, Any], str | None]:
    p = paths or get_paths()
    data = datafiles.read("now", p)
    if data is None:
        return {}, None
    return dict(data), datafiles.file_sha("now", p)


def now_changes(current: dict[str, Any], form: dict[str, str], keys: list[str] | None = None) -> dict[str, Any]:
    """Turn submitted fields into typed changes (lists one per line), keeping only real changes."""
    changes: dict[str, Any] = {}
    for key in keys or list(current):
        if key not in form:
            continue
        old = current.get(key)
        raw = form[key].replace("\r\n", "\n")
        if isinstance(old, list):
            new: Any = [line.strip() for line in raw.split("\n") if line.strip()]
            if [str(x) for x in old] == new:
                continue
        else:
            new = raw.strip()
            if str(old if old is not None else "") == new:
                continue
        changes[key] = new
    return changes


def save_now(form: dict[str, str], expected_sha: str, keys: list[str] | None = None,
             paths: Paths | None = None) -> tuple[str, list[str]]:
    p = paths or get_paths()
    current, sha = now_form(p)
    if sha is None:
        raise WeeklyError("缺少 _data/now.yml。")
    changes = now_changes(current, form, keys)
    if not changes:
        if sha != expected_sha:
            raise StaleFileError("now.yml changed on disk since it was loaded")
        return sha, []
    return datafiles.update_now(changes, expected_sha, p), list(changes)
