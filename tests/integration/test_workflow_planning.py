"""Construction uses compact discovery, exact verification, and native structured inputs."""
import asyncio
import json

import httpx
import pytest

from easyagent.contracts import ModelRequest, ModelResult, ToolSpec
from easyagent.models import ModelRegistry
from easyagent.store import encode
from test_workspace_chat import settled


async def test_generated_workflow_strict_business_contract_reuses_through_chat_and_api(api):
    from test_autonomy import Scripted
    url, hub = api
    workflow = {'name': 'business inputs only', 'metadata': {'input_schema': {
        'type': 'object', 'properties': {'value': {'type': 'string'}},
        'required': ['value'], 'additionalProperties': False}}, 'steps': [
        {'id': 'result', 'kind': 'transform', 'input': {'text': {'$ref': '$input.value'}}}]}
    planner = Scripted([
        ('workflows.save', {'id': 'business', 'workflow': workflow}),
        ('workflows.run', {'id': 'business', 'revision': 1, 'inputs': {'value': 'first'}})],
        dispatch=lambda context: {'action': 'use', 'candidate': 'business@1', 'confidence': 1,
                                  'inputs': {'value': 'second'}, 'message': 'Reuse business contract'})
    hub.models.register('business', planner, 'fixture', ['chat', 'decision'])
    hub.autonomy.configure({'reflection': False})
    conversation = await hub.conversations.create({'workspace': True, 'model': 'business'})
    await hub.conversations.send(conversation['id'], {'text': 'Create and run', 'intent': 'create', 'execution': 'automatic'})
    result = await settled(hub, conversation['id'])
    assert result['turns'][0]['task']['phase'] == 'completed'
    saved = hub.development.get('workflow', 'business')
    assert saved['workflow']['inputs'] == {}
    # Reproduce old saved versions, without migrating their immutable definitions.
    workflow['inputs'] = {'message': '', 'attachments': [], 'attachment_ids': []}
    legacy = hub.development.save_workflow('legacy', workflow)
    for key in ('business@1', 'legacy@1'):
        planner.dispatch = lambda context, key=key: {'action': 'use', 'candidate': key, 'confidence': 1,
                                                     'inputs': {'value': 'second'}, 'message': 'Reuse'}
        await hub.conversations.send(conversation['id'], {'text': 'Run with second', 'workflow': key, 'execution': 'automatic'})
        result = await settled(hub, conversation['id'])
        assert result['turns'][-1]['task']['phase'] == 'completed', result['turns'][-1]
        run = hub.store.run(result['turns'][-1]['task']['run_id'])
        assert run['steps'][0]['output'] == {'text': 'second'}
        async with httpx.AsyncClient(base_url=url) as client:
            path = '/v1/workflows/' + key.split('@')[0] + '/runs'
            response = await client.post(path, json={'inputs': {'value': 'third'}})
            assert response.status_code == 201, response.text
            run = await hub.wait(response.json()['id'])
            assert run['steps'][0]['output'] == {'text': 'third'}
            # A caller cannot smuggle an undeclared field through legacy compatibility.
            for extra in ('message', 'undeclared'):
                response = await client.post(path, json={'inputs': {'value': 'third', extra: ''}})
                assert response.status_code == 422
    assert hub.development.get('workflow', 'legacy') == legacy


async def test_context_matches_legacy_serialization_without_extra_tool(hub):
    requests = []
    class Capture:
        async def generate(self, request, model):
            requests.append(request)
            return ModelResult(text=request.messages[-1]['content'])
    hub.models.register('capture', Capture(), 'fixture', ['chat'])
    data = {'客户': ['Alice', 'Bob'], 'amount': 17}
    old = hub.submit({'name': 'legacy context', 'steps': [
        {'id': 'data', 'kind': 'transform', 'input': data},
        {'id': 'text', 'target': 'core.to_text', 'depends_on': ['data'], 'input': {'value': {'$ref': 'data'}}},
        {'id': 'answer', 'kind': 'model', 'target': 'capture', 'depends_on': ['text'],
         'input': {'messages': [{'role': 'system', 'content': 'Check data'},
                                {'role': 'user', 'content': {'$ref': 'text.text'}}]}}]})
    old_result = await hub.wait(old)
    new = hub.submit({'name': 'native context', 'steps': [
        {'id': 'data', 'kind': 'transform', 'input': data},
        {'id': 'answer', 'kind': 'model', 'target': 'capture', 'depends_on': ['data'],
         'input': {'messages': [{'role': 'system', 'content': 'Check data'}], 'context': {'$ref': 'data'}}}]})
    new_result = await hub.wait(new)
    assert requests[0].messages == requests[1].messages
    assert old_result['steps'][-1]['output']['text'] == new_result['steps'][-1]['output']['text']
    assert old_result['usage']['tool_calls'] == 1 and new_result['usage']['tool_calls'] == 0
    assert new_result['usage']['model_calls'] == old_result['usage']['model_calls'] == 1
    assert new_result['status'] == old_result['status'] == 'succeeded'


async def test_native_agent_context_is_pinned_and_not_reappended(hub):
    from easyagent.contracts import ToolCall
    class Capture:
        async def generate(self, request, model):
            content = json.dumps({'facts': ['original']}, ensure_ascii=False, indent=2)
            assert sum(m.get('content') == content for m in request.messages) == 1
            if not any(m['role'] == 'tool' for m in request.messages):
                return ModelResult(tool_calls=[ToolCall(id='read', name='core.echo', arguments={'ok': True})])
            return ModelResult(text='verified')
    hub.models.register('context-agent', Capture(), 'fixture', ['chat'])
    rid = hub.submit({'name': 'agent context', 'steps': [{'id': 'agent', 'kind': 'agent', 'target': 'context-agent',
        'input': {'prompt': 'Check these facts', 'context': {'facts': ['original']}, 'tools': ['core.echo']}}]})
    result = await hub.wait(rid)
    assert result['status'] == 'succeeded'
    state = result['steps'][0]['state']
    material = {'role': 'user', 'content': json.dumps({'facts': ['original']}, ensure_ascii=False, indent=2)}
    assert encode(material) in state['pinned_requests']


async def test_direct_model_context_keeps_prompt_and_zero_values():
    seen = []
    class Capture:
        async def generate(self, request, model):
            seen.append(request.messages)
            return ModelResult(text='ok')
    registry = ModelRegistry()
    registry.register('capture', Capture(), 'fixture', ['chat'])
    for value in (0, False, {}, []):
        await registry.generate(ModelRequest(model='capture', prompt='Task', context=value))
        assert seen[-1] == [{'role': 'user', 'content': 'Task'}, {'role': 'user', 'content': json.dumps(value, indent=2)}]


@pytest.mark.parametrize('decision', ['use', 'clarify', 'create'])
async def test_large_catalog_verifies_frozen_wiring_before_any_execution(hub, decision):
    requests, writes = [], []
    async def write(args, ctx):
        writes.append(args['value'])
        return {'ok': True}
    hub.tools.register(ToolSpec(name='test.write', effect='write'), write)
    for i in range(4):
        hub.development.save_workflow(f'candidate-{i}', {'name': f'Saved task {i}', 'inputs': {'message': ''},
            'steps': [{'id': 'write', 'target': 'test.write', 'input': {'value': f'original-{i}', 'large': 'x' * 6000}}]})
    class Router:
        async def generate(self, request, model):
            if request.response_schema['title'] == 'BuildDraft':
                assert decision == 'create' and len(requests) == 2 and not writes
                return ModelResult(data={'workflow': {'name': 'new task', 'steps': [
                    {'id': 'result', 'kind': 'transform', 'input': {'text': 'new processing'}}]},
                    'explanation': 'Candidate did not fit; built new processing', 'questions': []})
            context = json.loads(request.messages[-1]['content'])
            requests.append(context)
            if len(requests) == 1:
                assert all('steps' not in c and c['operations'][0]['target'] == 'test.write' for c in context['catalog'])
                assert not writes
                # A concurrent save must not replace the discovered frozen revision.
                hub.development.save_workflow('candidate-0', {'name': 'changed', 'steps': [
                    {'id': 'write', 'target': 'test.write', 'input': {'value': 'must not run'}}]}, 1)
            else:
                assert len(requests) == 2
                assert not writes
                assert context['selected_workflow'] is None
                assert context['proposed_workflow'] == 'candidate-0@1'
                assert len(context['catalog']) == 1
                assert context['catalog'][0]['steps'][0]['input']['value'] == 'original-0'
            return ModelResult(data={'action': 'use' if len(requests) == 1 else decision,
                'candidate': 'candidate-0@1', 'confidence': 1, 'inputs': {}, 'message': 'Checked actual wiring'})
    hub.models.register('router', Router(), 'fixture', ['decision'])
    conversation = await hub.conversations.create({'workspace': True, 'model': 'router'})
    await hub.conversations.send(conversation['id'], {'text': 'Handle the saved task', 'execution': 'automatic'})
    result = await settled(hub, conversation['id'])
    task = result['turns'][0]['task']
    assert len(requests) == 2
    assert writes == (['original-0'] if decision == 'use' else [])
    assert task['phase'] == ('clarification' if decision == 'clarify' else 'completed')
    assert len(task['runs']) == {'use': 3, 'clarify': 2, 'create': 4}[decision]
    keys = []
    with hub.store.connect() as db:
        keys = [r[0] for r in db.execute('SELECT idempotency_key FROM runs WHERE id IN (?,?)', task['runs'][:2])]
    assert len(set(keys)) == 2 and any(k.endswith(':verify') for k in keys)


async def test_native_context_preserves_dependency_validation(hub):
    with pytest.raises(ValueError, match='references must point'):
        hub.submit({'name': 'invalid context reference', 'steps': [
            {'id': 'data', 'kind': 'transform'},
            {'id': 'model', 'kind': 'model', 'target': 'mock', 'input': {'context': {'$ref': 'data'}}}]})


@pytest.mark.parametrize('explicit', [False, True])
async def test_one_large_candidate_needs_only_one_routing_call(hub, explicit):
    calls = []
    hub.development.save_workflow('single', {'name': 'single large workflow', 'steps': [
        {'id': 'data', 'kind': 'transform', 'input': {'text': 'x' * 18000}}]})
    class Router:
        async def generate(self, request, model):
            calls.append(request)
            catalog = json.loads(request.messages[-1]['content'])['catalog']
            assert len(catalog) == 1 and catalog[0]['steps'][0]['input']['text'] == 'x' * 18000
            return ModelResult(data={'action': 'use', 'candidate': 'single@1', 'confidence': 1,
                                     'inputs': {}, 'message': 'Use inspected workflow'})
    hub.models.register('single', Router(), 'fixture', ['decision'])
    conversation = await hub.conversations.create({'workspace': True, 'model': 'single'})
    await hub.conversations.send(conversation['id'], {'text': 'Run it', 'execution': 'automatic',
        **({'workflow': 'single@1'} if explicit else {})})
    result = await settled(hub, conversation['id'])
    assert result['turns'][0]['task']['phase'] == 'completed'
    assert len(calls) == 1


async def test_parallel_branches_and_reused_subworkflow_context(hub):
    """One composed flow joins independent branches without changing child boundaries."""
    entered = set()
    both = asyncio.Event()
    async def read(args, ctx):
        entered.add(args['value'])
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 3)
        return {'value': args['value']}
    hub.tools.register(ToolSpec(name='test.read'), read)
    saved = hub.development.save_workflow('pair', {'name': 'reusable pair', 'inputs': {'left': 2, 'right': 3},
        'metadata': {'outputs': {'left': {'$ref': 'left.value'}, 'right': {'$ref': 'right.value'}}}, 'steps': [
            {'id': 'left', 'target': 'test.read', 'input': {'value': {'$ref': '$input.left'}}},
            {'id': 'right', 'target': 'test.read', 'input': {'value': {'$ref': '$input.right'}}}]})
    rid = hub.submit({'name': 'one business flow', 'steps': [
        {'id': 'pair', 'kind': 'subworkflow', 'workflow_ref': {'id': saved['id'], 'revision': saved['revision']},
         'input': {'left': 2, 'right': 3}},
        {'id': 'report', 'kind': 'model', 'target': 'mock', 'depends_on': ['pair'],
         'input': {'context': {'left': {'$ref': 'pair.outputs.0.left'}, 'right': {'$ref': 'pair.outputs.0.right'}}}}]})
    result = await hub.wait(rid)
    assert result['status'] == 'succeeded', result['steps']
    assert json.loads(result['steps'][-1]['output']['text']) == {'left': 2, 'right': 3}
    assert len(result['children']) == 1 and entered == {2, 3}


@pytest.mark.parametrize(('intent', 'repair_change'), [
    ('auto', 'valid'), ('workflow', 'valid'), ('auto', 'write'),
    ('auto', 'inputs'), ('auto', 'still_fails'), ('auto', 'explicit_pin')])
async def test_reused_workflow_repair_preserves_writes_and_explicit_pin(hub, intent, repair_change):
    import copy
    writes, builds = [], []
    async def write(args, ctx):
        writes.append(args)
        return {'receipt': 'done'}
    async def checked(args, ctx):
        return {'text': args['value']}
    hub.tools.register(ToolSpec(name='test.once', effect='write', idempotent=False), write)
    hub.tools.register(ToolSpec(name='test.checked', input_schema={'type': 'object', 'properties': {
        'value': {'type': 'string', 'minLength': 1}}, 'required': ['value']}), checked)
    original = {'name': 'reusable task', 'inputs': {'message': '', 'account': 'original'},
                'limits': {'model_calls': 3, 'tool_calls': 4}, 'steps': [
        {'id': 'write', 'target': 'test.once', 'input': {'operation': 'one write'}},
        {'id': 'empty', 'kind': 'transform', 'depends_on': ['write'], 'input': {'value': ''}},
        {'id': 'check', 'target': 'test.checked', 'depends_on': ['empty'], 'input': {'value': {'$ref': 'empty.value'}}}]}
    saved = hub.development.save_workflow('reusable', original)
    class Planner:
        async def generate(self, request, model):
            if request.response_schema['title'] == 'DispatchDecision':
                return ModelResult(data={'action': 'use', 'candidate': 'reusable@1', 'confidence': 1,
                                         'inputs': {}, 'message': 'Use existing task'})
            assert request.response_schema['title'] == 'BuildDraft'
            content = json.loads(request.messages[-1]['content'])
            builds.append(content)
            repaired = copy.deepcopy(content['execution_feedback']['workflow'])
            if repair_change != 'still_fails':
                repaired['steps'][1]['input'] = {'value': 'fixed'}
            if repair_change == 'write':
                repaired['steps'][0]['input'] = {'operation': 'another write'}
            if repair_change == 'inputs':
                repaired['inputs']['account'] = 'different'
            return ModelResult(data={'workflow': repaired, 'explanation': 'Fix invalid input using receipts', 'questions': []})
    hub.models.register('repair', Planner(), 'fixture', ['decision'])
    conversation = await hub.conversations.create({'workspace': True, 'model': 'repair'})
    await hub.conversations.send(conversation['id'], {'text': 'Complete the task', 'execution': 'automatic', 'intent': intent,
        **({'workflow': 'reusable@1'} if intent == 'workflow' or repair_change == 'explicit_pin' else {})})
    result = await settled(hub, conversation['id'])
    assert len(writes) == 1
    assert hub.development.get('workflow', 'reusable')['workflow'] == saved['workflow']
    if intent == 'workflow' or repair_change == 'explicit_pin':
        assert not builds and result['turns'][0]['status'] == 'failed'
    elif repair_change != 'valid':
        assert len(builds) == 1 and result['turns'][0]['status'] == 'failed'
    else:
        assert len(builds) == 1 and result['turns'][0]['status'] == 'succeeded', result['turns']
        run = hub.store.run(result['turns'][0]['task']['run_id'])
        assert run['spec']['limits'] == saved['workflow']['limits']
        assert run['steps'][0]['spec']['kind'] == 'transform'
        assert run['steps'][0]['output'] == {'receipt': 'done'}
        assert run['steps'][-1]['output'] == {'text': 'fixed'}
