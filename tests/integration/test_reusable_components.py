"""Reuse saved components across unrelated workflows without duplicating implementation."""
import copy
import json
import os

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.authoring import validate_draft
from easyagent.contracts import DevelopmentGrant
from easyagent.runtime import Hub
from test_runtime_authoring_boundary import AuthorModel, parent
from test_interop import subprocess_output


def wrapper(identifier, revision=None, name='consumer', message='hello'):
    ref = {'id': identifier}
    if revision is not None:
        ref['revision'] = revision
    return {'name': name, 'inputs': {'message': message}, 'steps': [{
        'id': 'shared', 'kind': 'subworkflow', 'workflow_ref': ref,
        'input': {'message': {'$ref': '$input.message'}}}]}


async def test_shared_api_and_workflow_versions_across_two_consumers_and_restart(api, tmp_path):
    url, hub = api
    remote = FastAPI()
    @remote.post('/normalize')
    async def normalize(request: Request):
        body = await request.json()
        return {'normalized': body['text'].strip().lower()}
    async with live_server(remote) as origin, httpx.AsyncClient(base_url=url) as client:
        added = await client.post('/v1/studio/apis', json={
            'name': 'library.normalize', 'description': 'Shared normalization service', 'method': 'POST',
            'url': origin+'/normalize', 'effect': 'read', 'input_schema': {'type': 'object', 'required': ['text'],
             'properties': {'text': {'type': 'string'}}, 'additionalProperties': False}})
        assert added.status_code == 201
        library = {'name': 'Reusable normalization', 'inputs': {'message': ''}, 'steps': [
            {'id': 'normalize', 'target': 'library.normalize', 'input': {'text': {'$ref': '$input.message'}}}]}
        component = (await client.post('/v1/studio/workflows', json=library)).json()
        first = (await client.post('/v1/studio/workflows', json=wrapper(component['id'], name='moving', message=' MOVE '))).json()
        second = (await client.post('/v1/studio/workflows', json=wrapper(component['id'], name='travel', message=' TRAVEL '))).json()
        for saved in (first, second):
            step = saved['workflow']['steps'][0]
            assert step['workflow_ref'] == {'id': component['id'], 'revision': 1}
            assert step['body']['steps'][0]['tool_revision'] == 1
        changed = copy.deepcopy(library)
        changed['steps'].append({'id': 'revision_note', 'kind': 'transform', 'input': {'revision': 2}})
        updated = await client.put('/v1/studio/workflows/'+component['id']+'?expected_revision=1', json=changed)
        assert updated.status_code == 200 and updated.json()['revision'] == 2
        await hub.stop()
        fresh = Hub(hub.store.path, concurrency=1, poll_seconds=.01)
        await fresh.start()
        try:
            for saved, expected in ((first, 'move'), (second, 'travel')):
                restored = fresh.development.get('workflow', saved['id'])['workflow']
                run = await fresh.wait(fresh.submit(restored))
                assert run['status'] == 'succeeded', run
                result = run['steps'][0]['output']['results'][0]
                assert result == {'normalize': {'normalized': expected}}
                assert run['spec']['steps'][0]['workflow_ref']['revision'] == 1
            latest = await fresh.wait(fresh.submit(wrapper(component['id'], name='new consumer')))
            assert latest['spec']['steps'][0]['workflow_ref']['revision'] == 2
            assert latest['steps'][0]['output']['results'][0]['revision_note'] == {'revision': 2}
            direct = await fresh.wait(fresh.submit({'name': 'another direct API consumer', 'steps': [
                {'id': 'n', 'target': 'library.normalize', 'tool_revision': 1, 'input': {'text': ' DIRECT '}}]}))
            assert direct['steps'][0]['output'] == {'normalized': 'direct'}
            adapter = wrapper(component['id'], 1)
            adapter['steps'][0]['input']['message'] = {'$ref': '$input.item'}
            batch = await fresh.wait(fresh.submit({'name': 'batch reuse', 'steps': [{
                'id': 'each', 'kind': 'foreach', 'body': adapter, 'input': {'items': [' A ', ' B ']}}]}))
            assert batch['status'] == 'succeeded' and len(batch['children']) == 2
            assert [r['shared']['results'][0]['normalize']['normalized'] for r in batch['steps'][0]['output']['results']] == ['a', 'b']
            # The typed Rust SDK must preserve the reference AND pinned tool version.
            v2_api = fresh.development.get('api', 'library.normalize')['definition'] | {'response_mode': 'text'}
            fresh.development.save_api(v2_api, 1)
            typed = wrapper(component['id'], 1, name='Rust typed component reuse', message=' RUST ')
            typed['steps'].append({'id': 'direct', 'target': 'library.normalize', 'tool_revision': 1, 'input': {'text': ' PINNED '}})
            path = tmp_path/'typed.json'
            path.write_text(json.dumps(typed), encoding='utf-8')
            result = json.loads(await subprocess_output(['cargo', 'run', '--quiet', '--manifest-path', 'sdk/rust/Cargo.toml',
                                '--example', 'reuse'], {**os.environ, 'EAH_URL': url, 'EAH_WORKFLOW_FILE': str(path)}, timeout=240))
            nodes = {node['id']: node for node in result['steps']}
            assert nodes['shared']['output']['results'][0]['normalize']['normalized'] == 'rust'
            assert nodes['shared']['spec']['workflow_ref']['revision'] == 1
            assert nodes['direct']['spec']['tool_revision'] == 1
            assert nodes['direct']['output']['normalized'] == 'pinned'
        finally:
            await fresh.stop()


async def test_agent_reuses_shared_component_only_with_transitive_permissions(hub):
    component = hub.development.save_workflow('library.echo', {'name': 'shared echo', 'steps': [
        {'id': 'echo', 'target': 'core.echo', 'input': {'message': {'$ref': '$input.message'}}}]})
    actions = [('get_workflow', {'id': 'library.echo', 'revision': 1}),
               ('save_workflow', {'id': 'consumer.wrap', 'expected_revision': 0,
                                  'workflow': wrapper('library.echo', 1, message='reused by another agent')}),
               ('run_workflow', {'id': 'consumer.wrap', 'revision': 1})]
    hub.models.register('author', AuthorModel(actions), 'fixture', ['chat'])
    grant = {'namespace': 'consumer', 'workflows': {component['id']: 1}, 'tools': ['core.echo']}
    run = await hub.wait(hub.submit(parent(grant)))
    assert run['status'] == 'succeeded'
    results = json.loads(run['steps'][0]['output']['text'])
    assert all('error' not in r for r in results), results
    assert results[-1]['outputs']['shared']['results'][0]['echo']['message'] == 'reused by another agent'
    assert len(hub.development.list_versions('workflow', 'library.echo')) == 1
    # A readable component does not silently grant all of its tools.
    no_tools = copy.deepcopy(grant)
    no_tools['tools'] = []
    denied = await hub.wait(hub.submit(parent(no_tools)))
    denied_results = json.loads(denied['steps'][0]['output']['text'])
    assert denied_results[0]['error'] == 'PermissionError'
    # A supplied body cannot disguise the actual referenced workflow.
    disguised = wrapper('library.echo', 1)
    disguised['steps'][0]['body'] = {'name': 'innocent body', 'steps': [{'id': 'safe', 'kind': 'transform'}]}
    with pytest.raises(PermissionError, match='unselected API'):
        validate_draft(hub, disguised, allowed=set())
    with pytest.raises(PermissionError, match='not been granted'):
        hub.development.check_workflow(disguised, DevelopmentGrant(namespace='unauthorized', tools=['core.echo']))


async def test_legacy_version_and_inline_nesting_stay_bounded(hub):
    original = {'name': 'legacy component', 'steps': [{'id': 'v', 'kind': 'transform', 'input': {'v': 0}}]}
    hub.store.memory_put('studio-workflows', 'legacy.component', original, 'old-studio')
    prepared = hub.prepare(wrapper('legacy.component'))
    assert prepared.steps[0].workflow_ref.revision == 0
    changed = copy.deepcopy(original)
    changed['steps'][0]['input']['v'] = 1
    hub.development.save_workflow('legacy.component', changed, 0)
    run = await hub.wait(hub.submit(prepared))
    assert run['status'] == 'succeeded'
    assert run['steps'][0]['output']['results'][0]['v'] == {'v': 0}
    assert hub.development.get('workflow', 'legacy.component', 0)['workflow'] == original
    nested = original
    for _ in range(9):
        nested = {'name': 'nested', 'steps': [{'id': 'child', 'kind': 'subworkflow', 'body': nested}]}
    with pytest.raises(ValueError, match='nesting exceeds'):
        hub.submit(nested)
