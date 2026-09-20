"""Result serialization shared by local and HTTP callers."""
from .runtime import named_outputs


def run_result(hub, run_id):
    run = hub.store.run(run_id)
    with hub.store.connect() as db:
        artifacts = [dict(r) for r in db.execute('''WITH RECURSIVE tree(id) AS
            (SELECT ? UNION ALL SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id)
            SELECT id,run_id,name,media_type,digest,created,length(content) AS size FROM artifacts
            WHERE run_id IN (SELECT id FROM tree) ORDER BY created,id''', (run_id,))]
    return {'id': run_id, 'status': run['status'],
            'outputs': named_outputs(run) if run['status'] == 'succeeded' else {}, 'artifacts': artifacts,
            'approvals': run['approvals'], 'input_requests': run['input_requests'],
            'errors': [{'step': s['id'], 'error': s['error']} for s in run['steps'] if s['error']]}
