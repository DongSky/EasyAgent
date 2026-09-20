import asyncio

import httpx
import pytest

from easyagent.api import create_app
from easyagent.runtime import Hub
from easyagent_client import HubClient, RunStopped
from conftest import live_server


def flow():
    return {'name': 'named result', 'metadata': {
        'input_schema': {'type': 'object', 'properties': {'name': {'type': 'string', 'minLength': 1}}, 'required': ['name'], 'additionalProperties': False},
        'outputs': {'answer': {'$ref': 'echo.name'}, 'file': {'$ref': 'save'}}},
        'steps': [{'id': 'echo', 'target': 'core.echo', 'input': {'name': {'$ref': '$input.name'}}},
                  {'id': 'save', 'kind': 'artifact', 'depends_on': ['echo'], 'input': {'name': 'answer.txt', 'content': {'$ref': 'echo.name'}}}]}


async def test_named_call_validation_idempotency_version_update_and_restart(api, tmp_path):
    url, hub = api
    async with HubClient(url) as client:
        await client.request('PUT', '/v1/workflows/demo.named', flow())
        with pytest.raises(httpx.HTTPStatusError) as invalid:
            await client.workflow('demo.named').start({'wrong': 'input'})
        assert invalid.value.response.status_code == 422
        jobs = await asyncio.gather(*[client.workflow('demo.named').start({'name': 'Alice'}, key='same-operation') for _ in range(5)])
        assert len({job.id for job in jobs}) == 1
        output = await jobs[0].result(poll_interval=.02)
        assert output.outputs['answer'] == 'Alice'
        await output.download('file', tmp_path/'answer.txt')
        assert (tmp_path/'answer.txt').read_text(encoding='utf-8') == 'Alice'
        changed = flow()
        changed['steps'][0]['input'] = {'name': 'new version'}
        await client.request('PUT', '/v1/workflows/demo.named?expected_revision=1', changed)
        again = await client.workflow('demo.named').start({'name': 'Alice'}, key='same-operation')
        assert again.id == jobs[0].id
        assert (await (await client.workflow('demo.named', revision=1).start({'name': 'Bob'})).result(poll_interval=.02)).outputs['answer'] == 'Bob'
        with pytest.raises(httpx.HTTPStatusError) as conflict:
            await client.workflow('demo.named').start({'name': 'Carol'}, key='same-operation')
        assert conflict.value.response.status_code == 409
    restored = Hub(hub.store.path)
    async with live_server(create_app(restored, manage_workers=False)) as restored_url, HubClient(restored_url) as client:
        assert (await client.workflow('demo.named').start({'name': 'Alice'}, key='same-operation')).id == jobs[0].id


async def test_handles_pause_resume_timeout_and_errors(api):
    url, hub = api
    definition = {'name': 'needs input', 'metadata': {'outputs': {'answer': {'$ref': 'input.name'}}}, 'steps': [
        {'id': 'input', 'kind': 'input', 'input': {'schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}}}]}
    async with HubClient(url) as client:
        await client.request('PUT', '/v1/workflows/demo.input', definition)
        job = await client.workflow('demo.input').start()
        with pytest.raises(RunStopped) as paused:
            await job.result(poll_interval=.01)
        assert paused.value.run_id == job.id and paused.value.status == 'waiting_input'
        await client.respond(paused.value.state['input_requests'][0]['id'], {'name': 'confirmed'})
        assert (await client.run_handle(job.id).result(poll_interval=.01)).outputs['answer'] == 'confirmed'
        delayed = {'name': 'later', 'steps': [{'id': 'future', 'target': 'core.echo', 'not_before': 9999999999}]}
        await client.request('PUT', '/v1/workflows/demo.later', delayed)
        later = await client.workflow('demo.later').start()
        with pytest.raises(TimeoutError, match=later.id):
            await later.result(timeout=.03, poll_interval=.01)
        assert (await client.run(later.id))['status'] != 'cancelled'
        await later.cancel()
        with pytest.raises(RunStopped) as cancelled:
            await later.result()
        assert cancelled.value.status == 'cancelled'
