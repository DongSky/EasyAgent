import asyncio
import base64
import io
import json

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image

from conftest import live_server
from easyagent.contracts import ModelResult, ToolSpec
from easyagent.http_tools import HTTPTool


def image_response():
    file = io.BytesIO()
    Image.new('RGB', (8, 8), 'green').save(file, format='PNG')
    return {'data': [{'b64_json': base64.b64encode(file.getvalue()).decode()}]}


def install_image(hub, endpoint):
    return hub.development.save_api(HTTPTool(name='media.test.edits', description='Local image test',
        method='POST', url=endpoint+'/images/edits', timeout_seconds=None, response_mode='media',
        max_response_bytes=10_000_000))


async def test_automatic_python_and_child_operations_inherit_user_choice(hub, tmp_path):
    await hub.execution.configure({'terminal_enabled': True, 'workspace': str(tmp_path)})
    flow = {'name': 'Python and child', 'steps': [
        {'id': 'python', 'target': 'backend.terminal', 'requires_approval': True, 'input': {
            'operation': 'execute', 'payload': {'python': "from pathlib import Path; Path('answer.txt').write_text('42'); print(42)"}}},
        {'id': 'child', 'kind': 'subworkflow', 'depends_on': ['python'], 'body': {'name': 'read result', 'steps': [
            {'id': 'read', 'target': 'backend.terminal', 'input': {'operation': 'execute', 'payload': {
                'python': "from pathlib import Path; print(Path('answer.txt').read_text())"}}}]}}]}
    run = await hub.wait(hub.submit(flow, execution='automatic'))
    assert run['status'] == 'succeeded', run
    assert run['execution'] == 'automatic' and not run['approvals']
    assert (tmp_path/'answer.txt').read_text() == '42'
    child = hub.store.run(run['children'][0]['id'])
    assert child['execution'] == 'automatic' and child['status'] == 'succeeded'
    assert not any(e['kind'] == 'tool.approval_required' for e in hub.store.events(run['id']))


async def test_workflow_metadata_cannot_grant_automatic_execution(hub):
    async def write(args, ctx):
        return {'ok': True}
    hub.tools.register(ToolSpec(name='test.write', effect='write', idempotent=False), write)
    run = await hub.wait(hub.submit({'name': 'untrusted metadata', 'metadata': {'execution': 'automatic'},
                                   'steps': [{'id': 'write', 'target': 'test.write'}]}))
    assert run['status'] == 'waiting_approval' and run['execution'] == 'confirm'


async def test_automatic_image_retries_service_failure_without_manual_receipt(hub, monkeypatch):
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda *args: .01)
    remote, calls = FastAPI(), []
    @remote.post('/images/edits')
    async def edit():
        calls.append(True)
        return JSONResponse({'error': {'message': 'temporary failure'}}, status_code=503) if len(calls) < 3 else image_response()
    async with live_server(remote) as endpoint:
        install_image(hub, endpoint)
        run = await hub.wait(hub.submit({'name': 'regenerate', 'steps': [
            {'id': 'edit', 'target': 'media.test.edits', 'max_attempts': 1, 'requires_approval': True}]}, execution='automatic'))
        assert run['status'] == 'succeeded' and len(calls) == 3, run
        assert run['steps'][0]['output']['artifacts'][0]['media_type'] == 'image/png'
        assert not run['approvals']
        assert len([e for e in hub.store.events(run['id']) if e['kind'] == 'media.regeneration_needed']) == 2


async def test_media_response_processing_reuses_saved_bytes_without_second_post(hub, monkeypatch):
    from easyagent import media
    original = media.media_result
    parsed, posted = [], []
    def flaky(*args, **kwargs):
        parsed.append(True)
        if len(parsed) == 1:
            raise RuntimeError('local file processing interrupted')
        return original(*args, **kwargs)
    monkeypatch.setattr(media, 'media_result', flaky)
    monkeypatch.setattr('easyagent.runtime.retry_delay', lambda *args: .01)
    remote = FastAPI()
    @remote.post('/images/edits')
    async def edit():
        posted.append(True)
        return image_response()
    async with live_server(remote) as endpoint:
        install_image(hub, endpoint)
        run = await hub.wait(hub.submit({'name': 'recover files', 'steps': [
            {'id': 'edit', 'target': 'media.test.edits'}]}, execution='automatic'))
        assert run['status'] == 'succeeded', run
        assert len(posted) == 1 and len(parsed) == 2


async def test_cancelled_uncertain_image_continues_in_place_only_on_user_request(api):
    url, hub = api
    ready, calls = False, []
    remote = FastAPI()
    @remote.post('/images/edits')
    async def edit():
        calls.append(True)
        return image_response() if ready else JSONResponse({}, status_code=503)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        install_image(hub, endpoint)
        waiting = await hub.wait(hub.submit({'name': 'old image failure', 'steps': [
            {'id': 'prepare', 'target': 'core.echo', 'input': {'original': 'preserve'}},
            {'id': 'edit', 'target': 'media.test.edits', 'depends_on': ['prepare']},
            {'id': 'deliver', 'target': 'core.echo', 'depends_on': ['edit'], 'input': {'image': {'$ref': 'edit.artifacts'}}}]}))
        hub.tools.approve(hub.store, waiting['approvals'][0]['id'], True)
        failed = await hub.wait(waiting['id'])
        assert failed['status'] == 'needs_attention'
        hub.store.cancel(failed['id'])
        cancelled = hub.store.run(failed['id'])
        await asyncio.sleep(.05)
        assert len(calls) == 1  # Stopping is never implicitly revoked.
        ready = True
        body = {'expected_updated': cancelled['updated']}
        response = await client.post('/v1/runs/'+failed['id']+'/continue-automatically', json=body)
        assert response.status_code == 200, response.text
        assert (await client.post('/v1/runs/'+failed['id']+'/continue-automatically', json=body)).status_code == 409
        completed = await hub.wait(failed['id'])
        assert completed['status'] == 'succeeded', completed
        assert completed['steps'][0]['attempts'] == 1
        assert len(calls) == 2 and completed['steps'][-1]['output']['image']


async def test_generic_uncertain_write_is_never_silently_resubmitted(api):
    url, hub = api
    calls = []
    async def write(args, ctx):
        calls.append(True)
        raise RuntimeError('reply lost after external write')
    hub.tools.register(ToolSpec(name='test.write', effect='write', idempotent=False), write)
    run = await hub.wait(hub.submit({'name': 'external write', 'steps': [{'id': 'write', 'target': 'test.write'}]}, execution='automatic'))
    assert run['status'] == 'needs_attention'
    async with httpx.AsyncClient(base_url=url) as client:
        r = await client.post('/v1/runs/'+run['id']+'/continue-automatically', json={'expected_updated': run['updated']})
        assert r.status_code == 409
    assert len(calls) == 1 and hub.store.run(run['id'])['status'] == 'needs_attention'


async def test_chat_defaults_to_automatic_python_execution_in_browser(api, tmp_path):
    from playwright.async_api import async_playwright, expect
    url, hub = api
    await hub.execution.configure({'terminal_enabled': True, 'workspace': str(tmp_path)})
    class Builder:
        calls = 0
        async def generate(self, request, model):
            assert request.response_schema['title'] == 'BuildDraft'
            self.calls += 1
            script = "from pathlib import Path; Path('result.txt').write_text('42'); print(42)"
            if self.calls == 1:
                script = "raise ValueError('fixture: incorrect Python implementation')"
            else:
                assert 'incorrect Python implementation' in request.messages[-1]['content']
            return ModelResult(data={'explanation': '用 Python 写入答案并读取结果', 'workflow': {
                'name': 'Python 任务', 'steps': [{'id': 'python', 'target': 'backend.terminal',
                    'requires_approval': True, 'input': {'operation': 'execute', 'payload': {
                        'python': script}}}]}})
    builder = Builder()
    hub.models.register('planner', builder, 'fixture', ['decision'])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.goto(url+'/#conversations')
            await expect(page.locator('#conversations [data-execution]')).to_have_value('automatic')
            await page.locator('[data-destination]').select_option('create')
            await page.locator('#conversations [data-text]').fill('使用 Python 生成答案文件')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('.chat-task-heading')).to_contain_text('处理完成', timeout=10000)
            assert (tmp_path/'result.txt').read_text() == '42'
            assert builder.calls == 2
            with hub.store.connect() as db:
                state = json.loads(db.execute('SELECT state FROM conversation_jobs').fetchone()[0])
            run = hub.store.run(state['run_id'])
            assert run['execution'] == 'automatic' and not run['approvals']
        finally:
            await browser.close()
