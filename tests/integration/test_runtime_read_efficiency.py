"""Progress reads preserve control decisions without loading prompt/receipt bodies."""
import json
from contextlib import contextmanager

import httpx

from easyagent.store import encode


async def test_progress_preserves_graph_retry_and_approvals_without_payload(api):
    url, hub = api
    rid = hub.submit({'name': 'read projections', 'inputs': {'value': 7},
        'metadata': {'step_labels': {'pause': '确认'}, 'private_large_metadata': 'x' * 200000},
        'steps': [{'id': 'prepare', 'kind': 'transform', 'input': {'value': 7}},
                  {'id': 'pause', 'target': 'core.echo', 'requires_approval': True,
                   'depends_on': ['prepare'], 'input': {'payload': 'y' * 200000}}]})
    full = await hub.wait(rid)
    assert full['status'] == 'waiting_approval'
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        progress = (await client.get(f'/v1/runs/{rid}?progress=true')).json()
        complete = (await client.get(f'/v1/runs/{rid}')).json()
    assert complete == hub.store.run(rid)
    for key in ('id', 'name', 'status', 'approvals', 'input_requests', 'children', 'usage', 'retry'):
        assert progress[key] == full[key]
    assert [s['spec']['depends_on'] for s in progress['steps']] == [s['spec']['depends_on'] for s in full['steps']]
    assert all('output' not in s and 'state' not in s and 'input' not in s['spec'] for s in progress['steps'])
    assert progress['spec']['metadata'] == {'step_labels': {'pause': '确认'}}
    assert len(encode(progress)) < len(encode(full)) / 2
    assert hub.store.execution_inputs(rid) == {'$input': {'value': 7}, 'prepare': {'value': 7}}
    assert hub.store.run_status(rid) == full['status']


async def test_progress_retains_incremental_build_label(hub):
    rid = hub.submit({'name': 'builder progress', 'steps': [{'id': 'compile', 'kind': 'transform', 'not_before': 9999999999}]})
    with hub.store.connect() as db:
        db.execute("UPDATE steps SET status='running',state=? WHERE run_id=?", (
            encode({'build_requests': {'first': {'segments': {'draft': 'x' * 50000, 'turns': 3, 'complete': True}},
                                      'second': {'segments': {'draft': 'y' * 50000, 'turns': 5}}}}), rid))
    progress = hub.store.run(rid, progress=True)
    assert progress['steps'][0]['build_turns'] == 5
    assert len(encode(progress)) < 5000


async def test_conversation_enrichment_batches_turn_states(hub, monkeypatch):
    conversation = await hub.conversations.create({'workspace': True})
    with hub.store.connect() as db:
        for i in range(40):
            db.execute("INSERT INTO conversation_turns VALUES(?,?,?,'follow_up','succeeded',NULL,NULL,?,?)",
                       (str(i), conversation['id'], 'message', str(i), i))
            db.execute('INSERT INTO conversation_jobs VALUES(?,?)', (str(i), json.dumps({'phase': 'completed', 'message': str(i)})))
    original = hub.store.connect
    queries = []
    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(queries.append)
            yield db
    monkeypatch.setattr(hub.store, 'connect', traced)
    result = hub.conversations.get(conversation['id'])
    assert len(result['turns']) == 40
    assert all(t['task']['message'] == t['id'] for t in result['turns'])
    assert len([q for q in queries if 'SELECT' in q and 'conversation_jobs' in q]) == 1
