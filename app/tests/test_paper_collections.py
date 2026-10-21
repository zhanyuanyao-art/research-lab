"""Personal filing must preserve research links and reject stale renames."""
import pytest
from research_lab import paper_collections as C


def paper(conn, ref='paper-one', original=True, direction=None):
    conn.execute("INSERT INTO papers(id,title,title_hash,kind,direction,created_at,updated_at) VALUES (?,?,?,'article',?,'2026-10-02','2026-10-02')", (ref, ref, ref, direction))
    if original:
        conn.execute("INSERT INTO paper_files(paper_id,sha256,rel_path,bytes,page_count,has_text_layer,extractor,created_at) VALUES (?,?,?,1,1,1,'test','2026-10-02')", (ref, ref, ref+'.pdf'))
    return ref


def test_names_versions_and_membership_are_preserved(conn):
    ref = C.create(conn, '  联盟   文献  ')
    assert C.get(conn, ref)['name'] == '联盟 文献'
    for name in ['', ' '*3, 'x'*61, '联盟 文献']:
        with pytest.raises(C.CollectionError): C.create(conn, name)
    p = paper(conn, direction='alliance-us')
    conn.execute('INSERT INTO paper_projects VALUES (?,?)', (p,'us-rok-reassurance'))
    other = C.create(conn, '理论')
    C.assign(conn, ref, p); C.assign(conn, other, p); C.assign(conn, ref, p)
    C.rename(conn, ref, '联盟经典', 1)
    with pytest.raises(C.CollectionError): C.rename(conn, ref, '旧窗口覆盖', 1)
    with pytest.raises(C.CollectionError): C.rename(conn, ref, '理论', 2)
    assert C.get(conn, ref)['name'] == '联盟经典'
    C.assign(conn, ref, p, remove=True)
    assert conn.execute('SELECT collection_id FROM paper_collection_items').fetchone()[0] == other
    assert conn.execute('SELECT direction FROM papers WHERE id=?',(p,)).fetchone()[0] == 'alliance-us'
    assert conn.execute('SELECT project_slug FROM paper_projects WHERE paper_id=?',(p,)).fetchone()[0] == 'us-rok-reassurance'


def test_collection_counts_and_unfiled_exclude_missing_originals(conn, client):
    from research_lab.web.routes.papers import archive_rail
    ref = C.create(conn, '待精读')
    a, b = paper(conn), paper(conn,'metadata-only',False)
    C.assign(conn, ref, a); C.assign(conn, ref, b)
    assert next(c for c in C.listing(conn) if c['id'] == ref)['total'] == 1
    assert archive_rail(conn)['unsorted'] == 0
    page = client.get('/papers?collection='+ref)
    assert page.status_code == 200 and 'paper-one' in page.text and 'metadata-only' not in page.text
    missing = client.get('/papers?collection='+ref+'&src=missing')
    assert 'metadata-only' in missing.text and 'data-pid="paper-one"' not in missing.text
    assert client.get('/papers?collection=missing').status_code == 404
    C.assign(conn, ref, a, remove=True)
    assert archive_rail(conn)['unsorted'] == 1


def test_library_forms_create_rename_and_file(client, conn):
    header = {'X-Library-Form':'1'}
    response = client.post('/paper-collections',data={'name':'<文献> & 理论'},headers=header)
    assert response.status_code == 200
    url = response.json()['url']; ref = url.split('=')[-1]
    html = client.get(url).text
    assert '&lt;文献&gt; &amp; 理论' in html
    assert '这个集合还没有文献' in html and '导入第一篇文献' not in html
    assert client.post('/paper-collections', data={'name':'<文献> & 理论'},headers=header).status_code == 400
    assert client.post(f'/paper-collections/{ref}/rename',data={'name':'我的分类','version':'1'},headers=header).status_code == 200
    assert client.post(f'/paper-collections/{ref}/rename',data={'name':'过时草稿','version':'1'},headers=header).status_code == 400
    p = paper(conn)
    assert client.post(f'/papers/{p}/archive',data={'collection':ref},headers=header).status_code == 200
    assert 'paper-one' in client.get(url).text
    assert client.post(f'/papers/{p}/archive',data={'collection':'missing'},headers=header).status_code == 400
    assert client.post(f'/papers/{p}/archive',data={'collection':ref,'remove':'1'},headers=header).status_code == 200
    assert not conn.execute('SELECT 1 FROM paper_collection_items').fetchone()
    C.assign(conn, ref, p)
    client.post(f'/papers/{p}/archive',data={'direction':''})
    assert not conn.execute('SELECT 1 FROM paper_collection_items').fetchone()


def test_legacy_directions_become_named_folders_once_without_losing_links(conn):
    from research_lab.files import datafiles
    p = paper(conn, direction='alliance-us')
    q = paper(conn, 'unknown-direction', False, 'historical-direction')
    conn.execute('INSERT INTO paper_projects VALUES (?,?)', (p,'us-rok-reassurance'))
    title = next(d['name'] for d in datafiles.read('directions') if d['slug']=='alliance-us')
    existing = C.create(conn,title)
    custom = C.create(conn,'我的归档')
    C.assign(conn,custom,p)
    C.migrate_legacy(conn)
    folders = C.listing(conn)
    assert sum(c['name']==title for c in folders) == 1
    assert {r[0] for r in conn.execute('SELECT collection_id FROM paper_collection_items WHERE paper_id=?',(p,))} == {existing,custom}
    assert conn.execute('SELECT 1 FROM paper_collection_items WHERE paper_id=?',(q,)).fetchone()
    C.move(conn,p)
    C.rename(conn,existing,'同盟文献',1)
    C.migrate_legacy(conn)
    assert not conn.execute('SELECT 1 FROM paper_collection_items WHERE paper_id=?',(p,)).fetchone()
    assert C.get(conn,existing)['name'] == '同盟文献'
    assert conn.execute('SELECT direction FROM papers WHERE id=?',(p,)).fetchone()[0] == 'alliance-us'
    assert conn.execute('SELECT project_slug FROM paper_projects WHERE paper_id=?',(p,)).fetchone()[0] == 'us-rok-reassurance'
    assert conn.execute('SELECT COUNT(*) FROM paper_files WHERE paper_id=?',(p,)).fetchone()[0] == 1


def test_legacy_folder_copy_rolls_back_on_failure(conn, monkeypatch):
    from research_lab.db.connection import get_setting
    create = C.create
    calls = []
    def fail(conn, value):
        calls.append(value)
        if len(calls) == 2: raise RuntimeError('interrupted migration')
        return create(conn,value)
    monkeypatch.setattr(C,'create',fail)
    with pytest.raises(RuntimeError): C.migrate_legacy(conn)
    assert not C.listing(conn) and not get_setting(conn,'library.flat_folders_v1')
    monkeypatch.setattr(C,'create',create)
    C.migrate_legacy(conn)
    assert C.listing(conn) and get_setting(conn,'library.flat_folders_v1')


def test_folder_reorder_rejects_stale_and_incomplete_orders(conn):
    a,b = C.create(conn,'A'),C.create(conn,'B')
    version = C.order_version(conn)
    C.reorder(conn,[b,a],version)
    assert [r['id'] for r in C.listing(conn)] == [b,a]
    with pytest.raises(C.CollectionError): C.reorder(conn,[a,b],version)
    with pytest.raises(C.CollectionError): C.reorder(conn,[a,a],C.order_version(conn))
    version = C.order_version(conn)
    C.create(conn,'C')
    with pytest.raises(C.CollectionError): C.reorder(conn,[a,b],version)
    assert [r['id'] for r in C.listing(conn)][:2] == [b,a]


def test_folder_move_is_atomic_and_unfiled_keeps_research_metadata(client,conn):
    from research_lab.web.routes.papers import archive_rail
    p = paper(conn,direction='alliance-us')
    conn.execute('INSERT INTO paper_projects VALUES (?,?)',(p,'us-rok-reassurance'))
    a,b = C.create(conn,'A'),C.create(conn,'B')
    C.assign(conn,a,p)
    assert client.post(f'/papers/{p}/archive',data={'move':'1','collection':'missing'}).status_code == 400
    assert conn.execute('SELECT collection_id FROM paper_collection_items WHERE paper_id=?',(p,)).fetchone()[0] == a
    assert client.post(f'/papers/{p}/archive',data={'move':'1','collection':b}).status_code == 200
    assert [r[0] for r in conn.execute('SELECT collection_id FROM paper_collection_items WHERE paper_id=?',(p,))] == [b]
    assert client.post(f'/papers/{p}/archive',data={'move':'1'}).status_code == 200
    assert archive_rail(conn)['unsorted'] == 1 and p in client.get('/papers?direction=none').text
    assert conn.execute('SELECT direction FROM papers WHERE id=?',(p,)).fetchone()[0] == 'alliance-us'
    assert conn.execute('SELECT project_slug FROM paper_projects WHERE paper_id=?',(p,)).fetchone()[0] == 'us-rok-reassurance'
    order = [r['id'] for r in C.listing(conn)]
    version = C.order_version(conn)
    assert client.post('/paper-collections/order',data={'ids':order[::-1],'version':version}).status_code == 200
    assert client.post('/paper-collections/order',data={'ids':order,'version':version}).status_code == 409
    page = client.get('/papers?view=folders').text
    assert 'data-drop-direction=' not in page and 'data-drop-project=' not in page
    assert 'data-folder-id=' in page and 'data-new-folder' in page


def test_folders_live_in_content_and_breadcrumb_opens_folder(client,conn):
    ref = C.create(conn,'理论文献')
    p = paper(conn)
    C.assign(conn,ref,p)
    page = client.get('/papers?view=folders').text
    sidebar = page.split('id="sidebar"')[1].split('</aside>',1)[0]
    main = page.split('<main class="content">')[1].split('</main>',1)[0]
    assert 'data-folder-id=' not in sidebar
    assert 'class="coll-lines"' in main and 'data-folder-id=' in main      # card 0132: the 集合 page
    assert f'href="/papers?collection={ref}"' in main
    assert f'data-paper-id="{p}"' not in main  # papers appear inside their collection
    folder = client.get('/papers?collection='+ref).text
    crumbs = folder.split('aria-label="位置"')[1].split('</nav>')[0]
    assert 'href="/papers"' in crumbs and '理论文献' in crumbs
    assert 'class="coll-lines"' not in folder and f'data-paper-id="{p}"' in folder
    assert 'aria-label="重命名集合"' in folder and 'aria-label="阅读状态"' in folder
    assert f'data-paper-id="{p}"' in client.get('/papers?view=all').text
    assert '理论文献' in client.get('/papers?collection='+ref+'&status=done').text


def test_reorder_version_is_read_before_folder_listing(client,conn,monkeypatch):
    import re
    a,b = C.create(conn,'A'),C.create(conn,'B')
    old = [r['id'] for r in C.listing(conn)]
    original = C.listing
    def interleave(conn):
        visible = original(conn)
        C.reorder(conn,old[::-1],C.order_version(conn))
        return visible
    monkeypatch.setattr(C,'listing',interleave)
    page = client.get('/papers?view=folders').text
    version = re.search('data-folder-order="([^"]+)"',page)[1]
    assert client.post('/paper-collections/order',data={'ids':old,'version':version}).status_code == 409
    assert [r['id'] for r in original(conn)] == old[::-1]


def test_upload_in_folder_preserves_import_destination(client,conn,sample_pdf,monkeypatch):
    from types import SimpleNamespace
    from research_lab.intake import pipeline
    ref = C.create(conn,'精读')
    def ingest(conn, *args, **kwargs):
        return SimpleNamespace(created=True,paper_id=paper(conn,'new-import'),duplicate_of=None,pending=None)
    monkeypatch.setattr(pipeline,'ingest',ingest)
    response = client.post('/papers',data={'page':'/papers?collection='+ref},files={'files':('sample.pdf',sample_pdf.read_bytes(),'application/pdf')},follow_redirects=False)
    assert response.status_code == 303
    assert conn.execute('SELECT paper_id FROM paper_collection_items WHERE collection_id=?',(ref,)).fetchone()[0] == 'new-import'


@pytest.mark.parametrize('action',['attach','import'])
def test_pending_upload_remembers_folder_only_after_confirmation(client,conn,sample_pdf,monkeypatch,action):
    from types import SimpleNamespace
    from research_lab.intake import pipeline
    from urllib.parse import parse_qs,urlparse
    folder = C.create(conn,'待核对')
    existing = paper(conn,'existing')
    sha = 'abcdef123456'
    monkeypatch.setattr(pipeline,'ingest',lambda *args,**kwargs: SimpleNamespace(created=False,paper_id=None,duplicate_of=existing,pending=sha))
    result = client.post('/papers',data={'page':'/papers?collection='+folder},files={'files':('sample.pdf',sample_pdf.read_bytes(),'application/pdf')},follow_redirects=False)
    url = result.headers['location']
    assert parse_qs(urlparse(url).query)['collection'] == [folder]
    assert not conn.execute('SELECT 1 FROM paper_collection_items').fetchone()
    monkeypatch.setattr(pipeline,'pending_info',lambda _: dict(original_name='sample.pdf',duplicate_of=existing,reason='same DOI'))
    page = client.get(url).text
    assert page.count(f'name="collection" value="{folder}"') >= 2
    def attach(*args,**kwargs): return 1
    def separate(conn,*args,**kwargs): return SimpleNamespace(paper_id=paper(conn,'separate'))
    monkeypatch.setattr(pipeline,'attach_pending',attach)
    monkeypatch.setattr(pipeline,'import_pending',separate)
    response = client.post(f'/papers/pending/{sha}/{action}',data={'paper_id':existing,'collection':folder},follow_redirects=False)
    assert response.status_code == 303
    assert conn.execute('SELECT paper_id FROM paper_collection_items WHERE collection_id=?',(folder,)).fetchone()[0] == (existing if action=='attach' else 'separate')


def test_library_root_is_direction_first_with_optional_folders(client,conn):
    p = paper(conn)
    conn.execute("UPDATE papers SET direction='alliance-us' WHERE id=?",(p,))
    ref = C.create(conn,'精读')
    C.assign(conn,ref,p)
    root = client.get('/papers').text
    main = root.split('<main class="content">')[1].split('</main>',1)[0]
    # card 0128: the root is every paper grouped by 方向, filed or not; the group name opens the direction
    assert 'class="lib-group"' in main and 'href="/papers?direction=alliance-us"' in main
    assert 'data-folder-id=' not in main and f'data-paper-id="{p}"' in main
    assert 'href="/papers?view=folders"' in main
    direction = client.get('/papers?direction=alliance-us').text
    assert f'data-paper-id="{p}"' in direction and '联盟政治与美国研究' in direction
    # the crumb back to 全部 clears the remembered direction (card 0066), or /papers would reopen this one
    assert 'id="library-folders"' not in direction and 'href="/papers?direction="' in direction.split('aria-label="位置"')[1].split('</nav>')[0]
    conn.execute("UPDATE papers SET direction='retired-direction' WHERE id=?",(p,))
    root = client.get('/papers?direction=').text   # 全部: the pick above is remembered until then (card 0066)
    assert 'href="/papers?direction=_none"' in root
    assert f'data-paper-id="{p}"' in client.get('/papers?direction=_none').text


# ---------------------------------------------------------------- card 0132: 集合

def test_a_paper_sits_in_several_collections_and_leaving_one_keeps_it(client, conn):
    p = paper(conn, direction='alliance-us')
    a, b = C.create(conn, '开题必读'), C.create(conn, '待引用')
    header = {'X-Library-Form': '1'}
    for ref in (a, b):
        assert client.post(f'/papers/{p}/archive', data={'collection': ref}, headers=header).status_code == 200
    assert {r[0] for r in conn.execute('SELECT collection_id FROM paper_collection_items WHERE paper_id=?', (p,))} == {a, b}
    in_a = client.get(f'/papers?collection={a}').text
    assert 'Remove from collection' in in_a and 'Add to collection' in in_a
    assert client.post(f'/papers/{p}/archive', data={'collection': a, 'remove': '1'}, headers=header).status_code == 200
    assert [r[0] for r in conn.execute('SELECT collection_id FROM paper_collection_items WHERE paper_id=?', (p,))] == [b]
    assert conn.execute('SELECT direction FROM papers WHERE id=?', (p,)).fetchone()[0] == 'alliance-us'
    assert f'data-paper-id="{p}"' in client.get('/papers?view=all').text


def test_the_collections_page_rows(client, conn):
    a = C.create(conn, '开题必读')
    C.create(conn, '空的')
    p1, p2 = paper(conn, 'p1', direction='alliance-us'), paper(conn, 'p2', direction='sea-asean')
    paper(conn, 'p3', original=False, direction='periphery-nts')
    conn.execute("UPDATE papers SET read_depth='read', updated_at='2026-10-09T08:00:00' WHERE id='p2'")
    conn.execute("UPDATE paper_collections SET created_at='2026-10-01T00:00:00' WHERE id=?", (a,))
    for x in ('p1', 'p2', 'p3'):
        C.assign(conn, a, x)
    row = next(r for r in C.listing(conn) if r['id'] == a)
    assert row['total'] == 2 and row['unread'] == 1                    # the one without an original is not counted
    assert row['directions'] == sorted(row['directions'], key=['alliance-us', 'sea-asean'].index) and len(row['directions']) == 2
    page = client.get('/papers?view=folders').text
    main = page.split('<main class="content">')[1].split('</main>', 1)[0]
    assert '<h1 class="page-title">集合</h1>' in main and '>New collection</button>' in main
    assert '2 篇 · 1 篇未读' in main and '10月9日' in main and main.count('class="dir-dot"') == 2
    assert '>Rename</button>' in main and '>Delete</button>' in main and '0 篇' in main
    assert 'data-new-folder-form' in main and '文件夹' not in main
    crumbs = page.split('aria-label="位置"')[1].split('</nav>')[0]
    assert 'href="/papers"' in crumbs and '集合' in crumbs
    alias = client.get('/papers?view=collections').text
    assert '开题必读' in alias and '空的' in alias and 'class="coll-line add"' in alias
    lib = client.get('/papers').text
    assert 'aria-label="集合"' in lib and f'data-drop-collection="{a}"' in lib and '全部集合' in lib


def test_deleting_a_collection_keeps_its_papers(client, conn):
    a = C.create(conn, '待引用')
    p = paper(conn, direction='alliance-us')
    C.assign(conn, a, p)
    r = client.post(f'/paper-collections/{a}/delete', data={'version': '1'}, follow_redirects=False)
    assert r.headers['location'] == '/papers?view=folders&folder=deleted'
    assert '已删除集合，里面的文献还在。' in client.get(r.headers['location']).text
    assert not C.get(conn, a) and conn.execute('SELECT direction FROM papers WHERE id=?', (p,)).fetchone()[0] == 'alliance-us'


def directions_by_name():
    from research_lab.intake import directions
    return {str(d['name']): str(d['slug']) for d in directions.load()}


def test_cleanup_removes_only_complete_copies_of_a_direction(conn):
    names = directions_by_name()
    (n1, s1), (n2, s2), (n3, s3), (n4, s4) = list(names.items())[:4]
    copy = C.create(conn, n1)                                   # every member carries the direction: a copy
    for x in ('a1', 'a2'):
        C.assign(conn, copy, paper(conn, x, direction=s1))
    empty = C.create(conn, n2)                                  # the direction's folder that never filled: a copy too
    mixed = C.create(conn, n3)                                  # one member from elsewhere: kept
    C.assign(conn, mixed, paper(conn, 'b1', direction=s3)); C.assign(conn, mixed, paper(conn, 'b2', direction=s4))
    near = C.create(conn, n4 + ' 精读')                         # not exactly a direction's name: kept
    C.assign(conn, near, paper(conn, 'c1', direction=s4))
    mine = C.create(conn, '开题必读')
    C.assign(conn, mine, 'a1')
    plan = {r['name']: r for r in C.remove_direction_copies(conn, dry_run=True)}
    assert plan[n1]['action'] == plan[n2]['action'] == 'delete'
    assert plan[n3]['action'] == 'skip' and '1 篇不属于' in plan[n3]['reason'] and '《b2》' in plan[n3]['reason']
    assert plan[n4 + ' 精读']['action'] == 'skip' and '名字和任何研究方向都不完全相同' in plan[n4 + ' 精读']['reason']
    assert plan['开题必读']['action'] == 'skip'
    assert len(C.listing(conn)) == 5                            # a dry run changes nothing
    C.remove_direction_copies(conn, dry_run=False)
    left = {r['name'] for r in C.listing(conn)}
    assert left == {n3, n4 + ' 精读', '开题必读'}
    assert conn.execute("SELECT direction FROM papers WHERE id='a1'").fetchone()[0] == s1         # papers untouched
    assert [r[0] for r in conn.execute("SELECT collection_id FROM paper_collection_items WHERE paper_id='a1'")] == [mine]


def test_cleanup_command_dry_run_lists_and_the_real_run_deletes(conn):
    from typer.testing import CliRunner
    from research_lab.cli import app
    names = directions_by_name()
    name, slug = next(iter(names.items()))
    copy = C.create(conn, name)
    C.assign(conn, copy, paper(conn, 'a1', direction=slug))
    kept = C.create(conn, '第二章材料')
    dry = CliRunner().invoke(app, ['paper', 'collections-cleanup', '--dry-run'])
    assert dry.exit_code == 0, dry.output
    assert f'[会删除] {name}（1 篇）' in dry.output and '[跳过] 第二章材料（0 篇）' in dry.output and '演练' in dry.output
    assert C.get(conn, copy) is not None
    real = CliRunner().invoke(app, ['paper', 'collections-cleanup'])
    assert real.exit_code == 0 and f'[已删除] {name}' in real.output
    assert C.get(conn, copy) is None and C.get(conn, kept) is not None


def test_cleanup_takes_the_legacy_copies_and_they_stay_gone(conn):
    """The 10-02 migration's folders fall behind their directions (LIBRARY-UI §1); the cleanup removes all four,
    and the once-only migration does not bring them back."""
    names = directions_by_name()
    slugs = list(names.values())
    for i, slug in enumerate(slugs):
        paper(conn, f'old-{i}', direction=slug)
    C.migrate_legacy(conn)
    assert {r['name'] for r in C.listing(conn)} >= set(names)
    paper(conn, 'later', direction=slugs[0])                     # imported after the copy: the folder is incomplete
    plan = C.remove_direction_copies(conn, dry_run=False)
    assert sorted(r['name'] for r in plan if r['action'] == 'delete') == sorted(names)
    assert not C.listing(conn)
    C.migrate_legacy(conn)
    assert not C.listing(conn)
    assert {r[0] for r in conn.execute('SELECT direction FROM papers')} == set(slugs)
