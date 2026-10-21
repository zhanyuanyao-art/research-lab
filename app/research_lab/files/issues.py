"""One issue view over existing projects, artifacts and tasks, without copying research documents.

Canonical task fields still live in tasks.yml; artifact title/project stay in front matter.
The additive issues.yml sidecar holds workflow metadata for artifacts, labels, responsibility
and comments, each entry stamped `updated` on write. Projects appear as virtual parent issues, never
duplicate tasks. GETs never migrate content.

Writes come from the user's forms and from the assistant's issue tools under the confirmation rules
of docs/design/ISSUE-AGENT.md §5. The assistant goes through pm/changes.py, which records each change
in assistant_changes and calls the field-checked helpers at the end of this file:

    change_fields(ref, [(field, seen_old, new)], source={'title', 'url'}) -> {applied, skipped, same, failed, line}
    create_checked(title, project=, parent=, source=, **props) -> (ref, snapshot)    origin: assistant
    comment_checked(ref, text, by='assistant'|'user', via=, link=) -> None
    remove_created(ref, snapshot, source=) -> 'deleted' | 'parked' | 'kept' | 'gone'  (undo only)
    activity(ref, text, by=, link=)            one line in the issue's activity (issues.yml or updates.yml)
    phrase(field, old, new) / display(field, value)   Chinese wording for lines and cards
    last_touched(rows) -> {ref: 'YYYY-MM-DD'}  newest change or comment, for the 卡住 check

They reuse update() and create(): same validation, parent/child rules and postponement counting.

No plan week (Leo 10-10, docs/design/ISSUES-LINEAR.md §3): issues are scheduled by `due` only; rows have no
`week` and nothing writes one (the assistant moved over in card 0107). week_due() remains for confirmation
rows stored before card 0093, which pm/changes.py and chat/cards.py still translate. issues.yml entries of
outputs and 课题 written before may still hold `week`: an open one without a due date reads as due on that
week's Sunday until `rl tasks migrate-week` (migrate_week).
Research content stays out of reach: no artifact body, Claim, maturity or archive field is editable
here, and the assistant cannot retitle a 课题 or overwrite its description (the research question).
"""
from __future__ import annotations

import fcntl
import json
import logging
import re
import secrets
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ruamel.yaml.error import YAMLError
from ruamel.yaml.comments import CommentedSeq
from ruamel.yaml.scalarstring import SingleQuotedScalarString

from . import artifacts as A, datafiles, deliverables, tasks as T, updates
from .yamlio import load_yaml, save_yaml, sha256_text, StaleFileError, dump_yaml, atomic_write_many, write_lock
from ..config import get_paths
from .. import i18n

STATES = i18n.ISSUE_STATUS
PRIORITIES = i18n.ISSUE_PRIORITY
LABELS = i18n.ISSUE_LABELS
log = logging.getLogger("research_lab.issues")


class IssueError(ValueError):
    pass


# 分配人 (Leo 10-10, ISSUES-LINEAR.md §7): Leo himself by default, shown by name, or one of the assistants
# from agent/skills.py ASSISTANTS (0088), stored by key. Leo is the empty value; 'Leo' and the full name,
# as older entries and forms wrote him, mean the same. Giving an issue to an assistant hands it over: a plan first,
# work only after Leo's 开始 (agent/tasks.py, card 0116). The assistant cannot change this field (ASSISTANT_FIELDS).
OWNER = 'Zhanyuan Yao'
OWNER_VALUES = ('', 'Leo', OWNER)


def _profile() -> dict | None:
    """profile.yml as a dict; None when the file is missing (a legacy workspace), {} when unreadable."""
    try:
        data = load_yaml(get_paths().data_yml / 'profile.yml')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _plain_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ''
    # The name also appears in Markdown comments and prompts; keep it plain text.
    valid = all(unicodedata.category(c)[0] in 'LNM' or c in " .-'’" for c in name)
    return name if 0 < len(name) <= 80 and valid else ''


def owner_name() -> str:
    """Keep legacy workspaces unchanged while new ones identify their own user."""
    profile = _profile()
    if profile is None:
        return OWNER
    return _plain_name(profile.get('owner_name')) or '我'


def call_name() -> str:
    """What the assistant calls the user: profile.yml `call_name`; 'Leo' in the legacy workspace (no profile.yml,
    Leo's own lab); else the owner's name, or '' while that is still the placeholder 我."""
    profile = _profile()
    if profile is None:
        return 'Leo'
    name = _plain_name(profile.get('call_name')) or owner_name()
    return '' if name == '我' else name


def research_field() -> str:
    """The workspace's research directions for prompts (name and English name), or '' when none are set."""
    try:
        rows = datafiles.read('directions')
    except (OSError, UnicodeError, YAMLError):
        return ''
    names = []
    for d in rows if isinstance(rows, list) else []:
        if not isinstance(d, dict):
            continue
        name, en = (' '.join(str(d.get(k) or '').split())[:80] for k in ('name', 'name_en'))
        if name or en:
            names.append(f'{name} ({en})' if name and en and en != name else name or en)
    return '; '.join(names[:12])


def user_facts() -> str:
    """The line every model prompt carries instead of assuming who the user is or what they study."""
    return (f"The user: {call_name() or 'unnamed'}. "
            f"Research directions: {research_field() or 'not set yet'}.")


def assignees() -> dict[str, str]:
    """value → name for the 分配人 menu: Leo first, then every assistant in the roster."""
    from ..agent.skills import ASSISTANTS
    return {'': owner_name(), **{key: a.name for key, a in ASSISTANTS.items()}}


def _check_assignee(value: str) -> str:
    if value not in OWNER_VALUES and value not in assignees():
        raise IssueError('分配人只能是本人或助手名单里的一位。')
    return value


def path():
    return get_paths().data_yml / 'issues.yml'


def _metadata():
    """issues.yml as a mapping. A file that does not parse reads as empty (and is logged) so the
    pages still open; writes go through _meta_for_write, which refuses to replace it."""
    try:
        data = load_yaml(path()) if path().exists() else None
    except Exception as e:  # noqa: BLE001 - YAML errors come in many classes
        log.warning("issues.yml does not parse, reading it as empty: %s", str(e).splitlines()[0] if str(e) else type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _meta_for_write():
    try:
        data = load_yaml(path()) if path().exists() else None
    except Exception:  # noqa: BLE001
        data = False
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise IssueError('议题属性文件 _data/issues.yml 格式有误，请先修好这个文件再保存。')
    return data


def _entry(meta, ref):
    m = meta.get(ref)
    return m if isinstance(m, dict) else {}


def _refs(value):
    return [str(v) for v in value] if isinstance(value, list) else []


def _front_related(doc):
    return _refs(doc.get('related'))


def _meta_sha():
    return sha256_text(path().read_text() if path().exists() else '')


def _documents():
    docs = {}
    for p in A.list_paths():
        try:
            a = A.load(p)
        except Exception as e:  # noqa: BLE001 - changed between listing and loading
            log.warning("skipping unreadable output %s: %s", p.name, type(e).__name__)
            continue
        if a.ref:
            docs[a.ref] = a
    return docs


def project_ref(slug: str) -> str:
    return 'PRJ-' + slug


def url(ref: str) -> str:
    return '/issues?project=' + ref[4:] if ref.startswith('PRJ-') else '/issues/' + ref


# ---------------------------------------------------------------- issue keys (ISSUES-LINEAR.md 附：议题编号, Leo 10-11)
# APUS-0002: the direction's prefix (directions.yml `key`; LAB without a direction) and a number counted per prefix,
# 课题 and sub-issues alike, that never goes down, so no number is issued twice. A key is what pages show and what
# Leo and the assistant say. The internal ids (T-…, ART-…, PRJ-…) stay what forms, links and the other files use,
# because a key changes when its issue changes direction: it takes the next number of the new prefix, and the old
# key stays as an alias that still finds it (/issues/<old key> redirects). A direction's prefix itself changes only
# through `rl tasks rekey` (rekey_direction, Leo 10-16): same numbers, new prefix, the old keys again kept as aliases.
# _data/issue_keys.yml holds the last number per prefix, each issue's key and the aliases. It is apart from
# issues.yml on purpose: numbering is the app's, not an edit, so it never makes a page's revision stale. Nothing is
# numbered before `rl tasks migrate-keys` has written the file.

NO_DIRECTION = datafiles.RESERVED_KEYS[0]
KEY_RE = re.compile(r'^([A-Za-z]{2,5})-0*(\d{1,6})$')
KEYS_HEADER = ('Issue keys (docs/design/ISSUES-LINEAR.md 附：议题编号). Written by the app, not by hand: `last` is the\n'
               'highest number issued per prefix, `refs` each issue\'s key, `aliases` the keys an issue had before a\n'
               'change of direction or of prefix. Numbers are never reused.')


def keys_path(paths=None) -> Path:
    return (paths or get_paths()).data_yml / 'issue_keys.yml'


@contextmanager
def _keys_lock(paths=None):
    """One numbering at a time, across request threads, the CLI and the app: reading the file, counting and writing
    all happen inside, so two issues created at once never get the same number or lose one (0121 review). The lock
    file sits in the app's data folder, outside the research content; the key file itself is replaced on every
    write, so it cannot carry the lock."""
    lock = (paths or get_paths()).data / 'issue_keys.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _keys(paths=None) -> dict[str, dict]:
    """{'last': {prefix: n}, 'refs': {ref: key}, 'aliases': {old key: ref}}, each {} when missing or unreadable."""
    p = keys_path(paths)
    try:
        data = load_yaml(p) if p.exists() else None
    except Exception as e:  # noqa: BLE001 - YAML errors come in many classes
        log.warning("issue_keys.yml does not parse, reading it as empty: %s", type(e).__name__)
        data = None
    data = data if isinstance(data, dict) else {}
    return {part: {str(k): v for k, v in data[part].items()} if isinstance(data.get(part), dict) else {}
            for part in ('last', 'refs', 'aliases')}


def _keys_doc(paths=None):
    """The key file for a write, with its three maps; a file that does not parse is refused, never replaced."""
    from ruamel.yaml.comments import CommentedMap
    p = keys_path(paths)
    try:
        doc = load_yaml(p) if p.exists() else None
    except Exception:  # noqa: BLE001
        raise IssueError('议题编号文件 _data/issue_keys.yml 格式有误，请先修好它。') from None
    if doc is None:
        doc = CommentedMap()
        doc.yaml_set_start_comment(KEYS_HEADER)
    if not isinstance(doc, dict):
        raise IssueError('议题编号文件 _data/issue_keys.yml 格式有误，请先修好它。')
    for part in ('last', 'refs', 'aliases'):
        if not isinstance(doc.get(part), dict):
            doc[part] = CommentedMap()
    return doc


def prefixes(paths=None) -> dict[str, str]:
    """direction slug → its prefix, for the directions that have a valid one."""
    return {str(d.get('slug')): str(d.get('key')) for d in datafiles.read('directions', paths) or []
            if datafiles.DIRECTION_KEY_RE.match(str(d.get('key') or ''))}


def prefix(direction: str, known: dict[str, str] | None = None) -> str:
    return (prefixes() if known is None else known).get(str(direction or '')) or NO_DIRECTION


def normal_key(text) -> str:
    """'apus-2' → 'APUS-0002'; '' when the text is not shaped like a key."""
    m = KEY_RE.match(str(text or '').strip())
    return f"{m[1].upper()}-{int(m[2]):04d}" if m else ''


def resolve(text) -> str | None:
    """The internal ref of the issue named by its ref, its key or a key it had before it changed direction (any
    case, padding optional). None when nothing matches."""
    text = str(text or '').strip()
    key = normal_key(text)
    if key:
        k = _keys()
        return next((r for r, v in k['refs'].items() if str(v) == key), None) or (str(k['aliases'].get(key) or '') or None)
    return text if text and get(text) else None


def named(rows: list[dict] | None = None) -> dict[str, str]:
    """ref → how a line names an issue it mentions: 「APUS-0003 引言」 once numbered, the title before."""
    return {r['id']: f"{r['key']} {r['title']}" if r.get('key') else r['title'] for r in (rows or read() + project_roots())}


def key_of(ref: str) -> str:
    return str(_keys()['refs'].get(ref) or '')


def key_url(ref: str, key: str | None = None) -> str:
    """The link to copy and to say: /issues/APUS-0002 (a 课题's redirects to its page); the internal one without a key."""
    key = key_of(ref) if key is None else key
    return '/issues/' + key if key else url(ref)


def _number(key: str) -> int:
    return int(key.rsplit('-', 1)[1]) if normal_key(key) == key else 0


def _next_key(doc, pfx: str) -> str:
    """The next number of a prefix: past the stored count and past any key or alias already using it, so a count
    edited down by hand still never hands out a number twice."""
    used = [_number(str(k)) for k in [*doc['refs'].values(), *doc['aliases'].keys()] if str(k).startswith(pfx + '-')]
    n = max([int(doc['last'].get(pfx) or 0), *used]) + 1
    doc['last'][pfx] = n
    return f"{pfx}-{n:04d}"


def _key_order(rows: list[dict], known: dict[str, str]) -> list[dict]:
    """The numbering order (Leo 10-11): per direction, its 课题 by creation, each followed by its issues by
    creation; then the direction's other issues (under a 课题 of another direction) by creation; LAB last."""
    def when(r):
        return (str(r.get('created') or ''), r['id'])
    out = []
    for pfx in dict.fromkeys([*known.values(), NO_DIRECTION]):
        members = [r for r in rows if prefix(r['direction'], known) == pfx]
        placed = set()
        for root in sorted((r for r in members if r['project_root']), key=when):
            for r in [root, *sorted((r for r in members if not r['project_root'] and r['project'] == root['project']), key=when)]:
                out.append(r)
                placed.add(r['id'])
        out += sorted((r for r in members if r['id'] not in placed), key=when)
    return out


def sync_keys(*, write: bool = True, create: bool = False, moves: bool = True) -> list[dict]:
    """Every issue's key in line with its direction. One without a key takes the next number of its prefix, in
    _key_order; one whose direction changed takes the next number of the new prefix, its old key kept as an
    alias (moves=False leaves those alone: fill_keys). Returns [{ref, title, old, new}] per change, old '' for a
    first key. Does nothing before the key file exists unless create (the migration); writes only with
    write=True."""
    p = keys_path()
    if not p.exists() and not create:
        return []
    rows = read() + project_roots()
    with _keys_lock():
        for _ in range(_TRIES):
            # The prefixes are read under the lock: one read before it could predate a rekey_direction that held
            # the lock meanwhile, and every key it rewrote would look like a move.
            known = prefixes()
            sha = sha256_text(p.read_text(encoding='utf-8')) if p.exists() else None
            doc = _keys_doc()
            changes = []
            for r in _key_order(rows, known):
                want, cur = prefix(r['direction'], known), str(doc['refs'].get(r['id']) or '')
                if cur.startswith(want + '-') or (cur and not moves):
                    continue
                new = _next_key(doc, want)
                if cur:
                    doc['aliases'][cur] = r['id']
                doc['refs'][r['id']] = new
                changes.append(dict(ref=r['id'], title=r['title'], old=cur, new=new))
            if not write or not changes:
                return changes
            try:
                save_yaml(p, doc, sha)      # the sha still catches a hand edit made while the lock was held
                return changes
            except StaleFileError:
                continue
    raise IssueError(BUSY)


def fill_keys() -> None:
    """Number the issues that have no key yet: one whose numbering at creation failed, or one made by a path that
    does not number (an output written by hand). The issue pages call it before they read, so what they show has
    its key; only the app's key file is written, never a move, and nothing before the migration."""
    if not keys_path().exists():
        return
    try:
        refs = _keys()['refs']
        if any(not refs.get(r['id']) for r in read() + project_roots()):
            sync_keys(moves=False)
    except Exception as e:  # noqa: BLE001 - a page must open even when numbering cannot run
        log.warning("filling in missing keys failed: %s", type(e).__name__)


def assign_key(ref: str, direction: str | None = None, paths=None, *, project: str = '') -> str:
    """A just-created issue's key (tasks.add, artifacts.create, datafiles.add_project): the next number of its
    direction's prefix, its 课题's direction when none is given. '' before the migration, or when the file keeps
    changing or does not parse (the next sync_keys numbers it). Never raises: the issue has been created."""
    p = keys_path(paths)
    if not p.exists():
        return ''
    try:
        if direction is None:
            direction = str((datafiles.project(project, paths) or {}).get('direction') or '')
        with _keys_lock(paths):
            pfx = prefix(direction, prefixes(paths))     # under the lock, for the same reason as in sync_keys
            for _ in range(_TRIES):
                sha = sha256_text(p.read_text(encoding='utf-8'))
                doc = _keys_doc(paths)
                if doc['refs'].get(ref):
                    return str(doc['refs'][ref])
                key = doc['refs'][ref] = _next_key(doc, pfx)
                try:
                    save_yaml(p, doc, sha)
                    return key
                except StaleFileError:
                    continue
    except Exception as e:  # noqa: BLE001 - the creation it follows must stand
        log.warning("no key for %s yet: %s", ref, type(e).__name__)
    return ''


def note_key_moves(moves: list[dict], meta_sha: str | None = None) -> str:
    """A grey line in the activity of each issue whose key changed, 「编号 SSIG-0003 → APUS-0005」, all in one
    issues.yml write (outputs' events live there too). Returns issues.yml's sha after it."""
    sha = _meta_sha() if meta_sha is None else meta_sha
    moves = [m for m in moves if m.get('old')]
    if not moves:
        return sha
    data = _meta_for_write()
    for m in moves:
        entry = data.get(m['ref'])
        if not isinstance(entry, dict):
            entry = data[m['ref']] = {}
        entry['comments'] = list(entry.get('comments', []) or []) + [
            {'by': 'system', 'kind': 'event', 'changes': [{'field': 'key', 'old': m['old'], 'new': m['new']}],
             'text': f"编号 {m['old']} → {m['new']}", 'at': _stamp()}]
    return save_yaml(path(), data, sha)


def _renumber(meta_sha: str) -> str:
    """After a change of direction or 课题 in update(): new keys and their activity lines, chained on the sha this
    request's own writes left. A failure is logged; the change itself stands and the next sync catches up."""
    try:
        return note_key_moves(sync_keys(), meta_sha)
    except (IssueError, StaleFileError, OSError) as e:
        log.warning("renumbering after a move not finished: %s", type(e).__name__)
        return meta_sha


def renumber_quietly() -> None:
    """The same after a direction change made outside update() (a 课题 edited on its page, a task moved on /tasks)."""
    try:
        note_key_moves(sync_keys())
    except Exception as e:  # noqa: BLE001 - the change it follows has been saved
        log.warning("renumbering after a move not finished: %s", type(e).__name__)


def rekey_direction(slug: str, new_key: str, *, write: bool = True) -> dict:
    """A direction's new prefix with every number kept (`rl tasks rekey`, Leo 10-16: APUS → US). Each key with the
    old prefix becomes <new>-<same number> and the old key an alias that still finds its issue (search,
    /issues/APUS-0008, text that names it); the old prefix's count moves to the new one, so new issues continue
    after it; directions.yml takes the new key. Both files go in one write under the numbering lock: a key file
    and a directions.yml that disagree would make sync_keys renumber every issue as a move.
    The new prefix passes datafiles.direction_key and must never have been a prefix in the key file, where an old
    key of it would then name two issues. The old prefix leaves `last`, but its keys stay as aliases, which
    _next_key counts, so a number an old key names is never issued again. Running it again changes nothing.
    Returns {slug, old, new, changes: [{ref, title, old, new}], next, written}; write=False only plans. Each
    issue that still exists gets the activity line 「编号 APUS-0008 → US-0008」 (note_key_moves, one write).
    Raises IssueError, datafiles.DataFileError."""
    p = keys_path()
    titles = {r['id']: r['title'] for r in read() + project_roots()}
    with _keys_lock():
        for _ in range(_TRIES):
            try:
                dir_path, dir_sha, old, directions = datafiles.direction_with_key(slug, new_key)
            except KeyError:
                raise IssueError(f'没有方向 {slug}。') from None
            new = str(new_key or '').strip()
            plan = dict(slug=slug, old=old, new=new, changes=[], next='', written=False)
            if new == old:
                return plan
            files = [(dir_path, dump_yaml(directions), dir_sha)]
            if p.exists():
                sha = sha256_text(p.read_text(encoding='utf-8'))
                doc = _keys_doc()
                keys = [str(k) for k in [*doc['refs'].values(), *doc['aliases'].keys()]]
                if new in {str(k) for k in doc['last']} | {k.partition('-')[0] for k in keys}:
                    raise IssueError(f'前缀 {new} 在议题编号里用过，再用它，旧编号会指向两个议题。请换一个。')
                for ref, key in list(doc['refs'].items()):
                    if str(key).startswith(old + '-'):
                        plan['changes'].append(dict(ref=ref, title=titles.get(ref, ''), old=str(key),
                                                    new=new + str(key)[len(old):]))
                for c in plan['changes']:
                    doc['refs'][c['ref']] = c['new']
                    doc['aliases'][c['old']] = c['ref']
                if plan['changes'] and hasattr(doc['aliases'], 'fa'):
                    doc['aliases'].fa.set_block_style()      # `aliases: {}` would grow into one long line
                last = doc['last']
                if old in last:
                    at, count = list(last).index(old), last.pop(old)
                    last.insert(at, new, count)
                top = max([int(last.get(new) or 0), *(_number(c['new']) for c in plan['changes'])])
                plan['next'] = f"{new}-{top + 1:04d}"
                if plan['changes'] or new in last:
                    files.append((p, dump_yaml(doc), sha))
            if not write:
                return plan
            try:
                atomic_write_many(files)
            except StaleFileError:
                continue
            plan['written'] = True
            break
        else:
            raise IssueError(BUSY)
    try:
        note_key_moves([c for c in plan['changes'] if c['ref'] in titles])
    except (IssueError, StaleFileError, OSError) as e:
        log.warning("activity lines after a rekey not written: %s", type(e).__name__)
    return plan


def project_roots():
    """Project identity and research question remain canonical; workflow is an additive overlay."""
    meta = _metadata()
    keys = _keys()['refs']
    rows = []
    for p in datafiles.read('projects') or []:
        slug = str(p['slug'])
        ref = project_ref(slug)
        m = _entry(meta, ref)
        rows.append(dict(id=ref, key=str(keys.get(ref) or ''), title=str(p['name']), project=slug, project_root=True, url=url(ref),
                         direction=str(p.get('direction') or ''), parent='', artifact=False, archived=False,
                         description=str(m.get('description', p.get('question') or '')),
                         status=m.get('status') if m.get('status') in STATES else 'todo',
                         priority=m.get('priority') if m.get('priority') in PRIORITIES else 'none',
                         assignee=m.get('assignee', ''), labels=_refs(m.get('labels', [])), related=_refs(m.get('related')),
                         due=_meta_due(m), created=str(p.get('since') or '')))
    return rows


def _meta_due(m: dict) -> str:
    """An output's or 课题's due date from issues.yml; an unmigrated open entry with a plan week and no
    due date reads as due on that week's Sunday (tasks.py does the same for tasks.yml)."""
    due, week = str(m.get('due') or ''), str(m.get('week') or '')
    if not due and m.get('status', 'todo') in T.OPEN and T.WEEK_RE.match(week):
        return T.sunday(week)
    return due




def _revision(meta_sha, task_sha, project_sha, doc_sha=''):
    return sha256_text(meta_sha + task_sha + project_sha + doc_sha)


def revision(ref: str = '') -> str:
    doc = _documents().get(ref)
    return _revision(_meta_sha(), T.file_sha(), datafiles.file_sha('projects'), doc.sha if doc else '')


def _check(ref, expected):
    doc = _documents().get(ref)
    meta_sha, task_sha, project_sha = _meta_sha(), T.file_sha(), datafiles.file_sha('projects')
    if not expected or expected != _revision(meta_sha, task_sha, project_sha, doc.sha if doc else ''):
        raise StaleFileError('议题已在别处更新，请刷新后再修改。')
    return meta_sha, task_sha, project_sha, doc


def read() -> list[dict[str, Any]]:
    """Relations have one home each: a link between two outputs is the outputs' front matter
    `related` (the output page, its backlinks and the index read it, and it counts from either end);
    issues.yml holds only links that involve a task or a project, written on both ends."""
    projects = {str(p['slug']): p for p in datafiles.read('projects') or []}
    meta = _metadata()
    docs = _documents()
    incoming: dict[str, list[str]] = {}
    for ref, a in docs.items():
        for other in _front_related(a):
            incoming.setdefault(other, []).append(ref)
    rows = []
    for ref, a in docs.items():
        m = _entry(meta, ref)
        project = str(a.get('project'))
        links = _front_related(a) + incoming.get(ref, []) + [x for x in _refs(m.get('related')) if x not in docs]
        # An output's front-matter `week` is the week it was written, not a schedule: it is off the home
        # 待办 and the 本周 filter until the issue itself gets a due date.
        rows.append(dict(id=ref, title=str(a.get('title')), project=project,
                         direction=str(a.get('direction') or projects.get(project, {}).get('direction') or ''),
                         status=m.get('status', 'todo'), due=_meta_due(m), done='', parent='',
                         description=m.get('description', ''),
                         labels=_refs(m.get('labels', [i18n.KIND.get(i18n.TYPE_KIND.get(a.get('type')), '问题')])),
                         artifact=True, archived=a.get('status') in ('superseded', 'absorbed-into-paper', 'archived'),
                         related=list(dict.fromkeys(x for x in links if x != ref)), created=str(a.get('created'))))
    for t in T.read():
        project = t['project'] or ''
        # New task properties are saved together with creation. Old sidecar values remain readable.
        props = dict(_entry(meta, t['id']))
        props.update({k: t[k] for k in ('priority', 'assignee', 'labels') if t.get(k) is not None})
        meta[t['id']] = props
        # A sub-issue inherits its 课题's direction unless issues.yml holds its own (field 'direction').
        rows.append(dict(id=t['id'], title=t['title'], project=project,
                         direction=str(props.get('direction') or projects.get(project, {}).get('direction') or ''),
                         status=t['status'], due=t['due'] or '', done=t['done'] or '',
                         parent=t['parent'] or (t['source'] if t['source'].startswith('ART-') else ''),
                         description=t['note'], labels=_refs(props.get('labels', [])),
                         artifact=False, archived=False, related=_refs(props.get('related')), created=t['created']))
    ids = {r['id'] for r in rows}
    keys = _keys()['refs']
    for r in rows:
        m = _entry(meta, r['id'])
        r.update(project_root=False, url=url(r['id']), priority=m.get('priority', 'none'), assignee=m.get('assignee', ''),
                 key=str(keys.get(r['id']) or ''))
        if r['parent'] not in ids:
            r['parent'] = ''
        r['due'] = str(r['due'] or '')
        r['status'] = r['status'] if r['status'] in STATES else 'todo'
        r['priority'] = r['priority'] if r['priority'] in PRIORITIES else 'none'
    return rows


def get(ref: str):
    return next((r for r in (project_roots() if ref.startswith('PRJ-') else read()) if r['id'] == ref), None)


def descendants(ref: str, rows: list[dict]):
    found, pending = [], [ref]
    seen = {ref}
    while pending:
        parent = pending.pop(0)
        for r in rows:
            if r['parent'] == parent and r['id'] not in seen:
                seen.add(r['id'])
                found.append(r)
                pending.append(r['id'])
    return found


def in_view(row: dict, view: str, today: date | None = None) -> bool:
    """week: due by this Sunday (overdue included) or finished since Monday (本周, as on 首页);
    next: due next Monday–Sunday."""
    today = today or date.today()
    if view == 'week':
        return T.due_this_week(row, today) or T.done_this_week(row, today)
    try:
        due = date.fromisoformat(row['due']) if row.get('due') else None
    except ValueError:
        return False
    end = T.week_end(today)
    return due is not None and end < due <= end + timedelta(days=7)


def board(*, direction='', view='all', state='', label='', project='', query='', group='status', archived=None):
    rows = [r for r in read() if not project or r['project'] == project]
    def matches(r):
        if bool(r['archived']) != (view == 'archived' if archived is None else archived):
            return False
        if direction and r['direction'] != direction:
            return False
        if state and r['status'] != state:
            return False
        if label and label not in r['labels']:
            return False
        if project and r['project'] != project:
            return False
        if query and query.casefold() not in r['title'].casefold():
            return False
        if view in ('week', 'next'):
            return in_view(r, view)
        return True
    matched = {r['id'] for r in rows if matches(r)}
    by_id = {r['id']: r for r in rows}
    shown = set(matched)
    for ref in list(matched):
        seen = {ref}
        parent = by_id[ref]['parent']
        while parent in by_id and parent not in seen:
            seen.add(parent)
            shown.add(parent)
            parent = by_id[parent]['parent']
    def node(r, trail):
        kids = [node(c, trail | {r['id']}) for c in rows if c['parent'] == r['id'] and c['id'] in shown and c['id'] not in trail]
        all_kids = [c for c in rows if c['parent'] == r['id'] and c['status'] != 'parked']
        return dict(issue=r, children=kids, context=r['id'] not in matched,
                    done=sum(c['status'] == 'done' for c in all_kids), total=len(all_kids))
    roots = [node(r, set()) for r in rows if r['id'] in shown and r['parent'] not in shown]
    if group == 'none':
        return [dict(key='all', title='', items=roots)], len(matched)
    if group == 'priority':
        groups = [(value, title) for value, title in PRIORITIES.items()]
    else:
        group = 'status'
        groups = list(STATES.items())
    return [dict(key=k, title=title, items=[n for n in roots if n['issue'][group] == k]) for k, title in groups], len(matched)


def _save_meta(ref, changes, expected_sha, *, drop_week=False):
    data = _meta_for_write()
    entry = data.get(ref)
    if not isinstance(entry, dict):
        entry = data[ref] = {}
    entry.update(changes)
    if drop_week:
        entry.pop('week', None)        # an explicit due date replaces an unmigrated plan week
    entry['updated'] = SingleQuotedScalarString(date.today().isoformat())   # 卡住 reads it for outputs and 课题
    return save_yaml(path(), data, expected_sha)


def _save_related(ref, row, value, doc, meta_sha):
    """Store a relation edit where each link lives (see read): output↔output in front matter,
    anything with a task or a project in issues.yml on both ends."""
    docs = _documents()
    data = _meta_for_write()
    dirty = False

    def set_links(target, links):
        nonlocal dirty
        entry = data.get(target)
        if not isinstance(entry, dict):
            entry = data[target] = {}
        if _refs(entry.get('related')) != links:
            entry['related'] = links
            dirty = True

    if row['artifact']:
        wanted = [v for v in value if v in docs]
        own = _front_related(doc)
        incoming = [x for x, d in docs.items() if x != ref and ref in _front_related(d)]
        # A ref to an output that no longer exists stays: the issue view cannot show it to untick.
        new_own = [x for x in own if x not in docs or x in wanted] + [x for x in wanted if x not in own and x not in incoming]
        if new_own != own:
            seq = CommentedSeq(new_own)
            seq.fa.set_flow_style()
            A.set_front(doc, 'related', seq)
            A.save(doc)
        for other in incoming:
            if other not in wanted:
                d = docs[other]
                seq = CommentedSeq([x for x in _front_related(d) if x != ref])
                seq.fa.set_flow_style()
                A.set_front(d, 'related', seq)
                A.save(d)
        stored = [v for v in value if v not in docs]
        if stored != [x for x in _refs(_entry(data, ref).get('related')) if x not in docs]:
            set_links(ref, stored)
    else:
        set_links(ref, value)
    for other in read() + project_roots():
        if other['id'] == ref or (row['artifact'] and other['artifact']):
            continue
        links = _refs(_entry(data, other['id']).get('related'))
        if other['id'] in value and ref not in links:
            set_links(other['id'], links + [ref])
        elif other['id'] not in value and ref in links:
            set_links(other['id'], [x for x in links if x != ref])
    return save_yaml(path(), data, meta_sha) if dirty else meta_sha


EDITABLE = ('title', 'description', 'status', 'priority', 'assignee', 'due', 'labels', 'project', 'related',
            'direction')


def _validated(ref: str, row: dict, field: str, value: Any) -> Any:
    """The value as update() stores it, or IssueError. One rule set for the page and the assistant."""
    if field not in EDITABLE:
        raise IssueError('不能修改这个属性。')
    if row['project_root'] and field == 'project':
        raise IssueError('课题本身就是父议题，不能再归入另一个课题。')
    if field in ('labels', 'related'):
        value = list(dict.fromkeys(str(v).strip() for v in value if str(v).strip()))
    else:
        value = str(value or '').strip()
    if field == 'title' and (not value or len(value) > 120):
        raise IssueError('标题需要 1–120 个字。')
    if field == 'description' and len(value) > 30000:
        raise IssueError('描述请控制在三万个字以内。')
    if field == 'status' and value not in STATES:
        raise IssueError('请选择有效的状态。')
    if field == 'priority' and value not in PRIORITIES:
        raise IssueError('请选择有效的优先级。')
    if field == 'assignee':
        _check_assignee(value)
    if field == 'due' and value:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError()
        except ValueError:
            raise IssueError('请输入有效的截止日期。') from None
    if field == 'direction' and value and value not in {str(d.get('slug')) for d in datafiles.read('directions') or []}:
        raise IssueError('请选择已有研究方向。')
    if field == 'labels' and any(v not in LABELS for v in value):
        raise IssueError('请选择已有标签。')
    if field == 'related' and any(v == ref or not get(v) for v in value):
        raise IssueError('关联必须是其他已有议题。')
    if field == 'project':
        projects = {str(p['slug']): p for p in datafiles.read('projects') or []}
        if value not in projects:
            raise IssueError('请选择已有课题。')
        if value != row['project'] and (row['parent'] or descendants(ref, read())):
            raise IssueError('父子议题需要属于同一个课题，请先在原课题内整理父子关系。')
    return value


def week_due(value: Any) -> str:
    """A plan week in a confirmation row stored before card 0093, as a due date on that week's Friday;
    empty stays empty (no due date). Nothing current writes a week."""
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        year, week = value.split('-W')
        date.fromisocalendar(int(year), int(week), 1)
        if not T.WEEK_RE.match(value):
            raise ValueError()
    except ValueError:
        raise IssueError('请输入有效的计划周。') from None
    return T.week_due(value)


def update(ref: str, field: str, value: Any, expected: str, *, carried_delta: int | None = None,
           actor: str | None = None) -> str:
    """Returns the issue's revision as this write left it, built from the shas the write produced
    (not re-read), so a change made elsewhere in between still fails the next save. carried_delta is
    for undo only: a task's due date then changes the postponement count by it instead of by the
    usual rule. actor='user' (Leo's own edit on the page) also writes a grey line in the activity
    (card 0097, note_change); the assistant's changes write their own line in change_fields."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    value = _validated(ref, row, field, value)
    old = value_of(row, field) if field in EVENT_FIELDS else None
    if field == 'related':
        meta_sha = _save_related(ref, row, value, doc, meta_sha)
        if actor == 'user':
            meta_sha = note_change(ref, field, old, value, meta_sha)
        return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')
    projects = {str(p['slug']): p for p in datafiles.read('projects') or []} if field == 'project' else {}
    if row['project_root']:
        if field in ('title', 'direction'):
            # A 课题's direction lives in projects.yml; its sub-issues inherit it (read()).
            # A new direction renumbers the 课题 and its issues below (_renumber), so not in update_project too.
            extra = {'renumber': False} if field == 'direction' else {}
            project_sha = datafiles.update_project(row['project'], {'name' if field == 'title' else 'direction': value}, project_sha,
                                                   **extra)
        else:
            meta_sha = _save_meta(ref, {field: value}, meta_sha, drop_week=field == 'due')
    elif not row['artifact'] and field in ('title', 'description', 'status', 'due', 'project', 'priority', 'assignee', 'labels'):
        task_sha = T.update(ref, {'note' if field == 'description' else field: value}, task_sha, cascade_status=False,
                            adjust_postponed=carried_delta if field == 'due' else None,
                            **({'renumber': False} if field == 'project' else {}))
    elif row['artifact'] and field == 'direction':
        A.set_front(doc, 'direction', value or None, after='project')    # an output's own direction, as the move below keeps it
        A.save(doc)
    elif row['artifact'] and field in ('title', 'project'):
        a = doc
        if field == 'project':
            # A direction copied from the old project follows the move; one the user chose stays.
            inherited = str(a.get('direction') or '') in ('', str(projects.get(row['project'], {}).get('direction') or ''))
            if inherited:
                A.set_front(a, 'direction', str(projects[value].get('direction') or '') or None, after='project')
        a.front[field] = value
        A.save(a)
    else:
        meta_sha = _save_meta(ref, {field: value}, meta_sha, drop_week=field == 'due')
    if actor == 'user' and field in EVENT_FIELDS:
        meta_sha = note_change(ref, field, old, value, meta_sha)
    if field in ('direction', 'project'):
        meta_sha = _renumber(meta_sha)        # after Leo's own line, so 「编号 … → …」 reads as its consequence
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


# ---------------------------------------------------------------- the activity thread (card 0097, ISSUES-LINEAR §2.3, §2.6)
# One thread per issue: Leo's and the assistant's comments, the assistant's change lines, Leo's own property
# changes as grey events, and an output's 记录 from updates.yml. Comments are Markdown (files/markdown.py renders
# them); Leo can edit and delete his own. Events are entries with kind: event and the changes they sum up.

EVENT_FIELDS = ('title', 'status', 'priority', 'due', 'labels', 'related', 'project', 'direction', 'assignee')
EVENT_MERGE = timedelta(minutes=1)    # Leo's changes to one issue within a minute read as one line
COMMENT_MAX = 4000


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime('%Y-%m-%d %H:%M:%S')


def _at(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or '').replace(' ', 'T'))
    except ValueError:
        return None


def _event_text(changes: list[dict]) -> str:
    related = any(c['field'] == 'related' for c in changes)
    titles = named() if related else None
    return '；'.join(phrase(c['field'], c['old'], c['new'], titles=titles) for c in changes)


def note_change(ref: str, field: str, old, new, meta_sha: str, *, now: datetime | None = None) -> str:
    """Leo changed a property: append a grey event, or fold it into his event of the last minute (a field changed
    twice keeps its first old value; one changed back drops out; nothing left removes the event). Returns the
    issues.yml sha after the write; a failure is logged and leaves the sha as it was (the change itself stands)."""
    now = now or datetime.now()
    old = _as_list(old) if field in ('labels', 'related') else str(old or '').strip()
    new = _as_list(new) if field in ('labels', 'related') else str(new or '').strip()
    try:
        data = _meta_for_write()
        entries = list(_entry(data, ref).get('comments', []) or [])
        last = entries[-1] if entries else None
        recent = (isinstance(last, dict) and last.get('kind') == 'event' and last.get('by') == 'user'
                  and (t := _at(last.get('at'))) is not None and now - t <= EVENT_MERGE)
        changes = [dict(c) for c in (last.get('changes') or [])] if recent else []
        mine = next((c for c in changes if c.get('field') == field), None)
        if mine is None:
            changes.append({'field': field, 'old': old, 'new': new})
        else:
            mine['new'] = new
        changes = [c for c in changes if not same(c['field'], c['old'], c['new'])]
        if recent:
            entries.pop()
        if changes:
            entries.append({'by': 'user', 'kind': 'event', 'changes': changes, 'text': _event_text(changes), 'at': _stamp(now)})
        elif not recent:
            return meta_sha
        return _save_meta(ref, {'comments': entries}, meta_sha)
    except (StaleFileError, OSError, IssueError, datafiles.DataFileError) as e:
        log.warning("change line for %s not written: %s", ref, type(e).__name__)
        return meta_sha


def _comment_index(entries: list, key: str) -> int | None:
    """By id; a comment from before ids is named by its time, refused when two share that second (the assistant
    once wrote several lines for Leo in one request), so an edit never lands on the wrong one."""
    hits = [i for i, c in enumerate(entries)
            if isinstance(c, dict) and (str(c.get('id') or '') == key or (not c.get('id') and str(c.get('at') or '') == key))]
    if len(hits) > 1:
        raise IssueError('有两条同一时间的评论，请刷新后重试。')
    return hits[0] if hits else None


def _own(c) -> bool:
    return isinstance(c, dict) and c.get('by') == 'user' and c.get('kind') != 'event'


def _comment_text(text) -> str:
    text = str(text or '').replace('\r\n', '\n').strip()
    if not text or len(text) > COMMENT_MAX:
        raise IssueError(f'评论需要 1–{COMMENT_MAX} 个字。')
    return text


def edit_comment(ref: str, key: str, text, expected: str) -> str:
    """Leo rewrites one of his comments. Returns the revision this write left."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    text = _comment_text(text)
    entries = list(_entry(_meta_for_write(), ref).get('comments', []) or [])
    i = _comment_index(entries, key)
    if i is None or not _own(entries[i]):
        raise IssueError('只能修改自己的评论。')
    entries[i] = {**dict(entries[i]), 'text': text, 'edited': _stamp()}
    meta_sha = _save_meta(ref, {'comments': entries}, meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def delete_comment(ref: str, key: str, expected: str) -> str:
    """Leo deletes one of his comments. Returns the revision this write left."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    entries = list(_entry(_meta_for_write(), ref).get('comments', []) or [])
    i = _comment_index(entries, key)
    if i is None or not _own(entries[i]):
        raise IssueError('只能删除自己的评论。')
    del entries[i]
    meta_sha = _save_meta(ref, {'comments': entries}, meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


# ---------------------------------------------------------------- attachments and materials (card 0099, ISSUES-LINEAR §2.9)
# Under the description: files attached to the issue and papers / datasets it draws on. Both are lists in the issue's
# issues.yml entry, for every kind of issue (a task's entry there already holds its comments):
#   attachments  "private/<name>" | "drafts/<name>": a file in the 课题's folders (files/deliverables.py). It goes to
#                private/<slug>/ first (stays on this Mac); Leo can move it to drafts/ (in git). Detaching only drops
#                the reference, the file stays.
#   materials    "paper:<id>" | "dataset:DS-YYYY-NNNN": rows of the library's SQLite. The 课题 page counts the
#                papers its issues name among its 间接关联 (read only; paper_projects is not written).

ATTACH_TYPES = {'.pdf', '.docx', '.doc', '.pages', '.odt', '.rtf', '.txt', '.md', '.tex', '.csv', '.xlsx', '.xls',
                '.pptx', '.key', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic',
                '.epub', '.mobi', '.azw3', '.azw', '.djvu', '.fb2', '.html', '.htm'}
ATTACH_MAX = 200 * 1024 * 1024
MATERIAL_RE = re.compile(r'^(paper):([A-Za-z0-9_.:\-]{1,120})$|^(dataset):(DS-\d{4}-\d{4})$')


def _refs_list(ref: str, key: str) -> list[str]:
    return [str(x) for x in (_entry(_metadata(), ref).get(key) or []) if str(x or '').strip()]


def _attach_slug(row: dict) -> str:
    slug = str(row.get('project') or '')
    if not slug:
        raise IssueError('先给议题选一个课题，附件放在课题的文件夹里。')
    return slug


def attachments(ref: str) -> list[dict]:
    """The issue's attached files: rel, name, folder (private | drafts) and whether the file is still there."""
    row = get(ref)
    out = []
    for rel in _refs_list(ref, 'attachments'):
        key, _, name = rel.partition('/')
        try:
            there = bool(row and row.get('project')) and deliverables.resolve(row['project'], rel).is_file()
        except deliverables.DeliverableError:
            there = False
        out.append({'rel': rel, 'name': name, 'folder': key, 'folder_label': '产出' if key == 'drafts' else dict(deliverables.FOLDERS).get(key, key),
                    'exists': there})
    return out


def _save_list(ref: str, key: str, values: list[str], meta_sha: str) -> str:
    return _save_meta(ref, {key: values} if values else {key: []}, meta_sha)


class AttachmentLinkError(IssueError):
    def __init__(self, name, stale):
        super().__init__(f'文件「{name}」已保存到课题文件夹；议题关联失败，请刷新后核对。')
        self.stale = stale


def _content_write(fn):
    from functools import wraps
    @wraps(fn)
    def guarded(*args, **kwargs):
        with write_lock(path().parent):
            return fn(*args, **kwargs)
    return guarded


@_content_write
def attach_upload(ref: str, filename: str, data: bytes, expected: str, *, folder_key: str = 'private') -> str:
    """Store an upload in the issue's private/ or drafts/ folder and attach it. Only document, ebook, web page and
    image types, at most ATTACH_MAX; the name is cleaned by deliverables.save_upload (no folders, no control
    characters) and never overwrites a file. A web page is as safe as the rest because no attachment is ever served
    by the app: they open with the system's program (deliverables.open_with_system), outside the app's origin.
    Returns the revision this write left."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    slug = _attach_slug(row)
    if folder_key not in ('private', 'drafts'):
        raise IssueError('没有这个附件文件夹。')
    try:
        name = deliverables.clean_name(filename)
    except deliverables.DeliverableError as e:
        raise IssueError(str(e)) from None
    # The type is the cleaned name's: "a.html\.pdf" cleans to "pdf", which has none and is refused.
    suffix = Path(name).suffix.lower()
    if suffix not in ATTACH_TYPES or not Path(name).stem:
        raise IssueError('只能附上文档、电子书、表格、幻灯片、网页或图片（PDF、Word、EPUB、MOBI、AZW、DJVU、FB2、HTML 等）。')
    if len(data) > ATTACH_MAX:
        raise IssueError(f'附件不能超过 {ATTACH_MAX // (1024 * 1024)} MB。')
    try:
        saved = deliverables.save_upload(slug, name, data, folder_key=folder_key)
    except deliverables.DeliverableError as e:
        raise IssueError(str(e)) from None
    rels = _refs_list(ref, 'attachments') + [f'{folder_key}/{saved.name}']
    try:
        meta_sha = _save_list(ref, 'attachments', rels, meta_sha)
    except Exception as e:
        # Keep the file: an external editor may already have changed the saved copy.
        raise AttachmentLinkError(saved.name, isinstance(e, StaleFileError)) from e
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def detach(ref: str, rel: str, expected: str) -> str:
    """Drop an attachment from the issue; the file stays in the 课题's folder."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    rels = _refs_list(ref, 'attachments')
    if rel not in rels:
        raise IssueError('这个附件已经不在议题上了。')
    meta_sha = _save_list(ref, 'attachments', [r for r in rels if r != rel], meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def move_attachment(ref: str, rel: str, to: str, expected: str) -> str:
    """私有 ↔ 草稿: move the file between the 课题's private/ and drafts/ folders and follow it."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    rels = _refs_list(ref, 'attachments')
    if rel not in rels or to not in dict(deliverables.FOLDERS):
        raise IssueError('这个附件已经不在议题上了。')
    slug = _attach_slug(row)
    if rel.startswith(to + '/'):
        return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')
    try:
        src = deliverables.resolve(slug, rel)
    except deliverables.DeliverableError:
        raise IssueError('找不到这个文件，可能已被移动或删除。') from None
    target_dir = deliverables.folder(slug, to)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = deliverables.unique(target_dir / src.name)
    src.rename(target)
    new = f'{to}/{target.name}'
    meta_sha = _save_list(ref, 'attachments', [new if r == rel else r for r in rels], meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def attachment_path(ref: str, rel: str) -> Path:
    """The file behind one of the issue's attachments (for 用系统程序打开), or IssueError."""
    row = get(ref)
    if row is None or rel not in _refs_list(ref, 'attachments'):
        raise IssueError('这个附件已经不在议题上了。')
    try:
        return deliverables.resolve(_attach_slug(row), rel)
    except deliverables.DeliverableError:
        raise IssueError('找不到这个文件，可能已被移动或删除。') from None


def _material_exists(conn, item: str) -> bool:
    m = MATERIAL_RE.match(item or '')
    if not m:
        return False
    if m.group(1):
        return conn.execute('SELECT 1 FROM papers WHERE id = ?', (m.group(2),)).fetchone() is not None
    return conn.execute('SELECT 1 FROM datasets WHERE id = ?', (m.group(4),)).fetchone() is not None


@_content_write
def set_material(conn, ref: str, item: str, on: bool, expected: str) -> str:
    """Link or unlink a paper or dataset of the library. Returns the revision this write left."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    if not get(ref):
        raise IssueError('找不到这个议题。')
    items = _refs_list(ref, 'materials')
    if on:
        if not _material_exists(conn, item):
            raise IssueError('文献库里没有这篇文献或这个数据集。')
        if item not in items:
            items.append(item)
    else:
        items = [x for x in items if x != item]
    meta_sha = _save_list(ref, 'materials', items, meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def _paper_label(r) -> str:
    try:
        names = json.loads(r['authors_json'] or '[]')
    except (TypeError, ValueError):
        names = []
    first = names[0] if names else ''
    if isinstance(first, dict):
        first = first.get('family') or first.get('name') or ''
    surname = str(first).split(',')[0].split()[-1] if str(first).strip() else ''
    lead = ' '.join(x for x in (surname, str(r['year'] or '')) if x)
    return f"{lead} · {r['title']}" if lead else str(r['title'])


def materials(conn, ref: str) -> list[dict]:
    """The issue's papers and datasets for the strip: item, kind, label, meta, url; a row since deleted from the
    library shows as missing rather than vanishing."""
    out = []
    for item in _refs_list(ref, 'materials'):
        m = MATERIAL_RE.match(item)
        if not m:
            continue
        if m.group(1):
            r = conn.execute('SELECT id, title, year, venue, authors_json FROM papers WHERE id = ?', (m.group(2),)).fetchone()
            out.append({'item': item, 'kind': 'paper', 'label': _paper_label(r) if r else m.group(2),
                        'meta': '' if r else '已从文献库删除', 'url': f'/papers/{m.group(2)}' if r else None})
        else:
            r = conn.execute('SELECT id, name, rows, cols FROM datasets WHERE id = ?', (m.group(4),)).fetchone()
            out.append({'item': item, 'kind': 'dataset', 'label': r['name'] if r else '数据集',
                        'meta': f"{r['rows']} 行" if r else '已删除', 'url': f'/data/datasets/{m.group(4)}' if r else None})
    return out


def search_materials(conn, ref: str, q: str, limit: int = 8) -> dict:
    """The picker's three groups: this 课题's papers, other papers in the library, datasets. Each hit says whether
    the issue already names it."""
    row = get(ref) or {}
    linked = set(_refs_list(ref, 'materials'))
    slug = str(row.get('project') or '')
    q = ' '.join(str(q or '').split())[:80]
    like = f'%{q}%'
    mine = {r[0] for r in conn.execute('SELECT paper_id FROM paper_projects WHERE project_slug = ?', (slug,))} if slug else set()
    where, args = ('(p.title LIKE ? OR p.id LIKE ? OR p.authors_json LIKE ? OR p.venue LIKE ?)', [like] * 4) if q else ('1', [])
    rows = conn.execute(f'SELECT p.id, p.title, p.year, p.venue, p.authors_json, '
                        f'(SELECT COUNT(*) FROM excerpts e WHERE e.paper_id = p.id) AS n_excerpts FROM papers p WHERE {where} '
                        f'ORDER BY p.year DESC, p.title LIMIT 200', args).fetchall()

    def paper(r):
        item = f"paper:{r['id']}"
        meta = ' · '.join(x for x in (r['venue'] or '', f"有 {r['n_excerpts']} 条划线" if r['n_excerpts'] else '') if x)
        return {'item': item, 'kind': 'paper', 'label': _paper_label(r), 'meta': meta, 'linked': item in linked}
    here = [paper(r) for r in rows if r['id'] in mine][:limit]
    other = [paper(r) for r in rows if r['id'] not in mine][:limit]
    dwhere, dargs = ('(name LIKE ? OR original_name LIKE ? OR id LIKE ?)', [like] * 3) if q else ('1', [])
    data = [{'item': f"dataset:{r['id']}", 'kind': 'dataset', 'label': r['name'], 'meta': f"{r['rows']} 行 · {r['cols']} 列",
             'linked': f"dataset:{r['id']}" in linked}
            for r in conn.execute(f'SELECT id, name, rows, cols FROM datasets WHERE {dwhere} ORDER BY created_at DESC LIMIT ?',
                                  [*dargs, limit])]
    return {'here': here, 'other': other, 'datasets': data, 'q': q}


def project_material_ids(slug: str) -> dict[str, set[str]]:
    """Papers and datasets the issues of one 课题 name (its own entry included), for the 课题 page's 间接关联."""
    refs = {r['id'] for r in read() if r.get('project') == slug} | {project_ref(slug)}
    out = {'paper': set(), 'dataset': set()}
    for ref in refs:
        for item in _refs_list(ref, 'materials'):
            m = MATERIAL_RE.match(item)
            if m and m.group(1):
                out['paper'].add(m.group(2))
            elif m:
                out['dataset'].add(m.group(4))
    return out


# ---------------------------------------------------------------- papers ↔ issues (card 0131, LIBRARY-UI.md §3.6, 卡 F)
# A link has one home, the issue's `materials` in issues.yml (card 0099 writes `paper:<id>` there and set_material
# changes it); the library only reads it backwards. The index is rebuilt when a file it reads changes, so a list of
# fifty paper rows reads them once.

_paper_index: tuple = ((), {})


def _index_stamp() -> tuple:
    files = [path(), T.path(), keys_path(), get_paths().data_yml / 'projects.yml', *A.list_paths()]
    return tuple((str(f), f.stat().st_mtime_ns if f.exists() else 0) for f in files)


def paper_issue_index() -> dict[str, list[dict]]:
    """paper id → the issues whose materials name it, each {id, key, title, url, status, project, label}, by key.
    Archived outputs are left out, as everywhere else issues are listed."""
    global _paper_index
    stamp = _index_stamp()
    if _paper_index[0] == stamp:
        return _paper_index[1]
    rows = {r['id']: r for r in read() + project_roots() if not r.get('archived')}
    index: dict[str, list[dict]] = {}
    for ref, entry in _metadata().items():
        r = rows.get(str(ref))
        if r is None or not isinstance(entry, dict):
            continue
        for item in entry.get('materials') or []:
            m = MATERIAL_RE.match(str(item))
            if m and m.group(1):
                index.setdefault(m.group(2), []).append(dict(
                    {k: r.get(k, '') for k in ('id', 'key', 'title', 'url', 'status', 'project')},
                    label=f"{r['key']} · {r['title']}" if r.get('key') else r['title']))
    for found in index.values():
        found.sort(key=lambda x: (x['key'] or '~', x['title']))
    _paper_index = (stamp, index)
    return index


def revisions(refs) -> dict[str, str]:
    """Each issue's revision as revision(ref) gives it, the shared files read once."""
    docs = _documents()
    meta_sha, task_sha, project_sha = _meta_sha(), T.file_sha(), datafiles.file_sha('projects')
    return {ref: _revision(meta_sha, task_sha, project_sha, docs[ref].sha if ref in docs else '') for ref in refs}


def paper_issues(paper_id, *, with_revisions: bool = False) -> list[dict]:
    """The issues that use one paper; with_revisions adds each one's revision, for an Unlink that is version-checked."""
    found = [dict(x) for x in paper_issue_index().get(str(paper_id), [])]
    if with_revisions and found:
        revs = revisions([x['id'] for x in found])
        for x in found:
            x['revision'] = revs[x['id']]
    return found


def issues_for_paper(conn, paper_id: str, q: str = '', limit: int = 12) -> dict:
    """The 议题 picker on a paper's page: issues whose key, title or 课题 match q; the ones already linked first, then
    those in the paper's own 课题, open before finished. Each hit says whether it links the paper and carries its
    revision (set_material checks it)."""
    linked = {x['id'] for x in paper_issues(paper_id)}
    mine = {r[0] for r in conn.execute('SELECT project_slug FROM paper_projects WHERE paper_id = ?', (paper_id,))}
    names = {str(p['slug']): str(p.get('name') or p['slug']) for p in datafiles.read('projects') or []}
    q = ' '.join(str(q or '').split())[:80]
    target = resolve(q) if normal_key(q) else None          # APUS-0002, apus-2, or a key from before a move

    def match(r):
        if not q:
            return True
        if target:
            return r['id'] == target
        hay = ' '.join([r.get('key') or '', r['title'], names.get(r['project'], '')]).casefold()
        return all(w.casefold() in hay for w in q.split())
    found = sorted((r for r in read() + project_roots() if not r.get('archived') and match(r)),
                   key=lambda r: (r['id'] not in linked, r['project'] not in mine, r['status'] in ('done', 'parked'),
                                  r.get('key') or '~', r['title']))[:limit]
    revs = revisions([r['id'] for r in found])
    return {'q': q, 'hits': [dict(ref=r['id'], key=r.get('key', ''), title=r['title'],
                                  project='课题' if r['project_root'] else names.get(r['project'], ''),
                                  linked=r['id'] in linked, revision=revs[r['id']]) for r in found]}


def create(title: str, project: str, parent: str, expected: str, *, with_revision: bool = False,
           origin: str = 'user', **properties):
    """Returns the new task's ref; with_revision=True returns (ref, the parent's revision as this
    write left it), so the page that created it can keep saving without a reload."""
    meta_sha, task_sha, project_sha, doc = _check(parent, expected)
    values = {'title': title, 'project': project, 'origin': origin}
    if parent:
        row = get(parent)
        if row is None:
            raise IssueError('找不到父议题。')
        values.update(project=row['project'])
        if not row['project_root']:
            values['source' if row['artifact'] else 'parent'] = parent
    allowed = {'description', 'status', 'due', 'priority', 'assignee', 'labels'}
    if set(properties) - allowed:
        raise IssueError('不能设置这个属性。')
    if properties.get('assignee'):
        _check_assignee(str(properties['assignee']))
    for k, v in properties.items():
        values['note' if k == 'description' else k] = v
    ref, task_sha = T.add(values, task_sha)
    return (ref, _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')) if with_revision else ref


def comments(ref):
    """The issue's thread, oldest first: issues.yml comments and events plus an output's updates.yml 记录. Each item
    keeps its stored keys (by, text, at, via, link) and adds key (what edit/delete name it by), source (issue |
    update), kind (comment | event | system) and own (Leo may edit or delete it)."""
    items = []
    for u in reversed(updates.for_target(ref)):     # oldest first, so entries of the same minute keep their order
        kind = 'system' if u['by'] == 'system' else 'comment'
        items.append({**u, 'key': u['id'], 'source': 'update', 'kind': kind, 'own': u['by'] == 'user'})
    for c in _entry(_metadata(), ref).get('comments', []) or []:
        if not isinstance(c, dict) or not c.get('text'):
            continue
        c = dict(c)
        if not updates.LINK_RE.match(str(c.get('link') or '')):
            c.pop('link', None)            # a hand edit never becomes an off-site or javascript: href
        if c.get('by') != 'assistant' or not START_RE.match(str(c.get('start') or '')):
            c.pop('start', None)           # nor a button that posts anywhere but a hand-over's 开始
        kind = 'event' if c.get('kind') == 'event' else 'comment'
        items.append({**c, 'key': str(c.get('id') or c.get('at') or ''), 'source': 'issue', 'kind': kind,
                      'own': _own(c), 'at': str(c.get('at') or '')})
    return sorted(items, key=lambda x: str(x.get('at') or ''))


# The 开始 button under an assistant's hand-over plan (card 0116) posts here; nothing else is drawn as a button.
START_RE = re.compile(r'^/agent/handover/[A-Za-z0-9_-]+/start$')


def comment(ref, text, expected, *, by='user', via='', link='', start='', attachments=()):
    """by: user | assistant. via='assistant' marks Leo's own words typed in through the assistant
    (shown under his name, 经助手录入); link is an in-app path, e.g. the conversation; start, on the
    assistant's hand-over plan only, is where its 开始 button posts (agent/tasks.py). attachments (card 0176)
    are "drafts/<name>" or "private/<name>" files of the 课题 that already exist: they are listed on the comment
    and joined to the issue's own attachments, so an agent's output file travels with the note that reports it."""
    sha, _, _, _ = _check(ref, expected)
    row = get(ref)
    if not row:
        raise IssueError('找不到这个议题。')
    rels = _existing_rels(row, attachments)
    text = _comment_text(text)
    if by not in ('user', 'assistant') or via not in ('', 'assistant'):
        raise IssueError('未知的评论作者。')
    if link and not updates.LINK_RE.match(link):
        raise IssueError('评论只能链接到应用里的页面。')
    if start and (by != 'assistant' or not START_RE.match(start)):
        raise IssueError('只有助手的计划可以带「开始」按钮。')
    entry = {'id': 'c-' + secrets.token_hex(4), 'by': by, 'text': text, 'at': _stamp()}
    entry.update({k: v for k, v in (('via', via), ('link', link), ('start', start)) if v})
    if rels:
        entry['attachments'] = rels
    _append_comment(ref, entry, sha)
    if rels:
        have = _refs_list(ref, 'attachments')
        new = [r for r in rels if r not in have]
        if new:
            _save_list(ref, 'attachments', have + new, _meta_sha())


def _existing_rels(row: dict, rels) -> list[str]:
    """The given "<folder>/<name>" handles, each checked to be a file of the issue's 课题 (deliverables.resolve)."""
    out = []
    for rel in rels or ():
        rel = str(rel or '').strip()
        if not rel:
            continue
        try:
            deliverables.resolve(_attach_slug(row), rel)
        except deliverables.DeliverableError:
            raise IssueError(f'找不到课题文件 {rel}；只能附上 drafts/ 或 private/ 里已有的文件。') from None
        if rel not in out:
            out.append(rel)
    return out


def attach_existing(ref: str, rel: str, expected: str) -> str:
    """Link a file the 课题 folder already holds (an agent's drafts/ output, say) without uploading it (card 0176).
    Returns the revision this write left."""
    meta_sha, task_sha, project_sha, doc = _check(ref, expected)
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    rels = _existing_rels(row, [rel])
    have = _refs_list(ref, 'attachments')
    if rels and rels[0] not in have:
        meta_sha = _save_list(ref, 'attachments', have + rels, meta_sha)
    return _revision(meta_sha, task_sha, project_sha, doc.sha if doc else '')


def markdown(ref: str) -> str:
    """One issue as Markdown an agent can read whole (card 0176): properties, description, sub-issues, attachments,
    then the thread oldest first. Keys (APUS-0012) name everything; internal refs stay out."""
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    rows = read() + project_roots()
    names = named(rows)
    out = [f"# {key_of(ref) or ref} {row['title']}"]
    props = [('课题', names.get(project_ref(row['project']), row['project'])) if not row.get('project_root') else ('类型', '课题')]
    for field in ('status', 'priority', 'due', 'labels', 'assignee', 'direction'):
        value = value_of(row, field)
        if value not in (None, '', []):
            # ISO dates stay ISO here: an agent parses them; the page keeps its 10 月 21 日 wording.
            props.append((i18n.ISSUE_FIELD.get(field, field), str(value) if field == 'due' else display(field, value, titles=names)))
    out.append('')
    out += [f'- {k}：{v}' for k, v in props]
    if row.get('description'):
        out += ['', '## 描述', '', str(row['description']).strip()]
    children = [r for r in rows if r['id'] != ref and r.get('project') == row.get('project')
                and not r.get('project_root') and (r.get('parent') or '') == ('' if row.get('project_root') else ref)]
    if children:
        out += ['', '## 子议题', '']
        for c in sorted(children, key=lambda r: (str(r.get('due') or '9999'), r['id'])):
            due = f"，截止 {c['due']}" if c.get('due') else ''
            out.append(f"- {c.get('key') or ''} {c['title']}（{display('status', c.get('status'))}{due}）".strip())
    files = attachments(ref)
    if files:
        out += ['', '## 附件', ''] + [f"- {f['rel']}{'' if f['exists'] else '（文件已不在）'}" for f in files]
    thread = [c for c in comments(ref) if c.get('kind') == 'comment']
    if thread:
        out += ['', '## 评论', '']
        for c in thread:
            who = '助手' if c.get('by') == 'assistant' else owner_name()
            out.append(f"### {who} · {str(c.get('at') or '')[:16]}")
            out.append('')
            out.append(str(c.get('text') or '').strip())
            if c.get('attachments'):
                out.append('')
                out.append('附件：' + '、'.join(str(a) for a in c['attachments']))
            out.append('')
    return '\n'.join(out).rstrip() + '\n'


def project_markdown(slug: str) -> str:
    """The 课题 as its parent issue plus every sub-issue in full, for an agent that picks up the whole project."""
    root = project_ref(slug)
    parts = [markdown(root)]
    for r in sorted((r for r in read() if r.get('project') == slug and not r.get('project_root')),
                    key=lambda r: (str(r.get('due') or '9999'), r['id'])):
        parts.append(markdown(r['id']))
    return '\n---\n\n'.join(parts)


def clear_start(ref: str, start: str) -> bool:
    """Take the 开始 button off the comment that carries it, once the plan has started or the hand-over ended."""
    for _ in range(_TRIES):
        sha = _meta_sha()
        entries = list(_entry(_meta_for_write(), ref).get('comments', []) or [])
        hits = [i for i, c in enumerate(entries) if isinstance(c, dict) and c.get('start') == start]
        if not hits:
            return False
        for i in hits:
            entries[i] = {k: v for k, v in dict(entries[i]).items() if k != 'start'}
        try:
            _save_meta(ref, {'comments': entries}, sha)
            return True
        except StaleFileError:
            continue
    raise IssueError(BUSY)


def _append_comment(ref, entry, sha):
    entries = list(_entry(_meta_for_write(), ref).get('comments', []) or [])
    entries.append(entry)
    _save_meta(ref, {'comments': entries}, sha)


# ---------------------------------------------------------------- assistant writes (docs/design/ISSUE-AGENT.md §4, §5)
# Callers go through pm/changes.py, which records every row in assistant_changes first.

ASSISTANT_FIELDS = ('status', 'priority', 'due', 'labels', 'related', 'project', 'title', 'description', 'direction')
ASSISTANT_CREATE = ('description', 'status', 'due', 'priority', 'labels')   # no assignee, as on change
ERRORS = (IssueError, T.TaskError, datafiles.DataFileError, updates.UpdateError, KeyError)
BUSY = '议题一直在被修改，稍后再试。'
_TRIES = 3
_STATE = ('title', 'project', 'status', 'parent', 'due', 'words', 'file', 'source', 'postponed', 'note',
          'priority', 'assignee', 'labels')


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]


def error_text(e: Exception) -> str:
    return '找不到这个议题。' if isinstance(e, KeyError) else str(e)


def _key(field: str, value):
    """A status or priority given by its Chinese label (进行中, 高) as its stored key."""
    names = STATES if field == 'status' else PRIORITIES if field == 'priority' else None
    if names and isinstance(value, str) and value not in names:
        return next((k for k, v in names.items() if v == value.strip()), value)
    return value


def value_of(row: dict, field: str):
    """A field as change_fields compares it: lists for labels and related, stripped text otherwise."""
    return _as_list(row.get(field)) if field in ('labels', 'related') else str(row.get(field) or '').strip()


def same(field: str, a, b) -> bool:
    if field in ('labels', 'related'):
        return sorted(set(_as_list(a))) == sorted(set(_as_list(b)))
    return str(a or '').strip() == str(b or '').strip()


def display(field: str, value, *, titles: dict | None = None, today: date | None = None) -> str:
    """One value in words, the way activity lines and confirmation cards show it."""
    today = today or date.today()
    if field in ('labels', 'related'):
        items = _as_list(value)
        if field == 'related':
            titles = titles if titles is not None else named()
            items = [f"《{titles.get(v, v)}》" for v in items]
        return '、'.join(items) or '无'
    value = str(value or '').strip()
    if field == 'status':
        return STATES.get(value, value)
    if field == 'priority':
        return PRIORITIES.get(value, value)
    if field == 'assignee':
        return owner_name() if value in OWNER_VALUES else assignees().get(value, value)
    if not value:
        return '未排期' if field == 'week' else '无'
    if field == 'week' and T.WEEK_RE.match(value):
        y, w = value.split('-W')
        return f"第 {int(w)} 周" if int(y) == today.year else f"{y} 年第 {int(w)} 周"
    if field == 'due':
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return value
        return f"{d.month} 月 {d.day} 日" if d.year == today.year else f"{d.year} 年 {d.month} 月 {d.day} 日"
    if field == 'project':
        return next((str(p.get('name')) for p in datafiles.read('projects') or [] if str(p.get('slug')) == value), value)
    if field == 'direction':
        return next((str(d.get('name')) for d in datafiles.read('directions') or [] if str(d.get('slug')) == value), value)
    if field == 'title':
        return f"「{value}」"
    if field == 'description':
        return value if len(value) <= 40 else value[:40] + '…'
    return value


def phrase(field: str, old, new, *, titles: dict | None = None) -> str:
    """「状态 待办 → 进行中」; a description is only said to have changed, never quoted in full."""
    label = i18n.ISSUE_FIELD.get(field, field)
    if field == 'description':
        return '写了描述' if not str(old or '').strip() else ('清空了描述' if not str(new or '').strip() else '改写了描述')
    return f"{label} {display(field, old, titles=titles)} → {display(field, new, titles=titles)}"


def _line(phrases: list[str], source: dict | None, undo: bool) -> str:
    text = ('撤销：' if undo else '') + '；'.join(phrases)
    title = str((source or {}).get('title') or '').strip()
    return text + (f" · 来自对话《{title}》" if title else '')


def activity(ref: str, text: str, *, by: str = 'assistant', link: str = '', row: dict | None = None) -> bool:
    """One line in the issue's activity: an output's goes to its 记录 in updates.yml, a task's or a
    课题's to issues.yml comments. Never fails the change it describes (logged, returns False)."""
    row = row or get(ref)
    text = ' '.join(str(text or '').split())
    if row is None or not text:
        return False
    link = link if link and updates.LINK_RE.match(link) else ''
    try:
        entry = {'by': by, 'text': text[:4000], 'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        if link:
            entry['link'] = link
        for _ in range(_TRIES):
            try:
                if row['artifact']:
                    updates.add(ref, by, text[:updates.TEXT_MAX], updates.file_sha(), link=link)
                else:
                    _append_comment(ref, entry, _meta_sha())
                return True
            except StaleFileError:
                continue
    except (IssueError, updates.UpdateError, StaleFileError, OSError) as e:
        log.warning("activity line for %s not written: %s", ref, type(e).__name__)
    return False


def change_fields(ref: str, changes, *, actor: str = 'assistant', source: dict | None = None,
                  undo: bool = False, carried_delta: int | None = None) -> dict:
    """Write each field of one issue whose current value still equals what the caller saw.

    changes: [(field, seen_old, new)] in stored values (status keys like 'doing', ISO date,
    label names, refs; a status or priority may also be its Chinese label). Only ASSISTANT_FIELDS,
    whoever the actor. Everything is validated first (update()'s rules), then each field is re-read
    and written through update() with the revision read just then: only Leo changing that same field
    stops it, not his edits to other tasks in tasks.yml. Returns
      {ref, title, url, applied: [{field, old, new, carried?}], skipped: [{field, seen, current}],
       same: [{field, value}], failed: [{field, error}], line}
    applied old/new are the values as stored before and after the write (a title's spaces collapse
    on the way in), so undo compares against what is really there. skipped = Leo changed the field
    since; same = it already had the new value, nothing written; postponed = how much a task's due
    date change moved the postponement count (undo passes minus it as carried_delta, 0 included).
    The applied fields get one activity line (one request, one issue) by `actor`, linking to
    source['url'] and naming source['title'] (the conversation). undo=True words it as 撤销."""
    row = get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    out = dict(ref=ref, title=row['title'], url=row['url'], applied=[], skipped=[], same=[], failed=[], line='')
    pending = []
    for field, seen, new in changes:
        seen, new = _key(field, seen), _key(field, new)
        if field in ('labels', 'related'):
            seen, new = _as_list(seen), _as_list(new)
        if field not in ASSISTANT_FIELDS or (row['project_root'] and field in ('title', 'description')):
            out['failed'].append(dict(field=field, error='助手不能修改这个属性。'))
            continue
        try:
            pending.append((field, seen, _validated(ref, row, field, new)))
        except ERRORS as e:
            out['failed'].append(dict(field=field, error=error_text(e)))
    for field, seen, new in pending:
        for _ in range(_TRIES):
            expected = revision(ref)         # before the read: a write in between fails update() and retries
            row = get(ref)
            if row is None:
                out['failed'].append(dict(field=field, error='找不到这个议题。'))
                break
            current = value_of(row, field)
            if same(field, current, new):
                out['same'].append(dict(field=field, value=current))
                break
            if not same(field, current, seen):
                out['skipped'].append(dict(field=field, seen=seen, current=current))
                break
            task = field == 'due' and not row['artifact'] and not row['project_root']
            before = T.get(ref)['postponed'] if task else None
            try:
                update(ref, field, new, expected, carried_delta=carried_delta if task else None)
            except StaleFileError:
                continue
            except ERRORS as e:
                out['failed'].append(dict(field=field, error=error_text(e)))
                break
            after = get(ref)
            done = dict(field=field, old=current, new=value_of(after, field) if after else new)
            if task:
                done['postponed'] = (T.get(ref) or {'postponed': before})['postponed'] - before
            out['applied'].append(done)
            break
        else:
            out['failed'].append(dict(field=field, error=BUSY))
    if out['applied']:
        related = any(a['field'] == 'related' for a in out['applied'])
        titles = named() if related else None
        phrases = [phrase(a['field'], a['old'], a['new'], titles=titles) for a in out['applied']]
        out['line'] = _line(phrases, source, undo)
        activity(ref, out['line'], by=actor, link=str((source or {}).get('url') or ''))
    return out


def created_state(ref: str) -> dict | None:
    """What remove_created compares: the task's own fields, without dates it stamps itself."""
    t = T.get(ref)
    return {k: t.get(k) for k in _STATE} if t else None


def create_checked(title: str, *, project: str = '', parent: str = '', actor: str = 'assistant',
                   source: dict | None = None, **properties) -> tuple[str, dict]:
    """create() with a fresh revision of the parent (an add conflicts with nothing Leo is editing),
    origin: assistant, and a 「新建了这个议题」 line on the new issue. Returns (ref, created_state).
    Properties are limited to ASSISTANT_CREATE, the create-side twin of ASSISTANT_FIELDS."""
    if set(properties) - set(ASSISTANT_CREATE):
        raise IssueError('助手不能设置这个属性。')
    for _ in range(_TRIES):
        try:
            ref = create(title, project, parent, revision(parent), origin='assistant', **properties)
            break
        except StaleFileError:
            continue
    else:
        raise IssueError(BUSY)
    state = created_state(ref)
    activity(ref, _line(['新建了这个议题'], source, False), by=actor, link=str((source or {}).get('url') or ''))
    return ref, state


def comment_checked(ref: str, text: str, *, by: str = 'assistant', via: str = '', link: str = '', start: str = '',
                    attachments=()) -> None:
    for _ in range(_TRIES):
        try:
            return comment(ref, text, revision(ref), by=by, via=via, link=link, start=start, attachments=attachments)
        except StaleFileError:
            continue
    raise IssueError(BUSY)


def _untouched_meta(entry: dict) -> bool:
    """Only what the assistant itself put there: its own activity lines and the `updated` stamp."""
    return (all(k in ('comments', 'updated') for k in entry)
            and all(isinstance(c, dict) and (c.get('by') == 'assistant' or c.get('kind') == 'event')
                    for c in entry.get('comments') or []))


def _retire(ref: str) -> None:
    """Replace a deleted task's issues.yml entry with a tombstone (tasks.retired_ids reads it, so the
    id is never issued again). Re-checked at write time: anything Leo added meanwhile stays."""
    for _ in range(_TRIES):
        sha = _meta_sha()
        data = _meta_for_write()
        entry = data.get(ref)
        stamp = SingleQuotedScalarString(date.today().isoformat())
        if isinstance(entry, dict) and not _untouched_meta(entry):
            entry['retired'] = stamp
        else:
            data[ref] = {'retired': stamp}
        try:
            save_yaml(path(), data, sha)
            return
        except StaleFileError:
            continue
    log.warning("tombstone for %s not written: issues.yml kept changing", ref)


def remove_created(ref: str, snapshot: dict | None = None, *, source: dict | None = None) -> str:
    """Undo of an issue the assistant created. Deleted when nobody touched it since: same fields as
    `snapshot` (created_state at creation; without one it counts as touched), no sub-issues,
    relations or properties in issues.yml, no comment but the assistant's. Touched and still open:
    parked. Touched and done or parked: left as Leo has it. Returns 'deleted' | 'parked' | 'kept' |
    'gone'. The only delete the assistant has; the id is retired, never issued again."""
    for _ in range(_TRIES):
        sha = T.file_sha()
        t = T.get(ref)
        if t is None:
            return 'gone'
        if t.get('origin') != 'assistant':
            return 'kept'
        touched = (snapshot is None or created_state(ref) != snapshot or bool(T.children(ref))
                   or not _untouched_meta(_entry(_metadata(), ref)))
        if not touched:
            try:
                T.remove(ref, sha)
            except StaleFileError:
                continue
            _retire(ref)
            return 'deleted'
        row = get(ref)
        if row is None or row['status'] in ('done', 'parked'):
            return 'kept'
        result = change_fields(ref, [('status', row['status'], 'parked')], source=source, undo=True)
        if result['applied']:
            return 'parked'
        if not result['skipped']:
            return 'kept'
    return 'kept'


# ---------------------------------------------------------------- Leo deletes an issue (card 0143)
# Only his own click deletes, and only a 待办-type issue with its sub-issues: a 课题 is projects.yml's and an output a
# research document, so neither goes from here, and the assistant has no delete at all (remove_created above undoes
# only what it made). Everything comes back on Undo: the tasks.yml entries in their places, each id's issues.yml
# entry (properties, comments, materials, attachments), and the relations other issues held. The deleted root's
# tombstone keeps all of it under `deleted`; the sub-issues' tombstones name the root. Tombstones keep the ids
# from being issued again (tasks.retired_ids); keys stay in issue_keys.yml, so a key is never reused and nothing
# is renumbered. Attachment files stay where they are on disk.

def _trash_parent_url(row: dict) -> str:
    if row.get('parent'):
        return url(row['parent'])
    return url(project_ref(row['project'])) if row.get('project') else '/issues'


def delete_count(ref: str, rows: list[dict] | None = None) -> int:
    """How many sub-issues go with this one, for the confirmation."""
    return len(descendants(ref, read() if rows is None else rows))


@_content_write
def delete(ref: str, expected: str) -> dict:
    """Delete a 待办-type issue and its sub-issues. Returns {ref, ids, count, url} (url: where the page goes next,
    the parent or the 课题). StaleFileError when the page's revision is old: nothing is written."""
    import copy
    meta_sha, task_sha, project_sha, _ = _check(ref, expected)
    rows = read()
    row = next((r for r in rows if r['id'] == ref), None) or get(ref)
    if row is None:
        raise IssueError('找不到这个议题。')
    if row['project_root'] or row['artifact']:
        raise IssueError('课题和产出不能在这里删除。')
    kids = descendants(ref, rows)
    if any(k['artifact'] for k in kids):
        raise IssueError('下面挂着产出，不能一起删除。')
    ids = [ref, *[k['id'] for k in kids]]
    removed = T.entries(ids)
    data = _meta_for_write()
    related = {}
    for other, entry in data.items():
        if str(other) in ids or not isinstance(entry, dict):
            continue
        links = _refs(entry.get('related'))
        if any(x in ids for x in links):
            related[str(other)] = links
            entry['related'] = [x for x in links if x not in ids]
    stamp = SingleQuotedScalarString(date.today().isoformat())
    trash = {'at': _stamp(), 'tasks': [{'at': at, 'entry': entry} for at, entry in removed],
             'meta': {i: copy.deepcopy(data[i]) for i in ids if isinstance(data.get(i), dict)}, 'related': related}
    for i in ids:
        data[i] = {'retired': stamp, 'deleted': trash} if i == ref else {'retired': stamp, 'deleted_with': ref}
    task_text = T.delete_text(ids, task_sha)
    try:
        new_meta, new_tasks = atomic_write_many([(path(), dump_yaml(data), meta_sha), (T.path(), task_text, task_sha)])
    except OSError:
        raise IssueError('没能删除议题，请重试。') from None
    return {'ref': ref, 'ids': ids, 'count': len(kids), 'url': _trash_parent_url(row),
            'revision': _revision(new_meta, new_tasks, project_sha)}


@_content_write
def restore(ref: str, expected: str) -> dict:
    """Restore a deletion only at the version shown by its Undo offer."""
    meta_sha, task_sha, _, _ = _check(ref, expected)
    data = _meta_for_write()
    entry = _entry(data, ref)
    trash = entry.get('deleted') if isinstance(entry.get('deleted'), dict) else None
    if not trash or not trash.get('tasks'):
        raise IssueError('没有可以恢复的删除。')
    removed = [(int(t.get('at') or 0), t['entry']) for t in trash['tasks'] if isinstance(t, dict) and t.get('entry')]
    ids = [str(e.get('id')) for _, e in removed]
    root = next((e for _, e in removed if str(e.get('id')) == ref), None)
    parent = str((root or {}).get('parent') or '')
    if parent and T.get(parent) is None:
        raise IssueError('它的父议题已经不在了，没法恢复。')
    if any(T.get(i) is not None for i in ids):
        raise IssueError('这条待办已经在了。')
    for i in ids:
        old = (trash.get('meta') or {}).get(i)
        if isinstance(old, dict):
            data[i] = old
        else:
            data.pop(i, None)
    for other, original in (trash.get('related') or {}).items():
        current = data.get(other)
        if isinstance(current, dict):
            links = _refs(current.get('related'))
            current['related'] = links + [x for x in _refs(original) if x in ids and x not in links]
    task_text = T.restore_text(removed, task_sha)
    try:
        atomic_write_many([(path(), dump_yaml(data), meta_sha), (T.path(), task_text, task_sha)])
    except OSError:
        raise IssueError('没能恢复议题，请重试。') from None
    return {'ref': ref, 'url': url(ref)}


def last_touched(rows: list[dict] | None = None) -> dict[str, str]:
    """ref → ISO date of its newest change or comment: tasks.yml `created` / `updated`, the issues.yml
    `updated` stamp and comments, an output's 记录 in updates.yml and its `created`. The progress
    check's 卡住 rule reads it (pm/checkup.py)."""
    out: dict[str, str] = {}

    def bump(ref, day):
        day = str(day or '')[:10]
        if T.DATE_RE.match(day) and day > out.get(ref, ''):
            out[ref] = day

    for r in read() if rows is None else rows:
        bump(r['id'], r.get('created'))
    for t in T.read():
        bump(t['id'], t.get('created'))
        bump(t['id'], t.get('updated'))
    for ref, m in _metadata().items():
        if isinstance(m, dict):
            bump(str(ref), m.get('updated'))
            for c in m.get('comments') or []:
                if isinstance(c, dict):
                    bump(str(ref), c.get('at'))
    for u in updates.read():
        bump(u['target'], u['at'])
    return out


def migrate_week(*, write: bool = False) -> list[dict]:
    """`rl tasks migrate-week` for issues.yml: the outputs' and 课题' entries get the same rule as
    tasks.yml (tasks.migrate_week). Returns {ref, title, changes: [Chinese lines]} per changed entry;
    writes only with write=True."""
    if not path().exists():
        return []
    sha = _meta_sha()
    data = _meta_for_write()
    titles = {r['id']: r['title'] for r in read() + project_roots()}
    out = []
    for ref, entry in data.items():
        if not isinstance(entry, dict) or 'week' not in entry:
            continue
        week = str(entry.get('week') or '')
        status = entry.get('status', 'todo')
        if not entry.get('due') and status in T.OPEN and T.WEEK_RE.match(week):
            entry['due'] = SingleQuotedScalarString(T.sunday(week))
            line = f"截止设为 {T.sunday(week)}，即原计划周 {week} 的周日"
        elif entry.get('due') and status in T.OPEN:
            line = f"保留截止 {entry.get('due')}，去掉计划周 {week or '空'}"
        else:
            line = f"去掉计划周 {week or '空'}"
        entry.pop('week')
        out.append({'ref': str(ref), 'title': titles.get(str(ref), str(ref)), 'status': status, 'changes': [line]})
    if write and out:
        save_yaml(path(), data, sha)
    return out
