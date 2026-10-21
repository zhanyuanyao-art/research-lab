"""Model-facing tools. All state is scoped by the server's task id, never a model argument."""
from __future__ import annotations

import json
from . import retrieval, store
from .models import Checkpoint, Draft


def _result(value):
    return json.dumps(value, ensure_ascii=False)


def search(conn, args, task_id):
    s = store.session_for_task(conn, task_id)
    return _result(retrieval.search(conn, s['id'], args['backend'], args['query'], args.get('cutoff'), args.get('limit', 8)))


def read(conn, args, task_id):
    s = store.session_for_task(conn, task_id)
    try:
        return _result(retrieval.read(conn, s['id'], args['source'], args.get('offset', 0), args.get('page'), args.get('limit', 4000)))
    except Exception as exc:
        from ..db.connection import now_iso
        conn.execute('INSERT INTO research_searches(session_id,backend,query,status,results_json,error,created_at) VALUES(?,?,?,?,?,?,?)',
                     (s['id'], 'read', str(args.get('source', ''))[:600], 'error', '[]', type(exc).__name__, now_iso()))
        raise


def state(conn, args, task_id):
    s = store.session_for_task(conn, task_id)
    return _result(store.state(conn, s['id'], int(args.get('offset', 0)), 6000))


def checkpoint(conn, args, task_id):
    s = store.session_for_task(conn, task_id)
    value = store.save_checkpoint(conn, s['id'], args)
    return _result({'saved': True, 'checkpoint': value.model_dump()})


def draft(conn, args, task_id):
    d = store.save_draft(conn, task_id, args)
    return _result({'saved': True, 'revision': d['revision'], 'status': d['status'],
                    'link': f'[审阅产出](/chat/research/{d["session_id"]}/review)',
                    'note': 'Only Leo can adopt this draft into the research archive.'})


def register(Tool, obj):
    return [
        Tool('search_research', 'Search local materials, Crossref literature metadata, or configured web search. Logs empty/error/blocked separately. Use bilingual queries and record coverage.',
             obj({'backend': {'type': 'string', 'enum': ['local', 'literature', 'web']}, 'query': {'type': 'string'},
                  'cutoff': {'type': 'string', 'description': 'Publication cutoff YYYY-MM-DD; web/local dates also need manual verification'},
                  'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}}, ('backend', 'query')),
             search, lambda c,a: '检索资料：' + str(a.get('query', ''))[:50], True),
        Tool('read_research_source', 'Read and snapshot a source passage. source is paper:<id>, artifact:<ref>, SRC id returned by search, or a public URL. Metadata/snippets are not full text: fetch their URL. Use next_offset to continue; page is a 1-based PDF position. Returns a read_id for citations.',
             obj({'source': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
                  'page': {'type': 'integer', 'minimum': 1}, 'limit': {'type': 'integer', 'minimum': 100, 'maximum': 5000}}, ('source',)),
             read, lambda c,a: '核读一段来源', True),
        Tool('research_state', 'Restore research question, complete checkpoint, source/search ledgers and latest draft across engines. Continue via next_offset until needed state is read.',
             obj({'offset': {'type': 'integer', 'minimum': 0}}), state, lambda c,a: '恢复研究进度', True),
        Tool('save_research_checkpoint', 'Persist scope, exclusions, uncertainties and next actions before asking a question or exhausting the tool budget. This does not publish anything.',
             Checkpoint.model_json_schema(), checkpoint, lambda c,a: '保存研究进度', True),
        Tool('save_research_draft', 'Save a revision for human review. Cite actual read_id values and describe which passage supports which claim. Use working when scope/evidence is incomplete, review for a source-cited draft. expected_revision comes from research_state. Never writes an artifact or accepts evidence.',
             Draft.model_json_schema(), draft, lambda c,a: '保存待审阅产出', True),
    ]
