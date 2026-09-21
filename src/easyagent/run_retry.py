"""Resume failed work in place, preserving receipts and checkpoints atomically."""
import json
import time

from .contracts import Contract
from .store import Conflict, encode


class RetryRequest(Contract):
    expected_updated: float
    longer_wait: bool = False


def legacy_build_budget(spec):
    """Only upgrade the former internal builder default, never user-authored workflow limits."""
    steps = spec.get('steps', [])
    limits = spec.get('limits', {})
    return bool(spec.get('metadata', {}).get('assistant_builder') and len(steps) == 2
                and [(s['id'], s.get('target')) for s in steps] == [
                    ('compile', 'development.compile_build'), ('verify', 'development.verify_build')]
                and limits.get('model_calls') in (8, 24) and limits.get('tool_calls') == 8
                and limits.get('output_tokens') == 65536)


def retry_options(db, run, _nested=False):
    steps = db.execute("SELECT * FROM steps WHERE run_id=?", (run['id'],)).fetchall()
    spec = json.loads(run['spec']) if isinstance(run['spec'], str) else run['spec']
    invalid_verification = [s for s in steps if s['id'] == 'verify' and s['status'] == 'succeeded'
                            and json.loads(s['spec']).get('target') == 'development.verify_build'
                            and isinstance(output := json.loads(s['output'] or 'null'), dict) and output.get('errors')]
    legacy_invalid = bool(spec.get('metadata', {}).get('assistant_builder') and run['status'] == 'succeeded' and invalid_verification)
    failures = [s for s in steps if s['status'] == 'failed']
    if legacy_invalid:
        failures = invalid_verification
    reason = ''
    if (run['status'] != 'failed' and not legacy_invalid) or not failures:
        reason = '只有失败的任务可以从中断处重试。'
    elif db.execute("SELECT 1 FROM invocations WHERE run_id=? AND status IN ('started','uncertain','denied') LIMIT 1", (run['id'],)).fetchone():
        reason = '存在结果未核验或已拒绝的调用，请先处理原调用。'
    elif not _nested and db.execute('SELECT 1 FROM child_runs WHERE child_id=?', (run['id'],)).fetchone():
        reason = '请在主任务中重试，以便同步恢复失败的子流程。'
    elif any(json.loads(s['state']).get('compensation') for s in failures):
        reason = '失败步骤已进入补偿处理，请核对结果后调整流程。'
    limits = spec.get('limits', {})
    upgrade = legacy_build_budget(spec)
    if upgrade:
        limits = {**limits, 'model_calls': None, 'tool_calls': None, 'output_tokens': None, 'wall_time_seconds': None}
    wall_time = limits.get('wall_time_seconds')
    if not reason and wall_time is not None and time.time() - run['created'] >= wall_time:
        reason = '任务总时间预算已用尽，重试不会重置预算。'
    usage = db.execute('SELECT * FROM run_usage WHERE run_id=?', (run['id'],)).fetchone()
    if not reason and usage and any(limits.get(limit) is not None and usage[field] >= limits[limit] for field, limit in (
            ('model_calls', 'model_calls'), ('tool_calls', 'tool_calls'),
            ('output_reserved', 'output_tokens'), ('child_runs', 'child_runs'))):
        reason = '任务调用预算已用尽，重试不会重置已使用额度。'
    timeout = any(json.loads(s['retry_state']).get('error', {}).get('category') in
                  ('read_timeout', 'network_timeout', 'step_timeout') or 'Timeout' in (s['error'] or '')
                  for s in failures)
    if not reason:
        children = db.execute('SELECT r.* FROM child_runs c JOIN runs r ON r.id=c.child_id WHERE c.parent_id=?', (run['id'],)).fetchall()
        for child in children:
            if child['status'] == 'succeeded':
                continue
            nested = retry_options(db, dict(child), _nested=True)
            timeout = timeout or nested['longer_wait']
            if not nested['allowed']:
                reason = '子流程不能直接重试：' + nested['reason']
                break
    return {'allowed': not reason, 'reason': reason, 'longer_wait': not reason and timeout,
            'build_budget_upgrade': upgrade,
            'verification_failure': legacy_invalid,
            'failed_steps': [s['id'] for s in failures],
            'preserved_steps': sum(s['status'] == 'succeeded' for s in steps) - len(invalid_verification if legacy_invalid else [])}


async def retry_run(hub, run_id, body):
    async with hub.conversations.lock:
        with hub.store.transaction() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            run = dict(row)
            if run['updated'] != body.expected_updated:
                raise Conflict('任务状态已变化，请刷新后重试，避免重复提交。')
            options = retry_options(db, run)
            if not options['allowed']:
                raise Conflict(options['reason'])
            if body.longer_wait and not options['longer_wait']:
                raise ValueError('当前错误不属于超时，请使用普通重试或先修改配置。')
            if options['verification_failure']:
                # Old verifiers returned validation errors as a successful local tool result.
                # Recheck that internal result; never invalidate external calls or their receipts.
                db.execute("UPDATE invocations SET status='failed',error='validation failed' "
                           "WHERE run_id=? AND step_id='verify' AND tool='development.verify_build' AND status='succeeded'", (run_id,))
                db.execute("UPDATE steps SET status='failed',error='流程验证未通过，继续自动修正' WHERE run_id=? AND id='verify'", (run_id,))
                db.execute("UPDATE runs SET status='failed' WHERE id=?", (run_id,))
                hub.store.event(db, run_id, 'build.validation_resumed', {'step': 'verify', 'draft_preserved': True})
            # One run belongs to at most one chat turn. Advance it in the same transaction.
            turns = db.execute('SELECT t.*,j.state AS chat_state FROM conversation_turns t '
                'LEFT JOIN conversation_jobs j ON j.turn_id=t.id WHERE t.run_id=?', (run_id,)).fetchall()
            for turn in turns:
                if turn['status'] != 'failed' and not (options['verification_failure'] and turn['status'] == 'succeeded'):
                    raise Conflict('对话状态尚未同步或已变化，请刷新后重试。')
                if db.execute('SELECT 1 FROM conversation_turns WHERE conversation=? AND '
                    '(created>? OR (created=? AND id>?)) LIMIT 1',
                    (turn['conversation'], turn['created'], turn['created'], turn['id'])).fetchone():
                    raise Conflict('这条消息已有后续任务，请在最新消息中继续处理。')
                if turn['chat_state']:
                    state = json.loads(turn['chat_state'])
                    spec = json.loads(run['spec'])
                    # Both engines resume inside the single `working` phase; `mode` marks the compiler.
                    state['phase'] = state.pop('failed_phase', None) or 'working'
                    if state.get('phase') == 'working' and spec.get('metadata', {}).get('assistant_builder'):
                        state['mode'] = 'building'
                    state['message'] = '正在从失败处继续，已完成步骤和附件已保留。'
                    db.execute('UPDATE conversation_jobs SET state=? WHERE turn_id=?', (encode(state), turn['id']))
                db.execute("UPDATE conversation_turns SET status='running' WHERE id=?", (turn['id'],))
                db.execute('UPDATE conversations SET active_run=? WHERE id=?', (run_id, turn['conversation']))
                hub.conversations.append(db, turn['conversation'], turn['id'], 'assistant',
                    '已从失败处继续，完成的步骤、搜索资料和附件保持不变。')
            retry_ids = [r[0] for r in db.execute('WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL '
                'SELECT c.child_id FROM child_runs c JOIN tree ON c.parent_id=tree.id) '
                "SELECT r.id FROM tree JOIN runs r ON r.id=tree.id WHERE r.status='failed'", (run_id,))]
            for target_id in reversed(retry_ids):
                target = json.loads(db.execute('SELECT spec FROM runs WHERE id=?', (target_id,)).fetchone()[0])
                if legacy_build_budget(target):
                    before = dict(target['limits'])
                    target['limits'].update(model_calls=None, tool_calls=None, output_tokens=None, wall_time_seconds=None)
                    for s in target['steps']:
                        s['timeout_seconds'] = None
                        db.execute('UPDATE steps SET spec=? WHERE run_id=? AND id=?', (encode(s), target_id, s['id']))
                    db.execute('UPDATE runs SET spec=? WHERE id=?', (encode(target), target_id))
                    hub.store.event(db, target_id, 'build.budget_upgraded', {
                        'limits_before': before, 'limits_after': target['limits'], 'usage_preserved': True})
                reset_failed_steps(db, target_id, body.longer_wait)
                hub.store.reconcile(db, target_id)
            hub.store.event(db, run_id, 'run.retried', {'failed_steps': options['failed_steps'],
                'preserved_steps': options['preserved_steps'], 'longer_wait': body.longer_wait, 'runs': retry_ids})
    return hub.store.run(run_id)


def reset_failed_steps(db, run_id, longer_wait):
    for step in db.execute('SELECT * FROM steps WHERE run_id=?', (run_id,)).fetchall():
        if step['status'] == 'failed':
            retry = json.loads(step['retry_state'])
            retry.update(base_attempts=step['attempts'], manual_retries=retry.get('manual_retries', 0) + 1,
                         scheduled=False, attempt=0)
            if longer_wait:
                retry['model_timeout'] = None
                retry['step_timeout'] = None
            db.execute("UPDATE steps SET status='queued',error=NULL,ready_at=0,owner=NULL,lease_until=NULL,retry_state=? "
                       'WHERE run_id=? AND id=?', (encode(retry), run_id, step['id']))
        elif step['status'] == 'skipped' and step['error'] == 'dependency failed':
            db.execute("UPDATE steps SET status='queued',error=NULL,ready_at=0 WHERE run_id=? AND id=?",
                       (run_id, step['id']))
