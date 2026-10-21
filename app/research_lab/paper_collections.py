"""集合: user-named reading lists across directions (docs/design/LIBRARY-UI.md §3.4, card 0132). A paper can be in
several; taking it out of one never deletes it, and membership never changes its research direction. The table is
still paper_collections (the old 文件夹); no migration renames it."""
import sqlite3
import hashlib
import json
from uuid import uuid4
from .db.connection import now_iso, get_setting, set_setting


class CollectionError(ValueError):
    pass


def name(value):
    value = ' '.join(str(value or '').split())
    if not 1 <= len(value) <= 60:
        raise CollectionError('集合名称需要 1–60 个字。')
    return value


def get(conn, ref):
    row = conn.execute('SELECT * FROM paper_collections WHERE id=?', (ref,)).fetchone()
    return dict(row) if row else None


def create(conn, value):
    value = name(value)
    ref = uuid4().hex
    try:
        conn.execute('INSERT INTO paper_collections(id,name,created_at,sort_order) VALUES (?,?,?,(SELECT COALESCE(MAX(sort_order),0)+1 FROM paper_collections))', (ref, value, now_iso()))
    except sqlite3.IntegrityError:
        raise CollectionError('已有同名集合，请换一个名称。') from None
    return ref


def rename(conn, ref, value, version):
    value = name(value)
    try:
        result = conn.execute('UPDATE paper_collections SET name=?, version=version+1 WHERE id=? AND version=?', (value, ref, version))
    except sqlite3.IntegrityError:
        raise CollectionError('已有同名集合，请换一个名称。') from None
    if not result.rowcount:
        raise CollectionError('集合已在别处修改，请刷新后重试。')


def delete(conn, ref, version):
    """Remove a collection and its memberships; papers, files, highlights and directions stay."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        if not get(conn, ref):
            raise LookupError(ref)
        if not conn.execute('DELETE FROM paper_collections WHERE id=? AND version=?', (ref, version)).rowcount:
            raise CollectionError('集合已在别处修改，请刷新后重试。')
        conn.execute('DELETE FROM paper_collection_items WHERE collection_id=?', (ref,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def assign(conn, ref, paper_id, *, remove=False):
    if not get(conn, ref):
        raise CollectionError('没有这个集合。')
    if not conn.execute('SELECT 1 FROM papers WHERE id=?', (paper_id,)).fetchone():
        raise CollectionError('没有这篇文献。')
    if remove:
        conn.execute('DELETE FROM paper_collection_items WHERE collection_id=? AND paper_id=?', (ref, paper_id))
    else:
        conn.execute('INSERT OR IGNORE INTO paper_collection_items(collection_id,paper_id) VALUES (?,?)', (ref, paper_id))


def listing(conn):
    """Every collection in Leo's order, counting the members that have their original (as the library lists them):
    total, unread, the directions they come from (directions.yml order, then any other) and when one last changed
    (the collection's own creation when it is empty; memberships carry no date)."""
    from .intake import directions
    order = {str(d['slug']): i for i, d in enumerate(directions.load())}
    rows = []
    for row in conn.execute('''SELECT c.*, COUNT(p.id) AS total,
            COALESCE(SUM(p.id IS NOT NULL AND (p.read_depth IS NULL OR p.read_depth = 'abstract')), 0) AS unread,
            MAX(p.updated_at) AS last_paper, GROUP_CONCAT(DISTINCT p.direction) AS dirs
        FROM paper_collections c
        LEFT JOIN paper_collection_items pci ON pci.collection_id=c.id
        LEFT JOIN papers p ON p.id=pci.paper_id AND EXISTS (SELECT 1 FROM paper_files f WHERE f.paper_id=p.id)
        GROUP BY c.id ORDER BY c.sort_order,c.created_at,c.id'''):
        r = dict(row)
        r['directions'] = sorted({d for d in str(r.pop('dirs') or '').split(',') if d}, key=lambda d: (order.get(d, len(order)), d))
        r['updated'] = max(str(r.pop('last_paper') or ''), str(r['created_at'] or ''))
        rows.append(r)
    return rows


def order_version(conn):
    rows = [tuple(r) for r in conn.execute('SELECT id,version,sort_order FROM paper_collections ORDER BY sort_order,created_at,id')]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def reorder(conn, refs, version):
    conn.execute('BEGIN IMMEDIATE')
    try:
        if version != order_version(conn):
            raise CollectionError('集合已在别处修改，请刷新后重试。')
        current = {r[0] for r in conn.execute('SELECT id FROM paper_collections')}
        if len(refs) != len(current) or set(refs) != current:
            raise CollectionError('文件夹顺序不完整，请刷新后重试。')
        conn.executemany('UPDATE paper_collections SET sort_order=? WHERE id=?', enumerate(refs))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def move(conn, paper_id, ref=None):
    """Drag a row onto a collection: it leaves the others and joins this one (none: leaves them all). Research
    metadata, files and highlights never change."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        if ref and not get(conn, ref):
            raise CollectionError('没有这个集合。')
        if not conn.execute('SELECT 1 FROM papers WHERE id=?', (paper_id,)).fetchone():
            raise CollectionError('没有这篇文献。')
        conn.execute('DELETE FROM paper_collection_items WHERE paper_id=?', (paper_id,))
        if ref:
            conn.execute('INSERT INTO paper_collection_items(collection_id,paper_id) VALUES (?,?)', (ref, paper_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_legacy(conn):
    """Once-only additive migration of direction filing into flat, named folders.

    Runs at startup, never on GET. Existing custom memberships survive. The flag and
    copies commit together, so reopening cannot re-file papers the user has moved.
    """
    from .intake import directions
    if get_setting(conn, 'library.flat_folders_v1'):
        return                                   # every later start: no write lock
    conn.execute('BEGIN IMMEDIATE')
    try:
        if get_setting(conn, 'library.flat_folders_v1'):
            conn.commit()
            return
        names = {str(d['slug']): str(d.get('name') or d['slug']) for d in directions.load()}
        for row in conn.execute("SELECT DISTINCT direction FROM papers WHERE direction IS NOT NULL AND direction != ''"):
            names.setdefault(row[0], row[0])
        for slug, title in names.items():
            try:
                title = name(title[:60])
            except CollectionError:              # a blank direction name has no folder to become
                continue
            existing = conn.execute('SELECT id FROM paper_collections WHERE name=? COLLATE NOCASE', (title,)).fetchone()
            ref = existing[0] if existing else create(conn, title)
            conn.execute('INSERT OR IGNORE INTO paper_collection_items(collection_id,paper_id) SELECT ?,id FROM papers WHERE direction=?', (ref, slug))
        set_setting(conn, 'library.flat_folders_v1', True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- the 10-02 copies of the directions (card 0132)
# The legacy migration above turned each research direction into a folder of the same name, copying its papers once.
# Those folders fell behind the directions (LIBRARY-UI §1: 15 papers in a direction, 9 in its folder), and Leo agreed
# on 10-12 to delete them. Only a copy is deleted: the name is exactly a direction's name and every member carries that
# direction, so nothing is lost. Anything else stays and is reported with the reason.

def direction_copies(conn) -> list[dict]:
    """Every collection with the cleanup's verdict: action 'delete' or 'skip', and the reason in Chinese."""
    from .intake import directions
    by_name: dict[str, str] = {}
    for d in directions.load():
        if str(d.get('name') or '').strip():
            by_name.setdefault(str(d['name']), str(d['slug']))
    out = []
    for c in conn.execute('SELECT * FROM paper_collections ORDER BY sort_order, created_at, id').fetchall():
        members = conn.execute('SELECT p.id, p.title, p.direction FROM paper_collection_items pci JOIN papers p '
                               'ON p.id = pci.paper_id WHERE pci.collection_id = ? ORDER BY p.title', (c['id'],)).fetchall()
        slug = by_name.get(c['name'])
        row = {'id': c['id'], 'name': c['name'], 'version': c['version'], 'members': len(members), 'direction': slug}
        off = [m for m in members if m['direction'] != slug] if slug else []
        if slug is None:
            row.update(action='skip', reason='名字和任何研究方向都不完全相同')
        elif off:
            names = '、'.join(f"《{m['title']}》" for m in off[:5]) + ('等' if len(off) > 5 else '')
            row.update(action='skip', reason=f'有 {len(off)} 篇不属于方向「{c["name"]}」：{names}')
        else:
            row.update(action='delete', reason=f'名字就是研究方向「{c["name"]}」，' +
                       (f'{len(members)} 篇都属于这个方向' if members else '里面没有文献'))
        out.append(row)
    return out


def remove_direction_copies(conn, *, dry_run: bool = True) -> list[dict]:
    """The verdicts of direction_copies; without dry_run the 'delete' ones are deleted, checked again inside one
    write transaction so a paper filed in between keeps its collection. Papers and their directions never change."""
    if dry_run:
        return direction_copies(conn)
    conn.execute('BEGIN IMMEDIATE')
    try:
        plan = direction_copies(conn)
        for row in plan:
            if row['action'] == 'delete':
                conn.execute('DELETE FROM paper_collection_items WHERE collection_id=?', (row['id'],))
                conn.execute('DELETE FROM paper_collections WHERE id=?', (row['id'],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return plan
