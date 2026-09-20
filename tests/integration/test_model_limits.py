import json

import pytest

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from conftest import live_server
from easyagent.contracts import AgentConfig, ToolSpec
from easyagent.models import HTTPProvider
from easyagent.model_limits import context_error, read_limits


def test_limits_are_evidence_based_and_default_window_is_not_fixed():
    assert AgentConfig(prompt='hello').context_chars is None
    assert read_limits({'id': 'huge-1m-model'}) == {}
    assert read_limits({'context_length': 12000, 'top_provider': {'max_completion_tokens': 2000}}) == {
        'context_window': 12000, 'max_output_tokens': 2000}
    assert read_limits({'context_length': True, 'max_input_tokens': -1}) == {}
    error = context_error({'error': {'code': 'context_length_exceeded', 'message':
        'private input; maximum context length is 12,345 tokens'}})
    assert error.limit == 12345 and 'private' not in str(error)
    assert context_error({'error': {'message': 'authentication failed'}}) is None


async def test_discovery_tracks_model_selection_and_unknown_limits_in_ui(api):
    import httpx
    from playwright.async_api import async_playwright, expect
    url, hub = api
    remote, requests = FastAPI(), []

    @remote.get('/models')
    async def metadata(request: Request):
        requests.append(request.headers.get('authorization'))
        return {'data': [{'id': 'alpha', 'context_length': 16000, 'max_output_tokens': 4000},
                         {'id': 'beta', 'limits': {'context_window': 32000}}, {'id': 'unknown'}]}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        # Local test server sentinel; never a real service credential.
        body = {'alias': 'planner', 'base_url': endpoint, 'model': 'alpha', 'api_key': 'fixture-credential'}  # pragma: allowlist secret
        await client.post('/v1/studio/connections', json=body)
        first = (await client.get('/v1/studio/connections')).json()['connections'][-1]
        assert first['limits']['context_window'] == 16000
        assert first['limits']['source'] == 'provider_metadata'
        assert 'fixture-credential' not in json.dumps(first)
        await client.get('/v1/studio/connections')
        assert len(requests) == 1
        await client.put('/v1/studio/connections/planner', json={**body, 'model': 'beta', 'api_key': ''})
        second = (await client.get('/v1/studio/connections')).json()['connections'][-1]
        assert second['limits']['context_window'] == 32000
        assert requests[-1] == 'Bearer fixture-credential'
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url+'/#connections')
                await expect(page.locator('[data-model-row=planner] [data-model-limits]')).to_contain_text('32,000')
                await client.put('/v1/studio/connections/planner', json={**body, 'model': 'unknown'})
                await page.reload()
                await expect(page.locator('[data-model-row=planner] [data-model-limits]')).to_contain_text('服务未提供')
                assert 'context_window' not in hub.connections.model_catalog()['connections'][-1]['limits']
            finally:
                await browser.close()


async def test_agent_learns_context_limit_and_resumes_without_replaying_tools(hub):
    remote = FastAPI()
    calls, tools, sizes = [], [], []

    async def read(args, ctx):
        tools.append(args['number'])
        return {'number': args['number'], 'text': 'reference ' * 170}

    hub.tools.register(ToolSpec(name='test.read'), read)

    @remote.post('/chat/completions')
    async def generate(request: Request):
        body = await request.json()
        calls.append(body)
        messages = body['messages']
        assert messages[1]['content'] == 'Keep my original objective'
        ids = {c['id'] for m in messages for c in m.get('tool_calls', [])}
        assert ids == {m['tool_call_id'] for m in messages if m['role'] == 'tool'}
        observed = [json.loads(m['content'])['number'] for m in messages if m['role'] == 'tool']
        if len(calls) == 3:
            sizes.append(len(json.dumps(messages)))
            return JSONResponse({'error': {'code': 'context_length_exceeded', 'message':
                'maximum context length is 7000 tokens. private user details'}}, status_code=400)
        if len(calls) == 4:
            sizes.append(len(json.dumps(messages)))
            return {'choices': [{'message': {'content': 'done'}}]}
        number = max(observed, default=0) + 1
        return {'choices': [{'message': {'content': '', 'tool_calls': [{'id': str(number), 'type': 'function',
            'function': {'name': 'tool_0', 'arguments': json.dumps({'number': number})}}]}}]}

    async with live_server(remote) as endpoint:
        provider = HTTPProvider(endpoint)
        hub.models.register('automatic', provider, 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'automatic context', 'steps': [
            {'id': 'agent', 'kind': 'agent', 'target': 'automatic', 'input': {
                'prompt': 'Keep my original objective', 'tools': ['test.read']}}]}))
    assert run['status'] == 'succeeded', run
    assert tools == [1, 2] and sizes[1] < sizes[0]
    assert provider.limits.public('fixture')['context_window'] == 7000
    events = hub.store.events(run['id'])
    assert any(e['kind'] == 'context.compacted' for e in events)
    assert 'private user details' not in json.dumps(events)


async def test_agent_recovers_truncated_call_without_executing_partial_arguments(hub):
    remote = FastAPI()
    count, executed = 0, []

    async def tool(args, ctx):
        executed.append(args)
        return {'ok': True}

    hub.tools.register(ToolSpec(name='test.complete'), tool)

    @remote.post('/chat/completions')
    async def generate(request: Request):
        nonlocal count
        count += 1
        body = await request.json()
        if count == 1:
            return {'choices': [{'finish_reason': 'length', 'message': {'tool_calls': [{
                'id': 'partial', 'function': {'name': 'tool_0', 'arguments': '{"value":'}}]}}]}
        if count == 2:
            assert 'None of its tool calls were executed' in body['messages'][-1]['content']
            return {'choices': [{'message': {'tool_calls': [{'id': 'complete', 'function': {
                'name': 'tool_0', 'arguments': '{"value":42}'}}]}}]}
        return {'choices': [{'message': {'content': 'done'}}]}

    async with live_server(remote) as endpoint:
        hub.models.register('planner', HTTPProvider(endpoint), 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'truncation', 'steps': [
            {'id': 'agent', 'kind': 'agent', 'target': 'planner', 'input': {
                'prompt': 'use the tool', 'tools': ['test.complete']}}]}))
    assert run['status'] == 'succeeded', run
    assert executed == [{'value': 42}] and count == 3


@pytest.mark.parametrize('known', [True, False])
async def test_large_context_has_no_legacy_character_cap_and_output_cap_is_separate(hub, known):
    remote = FastAPI()

    @remote.get('/models/fixture')
    async def metadata():
        return {'id': 'fixture', **({'context_window': 120000, 'max_output_tokens': 1000} if known else {})}

    @remote.post('/chat/completions')
    async def generate(request: Request):
        body = await request.json()
        assert len(body['messages'][1]['content']) == 80000
        assert body['max_tokens'] == (1000 if known else 2048)
        return {'choices': [{'message': {'content': 'done'}}]}

    async with live_server(remote) as endpoint:
        hub.models.register('planner', HTTPProvider(endpoint), 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'large initial context', 'steps': [
            {'id': 'agent', 'kind': 'agent', 'target': 'planner', 'input': {'prompt': 'a' * 80000}}]}))
    assert run['status'] == 'succeeded', run
    assert not any(e['kind'] == 'context.compacted' for e in hub.store.events(run['id']))


async def test_session_history_can_compact_while_preserving_original_and_latest_request(hub, monkeypatch):
    remote = FastAPI()
    history = [{'role': 'user', 'content': 'original objective'},
               {'role': 'assistant', 'content': 'old discussion ' * 1200},
               {'role': 'user', 'content': 'latest correction'}]
    monkeypatch.setattr(hub.conversations, 'context', lambda job: history)

    @remote.get('/models/fixture')
    async def metadata():
        return {'id': 'fixture', 'context_window': 8000}

    @remote.post('/chat/completions')
    async def generate(request: Request):
        body = await request.json()
        assert [m['content'] for m in body['messages'][1:]] == ['original objective', 'latest correction']
        return {'choices': [{'message': {'content': 'done'}}]}

    async with live_server(remote) as endpoint:
        hub.models.register('planner', HTTPProvider(endpoint), 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'session context', 'steps': [
            {'id': 'agent', 'kind': 'agent', 'target': 'planner', 'input': {'prompt': 'latest correction'}}]}))
    assert run['status'] == 'succeeded', run
    assert any(e['kind'] == 'context.compacted' for e in hub.store.events(run['id']))
