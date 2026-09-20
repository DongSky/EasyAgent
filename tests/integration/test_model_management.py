"""Model management through real HTTP and browser controls, using local providers only."""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright, expect

from conftest import live_server
from easyagent.assistant_builder import select_model
from easyagent.build_capabilities import prepare_connected_media
from easyagent.runtime import Hub


def model_server():
    remote, calls = FastAPI(), []

    @remote.get('/models')
    async def models(request: Request):
        calls.append(('list', request.headers.get('authorization')))
        return {'data': [{'id': 'alpha'}, {'id': 'beta'}]}

    @remote.post('/chat/completions')
    async def completion(request: Request):
        data = await request.json()
        calls.append((data['model'], request.headers.get('authorization')))
        if data['model'] == 'broken':
            return JSONResponse({'error': 'private upstream details'}, status_code=503)
        title = data.get('response_format', {}).get('json_schema', {}).get('schema', {}).get('title')
        content = {'action': 'reply', 'message': '回答来自 ' + data['model']} if title == 'DispatchDecision' else None
        if title == 'BuildDraft':
            content = {'explanation': '保存输入', 'questions': [], 'workflow': {
                'name': '保存输入', 'inputs': {'message': ''},
                'steps': [{'id': 'save', 'kind': 'artifact', 'input': {'name': 'result.txt', 'content': {'$ref': '$input.message'}}}]}}
        return {'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False) if content else 'connected'}}]}

    return remote, calls


async def test_edit_discover_default_delete_and_restart(api):
    url, hub = api
    remote, calls = model_server()
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        body = {'alias': 'first', 'base_url': endpoint, 'model': 'alpha', 'api_key': 'fixture-value'}
        assert (await client.post('/v1/studio/connections', json=body)).status_code == 200
        assert (await client.post('/v1/studio/connections', json={**body, 'alias': 'second', 'model': 'beta'})).status_code == 200
        assert select_model(hub, 'auto') == 'second'
        assert (await client.put('/v1/studio/model-default', json={'alias': 'first'})).status_code == 200
        assert select_model(hub, 'auto') == 'first'
        assert select_model(hub, 'second') == 'second'
        catalog = await client.get('/v1/studio/connections')
        assert 'fixture-value' not in catalog.text and 'api_key' not in catalog.text
        assert catalog.json()['default_model'] == 'first'
        edited = {**body, 'api_key': '', 'model': 'beta'}
        assert (await client.put('/v1/studio/connections/first?test=true', json=edited)).status_code == 200
        assert calls[-1] == ('beta', 'Bearer fixture-value')
        failed = await client.put('/v1/studio/connections/first?test=true', json={**edited, 'model': 'broken', 'api_key': 'replacement-value'})
        assert failed.status_code == 502 and 'private upstream' not in failed.text
        assert hub.models.bindings['first'].model == 'beta'
        assert hub.connections.secret('model.first') == 'fixture-value'
        discovered = await client.post('/v1/studio/connections/discover?existing_alias=first', json=edited)
        assert discovered.json() == {'models': ['alpha', 'beta']}
        assert calls[-1] == ('list', 'Bearer fixture-value')
        before = len(calls)
        changed_origin = {**edited, 'base_url': endpoint + '/other'}
        for path in ('/v1/studio/connections/first?test=true', '/v1/studio/connections/discover?existing_alias=first'):
            response = await (client.put(path, json=changed_origin) if 'test=true' in path else client.post(path, json=changed_origin))
            assert response.status_code == 422
        assert len(calls) == before
        restored = Hub(hub.store.path)
        assert restored.models.bindings['first'].model == 'beta'
        assert select_model(restored, 'auto') == 'first'
        assert (await client.delete('/v1/studio/connections/first')).status_code == 200
        assert (await client.delete('/v1/studio/connections/mock')).status_code == 404
        assert (await client.put('/v1/studio/model-default', json={'alias': 'mock'})).status_code == 422
        assert hub.connections.default_model() is None
        assert select_model(hub, 'auto') == 'second'
        restored = Hub(hub.store.path)
        assert 'first' not in restored.models.bindings
        with pytest.raises(ValueError):
            restored.connections.secret('model.first')


async def test_connection_in_use_cannot_be_changed_and_workspace_checks_capability(api):
    url, hub = api
    remote, _ = model_server()
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        body = {'alias': 'working', 'base_url': endpoint, 'model': 'alpha'}
        await client.post('/v1/studio/connections', json=body)
        run = hub.submit({'name': 'pending', 'steps': [
            {'id': 'wait', 'kind': 'input', 'input': {'prompt': 'continue?', 'schema': {'type': 'object'}}},
            {'id': 'model', 'kind': 'model', 'target': 'working', 'depends_on': ['wait'], 'input': {'prompt': 'hello'}}]})
        await hub.wait(run)
        assert (await client.delete('/v1/studio/connections/working')).status_code == 409
        assert (await client.put('/v1/studio/connections/working', json=body)).status_code == 409
        await client.post('/v1/studio/connections', json={**body, 'alias': 'pictures', 'capabilities': ['image']})
        for alias in ('pictures', 'missing', 'mock'):
            assert (await client.post('/v1/conversations', json={'workspace': True, 'model': alias})).status_code == 422
        c = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'working'})).json()
        assert (await client.patch('/v1/conversations/'+c['id'], json={'model': 'pictures'})).status_code == 422


async def test_deleted_media_adapters_revoke_old_versions_even_after_recreation(api):
    url, hub = api
    remote, _ = model_server()
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        body = {'alias': 'pictures', 'base_url': endpoint, 'model': 'gpt-image-2',
                'capabilities': ['image'], 'api_key': 'fixture-value'}
        await client.post('/v1/studio/connections', json=body)
        prepare_connected_media(hub)
        prefix = 'media.' + hashlib.sha256(b'pictures').hexdigest()[:12]
        name = prefix + '.edits'
        old = hub.tools.versions[name, 1][1]
        assert hub.connections.secret(prefix) == 'fixture-value'
        assert (await client.delete('/v1/studio/connections/pictures')).status_code == 200
        assert name in hub.development.archived_ids('api')
        with pytest.raises(ValueError):
            hub.connections.secret(prefix)
        with pytest.raises(ValueError, match='模型连接已修改或删除'):
            await old({}, None)
        await client.post('/v1/studio/connections', json=body)
        prepare_connected_media(hub)
        assert hub.tools.latest[name] == 2
        assert name not in hub.development.archived_ids('api')
        restored = Hub(hub.store.path)
        with pytest.raises(ValueError, match='模型连接已修改或删除'):
            await restored.tools.versions[name, 1][1]({}, None)


async def test_browser_manage_models_and_select_chat_and_builder(api):
    url, hub = api
    remote, calls = model_server()
    async with live_server(remote) as endpoint, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(url+'/#connections')
            await page.locator('[data-add-model]').click()
            form = page.locator('#connectionForm')
            await form.locator('[name=alias]').fill('first')
            await form.locator('[name=base_url]').fill(endpoint)
            await form.locator('[name=api_key]').fill('fixture-value')
            await page.locator('#discoverConnectionModels').click()
            await expect(page.locator('#connectionModels option')).to_have_count(2)
            await form.locator('[name=model]').fill('alpha')
            await page.locator('#saveConnection').click()
            await expect(page.locator('#connectionDialog')).not_to_be_visible()
            row = page.locator('[data-model-row=first]')
            await expect(row).to_contain_text('alpha')
            await row.locator('[data-edit-model]').click()
            await expect(form.locator('[name=api_key]')).to_have_value('')
            await expect(form.locator('[name=alias]')).to_have_attribute('readonly', '')
            await form.locator('[name=model]').fill('beta')
            await page.locator('#saveConnectionOnly').click()
            await expect(row).to_contain_text('beta')
            assert hub.models.bindings['first'].provider.api_key == 'fixture-value'
            async with httpx.AsyncClient(base_url=url) as client:
                await client.post('/v1/studio/connections', json={'alias': 'second', 'base_url': endpoint, 'model': 'alpha'})
            await page.locator('[data-refresh]').first.click()
            await page.locator('[data-default-model]').select_option('second')
            await expect(page.locator('[data-default-model]')).to_have_value('second')
            await page.goto(url+'/#conversations')
            select = page.locator('#conversations [data-model]')
            await expect(select.locator('option')).to_have_count(3)
            await select.select_option('first')
            await page.locator('#workspaceMessage').fill('你好')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('.chat-result-message')).to_contain_text('回答来自 beta', timeout=20000)
            assert hub.conversations.list()[0]['model'] == 'first'
            if os.environ.get('EAH_UI_EVIDENCE'):
                evidence = Path(os.environ['EAH_UI_EVIDENCE'])
                evidence.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(evidence/'model-selection-chat.png'), full_page=True)
                await page.set_viewport_size({'width': 390, 'height': 844})
                await expect(select).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.screenshot(path=str(evidence/'model-selection-mobile.png'), full_page=True)
                await page.set_viewport_size({'width': 1440, 'height': 1000})
            await select.select_option('second')
            await page.locator('#workspaceMessage').fill('再回答一次')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('.chat-result-message').last).to_contain_text('回答来自 alpha', timeout=20000)
            await page.reload()
            await expect(select).to_have_value('second')
            await page.goto(url+'/#create')
            await page.locator('#assistantModel').select_option('first')
            await page.locator('#assistantName').fill('保存文本')
            await page.locator('#purpose').fill('将输入保存成文本文件')
            await page.locator('#buildAssistant').click()
            await expect(page.locator('#savedLabel')).to_have_text('工作流已生成并保存', timeout=20000)
            assert hub.store.memory_search('studio-assistants')[0]['value']['model'] == 'first'
            assert calls[-1][0] == 'beta'
            await page.goto(url+'/#connections')
            await expect(row).to_be_visible()
            if os.environ.get('EAH_UI_EVIDENCE'):
                await page.screenshot(path=str(evidence/'model-connections.png'), full_page=True)
            page.once('dialog', lambda dialog: dialog.accept())
            await row.locator('[data-delete-model]').click()
            await expect(row).to_have_count(0)
            await page.reload()
            await expect(page.locator('[data-model-row=second]')).to_be_visible()
            await expect(row).to_have_count(0)
            assert not errors
        finally:
            await browser.close()


async def test_test_and_save_cannot_restore_a_deleted_connection(api):
    import asyncio

    url, hub = api
    remote = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()

    @remote.post('/chat/completions')
    async def delayed():
        entered.set()
        await release.wait()
        return {'choices': [{'message': {'content': 'ok'}}]}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        body = {'alias': 'changing', 'base_url': endpoint, 'model': 'alpha'}
        await client.post('/v1/studio/connections', json=body)
        pending = asyncio.create_task(client.put('/v1/studio/connections/changing?test=true', json=body))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert (await client.delete('/v1/studio/connections/changing')).status_code == 200
        finally:
            release.set()
        assert (await pending).status_code == 409
        assert 'changing' not in hub.models.bindings
        assert not hub.store.memory_search('model-connections')
