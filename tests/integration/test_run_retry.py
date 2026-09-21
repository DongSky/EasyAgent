import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.contracts import ToolSpec
from easyagent.models import HTTPProvider, ProviderError
from easyagent.retry_policy import error_info, retry_after, retry_delay
from easyagent.run_retry import RetryRequest, retry_run
from easyagent.store import Conflict, Store


@pytest.mark.parametrize('streaming', [False, True])
async def test_default_model_wait_has_no_read_deadline_and_can_be_cancelled(hub, monkeypatch, streaming):
    from fastapi.responses import StreamingResponse
    from easyagent.contracts import Step
    from easyagent.retry_policy import MODEL_WAIT, model_timeout
    remote = FastAPI()
    entered, release, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    # Keep cancellation responsive without expiring a valid lease during slow disk I/O.
    hub.lease_seconds = 3
    seen = []
    client_type = httpx.AsyncClient

    class InspectClient(client_type):
        async def send(self, request, **kwargs):
            if request.method == 'POST':
                seen.append(request.extensions['timeout'])
            return await super().send(request, **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', InspectClient)

    @remote.post('/chat/completions')
    async def reply():
        async def chunks():
            entered.set()
            try:
                await release.wait()
                if streaming:
                    yield 'data: '+json.dumps({'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]})+'\n\n'
                    yield 'data: [DONE]\n\n'
                else:
                    yield json.dumps({'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}]})
            finally:
                disconnected.set()
        return StreamingResponse(chunks(), media_type='text/event-stream' if streaming else 'application/json')

    assert Step(id='model', kind='model').timeout_seconds is None
    assert Step(id='model', kind='model', timeout_seconds=15).timeout_seconds == 15
    token = MODEL_WAIT.set((3, {'model_timeout': 300}))
    try:
        assert model_timeout(None) is None  # Legacy retries must not reinstate a deadline.
    finally:
        MODEL_WAIT.reset(token)
    async with live_server(remote) as endpoint:
        hub.models.register('planner', HTTPProvider(endpoint), 'fixture', ['chat'])
        run_id = hub.submit({'name': 'unlimited cancellable response', 'steps': [
            {'id': 'wait', 'kind': 'model', 'target': 'planner', 'input': {'prompt': 'test', 'parameters': {'stream': streaming}}}]})
        try:
            await asyncio.wait_for(entered.wait(), 10)
            await asyncio.sleep(.15)
            assert hub.store.run(run_id)['status'] == 'running'
            assert seen[-1]['read'] is None and seen[-1]['write'] is None
            assert seen[-1]['connect'] == 20
            hub.store.cancel(run_id)
            assert (await hub.wait(run_id))['status'] == 'cancelled'
            await asyncio.wait_for(disconnected.wait(), 10)
        finally:
            release.set()


@pytest.mark.parametrize('before_send', [False, True])
async def test_continuous_wait_retry_removes_explicit_read_and_step_timeouts(hub, before_send):
    remote = FastAPI()
    calls, waits = [], []
    release_discovery = asyncio.Event()

    class Provider(HTTPProvider):
        async def _post(self, path, payload, wait, parser):
            waits.append(wait)
            return await super()._post(path, payload, wait, parser)

    @remote.post('/chat/completions')
    async def reply():
        calls.append(True)
        await asyncio.sleep(.1)
        return {'choices': [{'message': {'content': 'completed'}}]}

    async with live_server(remote) as endpoint:
        provider = Provider(endpoint, timeout=.02)
        if before_send:
            discover = provider.limits.discover

            async def delayed_discovery(model):
                await release_discovery.wait()
                return await discover(model)

            provider.limits.discover = delayed_discovery
        hub.models.register('planner', provider, 'fixture', ['chat'])
        failed = await hub.wait(hub.submit({'name': 'explicit timeout', 'steps': [
            {'id': 'wait', 'kind': 'model', 'target': 'planner', 'max_attempts': 1,
             'timeout_seconds': .08 if before_send else 5,
             'input': {'prompt': 'test'}}]}))
        assert failed['status'] == 'failed' and failed['retry']['longer_wait']
        if before_send:
            assert not calls
        release_discovery.set()
        await retry_run(hub, failed['id'], RetryRequest(expected_updated=failed['updated'], longer_wait=True))
        result = await hub.wait(failed['id'])
        assert result['status'] == 'succeeded', result
        assert result['steps'][0]['output']['text'] == 'completed'
        # A short step deadline can expire during discovery/client setup, before
        # the first HTTP request reaches the server. Verify the durable attempts
        # and effective deadlines instead of assuming two remote requests.
        assert result['steps'][0]['attempts'] == 2
        assert result['steps'][0]['retry_state']['step_timeout'] is None
        assert waits[-1] is None and calls


async def test_http_model_timeout_adapts_and_diagnostics_are_safe(hub, monkeypatch):
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)
    app = FastAPI()
    calls = []

    @app.post('/chat/completions')
    async def reply(request: Request):
        calls.append(await request.json())
        if len(calls) == 1:
            await asyncio.sleep(.15)
        return {'choices': [{'message': {'content': 'completed'}}]}

    waits = []

    class Provider(HTTPProvider):
        async def _post(self, path, payload, wait, parser):
            waits.append(wait)
            return await super()._post(path, payload, wait, parser)

    async with live_server(app) as origin:
        hub.models.register('planner', Provider(origin, timeout=.08), 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'network retry', 'steps': [
            {'id': 'plan', 'kind': 'model', 'target': 'planner', 'input': {'prompt': 'test'}}]}))
    assert run['status'] == 'succeeded', run
    assert waits == [.08, .12]
    events = hub.store.events(run['id'])
    failure = next(e for e in events if e['kind'] == 'model.failed')['payload']['detail']
    assert failure['category'] == 'read_timeout' and failure['model'] == 'planner'
    assert failure['timeout_seconds'] == .08
    assert '响应超时' in failure['message']
    retry = next(e for e in events if e['kind'] == 'step.retrying')['payload']
    assert retry['retry']['attempt'] == 1 and retry['next_retry_at']


def test_backoff_respects_rate_limit_and_classifies_auth():
    error = ProviderError(429, retry_after=17)
    info = error_info(error)
    assert info['retryable'] and retry_delay(info, 1) >= 17
    assert retry_after('99999') == 120 and retry_after('nonsense') is None
    assert 4 <= retry_delay(error_info(httpx.ReadTimeout('private URL')), 2) <= 4.8
    req = httpx.Request('GET', 'https://example.org/private')
    error = httpx.HTTPStatusError('private details', request=req, response=httpx.Response(401, request=req))
    info = error_info(error)
    assert info['category'] == 'configuration' and not info['retryable']
    assert 'private' not in json.dumps(info)


@pytest.mark.parametrize('reason,retryable,category', [
    ('max_output_tokens', False, 'output_limit'), ('server_error', True, 'response_incomplete'),
    ('private provider detail', False, 'response_incomplete')])
async def test_incomplete_model_response_has_safe_actionable_reason(hub, monkeypatch, reason, retryable, category):
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)

    class Provider(HTTPProvider):
        async def post(self, path, payload):
            return {'status': 'incomplete', 'incomplete_details': {'reason': reason}}

    hub.models.register('planner', Provider('https://example.org', dialect='responses'), 'fixture', ['chat'])
    run = await hub.wait(hub.submit({'name': 'incomplete', 'steps': [
        {'id': 'model', 'kind': 'model', 'target': 'planner', 'max_attempts': 2, 'input': {'prompt': 'test'}}]}))
    assert run['status'] == 'failed'
    step = run['steps'][0]
    assert step['attempts'] == (2 if retryable else 1)
    assert step['retry_state']['error']['category'] == category
    assert 'private provider detail' not in json.dumps(step['retry_state'])


async def test_permanent_auth_error_does_not_auto_retry(hub):
    count = 0

    async def reject(args, ctx):
        nonlocal count
        count += 1
        raise ProviderError(401)

    hub.tools.register(ToolSpec(name='test.auth'), reject)
    run = await hub.wait(hub.submit({'name': 'auth', 'steps': [{'id': 'call', 'target': 'test.auth'}]}))
    assert run['status'] == 'failed' and count == 1
    assert not run['steps'][0]['retry_state']['error']['retryable']


async def test_manual_retry_keeps_receipts_checkpoint_budget_and_skipped_condition(api):
    url, hub = api
    writes, checks = [], []
    ready = False

    async def write(args, ctx):
        writes.append(ctx.invocation_id)
        return {'receipt': ctx.invocation_id}

    async def work(args, ctx):
        if not ctx.job['state'].get('prepared'):
            checks.append('prepared')
            hub.store.checkpoint(ctx.job, {'prepared': True})
        if not ready:
            raise httpx.ReadTimeout('do not expose https://example.org/private')
        return {'result': 'verified'}

    hub.tools.register(ToolSpec(name='test.write', effect='write', idempotent=False), write)
    hub.tools.register(ToolSpec(name='test.check'), work)
    flow = {'name': 'manual resume', 'steps': [
        {'id': 'write', 'target': 'test.write'},
        {'id': 'verify', 'target': 'test.check', 'depends_on': ['write'], 'max_attempts': 1},
        {'id': 'next', 'target': 'core.echo', 'depends_on': ['verify']},
        {'id': 'conditional', 'target': 'core.echo', 'when': {'source': '$input.flag', 'equals': True}},
    ], 'inputs': {'flag': False}}
    run = await hub.wait(hub.submit(flow))
    hub.tools.approve(hub.store, run['approvals'][0]['id'], True)
    run = await hub.wait(run['id'])
    assert run['status'] == 'failed' and run['retry']['allowed']
    assert 'private' not in next(s for s in run['steps'] if s['id'] == 'verify')['error']
    usage = run['usage']['tool_calls']
    ready = True
    body = {'expected_updated': run['updated'], 'longer_wait': True}
    async with httpx.AsyncClient(base_url=url) as client:
        response = await client.post(f"/v1/runs/{run['id']}/retry", json=body)
        assert response.status_code == 200, response.text
        duplicate = await client.post(f"/v1/runs/{run['id']}/retry", json=body)
        assert duplicate.status_code == 409
    completed = await hub.wait(run['id'])
    assert completed['status'] == 'succeeded'
    assert len(writes) == 1 and checks == ['prepared']
    assert completed['usage']['tool_calls'] >= usage
    failed_step = next(s for s in completed['steps'] if s['id'] == 'verify')
    assert failed_step['attempts'] == 2 and failed_step['retry_state']['base_attempts'] == 1
    assert failed_step['retry_state']['model_timeout'] is None
    assert next(s for s in completed['steps'] if s['id'] == 'conditional')['status'] == 'skipped'
    reopened = Store(hub.store.path).run(run['id'])
    assert reopened['steps'] == completed['steps']


async def test_retry_unsafe_writes_and_expired_budget_are_refused(hub):
    async def unknown(args, ctx):
        raise httpx.ReadTimeout('write may already have happened')

    hub.tools.register(ToolSpec(name='test.unknown', effect='write', idempotent=False), unknown)
    run = await hub.wait(hub.submit({'name': 'unknown write', 'steps': [{'id': 'write', 'target': 'test.unknown'}]}))
    hub.tools.approve(hub.store, run['approvals'][0]['id'], True)
    run = await hub.wait(run['id'])
    assert run['status'] == 'needs_attention'
    with pytest.raises(Conflict):
        await retry_run(hub, run['id'], RetryRequest(expected_updated=run['updated']))
    hub.tools.register(ToolSpec(name='test.read_timeout'), unknown)
    failed = await hub.wait(hub.submit({'name': 'expired', 'limits': {'wall_time_seconds': 604800}, 'steps': [{'id': 'a', 'target': 'test.read_timeout', 'max_attempts': 1}]}))
    with hub.store.connect() as db:
        db.execute('UPDATE runs SET created=? WHERE id=?', (time.time()-700000, failed['id']))
    with pytest.raises(Conflict, match='时间预算'):
        await retry_run(hub, failed['id'], RetryRequest(expected_updated=failed['updated']))


async def test_retry_failed_child_tree_without_repeating_successful_sibling(hub):
    ready, calls = False, []

    async def work(args, ctx):
        calls.append(args['value'])
        if args['value'] == 'fail' and not ready:
            raise ValueError('fixture failure')
        return args

    hub.tools.register(ToolSpec(name='test.child'), work)
    run = await hub.wait(hub.submit({'name': 'branching task', 'steps': [
        {'id': 'batch', 'kind': 'foreach', 'input': {'items': ['ok', 'fail']}, 'body': {
            'name': 'child', 'steps': [{'id': 'work', 'target': 'test.child', 'max_attempts': 1,
                                      'input': {'value': {'$ref': '$input.item'}}}]}},
    ]}))
    assert run['status'] == 'failed' and run['retry']['allowed'], run
    ready = True
    await retry_run(hub, run['id'], RetryRequest(expected_updated=run['updated']))
    run = await hub.wait(run['id'])
    assert run['status'] == 'succeeded', run
    assert calls.count('ok') == 1 and calls.count('fail') == 2


async def test_chat_retry_restores_same_turn_and_preserves_failure_history(api):
    from test_workspace_chat import provider_app, settled
    url, hub = api
    ready = False

    async def work(args, ctx):
        if not ready:
            raise httpx.ReadTimeout('timeout')
        return {'text': '完成'}

    hub.tools.register(ToolSpec(name='test.chat_retry'), work)
    hub.development.save_workflow('resume', {'name': '恢复任务', 'steps': [
        {'id': 'work', 'target': 'test.chat_retry', 'max_attempts': 1}]})

    async def route(context):
        return {'action': 'use', 'candidate': 'resume@1', 'confidence': 1, 'inputs': {}, 'message': '复用'}

    remote, _ = provider_app(route)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('router', HTTPProvider(endpoint), 'fixture', ['decision'])
        c = await hub.conversations.create({'workspace': True, 'model': 'router'})
        await hub.conversations.send(c['id'], {'text': '处理', 'intent': 'workflow', 'workflow': 'resume@1'})
        c = await settled(hub, c['id'])
        turn = c['turns'][-1]
        assert turn['task']['failed_phase'] == 'executing'
        run = hub.store.run(turn['run_id'])
        ready = True
        response = await client.post('/v1/runs/'+run['id']+'/retry', json={'expected_updated': run['updated']})
        assert response.status_code == 200, response.text
        c = await settled(hub, c['id'])
    assert len(c['turns']) == 1 and c['turns'][0]['id'] == turn['id']
    assert c['turns'][0]['status'] == 'succeeded'
    assert c['turns'][0]['task']['run_id'] == run['id']
    assert len([m for m in c['messages'] if m['role'] == 'assistant']) >= 3


async def test_browser_retry_button_resumes_same_chat_and_refreshes_cached_failure(api):
    from playwright.async_api import async_playwright, expect
    from test_workspace_chat import provider_app, settled
    url, hub = api
    ready = False
    entered, release = asyncio.Event(), asyncio.Event()

    async def work(args, ctx):
        if not ready:
            raise httpx.ReadTimeout('timeout')
        assert ctx.job['retry_state']['model_timeout'] is None
        entered.set()
        await release.wait()
        return {'text': '重试成功，沿用原任务。'}

    hub.tools.register(ToolSpec(name='test.browser_retry'), work)
    hub.development.save_workflow('retry-ui', {'name': '重试界面验收', 'steps': [
        {'id': 'work', 'target': 'test.browser_retry', 'max_attempts': 1}]})

    async def route(context):
        return {'action': 'use', 'candidate': 'retry-ui@1', 'confidence': 1, 'inputs': {}, 'message': '复用'}

    remote, _ = provider_app(route)
    async with live_server(remote) as endpoint, async_playwright() as playwright:
        hub.models.register('router', HTTPProvider(endpoint), 'fixture', ['decision'])
        c = await hub.conversations.create({'workspace': True, 'model': 'router'})
        await hub.conversations.send(c['id'], {'text': '检查重试界面', 'intent': 'workflow', 'workflow': 'retry-ui@1'})
        c = await settled(hub, c['id'])
        turn = c['turns'][-1]
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.add_init_script('localStorage.setItem("easyagent.workspaceConversation", '+json.dumps(c['id'])+')')
            await page.goto(url+'/#conversations')
            card = page.locator('[data-turn="'+turn['id']+'"]')
            await expect(card.get_by_role('button', name='从失败处重试', exact=True)).to_be_visible()
            await card.get_by_role('button', name='展开步骤与结果').click()
            await expect(card.get_by_text('远程服务响应超时（等待返回数据超过时限）', exact=True)).to_be_visible()
            ready = True
            await card.locator('[data-retry]').get_by_role('button', name='持续等待重试').click()
            await asyncio.wait_for(entered.wait(), 5)
            await expect(card.locator('[data-retry-run]')).to_have_count(0)
            release.set()
            await expect(card.locator('[data-reply]')).to_have_text('重试成功，沿用原任务。', timeout=10000)
            assert not errors, errors
            assert sum(e['kind'] == 'run.retried' for e in hub.store.events(turn['run_id'])) == 1
        finally:
            release.set()
            await browser.close()


async def test_builder_retry_ui_keeps_existing_build(api, monkeypatch):
    from playwright.async_api import async_playwright, expect
    from easyagent.contracts import ModelResult
    url, hub = api
    ready = False
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)

    class Planner:
        async def generate(self, request, model):
            if not ready:
                raise httpx.ReadTimeout('fixture slow model')
            return ModelResult(data={'workflow': {'name': 'test', 'steps': [
                {'id': 'copy', 'target': 'core.echo', 'input': {'text': {'$ref': '$input.message'}}}]},
                'explanation': '保存输入', 'questions': []})

    hub.models.register('planner', Planner(), 'fixture', ['decision'])
    async with httpx.AsyncClient(base_url=url) as client:
        assistant = (await client.post('/v1/studio/assistants', json={
            'name': '重试构建', 'purpose': '保存输入', 'model': 'planner', 'construction': 'automatic'})).json()
        build = (await client.post('/v1/studio/assistants/'+assistant['id']+'/build')).json()
    failed = await hub.wait(build['id'])
    assert failed['status'] == 'failed'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(url+'/#home')
            await page.locator('[data-load="'+assistant['id']+'"]').click()
            await expect(page.locator('#assistantPlan [data-retry-run="longer"]')).to_be_visible()
            ready = True
            await page.locator('#assistantPlan [data-retry-run="longer"]').click()
            await expect(page.locator('#openPlanCanvas')).to_be_visible(timeout=10000)
            assert hub.store.run(build['id'])['status'] == 'succeeded'
        finally:
            await browser.close()


async def test_builder_manual_retry_reuses_research_and_completed_plan(api, monkeypatch):
    from easyagent.contracts import ModelResult
    url, hub = api
    ready, research_calls, plan_calls = False, [], []
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)

    async def research(hub, ctx, queries):
        research_calls.append(queries)
        return [{'source': 'fixture', 'text': 'persisted research'}]

    monkeypatch.setattr('easyagent.capability_research.research', research)

    class Planner:
        async def generate(self, request, model):
            content = json.loads(request.messages[-1]['content'])
            if 'research_evidence' not in content:
                plan_calls.append('plan')
                return ModelResult(data={'workflow': None, 'explanation': 'research', 'questions': [],
                                         'research_queries': ['public protocol documentation']})
            assert content['research_evidence'][0]['text'] == 'persisted research'
            if not ready:
                raise httpx.ReadTimeout('fixture read timeout')
            return ModelResult(data={'workflow': {'name': 'verified', 'steps': [
                {'id': 'copy', 'target': 'core.echo', 'input': {'text': {'$ref': '$input.message'}}}]},
                'explanation': '复用搜索结果', 'questions': []})

    hub.models.register('planner', Planner(), 'fixture', ['decision'])
    async with httpx.AsyncClient(base_url=url) as client:
        assistant = (await client.post('/v1/studio/assistants', json={
            'name': '恢复研究', 'purpose': '核验资料', 'model': 'planner', 'construction': 'automatic'})).json()
        build = (await client.post('/v1/studio/assistants/'+assistant['id']+'/build')).json()
        failed = await hub.wait(build['id'])
        assert failed['status'] == 'failed'
        assert failed['steps'][0]['status'] == 'succeeded'
        assert failed['steps'][1]['state']['researched']
        ready = True
        response = await client.post('/v1/runs/'+build['id']+'/retry', json={'expected_updated': failed['updated'], 'longer_wait': True})
        assert response.status_code == 200, response.text
        completed = await hub.wait(build['id'])
        assert completed['status'] == 'succeeded'
        assert len(research_calls) == 1 and plan_calls == ['plan']
        assert completed['steps'][0]['attempts'] == 1
        plan = (await client.get('/v1/studio/assistants/'+assistant['id']+'/workflow')).json()
        assert plan['status'] == 'ready', plan


async def test_retried_build_graph_animates_without_replacing_nodes(api, monkeypatch):
    from playwright.async_api import async_playwright, expect
    from easyagent.contracts import ModelResult
    from test_workspace_chat import settled
    url, hub = api
    ready = False
    release, request_seen, allow_request = asyncio.Event(), asyncio.Event(), asyncio.Event()
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda info, attempt: .01)

    class Planner:
        async def generate(self, request, model):
            if not ready:
                raise httpx.ReadTimeout('fixture build timeout')
            await release.wait()
            return ModelResult(data={'workflow': None, 'questions': ['还需要任务材料'], 'explanation': '请补充材料'})

    hub.models.register('planner', Planner(), 'fixture', ['decision'])
    c = await hub.conversations.create({'workspace': True, 'model': 'planner'})
    await hub.conversations.send(c['id'], {'text': '创建测试流程', 'intent': 'create'})
    c = await settled(hub, c['id'])
    turn = c['turns'][-1]
    # Both engines report the single `working` phase; `mode` records which one was running.
    assert turn['task']['failed_phase'] == 'working' and turn['task']['mode'] == 'building'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(reduced_motion='no-preference')
            await page.add_init_script('localStorage.setItem("easyagent.workspaceConversation", '+json.dumps(c['id'])+')')
            await page.goto(url+'/#conversations')
            card = page.locator('[data-turn="'+turn['id']+'"]')
            node = card.locator('[data-node="compile"]')
            await expect(node).to_have_attribute('data-status', 'failed')
            await node.evaluate('(el)=>window.retryNode=el')

            async def delay_retry(route):
                request_seen.set()
                await allow_request.wait()
                await route.continue_()

            await page.route('**/v1/runs/*/retry', delay_retry)
            ready = True
            await card.locator('[data-retry]').get_by_role('button', name='持续等待重试').click()
            await asyncio.wait_for(request_seen.wait(), 5)
            await expect(card.locator('[aria-busy="true"]')).to_have_text('正在提交重试…')
            await expect(card.locator('.retry-request-spinner')).to_have_css('animation-name', 'chatSpin')
            allow_request.set()
            await expect(node).to_have_attribute('data-status', 'running')
            await expect(node.locator('.chat-node-indicator')).to_have_css('animation-name', 'chatSpin')
            await expect(card.locator('.chat-task-mark')).to_have_class('chat-task-mark is-working')
            await expect(card.locator('.state-failed')).to_have_count(0)
            await page.wait_for_function('window.retryNode.getAnimations({subtree:true}).some(a=>a.currentTime>1000)')
            assert await node.evaluate('(el)=>el===window.retryNode')
            await page.emulate_media(reduced_motion='reduce')
            await expect(node.locator('.chat-node-indicator')).to_have_css('animation-name', 'none')
            await page.emulate_media(reduced_motion='no-preference')
            await page.reload()
            await expect(node).to_have_attribute('data-status', 'running')
            await expect(node.locator('.chat-node-indicator')).to_have_css('animation-name', 'chatSpin')
            release.set()
            await expect(node).to_have_attribute('data-status', 'succeeded', timeout=10000)
            await expect(card.locator('.chat-task-mark.is-working')).to_have_count(0)
        finally:
            allow_request.set()
            release.set()
            await browser.close()


@pytest.mark.parametrize('legacy_status', [False, True])
async def test_legacy_invalid_build_can_retry_from_chat_and_execute(api, legacy_status):
    from playwright.async_api import async_playwright, expect
    from easyagent.contracts import ModelResult
    from easyagent.store import encode
    from test_workspace_chat import settled
    url, hub = api
    release = asyncio.Event()
    calls = []
    draft = {'workflow': {'name': 'validated', 'steps': [
        {'id': 'work', 'target': 'test.schema', 'input': {'value': 12}}]}, 'explanation': '保存文字输入'}

    class Planner:
        async def generate(self, request, model):
            calls.append(request.response_schema['title'])
            if calls[-1] == 'BuildDraft':
                return ModelResult(data=draft)
            assert calls[-1] == 'BuildEdits'
            await release.wait()
            return ModelResult(data={'edits': [{'op': 'set', 'path': ['workflow', 'steps', 0, 'input', 'value'],
                                              'value': 'correct'}], 'done': True})

    async def echo(args, ctx):
        return args

    async def old_verify(args, ctx):
        hub.store.checkpoint(ctx.job, {'draft': args['draft'], 'attempt': 0, 'development': []})
        return {'draft': args['draft'], 'errors': ['old validation failure']}

    hub.tools.register(ToolSpec(name='test.schema', input_schema={
        'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}), echo)
    spec, current_verify = hub.tools.entries['development.verify_build']
    hub.tools.entries['development.verify_build'] = (spec, old_verify)
    hub.models.register('planner', Planner(), 'fixture', ['decision'])
    c = await hub.conversations.create({'workspace': True, 'model': 'planner'})
    await hub.conversations.send(c['id'], {'text': '创建保存输入的流程', 'intent': 'create'})
    c = await settled(hub, c['id'])
    turn = c['turns'][-1]
    assert turn['status'] == 'failed' and turn['task']['phase'] == 'failed'
    if legacy_status:
        # Older builds incorrectly reported validation failures as a successful
        # clarification. Those persisted conversations must remain retryable.
        with hub.store.transaction() as db:
            state = json.loads(db.execute('SELECT state FROM conversation_jobs WHERE turn_id=?',
                                          (turn['id'],)).fetchone()[0])
            state['phase'] = 'clarification'
            db.execute('UPDATE conversation_jobs SET state=? WHERE turn_id=?', (encode(state), turn['id']))
            db.execute("UPDATE conversation_turns SET status='succeeded' WHERE id=?", (turn['id'],))
    run_id = turn['run_id']
    hub.tools.entries['development.verify_build'] = (spec, current_verify)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(reduced_motion='no-preference')
            await page.add_init_script('localStorage.setItem("easyagent.workspaceConversation", '+encode(c['id'])+')')
            await page.goto(url+'/#conversations')
            card = page.locator('[data-turn="'+turn['id']+'"]')
            await card.get_by_role('button', name='从失败处重试').click()
            node = card.locator('[data-node="verify"]')
            await expect(node).to_have_attribute('data-status', 'running')
            await expect(node.locator('.chat-node-indicator')).to_have_css('animation-name', 'chatSpin')
            release.set()
            c = await settled(hub, c['id'])
            assert c['turns'][-1]['task']['phase'] == 'completed', c['turns'][-1]
            assert hub.store.run(run_id)['steps'][0]['attempts'] == 1
            executed = hub.store.run(c['turns'][-1]['run_id'])
            assert executed['steps'][0]['output'] == {'value': 'correct'}
            assert calls == ['BuildDraft', 'BuildEdits']
        finally:
            release.set()
            await browser.close()
