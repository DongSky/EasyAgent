import json
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from conftest import live_server
from easyagent.assistant_builder import build_status, start_build
from easyagent.contracts import ModelRequest, ModelResult, ToolSpec
from easyagent.models import HTTPProvider
from easyagent.retry_policy import ModelResponseError
from easyagent.run_retry import RetryRequest, retry_run
from easyagent.store import encode
from test_autonomous_build import CSVBuilder, assistant


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
async def test_truncated_tool_arguments_are_rejected_before_parsing(dialect, streaming):
    app = FastAPI()
    payloads = {
        'chat': {'choices': [{'finish_reason': 'length', 'message': {'tool_calls': [
            {'id': 'cut', 'function': {'name': 'tool_0', 'arguments': '{"text":'}}]}}]},
        'responses': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'},
                      'output': [{'type': 'function_call', 'call_id': 'cut', 'name': 'tool_0', 'arguments': '{'}]},
        'anthropic': {'stop_reason': 'max_tokens', 'content': [{'type': 'tool_use', 'input': None}]},
    }
    events = {
        'chat': [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'cut',
                    'function': {'name': 'tool_0', 'arguments': '{"text":'}}]}, 'finish_reason': 'length'}]}],
        'responses': [{'type': 'response.incomplete', 'response': payloads['responses']}],
        'anthropic': [{'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'}}],
    }

    @app.post('/{path:path}')
    async def reply(path):
        if not streaming:
            return payloads[dialect]
        async def stream():
            for event in events[dialect]:
                yield 'data: ' + json.dumps(event) + '\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream')

    async with live_server(app) as endpoint:
        provider = HTTPProvider(endpoint, dialect=dialect)
        with pytest.raises(ModelResponseError) as caught:
            await provider.generate(ModelRequest(model='fixture', prompt='test',
                tools=[ToolSpec(name='test.tool')], parameters={'stream': streaming}), 'fixture')
    assert caught.value.output_limited


async def test_segmented_build_preserves_branches_inputs_and_saved_progress_after_timeout(hub, monkeypatch):
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)
    calls = []
    timed_out = False

    class Builder:
        async def generate(self, request, model):
            nonlocal timed_out
            calls.append(request.response_schema['title'])
            if request.response_schema['title'] == 'BuildDraft':
                raise ModelResponseError('length')
            context = json.loads(request.messages[-1]['content'])
            draft = context['draft']
            if not draft:
                batch = {'edits': [{'op': 'set', 'path': [], 'value': {
                    'workflow': {'name': '分叉处理', 'steps': []}, 'explanation': '分别处理两个输入后汇合', 'questions': []}}], 'done': False}
            elif not timed_out:
                timed_out = True
                raise httpx.ReadTimeout('fixture interruption')
            elif not draft['workflow']['steps']:
                batch = {'edits': [{'op': 'append', 'path': ['workflow', 'steps'], 'value': {
                    'id': name, 'target': 'core.echo', 'input': {'value': {'$ref': '$input.'+name}}}}
                    for name in ('left', 'right')], 'done': False}
            else:
                assert [s['id'] for s in draft['workflow']['steps']] == ['left', 'right']
                batch = {'edits': [{'op': 'append', 'path': ['workflow', 'steps'], 'value': {
                    'id': 'join', 'target': 'core.echo', 'depends_on': ['left', 'right'],
                    'input': {'a': {'$ref': 'left.value'}, 'b': {'$ref': 'right.value'}}}}], 'done': True}
            return ModelResult(data=batch, usage={'input_tokens': 100, 'output_tokens': 200})

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('分别处理 left 和 right 两个输入，再汇合为 a 和 b。')
    run = await hub.wait(start_build(hub, 'segmented', body)['id'])
    assert run['status'] == 'succeeded', run
    assert run['steps'][0]['attempts'] == 2
    assert calls == ['BuildDraft', 'BuildEdits', 'BuildEdits', 'BuildEdits', 'BuildEdits']
    plan = build_status(hub, 'segmented', body)
    assert plan['status'] == 'ready', plan
    result = await hub.wait(hub.submit(plan['workflow'] | {'inputs': {'left': 'original', 'right': 'reference'}}))
    assert result['status'] == 'succeeded'
    assert result['steps'][-1]['output'] == {'a': 'original', 'b': 'reference'}
    assert sum(e['kind'] == 'build.output_recovery' for e in hub.store.events(run['id'])) == 1


async def test_segment_recovery_respects_explicit_limit_and_does_not_publish_invalid_draft(hub):
    class Builder:
        async def generate(self, request, model):
            if request.response_schema['title'] == 'BuildDraft':
                raise ModelResponseError('max_output_tokens')
            return ModelResult(data={'edits': [], 'done': True}, usage={'input_tokens': 1, 'output_tokens': 1})

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('never accept an empty draft')
    body['limits'] = {'model_calls': 13}
    run = await hub.wait(start_build(hub, 'invalid-segments', body)['id'])
    assert run['status'] == 'failed'
    assert run['usage']['model_calls'] == 13
    assert run['steps'][0]['retry_state']['error']['category'] == 'budget'
    assert not hub.development.workflows() and not hub.code.list()


async def test_segment_batch_is_atomic_and_truncated_edits_are_not_applied(hub):
    calls = 0

    class Builder:
        async def generate(self, request, model):
            nonlocal calls
            calls += 1
            if calls in (1, 3):
                raise ModelResponseError('length')
            context = json.loads(request.messages[-1]['content'])
            assert context['draft'] == {}  # Neither rejected batch left a partial edit.
            if calls == 2:
                return ModelResult(data={'edits': [
                    {'op': 'set', 'path': ['explanation'], 'value': 'must be rolled back'},
                    {'op': 'append', 'path': ['missing'], 'value': 1}], 'done': False})
            return ModelResult(data={'edits': [{'op': 'set', 'path': [], 'value': {
                'workflow': None, 'explanation': '请提供原图', 'questions': ['原图在哪里？']}}], 'done': True})

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('atomic draft')
    run = await hub.wait(start_build(hub, 'atomic', body)['id'])
    assert run['status'] == 'succeeded', run
    assert build_status(hub, 'atomic', body)['status'] == 'clarification'
    assert calls == 4


async def test_segmented_code_is_tested_and_repaired_before_publication(hub):
    class Builder(CSVBuilder):
        draft = None
        source = None

        async def generate(self, request, model):
            if request.response_schema['title'] == 'BuildEdits':
                context = json.loads(request.messages[-1]['content'])
                draft = context['draft']
                if not draft:
                    value = json.loads(json.dumps(self.draft))
                    value['code_candidate']['files']['extension.js'] = ''
                    return ModelResult(data={'edits': [{'op': 'set', 'path': [], 'value': value}], 'done': False})
                return ModelResult(data={'edits': [{'op': 'append_text',
                    'path': ['code_candidate', 'files', 'extension.js'], 'value': self.source}], 'done': True})
            result = await super().generate(request, model)
            if self.draft is None and result.data.get('code_candidate'):
                self.draft = result.data
                self.source = self.draft['code_candidate']['files']['extension.js']
                raise ModelResponseError('length')
            return result

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('Sum CSV amounts by customer.')
    run = await hub.wait(start_build(hub, 'segmented-code', body)['id'])
    assert run['status'] == 'succeeded', run
    plan = build_status(hub, 'segmented-code', body)
    assert plan['status'] == 'ready', plan
    assert [r['passed'] for r in plan['development']] == [False, True]
    assert [c['package']['manifest']['revision'] for c in hub.code.list() if c['status'] == 'published'] == [2]


async def test_legacy_builder_retry_migrates_default_limit_without_resetting_usage(hub):
    calls = 0

    class Builder:
        async def generate(self, request, model):
            nonlocal calls
            calls += 1
            if request.response_schema['title'] == 'BuildDraft':
                raise ModelResponseError('length')
            context = json.loads(request.messages[-1]['content'])
            if calls > 8:
                assert context['draft']['explanation'] == 'saved 8'
                batch = {'edits': [{'op': 'set', 'path': ['workflow'], 'value': {
                    'name': 'finished', 'steps': [{'id': 'echo', 'target': 'core.echo'}]}}], 'done': True}
            else:
                batch = {'edits': [{'op': 'set', 'path': ['explanation'], 'value': f'saved {calls}'}], 'done': False}
            return ModelResult(data=batch, usage={'input_tokens': 10, 'output_tokens': 10})

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('complete a legacy build')
    identifier = start_build(hub, 'legacy', body)['id']
    with hub.store.transaction() as db:
        spec = json.loads(db.execute('SELECT spec FROM runs WHERE id=?', (identifier,)).fetchone()[0])
        spec['limits'].update(model_calls=8, tool_calls=8, output_tokens=65536)
        db.execute('UPDATE runs SET spec=? WHERE id=?', (encode(spec), identifier))
    failed = await hub.wait(identifier)
    assert failed['status'] == 'failed' and failed['steps'][0]['retry_state']['error']['category'] == 'budget'
    assert calls == 8 and failed['retry']['allowed'] and failed['retry']['build_budget_upgrade']
    resumed = await retry_run(hub, identifier, RetryRequest(expected_updated=failed['updated']))
    assert resumed['usage'] == failed['usage']
    assert resumed['spec']['limits']['model_calls'] is None
    assert resumed['spec']['limits']['output_tokens'] is None
    assert all(s['spec']['timeout_seconds'] is None for s in resumed['steps'])
    completed = await hub.wait(identifier)
    assert completed['status'] == 'succeeded', completed
    assert calls == 9
    assert build_status(hub, 'legacy', body)['status'] == 'ready'
    assert any(e['kind'] == 'build.budget_upgraded' for e in hub.store.events(identifier))


async def test_internal_assembler_accepts_previous_call_signature(hub):
    from easyagent.build_recovery import assemble

    class Builder:
        async def generate(self, request, model):
            return ModelResult(data={'edits': [{'op': 'set', 'path': ['answer'], 'value': 42}], 'done': True})

    async def old_caller(args, ctx):
        progress = {'segments': {'draft': {}, 'turns': 0}}
        return await assemble(hub.build_capabilities, ctx, 'planner',
            {'type': 'object', 'properties': {'answer': {'const': 42}}, 'required': ['answer']},
            'Answer the task', {}, progress, lambda: hub.store.checkpoint(ctx.job, progress), None)

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    hub.tools.register(ToolSpec(name='test.old_caller'), old_caller)
    run = await hub.wait(hub.submit({'name': 'old call shape', 'steps': [{'id': 'old', 'target': 'test.old_caller'}]}))
    assert run['status'] == 'succeeded', run
    assert run['steps'][0]['output'] == {'answer': 42}


async def test_generated_runtime_type_error_is_fed_back_and_repaired(hub):
    class Builder(CSVBuilder):
        observed_type_error = False

        async def generate(self, request, model):
            if request.response_schema['title'] == 'BuildDraft':
                content = json.loads(request.messages[-1]['content'])
                if content.get('report'):
                    self.observed_type_error = 'TypeError' in json.dumps(content['report'])
            result = await super().generate(request, model)
            code = result.data.get('code_candidate')
            if code and code['manifest']['revision'] == 1:
                code['files']['extension.js'] = 'function handle(r){const missing=null;return missing.call();}'
            return result

    provider = Builder()
    hub.models.register('planner', provider, 'fixture', ['decision'])
    body = assistant('Sum CSV amounts by customer.')
    run = await hub.wait(start_build(hub, 'runtime-error', body)['id'])
    assert run['status'] == 'succeeded', run
    plan = build_status(hub, 'runtime-error', body)
    assert plan['status'] == 'ready', plan
    assert provider.observed_type_error
    assert [r['passed'] for r in plan['development']] == [False, True]


async def test_model_continuation_uses_remaining_output_allowance_instead_of_failing_early(hub):
    ceilings = []

    class Provider:
        async def generate(self, request, model):
            ceilings.append(request.max_output_tokens)
            return ModelResult(text='done', usage={'input_tokens': 1, 'output_tokens': 6 if len(ceilings) == 1 else 2})

    hub.models.register('planner', Provider(), 'fixture', ['chat'])
    run = await hub.wait(hub.submit({'name': 'use available capacity', 'limits': {'output_tokens': 10}, 'steps': [
        {'id': 'first', 'kind': 'model', 'target': 'planner', 'input': {'prompt': 'first'}},
        {'id': 'second', 'kind': 'model', 'target': 'planner', 'depends_on': ['first'], 'input': {'prompt': 'finish'}}]}))
    assert run['status'] == 'succeeded', run
    assert ceilings == [10, 4]
    assert run['usage']['output_reserved'] == 8 and run['spec']['limits']['output_tokens'] == 10


@pytest.mark.parametrize('interrupt_response', [False, True])
async def test_default_builder_completes_past_previous_call_segment_and_output_caps(hub, monkeypatch, interrupt_response):
    # This large graph validates many durable bindings synchronously; use the
    # production lease instead of the fixture's shorter lease.
    hub.lease_seconds = 30
    calls = 0

    class Builder:
        async def generate(self, request, model):
            nonlocal calls
            calls += 1
            if request.response_schema['title'] == 'BuildDraft':
                raise ModelResponseError('length')
            # Recovery may repeat a model request whose response was not saved.
            # Continue from the authoritative draft, not a process-local counter.
            draft = json.loads(request.messages[-1]['content'])['draft']
            done = False
            if not draft:
                edits = [{'op': 'set', 'path': [], 'value': {
                    'workflow': {'name': 'large graph', 'steps': []}, 'explanation': 'Complete all stages'}}]
            else:
                stage = len(draft['workflow']['steps']) + 3
                edits = [{'op': 'append', 'path': ['workflow', 'steps'], 'value': {
                    'id': 'stage'+str(stage), 'target': 'core.echo'}}]
                done = stage == 70
            return ModelResult(data={'edits': edits, 'done': done},
                               usage={'input_tokens': 100, 'output_tokens': 3000})

    interrupted = False
    if interrupt_response:
        generate = hub.generate

        async def lose_response(job, request):
            nonlocal interrupted
            result = await generate(job, request)
            if not interrupted and request.response_schema['title'] == 'BuildEdits' and any(
                edit.get('value', {}).get('id') == 'stage3' for edit in result.data['edits']
            ):
                interrupted = True
                # The response arrived, but its edit has not reached the checkpoint.
                with hub.store.transaction() as db:
                    db.execute('UPDATE steps SET lease_until=0 WHERE run_id=? AND id=?',
                               (job['run_id'], job['id']))
            return result

        monkeypatch.setattr(hub, 'generate', lose_response)

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('Complete a large graph without arbitrary construction caps')
    # Seventy durable model turns are intentionally much larger than a normal
    # fixture; slow CI disks must not be mistaken for a builder-imposed cap.
    run = await hub.wait(start_build(hub, 'large-build', body)['id'], timeout=90)
    assert run['status'] == 'succeeded', run
    assert run['usage']['model_calls'] == calls >= 70 and run['usage']['output_reserved'] > 131072
    assert all(run['spec']['limits'][key] is None for key in ('model_calls', 'tool_calls', 'output_tokens', 'wall_time_seconds'))
    plan = build_status(hub, 'large-build', body)
    assert plan['status'] == 'ready', plan
    assert [step['id'] for step in plan['workflow']['steps']] == ['stage'+str(n) for n in range(3, 71)]
    if interrupt_response:
        assert interrupted and run['steps'][0]['attempts'] >= 2


async def test_unlimited_builder_remains_cancellable_without_publishing_a_draft(hub):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class Builder:
        async def generate(self, request, model):
            if request.response_schema['title'] == 'BuildDraft':
                raise ModelResponseError('length')
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    identifier = start_build(hub, 'cancel-build', assistant('Wait until stopped'))['id']
    await asyncio.wait_for(entered.wait(), 30)
    hub.store.cancel(identifier)
    await asyncio.wait_for(cancelled.wait(), 30)
    assert hub.store.run(identifier)['status'] == 'cancelled'
    assert not hub.development.workflows() and not hub.code.list()


async def test_schema_upgrade_resumes_legacy_segment_without_replanning(hub):
    from easyagent.assistant_builder import BuildDraft
    observed = []

    class Builder:
        async def generate(self, request, model):
            assert request.response_schema['title'] == 'BuildEdits'
            content = json.loads(request.messages[-1]['content'])
            observed.append(content['draft']['workflow']['steps'][0]['id'])
            return ModelResult(data={'edits': [], 'done': True})

    async def resume(args, ctx):
        state = {'build_requests': {'previous-schema-hash': {'segments': {'turns': 10, 'draft': {
            'workflow': {'name': 'saved', 'steps': [{'id': 'saved-step', 'target': 'core.echo'}]},
            'explanation': 'Keep the previously assembled graph', 'questions': []}}}}}
        hub.store.checkpoint(ctx.job, state)
        return await hub.build_capabilities.ask(ctx, 'planner', BuildDraft.model_json_schema(),
            'finish saved draft', {}, check=BuildDraft.model_validate)

    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    hub.tools.register(ToolSpec(name='test.resume_schema'), resume)
    run = await hub.wait(hub.submit({'name': 'upgraded schema', 'steps': [{'id': 'resume', 'target': 'test.resume_schema'}]}))
    assert run['status'] == 'succeeded', run
    assert observed == ['saved-step'] and run['usage']['model_calls'] == 1


@pytest.mark.parametrize('interrupt_repair', [False, True])
async def test_legacy_validation_failure_resumes_existing_graph_and_rechecks_it(hub, monkeypatch, interrupt_repair):
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)
    calls = []
    draft = {'workflow': {'name': 'validated', 'steps': [
        {'id': 'work', 'target': 'test.schema', 'input': {'value': 'correct'}}]}, 'explanation': 'validate inputs'}

    class Builder:
        async def generate(self, request, model):
            calls.append(request.response_schema['title'])
            if calls[-1] == 'BuildDraft':
                return ModelResult(data=draft)
            context = json.loads(request.messages[-1]['content'])
            assert context['draft']['workflow']['steps'][0]['input']['value'] == 12
            if interrupt_repair and len(calls) == 2:
                raise httpx.ReadTimeout('interrupted verification repair')
            return ModelResult(data={'edits': [{'op': 'set', 'path': ['workflow', 'steps', 0, 'input', 'value'],
                                              'value': 'correct'}], 'done': True})

    async def echo(args, ctx):
        return args

    hub.tools.register(ToolSpec(name='test.schema', input_schema={
        'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}), echo)
    hub.models.register('planner', Builder(), 'fixture', ['decision'])
    body = assistant('validate the graph')
    original = await hub.wait(start_build(hub, 'legacy-verification', body)['id'])
    bad = json.loads(json.dumps(draft))
    bad['workflow']['steps'][0]['input']['value'] = 12
    state = original['steps'][1]['state'] | {'draft': bad}
    with hub.store.transaction() as db:
        db.execute('UPDATE steps SET state=?,output=? WHERE run_id=? AND id=?', (
            encode(state), encode({'draft': bad, 'errors': ['old validation failure']}), original['id'], 'verify'))
    invalid = hub.store.run(original['id'])
    assert invalid['retry']['allowed'] and invalid['retry']['verification_failure']
    assert invalid['retry']['preserved_steps'] == 1
    with hub.store.connect() as db:
        compiled = dict(db.execute("SELECT * FROM invocations WHERE run_id=? AND step_id='compile'", (original['id'],)).fetchone())
    await retry_run(hub, original['id'], RetryRequest(expected_updated=invalid['updated']))
    result = await hub.wait(original['id'])
    assert result['status'] == 'succeeded', result
    assert calls == ['BuildDraft', *(['BuildEdits'] * (2 if interrupt_repair else 1))]
    assert result['steps'][0]['attempts'] == 1
    with hub.store.connect() as db:
        assert dict(db.execute('SELECT * FROM invocations WHERE id=?', (compiled['id'],)).fetchone()) == compiled
    assert build_status(hub, 'legacy-verification', body)['status'] == 'ready'
