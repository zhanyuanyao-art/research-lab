"""Materials Leo hands the assistant (his own drafts and notes), and the 交付任务 plan built from them.

A material is converted once on upload and kept under `app/data/materials/<MAT-id>/` with the
original file, so the assistant can read it (to find the outline, say). The assistant only proposes
a plan: the 课题, the 交付任务 (title, due date, target words, which file is the Word draft) and the
outline sections with their due dates. Leo checks and edits it on the confirm page; `apply` then
copies the originals to private/<slug>/ and writes a parent 待办 (due, words, file) with one
sub-task per section (files/tasks.py). Leo writes in Word; nothing here writes text for him.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import Paths, get_paths
from ..db.connection import now_iso
from . import convert, datafiles, deliverables, tasks
from .yamlio import atomic_write

MAX_SECTIONS = 20
DEFAULT_QUESTION = "待定：由交付任务建立，研究问题等你写下。"
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ID = re.compile(r"^MAT-[0-9a-f]{8}$")


class PlanError(ValueError):
    """A plan that cannot be carried out as given; the message goes back to the model or to Leo."""


@dataclass(frozen=True)
class Section:
    index: int            # 1-based, as the assistant and the plan refer to it
    heading: str
    level: int
    paragraphs: tuple[str, ...]

    @property
    def chars(self) -> int:
        return sum(len(p) for p in self.paragraphs)


def root(paths: Paths | None = None) -> Path:
    return (paths or get_paths()).data / "materials"


# ---------------------------------------------------------------- materials

def save(src: Path, name: str, thread_id: str | None, paths: Paths | None = None) -> dict[str, Any]:
    """Convert and keep one uploaded file. Raises convert.ConvertError."""
    text = convert.to_markdown(src, name)
    if not text.strip():
        raise convert.ConvertError("文件里没有可读的文字")
    mid = "MAT-" + secrets.token_hex(4)
    d = root(paths) / mid
    d.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix.lower()
    shutil.copyfile(src, d / f"original{suffix}")
    (d / "text.md").write_text(text, encoding="utf-8")
    meta = {"id": mid, "name": Path(name).name, "suffix": suffix, "thread_id": thread_id, "created": now_iso(),
            "chars": len(text), "sections": len(sections(text))}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def get(mid: str, paths: Paths | None = None) -> dict[str, Any] | None:
    if not _ID.match(mid or ""):
        return None
    d = root(paths) / mid
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta["text"] = (d / "text.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    meta["original"] = d / f"original{meta.get('suffix', '')}"
    return meta


def for_thread(thread_id: str, paths: Paths | None = None) -> list[dict[str, Any]]:
    out = []
    base = root(paths)
    for meta_file in sorted(base.glob("MAT-*/meta.json")) if base.exists() else []:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("thread_id") == thread_id:
            out.append(meta)
    return sorted(out, key=lambda m: m.get("created", ""))


def sections(text: str) -> list[Section]:
    """Split Markdown at headings. Text before the first heading is its own section, 「开头」
    (「全文」 when the file has no headings)."""
    raw: list[tuple[str, int, list[str]]] = [("", 0, [])]
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            raw.append((m.group(2).strip(), len(m.group(1)), []))
        else:
            raw[-1][2].append(line)
    out = []
    for heading, level, lines in raw:
        paras = tuple(p.strip("\n") for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip())
        if not heading and not paras:
            continue
        if not heading:
            heading = "全文" if len(raw) == 1 else "开头"
        out.append(Section(len(out) + 1, heading, level, paras))
    return out


def section_of(material: dict[str, Any], index: int) -> Section:
    secs = sections(material["text"])
    if not 1 <= index <= len(secs):
        raise PlanError(f"{material['id']} has sections 1–{len(secs)}, not {index}")
    return secs[index - 1]


# ---------------------------------------------------------------- plans

def plan_path(task_id: str, paths: Paths | None = None) -> Path:
    if not re.match(r"^AT-[0-9a-f]+$", task_id or ""):
        raise PlanError("bad task id")
    return root(paths) / "plans" / f"{task_id}.json"


def load_plan(task_id: str, paths: Paths | None = None) -> dict[str, Any] | None:
    try:
        return json.loads(plan_path(task_id, paths).read_text(encoding="utf-8"))
    except (OSError, ValueError, PlanError):
        return None


def _date(v: Any, what: str) -> str | None:
    v = str(v or "").strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise PlanError(f"{what}: write dates as YYYY-MM-DD") from None


def _words(v: Any, what: str) -> int | None:
    v = str(v or "").strip().replace(",", "")
    if not v:
        return None
    if not v.isdigit() or not 100 <= int(v) <= 2_000_000:
        raise PlanError(f"{what}: a whole number of words between 100 and 2,000,000")
    return int(v)


def spread(due: str | None, n: int, today: date | None = None) -> list[str | None]:
    """Section due dates spaced evenly from today to the overall due date; the last one is the due date."""
    if not due or n <= 0:
        return [None] * n
    today = today or date.today()
    end = date.fromisoformat(due)
    span = max((end - today).days, 0)
    return [(today + timedelta(days=round(span * (i + 1) / n))).isoformat() for i in range(n)]


def validate(conn: sqlite3.Connection, plan: dict[str, Any], thread_id: str | None,
             paths: Paths | None = None) -> dict[str, Any]:
    """Check a proposed or edited plan against the files and the library; return it normalized.
    Keys the plan does not define are dropped, so nothing else reaches the files."""
    paths = paths or get_paths()
    proj = plan.get("project") if isinstance(plan.get("project"), dict) else {}
    slug = str(proj.get("slug") or "").strip()
    if not datafiles.SLUG_RE.match(slug):
        raise PlanError("project.slug: lowercase letters, digits and hyphens only, e.g. chapter2-report")
    existing = datafiles.project(slug, paths)
    directions = [str(d.get("slug")) for d in datafiles.read("directions", paths) or []]
    if existing:
        project = {"slug": slug, "name": str(existing.get("name") or slug), "direction": str(existing.get("direction") or ""),
                   "question": str(existing.get("question") or ""), "existing": True}
    else:
        name = " ".join(str(proj.get("name") or "").split())
        direction = str(proj.get("direction") or "").strip()
        if not name:
            raise PlanError("project.name is required for a new project")
        if direction not in directions:
            raise PlanError(f"project.direction must be one of: {', '.join(directions)}")
        project = {"slug": slug, "name": name[:120], "direction": direction,
                   "question": " ".join(str(proj.get("question") or "").split())[:600], "existing": False}

    dl = plan.get("deliverable") if isinstance(plan.get("deliverable"), dict) else {}
    title = " ".join(str(dl.get("title") or "").split())[:120]
    if not title:
        raise PlanError("deliverable.title is required (the title of the piece the user hands in)")
    due = _date(dl.get("due"), "deliverable.due")
    words = _words(dl.get("words"), "deliverable.words")

    thread_materials = {m["id"]: m for m in for_thread(thread_id, paths)} if thread_id else {}
    wanted = plan.get("materials")
    mats = [str(x) for x in wanted] if isinstance(wanted, list) else list(thread_materials)
    for mid in mats:
        if mid not in thread_materials:
            raise PlanError(f"unknown material {mid!r}; use list_materials")
    mats = list(dict.fromkeys(mats))
    file = str(dl.get("file") or "").strip() or None
    if file and (file not in mats or thread_materials[file].get("suffix") != ".docx"):
        raise PlanError("deliverable.file must be one of the copied materials and a .docx; leave it empty to start a new Word file")

    secs_in = [x for x in plan.get("sections") or [] if isinstance(x, dict)]
    if len(secs_in) > MAX_SECTIONS:
        raise PlanError(f"sections: at most {MAX_SECTIONS}")
    secs, seen = [], set()
    for i, s in enumerate(secs_in, 1):
        t = " ".join(str(s.get("title") or "").split())[:100]
        if not t:
            continue                                  # a title cleared on the confirm page drops the section
        if t in seen:
            raise PlanError(f"sections[{i}].title repeats {t!r}")
        seen.add(t)
        secs.append({"title": t, "due": _date(s.get("due"), f"sections[{i}].due"), "words": _words(s.get("words"), f"sections[{i}].words")})
    for s, d in zip(secs, spread(due, len(secs))):
        s["due"] = s["due"] or d
    if not due and any(s["due"] for s in secs):
        due = max(s["due"] for s in secs if s["due"])

    papers = []
    for pid in plan.get("papers") or []:
        pid = str(pid)
        if not conn.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone():
            raise PlanError(f"no paper {pid!r} in the library")
        if pid not in papers:
            papers.append(pid)
    return {"project": project, "deliverable": {"title": title, "due": due, "words": words, "file": file},
            "sections": secs, "materials": mats, "papers": papers,
            "note": " ".join(str(plan.get("note") or "").split())[:400]}


def propose(conn: sqlite3.Connection, task_id: str, thread_id: str | None, plan: dict[str, Any],
            paths: Paths | None = None) -> dict[str, Any]:
    """Validate and store the plan for Leo to confirm. A new proposal replaces an unconfirmed one."""
    current = load_plan(task_id, paths)
    if current and current.get("status") == "applied":
        raise PlanError("this plan was already carried out; start a new request for further changes")
    clean = validate(conn, plan, thread_id, paths)
    record = {"task_id": task_id, "thread_id": thread_id, "status": "proposed", "created": now_iso(), "plan": clean}
    p = plan_path(task_id, paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(p, json.dumps(record, ensure_ascii=False, indent=1))
    return record


NEW_FILE = "new"          # the Word-source choice "start a blank docx from the outline"
MERGE_NEW = "new"         # the merge choice "make a separate parent task"


def with_edits(plan: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    """Leo's changes on the confirm page: title, due, words, research question, the Word source, each
    section's title and due (fields are named by the section's index as rendered), the sections' new
    order (`section_order`: rendered indices, comma-separated), and the merge choice (`merge`)."""
    out = json.loads(json.dumps(plan))
    dl = out["deliverable"]
    for k in ("title", "due", "words"):
        if k in form:
            dl[k] = form[k]
    if "file" in form:
        dl["file"] = "" if form["file"].strip() in ("", NEW_FILE) else form["file"].strip()
    if "question" in form and not out["project"].get("existing"):
        out["project"]["question"] = form["question"]
    for i, s in enumerate(out["sections"]):
        for k in ("title", "due"):
            if f"section_{i}_{k}" in form:
                s[k] = form[f"section_{i}_{k}"]
    if form.get("section_order", "").strip():
        out["sections"] = reorder(out["sections"], form["section_order"])
    if "merge" in form:
        out["merge"] = form["merge"].strip()
    return out


def reorder(sections: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Sections in the order Leo arranged them: `order` lists rendered indices; indices it leaves out
    keep their relative order at the end, and anything that is not an index is ignored."""
    picked: list[int] = []
    for tok in order.split(","):
        tok = tok.strip()
        if tok.isdigit() and int(tok) < len(sections) and int(tok) not in picked:
            picked.append(int(tok))
    rest = [i for i in range(len(sections)) if i not in picked]
    return [sections[i] for i in picked + rest]


def same_title_parent(slug: str, title: str, paths: Paths | None = None) -> dict[str, Any] | None:
    """An open parent task in the 课题 with this title and a Word file: the 交付稿 Leo already set up.
    Submitting the same chapter from another conversation merges into it by default (TASKS.md §E, §H.4)."""
    want = " ".join(str(title or "").split())
    for t in tasks.for_project(slug, paths=paths):
        if not t.get("parent") and t.get("file") and tasks.is_open(t) and " ".join(t["title"].split()) == want:
            return t
    return None


def apply(conn: sqlite3.Connection, task_id: str, form: dict[str, str] | None = None,
          paths: Paths | None = None) -> dict[str, Any]:
    """Carry out a confirmed plan. Running it twice does nothing new, and a retry after a step failed
    half way reuses what the first attempt already wrote: identical copies, the Word file, and the 待办
    whose `source` is this plan's task id."""
    paths = paths or get_paths()
    record = load_plan(task_id, paths)
    if record is None:
        raise PlanError("没有找到这份方案")
    if record.get("status") == "applied":
        return record["result"]
    edited = with_edits(record["plan"], form or {})
    plan = validate(conn, edited, record.get("thread_id"), paths)
    proj, dl = plan["project"], plan["deliverable"]
    slug = proj["slug"]
    created_project = False
    if not datafiles.project(slug, paths):
        stages = [str(s.get("key")) for s in datafiles.read("stages", paths) or []]
        datafiles.add_project({"slug": slug, "name": proj["name"], "direction": proj["direction"],
                               "stage": stages[0] if stages else "idea", "question": proj["question"] or DEFAULT_QUESTION,
                               "since": date.today().strftime("%Y-%m")}, paths=paths)
        created_project = True

    copied: dict[str, str] = {}
    for mid in plan["materials"]:
        m = get(mid, paths)
        if not m or not Path(m["original"]).exists():
            continue
        dest = deliverables.folder(slug, "private", paths) / m["name"]
        if not (dest.exists() and dest.read_bytes() == Path(m["original"]).read_bytes()):
            dest = deliverables.unique(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(m["original"], dest)
        copied[mid] = dest.name
    # The same chapter set up before (another conversation, another day): add only the sections it
    # lacks to that parent task, unless Leo picked 另建一份 on the confirm page.
    existing = None if edited.get("merge") == MERGE_NEW else same_title_parent(slug, dl["title"], paths)
    merged = existing is not None and existing.get("source") != task_id

    file = copied.get(dl["file"] or "")
    if merged:
        file = existing.get("file") or file
    if not file:
        made = deliverables.outline_docx(slug, dl["title"], [s["title"] for s in plan["sections"]], paths)
        file = made.name if made else None

    # a sub-task carries its parent's source (files/tasks.py), so sections merged into a 交付稿 set up
    # elsewhere take that parent's source, or none for one Leo made by hand; a retry finds them by title (had)
    source = (existing.get("source") or None) if merged else task_id
    rows = tasks.for_project(slug, paths=paths)
    earlier = {(t["parent"], t["title"]): t["id"] for t in rows if t["source"] == task_id and tasks.is_open(t)}

    def ensure(title: str, parent: str | None, due: str | None, **extra: Any) -> str:
        title = title[:tasks.TITLE_MAX]
        if (parent, title) in earlier:
            return earlier[(parent, title)]
        values = {"title": title, "project": slug, "parent": parent, "due": due, "origin": "assistant", "source": source,
                  **extra}
        return tasks.add({k: v for k, v in values.items() if v not in (None, "")}, paths=paths)[0]

    if merged:
        parent_id = existing["id"]
        had = {" ".join(t["title"].split()): t for t in tasks.children(parent_id, rows)}
    else:
        parent_id = ensure(dl["title"], None, dl["due"], words=dl["words"], file=file, note="交付稿：在 Word 里写，应用按截止和字数提醒。")
        had = {}
    todo = []
    for s in plan["sections"]:
        old = had.get(s["title"])
        if old is not None:
            todo.append({"id": old["id"], "title": s["title"], "due": old.get("due"), "existing": True})
        else:
            todo.append({"id": ensure(s["title"], parent_id, s["due"], words=s.get("words")), "title": s["title"], "due": s["due"]})

    for pid in plan["papers"]:
        conn.execute("INSERT OR IGNORE INTO paper_projects(paper_id, project_slug) VALUES (?,?)", (pid, slug))

    result = {"project": slug, "name": proj["name"], "created_project": created_project, "parent": parent_id,
              "merged": merged, "title": dl["title"], "due": (existing.get("due") if merged else dl["due"]),
              "words": (existing.get("words") if merged else dl["words"]), "file": file,
              "copied": list(copied.values()), "tasks": todo, "added": sum(1 for t in todo if not t.get("existing")),
              "papers": len(plan["papers"])}
    record.update(status="applied", applied=now_iso(), plan=plan, result=result)
    atomic_write(plan_path(task_id, paths), json.dumps(record, ensure_ascii=False, indent=1))
    return result
