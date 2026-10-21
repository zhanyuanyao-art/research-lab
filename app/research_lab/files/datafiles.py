"""Targeted round-trip edits of the hand-owned _data/*.yml files, plus the app-owned lab.yml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Paths, get_paths
from .yamlio import load_yaml, save_yaml, sha256_text, yaml_rt

READ_ONLY = ("types", "levels", "stages", "archive")


def _path(name: str, paths: Paths | None = None) -> Path:
    return (paths or get_paths()).data_yml / f"{name}.yml"


def read(name: str, paths: Paths | None = None) -> Any:
    p = _path(name, paths)
    return load_yaml(p) if p.exists() else None


def read_all(paths: Paths | None = None) -> dict[str, Any]:
    return {n: read(n, paths) for n in ("now", "projects", "year", "types", "levels", "stages", "archive", "directions")}


def file_sha(name: str, paths: Paths | None = None) -> str:
    return sha256_text(_path(name, paths).read_text(encoding="utf-8"))


def update_now(changes: dict[str, Any], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    p = _path("now", paths)
    data = load_yaml(p)
    for k, v in changes.items():
        if v is None:
            continue
        data[k] = v
    return save_yaml(p, data, expected_sha)


def append_need_to_find_out(item: str, paths: Paths | None = None) -> bool:
    p = _path("now", paths)
    data = load_yaml(p)
    lst = data.setdefault("need_to_find_out", [])
    if item in lst:
        return False
    lst.append(item)
    save_yaml(p, data)
    return True


def set_project_stage(slug: str, stage: str, paths: Paths | None = None) -> bool:
    p = _path("projects", paths)
    data = load_yaml(p)
    for entry in data:
        if entry.get("slug") == slug:
            if entry.get("stage") == stage:
                return False
            entry["stage"] = stage
            save_yaml(p, data)
            return True
    raise KeyError(slug)


def project(slug: str, paths: Paths | None = None) -> dict | None:
    for entry in read("projects", paths) or []:
        if entry.get("slug") == slug:
            return dict(entry)
    return None


def set_year_done(label_prefix: str, done: int, paths: Paths | None = None) -> bool:
    p = _path("year", paths)
    data = load_yaml(p)
    for goal in data.get("goals", []):
        if str(goal.get("label", "")).lower().startswith(label_prefix.lower()) and "auto" not in goal:
            if goal.get("done") == done:
                return False
            goal["done"] = done
            save_yaml(p, data)
            return True
    return False


def write_lab(summary: dict[str, Any], paths: Paths | None = None) -> None:
    """App-owned _data/lab.yml so the Jekyll snapshot can show milestone state. No private data."""
    p = _path("lab", paths)
    save_yaml(p, summary)


# ---------------------------------------------------------------- in-app editing (V2)
#
# Every write below is a round trip through ruamel (comments, quoting and key order kept, so a one-field
# change diffs as one line) guarded by the sha of the file the user saw. Validation failures raise
# DataFileError with a message the page can show as is.

import re as _re

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import ScalarString

from .yamlio import StaleFileError, atomic_write

PROJECT_FIELDS = ("name", "question", "stage", "direction", "type", "since", "note")
PROJECT_REQUIRED = ("name", "question", "stage")
PROJECT_ORDER = ("name", "slug", "direction", "type", "stage", "question", "since", "note")   # type: i18n.ISSUE_TYPES
DIRECTION_FIELDS = ("name", "name_en", "keywords", "description")
DIRECTION_ORDER = ("slug", "key", "name", "name_en", "description", "keywords")
# A direction's `key` is its issue prefix (APUS-0001, docs/design/ISSUES-LINEAR.md 附：议题编号): given when the
# direction is created. The settings page and the assistant never edit it (update_direction refuses); only
# `rl tasks rekey` (files/issues.rekey_direction) does, rewriting every issue key under it in the same write with
# the numbers kept and the old keys left as aliases. LAB numbers issues without a direction; PRJ, ART and DS begin
# internal ids, which a key must never be mistaken for.
DIRECTION_KEY_RE = _re.compile(r"^[A-Z]{2,5}$")
RESERVED_KEYS = ("LAB", "PRJ", "ART", "DS")
PROJECT_FILES = ("index.md", "literature.md", "evidence.md")
SLUG_RE = _re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DIRECTION_SLUG_RE = _re.compile(r"^[a-z0-9-]+$")
SINCE_RE = _re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


class DataFileError(ValueError):
    """A user-facing validation failure (the page shows it, status 400)."""


def _check_sha(p: Path, expected_sha: str | None) -> None:
    if expected_sha is not None and sha256_text(p.read_text(encoding="utf-8")) != expected_sha:
        raise StaleFileError(f"{p.name} changed on disk since it was loaded")


def _styled(old: Any, new: Any) -> Any:
    """Keep the quoting style of the value being replaced (a quoted string stays quoted)."""
    if isinstance(old, ScalarString) and isinstance(new, str):
        return type(old)(new)
    return new


def _place(entry: CommentedMap, key: str, value: Any, order: tuple[str, ...]) -> None:
    if key in entry:
        entry[key] = _styled(entry[key], value)
        return
    keys = list(entry.keys())
    pos = len(keys)
    if key in order:
        before = [k for k in order[:order.index(key)] if k in keys]
        if before:
            pos = keys.index(before[-1]) + 1
    entry.insert(pos, key, value)


def _keywords(value: Any) -> CommentedSeq:
    items = value if isinstance(value, (list, tuple)) else _re.split(r"[,，;；\n]", str(value or ""))
    seq = CommentedSeq([str(x).strip() for x in items if str(x).strip()])
    seq.fa.set_flow_style()
    return seq


def _stage_keys(paths: Paths | None) -> list[str]:
    return [str(s["key"]) for s in (read("stages", paths) or [])]


def _direction_slugs(paths: Paths | None) -> list[str]:
    return [str(d["slug"]) for d in (read("directions", paths) or [])]


def _validate_project(values: dict[str, str], paths: Paths | None) -> None:
    stages = _stage_keys(paths)
    if "stage" in values and stages and values["stage"] not in stages:
        raise DataFileError(f"未知阶段：{values['stage']}")
    dirs = _direction_slugs(paths)
    if values.get("direction") and values["direction"] not in dirs:
        raise DataFileError(f"未知研究方向：{values['direction']}")
    from ..i18n import ISSUE_TYPES
    if values.get("type") and values["type"] not in ISSUE_TYPES:
        raise DataFileError("类型只能是论文、课题或手稿。")
    if values.get("since") and not SINCE_RE.match(values["since"]):
        raise DataFileError("起始时间请写成 YYYY-MM，例如 2026-09。")
    for k in PROJECT_REQUIRED:
        if k in values and not values[k]:
            raise DataFileError({"name": "项目名称不能为空。", "question": "研究问题不能为空。",
                                 "stage": "请选择阶段。"}[k])


def update_project(slug: str, changes: dict[str, Any], expected_sha: str | None = None,
                   paths: Paths | None = None, *, renumber: bool = True) -> str:
    """Edit name / question / stage / direction / since / note of one project in _data/projects.yml.
    Unchanged fields are not touched; an emptied optional field (direction, since, note) is removed.
    A new direction renumbers the 课题 and the issues that follow it (issues.sync_keys); files/issues.py,
    which writes its own activity in the same request, passes renumber=False and does it itself.
    Returns the new sha. Raises KeyError (unknown slug), DataFileError, StaleFileError."""
    p = _path("projects", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    entry = next((e for e in data if str(e.get("slug")) == slug), None)
    if entry is None:
        raise KeyError(slug)
    unknown = set(changes) - set(PROJECT_FIELDS)
    if unknown:
        raise DataFileError(f"不能修改字段：{', '.join(sorted(unknown))}")
    values = {k: " ".join(str(v).split()) if k != "note" else str(v).strip() for k, v in changes.items() if v is not None}
    _validate_project(values, paths)
    old_direction = str(entry.get("direction") or "")
    changed = False
    for k, v in values.items():
        old = entry.get(k)
        if str(old if old is not None else "") == v:
            continue
        if not v:
            if k in entry:
                del entry[k]
                changed = True
            continue
        _place(entry, k, v, PROJECT_ORDER)
        changed = True
    if not changed:
        return sha256_text(p.read_text(encoding="utf-8"))
    sha = save_yaml(p, data, expected_sha)
    if renumber and paths is None and str(entry.get("direction") or "") != old_direction:
        from . import issues
        issues.renumber_quietly()
    return sha


def add_project(values: dict[str, Any], expected_sha: str | None = None, paths: Paths | None = None,
                create_folder: bool = True) -> str:
    """Append a project to _data/projects.yml and (by default) create projects/<slug>/ from
    templates/project. Returns the new sha of projects.yml."""
    paths = paths or get_paths()
    p = _path("projects", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p) if p.exists() else CommentedSeq()
    slug = str(values.get("slug") or "").strip()
    if not SLUG_RE.match(slug):
        raise DataFileError("slug 只能用小写字母、数字和连字符，例如 asean-centrality。")
    if any(str(e.get("slug")) == slug for e in data):
        raise DataFileError(f"项目 {slug} 已存在。")
    vals = {k: " ".join(str(values.get(k) or "").split()) if k != "note" else str(values.get(k) or "").strip()
            for k in PROJECT_FIELDS}
    for k in PROJECT_REQUIRED:
        vals.setdefault(k, "")
    _validate_project(vals, paths)
    entry = CommentedMap()
    for k in PROJECT_ORDER:
        v = slug if k == "slug" else vals.get(k, "")
        if v:
            entry[k] = v
    data.append(entry)
    sha = save_yaml(p, data, expected_sha)
    from . import issues
    issues.assign_key("PRJ-" + slug, vals.get("direction", ""), paths)
    if create_folder:
        create_project_folder(slug, vals, paths)
    return sha


def create_project_folder(slug: str, values: dict[str, str], paths: Paths | None = None) -> list[Path]:
    """Copy templates/project into projects/<slug>/ with the placeholders filled. Existing files stay."""
    paths = paths or get_paths()
    src, dst = paths.templates / "project", paths.projects / slug
    name = values.get("name") or slug
    written: list[Path] = []
    for f in sorted(src.rglob("*")) if src.exists() else []:
        target = dst / f.relative_to(src)
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            continue
        text = f.read_text(encoding="utf-8")
        if f.suffix == ".md":
            text = (text.replace("<Project name>", name).replace("<Name>", name)
                    .replace("<stage>", values.get("stage") or "idea").replace("<YYYY-MM>", values.get("since") or ""))
            if f.name == "index.md":
                text = text.replace("\n---\n", f"\nslug: {slug}\n---\n", 1)
                if values.get("question"):
                    text = text.replace("## Question\n\n...", f"## Question\n\n{values['question']}", 1)
        atomic_write(target, text)
        written.append(target)
    if not written:
        dst.mkdir(parents=True, exist_ok=True)
    return written


def project_file_path(slug: str, name: str, paths: Paths | None = None) -> Path:
    if name not in PROJECT_FILES or not SLUG_RE.match(slug):
        raise KeyError(name)
    return (paths or get_paths()).projects / slug / name


def read_project_file(slug: str, name: str, paths: Paths | None = None) -> tuple[str, str]:
    """(text, sha); a missing file reads as empty with the sha of ''."""
    p = project_file_path(slug, name, paths)
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    return text, sha256_text(text)


def save_project_file(slug: str, name: str, text: str, expected_sha: str, paths: Paths | None = None) -> str:
    p = project_file_path(slug, name, paths)
    text = text.replace("\r\n", "\n")
    current = p.read_text(encoding="utf-8") if p.exists() else ""
    if sha256_text(current) != expected_sha:
        raise StaleFileError(f"{name} changed on disk since it was loaded")
    return atomic_write(p, text)


# ---- research directions (_data/directions.yml) ----

def direction(slug: str, paths: Paths | None = None) -> dict | None:
    for d in read("directions", paths) or []:
        if str(d.get("slug")) == slug:
            return dict(d)
    return None


def _direction_values(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in DIRECTION_FIELDS:
        if k not in values or values[k] is None:
            continue
        out[k] = _keywords(values[k]) if k == "keywords" else " ".join(str(values[k]).split())
    if "name" in out and not out["name"]:
        raise DataFileError("方向名称不能为空。")
    return out


def update_direction(slug: str, changes: dict[str, Any], expected_sha: str | None = None,
                     paths: Paths | None = None) -> str:
    """Edit name / name_en / keywords / description of one direction. The slug never changes.
    Returns the new sha. Raises KeyError, DataFileError, StaleFileError."""
    p = _path("directions", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    entry = next((e for e in data if str(e.get("slug")) == slug), None)
    if entry is None:
        raise KeyError(slug)
    if "key" in changes:
        raise DataFileError("方向的前缀创建后不能修改：改了它，这个方向下所有议题的编号都会变。")
    unknown = set(changes) - set(DIRECTION_FIELDS)
    if unknown:
        raise DataFileError(f"不能修改字段：{', '.join(sorted(unknown))}")
    changed = False
    for k, v in _direction_values(changes).items():
        old = entry.get(k)
        if k == "keywords":
            if [str(x) for x in (old or [])] == list(v):
                continue
        elif str(old if old is not None else "") == v:
            continue
        if (k == "keywords" and not len(v)) or (k != "keywords" and not v):
            if k in entry and k != "name":
                del entry[k]
                changed = True
            continue
        _place(entry, k, v, DIRECTION_ORDER)
        changed = True
    if not changed:
        return sha256_text(p.read_text(encoding="utf-8"))
    return save_yaml(p, data, expected_sha)


def add_direction(values: dict[str, Any], expected_sha: str | None = None, paths: Paths | None = None) -> str:
    p = _path("directions", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p) if p.exists() else CommentedSeq()
    slug = str(values.get("slug") or "").strip()
    if not DIRECTION_SLUG_RE.match(slug):
        raise DataFileError("slug 只能用小写字母、数字和连字符，例如 sea-asean。")
    if any(str(e.get("slug")) == slug for e in data):
        raise DataFileError(f"方向 {slug} 已存在。")
    key = direction_key(values.get("key"), data)
    vals = _direction_values({**values, "name": values.get("name") or ""})
    entry = CommentedMap()
    for k in DIRECTION_ORDER:
        v = slug if k == "slug" else key if k == "key" else vals.get(k)
        if v is not None and (len(v) if k == "keywords" else v):
            entry[k] = v
    data.append(entry)
    return save_yaml(p, data, expected_sha)


def direction_key(value: Any, directions: Any) -> str:
    """A new direction's issue prefix, checked against the directions already there."""
    key = str(value or "").strip()
    if not key:
        raise DataFileError("请给方向一个前缀，议题编号用它开头，例如 APUS-0001。")
    if not DIRECTION_KEY_RE.match(key):
        raise DataFileError("前缀要 2 到 5 个大写英文字母，例如 APUS。")
    if key in RESERVED_KEYS:
        raise DataFileError(f"前缀 {key} 已留作他用，请换一个。")
    if any(str(e.get("key") or "").strip().upper() == key for e in directions or []):
        raise DataFileError(f"前缀 {key} 已被另一个方向使用。")
    return key


def direction_with_key(slug: str, key: Any, paths: Paths | None = None) -> tuple[Path, str, str, Any]:
    """directions.yml with one direction's prefix replaced, not written: (path, sha it was read at, old prefix,
    document). The new prefix passes direction_key against the other directions; the current one returns the
    document unchanged. Only files/issues.rekey_direction calls it, which writes it together with the issue keys.
    Raises KeyError, DataFileError (a bad new prefix, or a direction that has none to replace)."""
    p = _path("directions", paths)
    text = p.read_text(encoding="utf-8")
    data = yaml_rt().load(text)
    entry = next((e for e in data or [] if str(e.get("slug")) == slug), None)
    if entry is None:
        raise KeyError(slug)
    old = str(entry.get("key") or "").strip()
    if not DIRECTION_KEY_RE.match(old) or old in RESERVED_KEYS:
        raise DataFileError(f"方向 {slug} 还没有前缀，它的议题记在 {RESERVED_KEYS[0]} 下，没有可以换的前缀。")
    new = str(key or "").strip()
    if new != old:
        entry["key"] = _styled(entry["key"], direction_key(new, [e for e in data if e is not entry]))
    return p, sha256_text(text), old, data


def direction_usage(slug: str, paths: Paths | None = None) -> dict[str, list[str]]:
    """Projects and artifacts (explicit front matter) that name this direction, plus `unreadable`:
    output files that do not parse, which might name it too."""
    from . import artifacts as artifacts_mod
    projects = [str(e.get("slug")) for e in (read("projects", paths) or []) if str(e.get("direction") or "") == slug]
    arts, unreadable = [], [f.name for f in artifacts_mod.unreadable_paths(paths)]
    for path in artifacts_mod.list_paths(paths):
        try:
            a = artifacts_mod.load(path)
        except Exception:  # noqa: BLE001 - changed since it was listed
            unreadable.append(path.name)
            continue
        if str(a.get("direction", "") or "") == slug:
            arts.append(a.ref or path.name)
    return {"projects": projects, "artifacts": arts, "unreadable": unreadable}


def delete_direction(slug: str, expected_sha: str | None = None, paths: Paths | None = None) -> str:
    """Remove a direction nobody uses. Raises KeyError, DataFileError (still in use), StaleFileError."""
    p = _path("directions", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    idx = next((i for i, e in enumerate(data) if str(e.get("slug")) == slug), None)
    if idx is None:
        raise KeyError(slug)
    use = direction_usage(slug, paths)
    if use["projects"] or use["artifacts"]:
        raise DataFileError(f"方向 {slug} 仍在使用：{len(use['projects'])} 个项目、{len(use['artifacts'])} 个 artifact。"
                            "先把它们改到其他方向再删除。")
    if use["unreadable"]:
        raise DataFileError(f"_artifacts 里有 {len(use['unreadable'])} 个产出文件读不出来：{'、'.join(use['unreadable'][:3])}，"
                            "无法确认它们有没有用到这个方向。先修好或移走这些文件再删除。")
    del data[idx]
    return save_yaml(p, data, expected_sha)


# ---- push sources (_data/feeds.yml) ----
#
# The assistant changes one source or one word list at a time behind a confirm card (SETTINGS-AGENT §3). Unlike
# the other writers the sha is required: Leo edits this file by hand, and a write made from a stale read would
# silently undo his edit.

FEED_FIELDS = {"news": ("enabled", "weight", "keep", "drop"), "journal": ("enabled", "weight", "gp_filter")}
FEED_TERMS = ("radar_terms", "gp_terms")
NEWS_ORDER = ("name", "url", "lane", "keep", "drop", "enabled", "note", "weight")
JOURNAL_ORDER = ("abbr", "name", "issn", "column", "pool", "weight", "gp_filter", "enabled")


def _feed_section(data: Any, kind: str) -> list:
    if kind not in FEED_FIELDS:
        raise DataFileError(f"未知的来源类型：{kind}")
    return (data or {}).get("news" if kind == "news" else "journals") or []


def _find_feed(section: list, kind: str, name: str) -> Any:
    """A news feed by name, a journal by abbr (or full name); exact first, then ignoring case."""
    keys = ("name",) if kind == "news" else ("abbr", "name")
    rows = [e for e in section if isinstance(e, dict)]
    for fold in (False, True):
        norm = (lambda s: s.casefold()) if fold else (lambda s: s)  # noqa: E731
        hit = next((e for k in keys for e in rows if norm(str(e.get(k) or "").strip()) == norm(name.strip())), None)
        if hit is not None:
            return hit
    raise KeyError(name)


def feed_source(kind: str, name: str, paths: Paths | None = None) -> dict | None:
    """One feeds.yml source as written in the file (kind news | journal), or None."""
    try:
        return dict(_find_feed(_feed_section(read("feeds", paths), kind), kind, name))
    except KeyError:
        return None


def _flow(items: list) -> CommentedSeq:
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def _feed_field(field: str, value: Any) -> Any:
    if field in ("enabled", "gp_filter"):
        if not isinstance(value, bool):
            raise DataFileError("开关只能是开或关。")
        return value
    if field == "weight":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise DataFileError("权重请填 0 到 100 的整数。")
        return value
    paths_ = [str(p).strip() for p in ([value] if isinstance(value, str) else value or []) if str(p).strip()]
    bad = [p for p in paths_ if not p.startswith("/")]
    if bad:
        raise DataFileError(f"栏目要写成链接路径，以 / 开头：{bad[0]}")
    return list(dict.fromkeys(paths_))


def update_feed_source(kind: str, name: str, changes: dict[str, Any], expected_sha: str,
                       paths: Paths | None = None) -> str:
    """Change fields of one source in _data/feeds.yml: news `enabled`, `weight`, `keep`, `drop`; journals `enabled`,
    `weight`, `gp_filter`. Turning a source on removes `enabled: false` (absent means on) and keeps its note; an
    emptied keep / drop is removed. Comments and the one-line flow style stay, so a change diffs as one line.
    Returns the new sha. Raises KeyError (no such source), DataFileError, StaleFileError (the file is not the one
    whose sha the caller read)."""
    if not expected_sha:
        raise ValueError("expected_sha is required")
    p = _path("feeds", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    entry = _find_feed(_feed_section(data, kind), kind, name)
    unknown = set(changes) - set(FEED_FIELDS[kind])
    if unknown:
        raise DataFileError(f"不能修改字段：{', '.join(sorted(unknown))}")
    order = NEWS_ORDER if kind == "news" else JOURNAL_ORDER
    changed = False
    for field, value in changes.items():
        v = _feed_field(field, value)
        if field == "enabled":
            if v and entry.get("enabled") is False:
                del entry["enabled"]
                changed = True
            elif not v and entry.get("enabled") is not False:
                _place(entry, "enabled", False, order)
                changed = True
        elif field == "gp_filter":
            if bool(entry.get("gp_filter")) != v:
                if v:
                    _place(entry, "gp_filter", True, order)
                else:
                    del entry["gp_filter"]
                changed = True
        elif field == "weight":
            if entry.get("weight") != v:
                _place(entry, "weight", v, order)
                changed = True
        else:
            old = entry.get(field)
            if [str(x) for x in ([old] if isinstance(old, str) else old or [])] == v:
                continue
            if v:
                _place(entry, field, _flow(v), order)
            elif field in entry:
                del entry[field]
            changed = True
    if not changed:
        return sha256_text(p.read_text(encoding="utf-8"))
    return save_yaml(p, data, expected_sha)


def set_feed_terms(list_name: str, terms: list[str], expected_sha: str, paths: Paths | None = None) -> str:
    """Replace radar_terms or gp_terms. Terms are lowercased word roots and keep their spaces (" war" is not
    "war"); a term already in the file keeps its quoting, a new one with a space is quoted like the others.
    Returns the new sha. Raises DataFileError, StaleFileError."""
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    if not expected_sha:
        raise ValueError("expected_sha is required")
    if list_name not in FEED_TERMS:
        raise DataFileError(f"未知的词根表：{list_name}")
    p = _path("feeds", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    old = {str(t): t for t in data.get(list_name) or []}
    new = list(dict.fromkeys(str(t).lower() for t in terms if str(t).strip()))
    if list(old) == new:
        return sha256_text(p.read_text(encoding="utf-8"))
    def plain_ok(t: str) -> bool:   # `null`, `true`, `1e3` would read back as None, True, 1000.0
        return " " not in t and isinstance(YAML(typ="safe").load(t), str)
    items = [old[t] if t in old else t if plain_ok(t) else DoubleQuotedScalarString(t) for t in new]
    if list_name in data:
        data[list_name] = _flow(items)
    else:
        data.insert(len(data), list_name, _flow(items))
    return save_yaml(p, data, expected_sha)


# The assistant also adds and removes whole sources (card 0211). Their text comes from Crossref or a feed, so it is
# validated here, at the write layer: no control characters, bounded lengths, well-formed ISSNs and URLs.

FEED_ABBR_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9]{1,11}$")
ISSN_RE = _re.compile(r"^\d{4}-\d{3}[\dX]$")
FEED_POOLS = ("core", "watch", "hunt", "expansion")
FEED_LANES = ("world", "research")
FEED_NAME_MAX = 200
FEED_URL_MAX = 500
_CONTROL = _re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def valid_issn(value: Any) -> bool:
    """Form and check digit (ISO 3297)."""
    s = str(value or "").strip().upper()
    if not ISSN_RE.match(s):
        return False
    digits = s.replace("-", "")
    check = (11 - sum(int(c) * (8 - i) for i, c in enumerate(digits[:7])) % 11) % 11
    return digits[7] == ("X" if check == 10 else str(check))


def _feed_text(value: Any, limit: int, what: str) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit or _CONTROL.search(text):
        raise DataFileError(f"{what}要在 1 到 {limit} 个字之间，不能有控制字符。")
    return text


def _feed_url(value: Any) -> str:
    from urllib.parse import urlsplit
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if (not parts or parts.scheme not in ("http", "https") or not parts.hostname or len(url) > FEED_URL_MAX
            or _CONTROL.search(url) or any(c.isspace() for c in url)):
        raise DataFileError("新闻源地址要是 http 或 https 开头的完整链接。")
    return url


def _url_key(url: Any) -> tuple:
    from urllib.parse import urlsplit
    try:
        p = urlsplit(str(url or "").strip())
        return p.netloc.lower(), p.path.rstrip("/"), p.query
    except ValueError:
        return ("invalid", str(url))


def _issns(value: Any) -> list[str]:
    return [str(i).strip().upper() for i in ([value] if isinstance(value, str) else value or []) if str(i).strip()]


def _new_feed_entry(kind: str, values: dict[str, Any]) -> CommentedMap:
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString as Q
    weight = values.get("weight", 60)
    if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 100:
        raise DataFileError("权重请填 0 到 100 的整数。")
    if values.get("enabled", True) not in (True, False):
        raise DataFileError("开关只能是开或关。")
    entry = CommentedMap()
    if kind == "journal":
        abbr = str(values.get("abbr") or "").strip()
        if not FEED_ABBR_RE.match(abbr):
            raise DataFileError("期刊缩写要以字母开头，由 2 到 12 个字母或数字组成。")
        issns = list(dict.fromkeys(_issns(values.get("issn"))))
        if not 1 <= len(issns) <= 4 or not all(valid_issn(i) for i in issns):
            raise DataFileError("ISSN 要写成 1234-567X 这样，校验位正确，最多 4 个。")
        pool = values.get("pool") or "core"
        if pool not in FEED_POOLS:
            raise DataFileError(f"期刊池只能是：{'、'.join(FEED_POOLS)}")
        entry.update(abbr=abbr, name=Q(_feed_text(values.get("name"), FEED_NAME_MAX, "期刊名")),
                     issn=_flow(issns), pool=pool, weight=weight)
    elif kind == "news":
        lane = values.get("lane") or "research"
        if lane not in FEED_LANES:
            raise DataFileError(f"新闻栏目只能是：{'、'.join(FEED_LANES)}")
        entry.update(name=Q(_feed_text(values.get("name"), FEED_NAME_MAX, "新闻源名称")),
                     url=Q(_feed_url(values.get("url"))), lane=lane, weight=weight)
    else:
        raise DataFileError(f"未知的来源类型：{kind}")
    if values.get("enabled") is False:
        entry["enabled"] = False
    entry.fa.set_flow_style()
    return entry


def feed_duplicate(kind: str, values: dict[str, Any], section: list) -> dict | None:
    """The source already in `section` that `values` would repeat: a journal by abbr or any shared ISSN, a news feed
    by name or URL (ignoring case and a trailing slash). Disabled entries count."""
    rows = [e for e in section or [] if isinstance(e, dict)]
    if kind == "journal":
        abbr = str(values.get("abbr") or "").strip().casefold()
        issns = set(_issns(values.get("issn")))
        return next((dict(e) for e in rows if str(e.get("abbr") or "").strip().casefold() == abbr
                     or issns & set(_issns(e.get("issn")))), None)
    name = str(values.get("name") or "").strip().casefold()
    key = _url_key(values.get("url"))
    return next((dict(e) for e in rows if str(e.get("name") or "").strip().casefold() == name
                 or _url_key(e.get("url")) == key), None)


def _feeds_for_write(p: Path, expected_sha: str) -> Any:
    """feeds.yml loaded for a write; a missing file is created only when the caller read it as missing (sha '')."""
    if p.exists():
        _check_sha(p, expected_sha)
        data = load_yaml(p)
    elif expected_sha:
        raise StaleFileError(f"{p.name} was removed since it was loaded")
    else:
        data = None
    if data is None:
        data = CommentedMap()
    if not isinstance(data, dict):
        raise DataFileError("推送配置文件格式有误，没有改动。")
    return data


def add_feed_source(kind: str, values: dict[str, Any], expected_sha: str, paths: Paths | None = None) -> str:
    """Append one journal (abbr, name, issn, pool, weight) or news feed (name, url, lane, weight) to _data/feeds.yml
    as a one-line entry like the hand-written ones, optionally `enabled: false`. Refuses a duplicate (DataFileError).
    expected_sha '' means the file did not exist when read. Returns the new sha."""
    if expected_sha is None:
        raise ValueError("expected_sha is required")
    p = _path("feeds", paths)
    data = _feeds_for_write(p, expected_sha)
    entry = _new_feed_entry(kind, values)
    key = "news" if kind == "news" else "journals"
    section = data.get(key)
    if section is None:
        section = data[key] = CommentedSeq()
    if not isinstance(section, list):
        raise DataFileError("推送配置文件格式有误，没有改动。")
    if feed_duplicate(kind, entry, section) is not None:
        raise DataFileError("这个来源已经在推送配置里了。")
    if not section and isinstance(section, CommentedSeq):
        section.fa.set_block_style()      # `journals: []` becomes a list of one-line entries
    section.append(entry)
    return save_yaml(p, data, expected_sha)


def remove_feed_source(kind: str, name: str, expected_sha: str, paths: Paths | None = None) -> str:
    """Delete one source (a journal by abbr or name, a news feed by name), for undoing an assistant's add. Raises
    KeyError when it is not there. Returns the new sha."""
    if not expected_sha:
        raise ValueError("expected_sha is required")
    p = _path("feeds", paths)
    _check_sha(p, expected_sha)
    data = load_yaml(p)
    section = _feed_section(data, kind)
    entry = _find_feed(section, kind, name)
    del section[next(i for i, e in enumerate(section) if e is entry)]
    return save_yaml(p, data, expected_sha)
