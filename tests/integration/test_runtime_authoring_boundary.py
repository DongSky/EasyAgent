"""Actual agent tool calls across HTTP services, SQLite recovery and Studio APIs."""
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from conftest import live_server
from easyagent.api import create_app
from easyagent.contracts import ModelResult, ToolCall
from easyagent.development import DEVELOPMENT_TOOLS
from easyagent.runtime import Hub


class AuthorModel:
    """Deterministic integration provider: the runtime still invokes every tool itself."""
    def __init__(self, actions):
        self.actions = actions

    async def generate(self, request, model):
        observed = [json.loads(m['content']) for m in request.messages if m['role'] == 'tool']
        index = len(observed)
        if index == len(self.actions):
            return ModelResult(text=json.dumps(observed))
        name, arguments = self.actions[index]
        if callable(arguments):
            arguments = arguments(observed)
        return ModelResult(tool_calls=[ToolCall(id=f'call-{index}', name='development.'+name, arguments=arguments)])


def parent(grant, *, limits=None):
    return {'name': 'Agent runtime development', 'limits': limits or {}, 'steps': [{
        'id': 'developer', 'kind': 'agent', 'target': 'author', 'timeout_seconds': 60,
        'input': {'prompt': 'Read docs; create, save, update and execute nodes/workflows.', 'tools': DEVELOPMENT_TOOLS,
                  'development': grant, 'max_turns': 32, 'max_tool_calls': 32}}]}


def definition(url, **changes):
    return {'name': 'test.classify', 'description': 'Test runtime API', 'url': url, 'method': 'POST', 'effect': 'read',
            'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                             'required': ['text'], 'additionalProperties': False}, **changes}


def graph(name='test.classify'):
    return {'name': 'Created by agent', 'inputs': {'text': 'move house'}, 'steps': [
        {'id': 'classify', 'target': name, 'input': {'text': {'$ref': '$input.text'}}},
        {'id': 'receipt', 'kind': 'artifact', 'depends_on': ['classify'],
         'input': {'name': 'receipt.json', 'media_type': 'application/json', 'content': {'$ref': 'classify'}}}]}


async def test_agent_authors_updates_executes_and_recovers_committed_save(tmp_path, monkeypatch):
    monkeypatch.setenv('RUNTIME_TEST_KEY', 'synthetic-secret')
    remote, calls = FastAPI(), []
    @remote.get('/api-docs')
    async def docs():
        return HTMLResponse('<h1>POST /evaluate</h1><p>Supply text, returns text and transport.</p>')
    @remote.post('/evaluate')
    async def evaluate(request: Request):
        assert request.headers['authorization'] == 'Bearer synthetic-secret'
        body = dict(request.query_params) if request.query_params else await request.json()
        calls.append(body)
        return {'text': body['text'], 'transport': 'query' if request.query_params else 'body'}
    async with live_server(remote) as origin:
        first = definition(origin+'/evaluate')
        second = first | {'parameter_locations': {'text': 'query'}, 'description': 'v2 uses query'}
        actions = [
            ('catalog', {}), ('read_document', {'url': origin+'/api-docs'}),
            ('save_api', {'service': 'decision', 'definition': first, 'expected_revision': 0}),
            ('save_workflow', {'id': 'test.flow', 'workflow': graph(), 'expected_revision': 0}),
            ('run_workflow', {'id': 'test.flow', 'revision': 1}),
            ('save_api', {'service': 'decision', 'definition': second, 'expected_revision': 1}),
            ('save_workflow', {'id': 'test.flow', 'workflow': graph() | {'inputs': {'text': 'book flight'}}, 'expected_revision': 1}),
            ('run_workflow', {'id': 'test.flow', 'revision': 2}),
            ('call_api', {'id': 'test.classify', 'revision': 1, 'arguments': {'text': 'old version still works'}}),
            ('get_workflow', {'id': 'test.flow', 'revision': 1})]
        hub = Hub(tmp_path/'agent.db', concurrency=1, poll_seconds=.01)
        hub.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
        # Crash after the definition commits, before ToolRegistry records success.
        spec, handler = hub.tools.entries['development.save_api']
        crashed = False
        async def crash_once(args, ctx):
            nonlocal crashed
            result = await handler(args, ctx)
            if not crashed:
                crashed = True
                raise RuntimeError('simulated lost acknowledgement after commit')
            return result
        hub.tools.entries['development.save_api'] = (spec, crash_once)
        grant = {'namespace': 'test', 'documents': [origin+'/api-docs'], 'services': {'decision': {
            'url': origin+'/evaluate', 'methods': ['POST'], 'effect': 'read', 'api_key_env': 'RUNTIME_TEST_KEY'}}}
        await hub.start()
        try:
            run = await hub.wait(hub.submit(parent(grant)))
        finally:
            await hub.stop()
        assert run['status'] == 'succeeded', run
        observed = json.loads(run['steps'][0]['output']['text'])
        assert all('error' not in item for item in observed), observed
        assert 'POST /evaluate' in observed[1]['text']
        assert observed[4]['outputs']['classify']['transport'] == 'body'
        assert observed[7]['outputs']['classify']['transport'] == 'query'
        assert observed[8]['transport'] == 'body'
        assert observed[9]['workflow']['steps'][0]['tool_revision'] == 1
        assert len(run['children']) == 2 and len(calls) == 3
        assert run['usage']['child_runs'] == 2
        assert len(hub.development.list_versions('api', 'test.classify')) == 2
        assert len(hub.development.list_versions('workflow', 'test.flow')) == 2
        fresh = Hub(hub.store.path, concurrency=1, poll_seconds=.01)
        await fresh.start()
        try:
            rerun = await fresh.wait(fresh.submit(fresh.development.get('workflow', 'test.flow', 1)['workflow']))
        finally:
            await fresh.stop()
        assert rerun['status'] == 'succeeded'
        assert rerun['steps'][0]['output']['transport'] == 'body'
        with hub.store.connect() as db:
            serialized = '\n'.join(db.iterdump())
        assert 'synthetic-secret' not in serialized


async def test_management_versions_pin_paused_run_across_restart(api):
    url, hub = api
    remote = FastAPI()
    @remote.post('/evaluate')
    async def evaluate(request: Request):
        return {'version': 'v2' if request.query_params else 'v1'}
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        body = definition(origin+'/evaluate')
        added = await client.post('/v1/studio/apis', json=body)
        assert added.status_code == 201
        flow = graph()
        flow['steps'].insert(0, {'id': 'wait', 'kind': 'input', 'input': {'prompt': 'Continue?'}})
        flow['steps'][1]['depends_on'] = ['wait']
        saved = (await client.post('/v1/studio/workflows', json=flow)).json()
        identifier = hub.submit(saved['workflow'])
        paused = await hub.wait(identifier)
        assert paused['status'] == 'waiting_input'
        changed = body | {'parameter_locations': {'text': 'query'}}
        assert (await client.put('/v1/studio/apis/test.classify?expected_revision=1', json=changed)).status_code == 200
        assert (await client.put('/v1/studio/apis/test.classify?expected_revision=1', json=changed)).status_code == 409
        assert (await client.post('/v1/studio/apis', json=body)).status_code == 409
        updated = await client.put('/v1/studio/workflows/'+saved['id']+'?expected_revision=1', json=flow)
        assert updated.status_code == 200 and updated.json()['revision'] == 2
        assert updated.json()['workflow']['steps'][1]['tool_revision'] == 2
        assert len((await client.get('/v1/studio/workflows/'+saved['id']+'/revisions')).json()) == 2
        await hub.stop()
        fresh = Hub(hub.store.path, concurrency=1, poll_seconds=.01)
        await fresh.start()
        try:
            await client.post('/v1/inputs/'+paused['input_requests'][0]['id'], json={})
            run = await fresh.wait(identifier)
        finally:
            await fresh.stop()
        assert run['status'] == 'succeeded'
        result = next(s for s in run['steps'] if s['id'] == 'classify')
        assert result['output'] == {'version': 'v1'}
        assert run['spec'] == paused['spec']


async def test_agent_child_input_restart_and_write_approval(tmp_path):
    remote, calls = FastAPI(), []
    @remote.post('/write')
    async def write(request: Request):
        calls.append(await request.json())
        return {'receipt': 'synthetic-confirmed'}
    async with live_server(remote) as origin:
        body = definition(origin+'/write', effect='write', idempotent=True)
        child = {'name': 'child with input and write', 'steps': [
            {'id': 'input', 'kind': 'input', 'input': {'prompt': 'Provide text', 'schema': body['input_schema']}},
            {'id': 'send', 'target': body['name'], 'depends_on': ['input'], 'input': {'text': {'$ref': 'input.text'}}}]}
        actions = [('save_api', {'service': 'write', 'definition': body, 'expected_revision': 0}),
                   ('save_workflow', {'id': 'test.flow', 'workflow': child, 'expected_revision': 0}),
                   ('run_workflow', {'id': 'test.flow', 'revision': 1})]
        grant = {'namespace': 'test', 'services': {'write': {'url': origin+'/write', 'methods': ['POST'], 'effect': 'write'}}}
        hub = Hub(tmp_path/'resume.db', concurrency=1, poll_seconds=.01, lease_seconds=.3)
        hub.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
        await hub.start()
        identifier = hub.submit(parent(grant))
        paused = await hub.wait(identifier)
        assert paused['status'] == 'waiting_input' and not calls
        await hub.stop()
        fresh = Hub(hub.store.path, concurrency=1, poll_seconds=.01, lease_seconds=.3)
        fresh.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
        async with live_server(create_app(fresh)) as server, httpx.AsyncClient(base_url=server) as client:
            response = await client.post('/v1/inputs/'+paused['input_requests'][0]['id'], json={'text': 'authorized'})
            assert response.status_code == 200
            approval = await fresh.wait(identifier)
            assert approval['status'] == 'waiting_approval' and not calls
            assert len(approval['approvals']) == 1
            response = await client.post('/v1/approvals/'+approval['approvals'][0]['id'], json={'approved': True})
            assert response.status_code == 200
            completed = await fresh.wait(identifier)
            assert completed['status'] == 'succeeded', completed
            assert len(completed['children']) == 1 and calls == [{'text': 'authorized'}]
            assert len(fresh.development.list_versions('api')) == 1
            assert len(fresh.development.list_versions('workflow')) == 1


async def test_agent_development_grant_rejects_escalation_and_recovers(hub):
    endpoint = 'https://allowed.example/evaluate'
    grant = {'namespace': 'test', 'services': {'decision': {'url': endpoint, 'methods': ['POST'], 'effect': 'write'}}}
    forbidden = [definition('https://attacker.example/evaluate'), definition(endpoint, api_key_env='PRIVATE_KEY'),
                 definition(endpoint), definition(endpoint, name='user.owned', effect='write')]
    actions = [('save_api', {'service': 'decision', 'definition': b, 'expected_revision': 0}) for b in forbidden]
    nested = {'name': 'escalation', 'steps': [{'id': 'agent', 'kind': 'agent', 'target': 'mock',
        'input': {'prompt': 'grant self access', 'development': grant, 'tools': DEVELOPMENT_TOOLS}}]}
    actions += [('save_workflow', {'id': 'test.bad', 'workflow': nested, 'expected_revision': 0}),
                ('read_document', {'url': 'http://127.0.0.1/private'})]
    hub.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
    run = await hub.wait(hub.submit(parent(grant)))
    assert run['status'] == 'succeeded'
    assert all(item['error'] == 'PermissionError' for item in json.loads(run['steps'][0]['output']['text']))
    assert not hub.development.list_versions('api') and not hub.development.list_versions('workflow')
    no_grant = parent(grant)
    del no_grant['steps'][0]['input']['development']
    with pytest.raises(PermissionError, match='explicit development grant'):
        hub.submit(no_grant)


async def test_agent_child_budget_and_cancellation(tmp_path):
    hub = Hub(tmp_path/'limits.db', concurrency=1, poll_seconds=.01)
    graph_input = {'name': 'waiting child', 'steps': [{'id': 'wait', 'kind': 'input'}]}
    actions = [('save_workflow', {'id': 'test.wait', 'workflow': graph_input, 'expected_revision': 0}),
               ('run_workflow', {'id': 'test.wait', 'revision': 1})]
    hub.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
    await hub.start()
    try:
        identifier = hub.submit(parent({'namespace': 'test'}, limits={'child_runs': 0}))
        failed = await hub.wait(identifier)
        assert failed['status'] == 'succeeded'  # the Agent receives a recoverable budget refusal
        results = json.loads(failed['steps'][0]['output']['text'])
        assert results[-1]['error'] == 'ValueError' and 'budget' in results[-1]['message']
        assert not failed['children']
        second_actions = [('run_workflow', {'id': 'test.wait', 'revision': 1})]
        hub.models.bindings['author'].provider = AuthorModel(second_actions)
        identifier = hub.submit(parent({'namespace': 'test'}))
        paused = await hub.wait(identifier)
        assert paused['status'] == 'waiting_input'
        hub.store.cancel(identifier)
        cancelled = hub.store.run(identifier)
        assert cancelled['status'] == 'cancelled'
        assert hub.store.run(cancelled['children'][0]['id'])['status'] == 'cancelled'
    finally:
        await hub.stop()
