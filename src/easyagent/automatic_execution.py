"""Operator-authorized execution and continuation; never trust workflow metadata as consent."""
import json
import time

from .contracts import Contract
from .store import Conflict, encode


class ContinueAutomatically(Contract):
    expected_updated: float


async def continue_automatically(hub, run_id, body):
    async with hub.conversations.lock:
        with hub.store.transaction() as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            if run['updated'] != body.expected_updated:
                raise Conflict('任务状态已变化，请刷新后继续。')
            if run['status'] not in ('waiting_approval', 'needs_attention', 'failed', 'cancelled'):
                raise Conflict('当前任务无需恢复。')
            if db.execute('SELECT 1 FROM child_runs WHERE child_id=?', (run_id,)).fetchone():
                raise Conflict('请从主任务继续。')
            ids = [r[0] for r in db.execute('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL '
                'SELECT child_id FROM child_runs JOIN tree ON parent_id=tree.id) SELECT id FROM tree', (run_id,))]
            for target in ids:
                if db.execute("SELECT 1 FROM steps WHERE run_id=? AND status='running'", (target,)).fetchone():
                    raise Conflict('仍有操作执行中，请待当前操作结束。')
                for call in db.execute("SELECT * FROM invocations WHERE run_id=? AND status IN ('started','uncertain','denied')", (target,)):
                    receipt = db.execute('SELECT 1 FROM http_receipts WHERE invocation_id=?', (call['id'],)).fetchone()
                    if call['status'] == 'denied' or not (receipt or hub.tools.regeneratable_media(call['tool'], call['tool_revision'])):
                        raise Conflict('此操作不能按生图任务重新提交，需要先取得外部操作结果。')
                    db.execute("UPDATE invocations SET status='failed' WHERE id=?", (call['id'],))
                    hub.store.event(db, target, 'media.recovery_authorized', {
                        'invocation_id': call['id'], 'source': 'operator',
                        'reuse_response': bool(receipt), 'possible_duplicate_generation': not bool(receipt)})
                db.execute("UPDATE invocations SET status='ready',approved=1 WHERE run_id=? AND status='approval'", (target,))
                for step in db.execute('SELECT * FROM steps WHERE run_id=?', (target,)).fetchall():
                    if step['status'] not in ('failed', 'cancelled', 'waiting_approval', 'needs_attention') and not (
                            step['status'] == 'skipped' and step['error'] == 'dependency failed'):
                        continue
                    retry = json.loads(step['retry_state'])
                    retry.update(base_attempts=step['attempts'], attempt=0, scheduled=False,
                                 manual_retries=retry.get('manual_retries', 0) + 1)
                    db.execute("UPDATE steps SET status='queued',error=NULL,owner=NULL,lease_until=NULL,ready_at=0,retry_state=? "
                               'WHERE run_id=? AND id=?', (encode(retry), target, step['id']))
                db.execute("UPDATE runs SET status='queued',updated=? WHERE id=? AND status!='succeeded'", (time.time(), target))
            turns = db.execute('SELECT t.*,j.state FROM conversation_turns t JOIN conversation_jobs j ON t.id=j.turn_id '
                               'WHERE t.run_id=?', (run_id,)).fetchall()
            for turn in turns:
                if db.execute('SELECT 1 FROM conversation_turns WHERE conversation=? AND '
                    '(created>? OR (created=? AND id>?))', (turn['conversation'], turn['created'], turn['created'], turn['id'])).fetchone():
                    raise Conflict('本条消息已有后续任务，请在最新任务中继续。')
                state = json.loads(turn['state'])
                state['request']['execution'] = 'automatic'
                # Both engines continue inside the single `working` phase; `mode` marks the compiler.
                state['phase'] = state.pop('failed_phase', None) or 'working'
                if state['phase'] == 'working' and json.loads(run['spec']).get('metadata', {}).get('assistant_builder'):
                    state['mode'] = 'building'
                state['message'] = '正在自动继续，已完成步骤和附件已保留。'
                db.execute('UPDATE conversation_jobs SET state=? WHERE turn_id=?', (encode(state), turn['id']))
                db.execute("UPDATE conversation_turns SET status='running' WHERE id=?", (turn['id'],))
                db.execute('UPDATE conversations SET active_run=? WHERE id=?', (run_id, turn['conversation']))
            db.execute("INSERT OR REPLACE INTO run_execution VALUES(?,'automatic')", (run_id,))
            hub.store.event(db, run_id, 'run.automatic_continued', {'source': 'operator', 'completed_steps_preserved': True})
            for target in reversed(ids):
                hub.store.reconcile(db, target)
    return hub.store.run(run_id)
