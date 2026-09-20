"""Default media definitions, scoped credentials, and real HTTP artifact/polling integration."""
import asyncio
import base64
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from playwright.async_api import async_playwright, expect

from conftest import live_server
from easyagent.default_media import definitions
from easyagent.runtime import Hub


async def approve(hub, run):
    pending = await hub.wait(run)
    assert pending['status'] == 'waiting_approval', pending
    for invocation in pending['approvals']:
        if invocation['status'] == 'approval':
            hub.tools.approve(hub.store, invocation['id'], True)


async def test_default_media_catalog_is_clean_unconnected_and_browser_binds(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        rows = (await client.get('/v1/library')).json()
        default = [row for row in rows if row.get('builtin_media')]
        assert {row['id'] for row in default} == set(definitions())
        assert len(default) == 5 and all(row['component_type'] == 'node' for row in default)
        assert all(not row['available'] and not row['installed'] for row in default)
        assert not hub.development.workflows() and not hub.store.runs()
        await client.post('/v1/studio/connections', json={'alias': 'media', 'base_url': 'http://127.0.0.1:1/v1',
                          'model': 'gpt-image-2.5-flare', 'capabilities': ['image']})
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(url+'/#workflow')
            await page.locator('.node-library > summary').click()
            button = page.locator('[data-library-id="library.media.image_edit"]')
            await expect(button).to_have_text('选择服务连接')
            await button.click()
            await expect(page.locator('#libraryMediaConnection')).to_have_value('media')
            await expect(page.locator('#libraryMediaModel')).to_have_value('gpt-image-2.5-flare')
            await page.locator('#libraryConnectionForm > button').click()
            await expect(page.locator('#libraryConnection')).not_to_be_visible()
            await expect(button).to_have_text('加入画布')
            await button.click()
            assert not hub.store.runs() and not hub.development.workflows()
            await page.reload()
            await expect(button).to_have_text('加入画布')
            assert not errors
        finally:
            await browser.close()


async def test_image_generation_edit_upload_and_video_poll_survive_restart(api):
    url, hub = api
    remote = FastAPI()
    counts = {'image': 0, 'edit': 0, 'upload': 0, 'submit': 0, 'poll': 0}
    ready = False
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII=')

    @remote.post('/v1/images/generations')
    async def image(request: Request):
        assert request.headers['authorization'] == 'Bearer synthetic-media-fixture'
        data = await request.json()
        assert data['model'] == 'image-fixture' and data['prompt'] == 'draw'
        counts['image'] += 1
        return {'data': [{'b64_json': base64.b64encode(png).decode()}]}

    @remote.post('/v1/images/edits')
    async def edit(request: Request):
        content = await request.body()
        assert png in content and b'name="image"' in content and b'image-fixture' in content
        counts['edit'] += 1
        return {'data': [{'b64_json': base64.b64encode(png).decode()}]}

    @remote.post('/openapi/v2/image/upload')
    async def upload(request: Request):
        assert png in await request.body()
        counts['upload'] += 1
        return {'Resp': {'img_url': 'https://example.com/reference.png'}}

    @remote.post('/api/v3/contents/generations/tasks')
    async def video(request: Request):
        data = await request.json()
        assert data['model'] == 'video-fixture'
        assert data['content'][1]['image_url']['url'] == 'https://example.com/reference.png'
        assert data['ratio'] == 'adaptive'
        counts['submit'] += 1
        return {'id': 'saved-task'}

    @remote.get('/api/v3/contents/generations/tasks/{identifier}')
    async def poll(identifier: str, request: Request):
        assert identifier == 'saved-task' and request.headers['authorization'] == 'Bearer synthetic-media-fixture'
        counts['poll'] += 1
        return {'id': identifier, 'status': 'succeeded' if ready else 'pending',
                **({'content': {'video_url': 'https://example.com/result.mp4'}} if ready else {})}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        await client.post('/v1/studio/connections', json={'alias': 'media', 'base_url': endpoint+'/v1',
                          'model': 'image-fixture', 'capabilities': ['image'], 'api_key': 'synthetic-media-fixture'})
        for identifier in definitions():
            r = await client.post('/v1/library/'+identifier+'/install', json={'connection': 'media',
                                 'model': 'video-fixture' if 'video_' in identifier else 'image-fixture'})
            assert r.status_code == 201, r.text
            assert r.json()['source']['kind'] == 'api'
        assert not any(counts.values())
        assert not hub.development.workflows()
        for operation in ('image_generate', 'image_edit'):
            original = hub.artifacts.put('original.png', png, 'image/png')
            inputs = {'model': 'image-fixture', 'prompt': 'draw'}
            if operation == 'image_edit':
                inputs['image'] = original['id']
            step = hub.library.instantiate('library.media.'+operation, {'input': inputs})['step']
            run_id = hub.submit({'name': 'image protocol', 'steps': [step]})
            await approve(hub, run_id)
            run = await hub.wait(run_id)
            assert run['status'] == 'succeeded', run
            artifact = run['steps'][0]['output']['artifacts'][0]
            assert hub.artifacts.get(artifact['id'])[1] == png
            assert hub.artifacts.get(original['id'])[1] == png
        upload_step = hub.library.instantiate('library.media.image_upload', {'step_id': 'upload', 'input': {'image': original['id']}})['step']
        submit_step = hub.library.instantiate('library.media.video_submit', {'step_id': 'submit', 'input': {
            'model': 'video-fixture', 'content': [{'type': 'text', 'text': 'animate'}, {'type': 'image_url',
                'image_url': {'url': {'$ref': 'upload.result.Resp.img_url'}}, 'role': 'first_frame'}]}})['step']
        submit_step['depends_on'] = ['upload']
        wait_step = hub.library.instantiate('library.media.video_wait', {'step_id': 'wait', 'input': {'id': {'$ref': 'submit.result.id'}}})['step']
        wait_step['depends_on'] = ['submit']
        run_id = hub.submit({'name': 'animate', 'steps': [upload_step, submit_step, wait_step]})
        await approve(hub, run_id)
        await approve(hub, run_id)
        async with asyncio.timeout(10):
            while hub.store.run(run_id)['steps'][2]['status'] != 'waiting_remote':
                await asyncio.sleep(.02)
        await hub.stop()
        ready = True
        restored = Hub(hub.store.path, poll_seconds=.01)
        with restored.store.connect() as db:
            db.execute("UPDATE steps SET ready_at=? WHERE run_id=? AND id='wait'", (time.time(), run_id))
        await restored.start()
        try:
            run = await restored.wait(run_id)
            assert run['status'] == 'succeeded', run
            assert counts == {'image': 1, 'edit': 1, 'upload': 1, 'submit': 1, 'poll': 2}
            assert run['steps'][2]['output']['content']['video_url'] == 'https://example.com/result.mp4'
            assert run['steps'][2]['attempts'] == 1
            assert len([r for r in restored.library.catalog() if r.get('builtin_media') and r['available']]) == 5
        finally:
            await restored.stop()


async def test_binding_reuse_revocation_and_export_never_include_key(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        connection = {'alias': 'media', 'base_url': 'http://127.0.0.1:1/v1', 'model': 'test-image',
                      'capabilities': ['image'], 'api_key': 'synthetic-media-fixture'}
        await client.post('/v1/studio/connections', json=connection)
        path = '/v1/library/library.media.image_edit'
        first = (await client.post(path+'/install', json={'connection': 'media'})).json()
        again = (await client.post(path+'/install', json={'connection': 'media'})).json()
        assert first == again
        source = first['source']
        old_handler = hub.tools.versions[source['id'], source['revision']][1]
        package = await client.get(path+'/package')
        assert package.status_code == 200 and 'synthetic-media-fixture' not in package.text
        assert 'synthetic-media-fixture' not in (await client.get('/v1/library')).text
        assert (await client.put('/v1/studio/connections/media', json={**connection, 'base_url': 'http://127.0.0.1:2/v1'})).status_code == 200
        rows = (await client.get('/v1/library')).json()
        assert not next(r for r in rows if r['id'] == 'library.media.image_edit')['available']
        with pytest.raises(ValueError, match='模型连接已修改或删除'):
            await old_handler({}, None)
        latest = (await client.post(path+'/install', json={'connection': 'media'})).json()
        assert latest['revision'] > first['revision']
        assert latest['source']['revision'] > first['source']['revision']
        with pytest.raises(ValueError, match='模型连接已修改或删除'):
            await old_handler({}, None)
        await client.delete('/v1/studio/connections/media')
        assert len([r for r in (await client.get('/v1/library')).json() if r.get('builtin_media')]) == 5


async def test_media_union_checks_literals_and_allows_symbolic_links():
    from easyagent.authoring import check_literals
    schema = definitions()['library.media.video_submit']['input_schema']
    valid = {'model': 'fixture', 'content': [{'type': 'image_url', 'image_url': {'url': {'$ref': 'upload.result.url'}}}]}
    check_literals(valid, schema)
    with pytest.raises(ValueError, match='no supported input variant'):
        check_literals({'model': 'fixture', 'content': [{'type': 'image_url', 'text': 'wrong shape'}]}, schema)


async def test_video_rebind_keeps_wait_on_same_connection(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        for alias, port in [('first', 1), ('second', 2)]:
            response = await client.post('/v1/studio/connections', json={'alias': alias,
                'base_url': f'http://127.0.0.1:{port}/v1', 'model': 'video-fixture', 'capabilities': ['chat']})
            assert response.status_code == 200
        submit_path = '/v1/library/library.media.video_submit/install'
        wait_path = '/v1/library/library.media.video_wait/install'
        first = (await client.post(submit_path, json={'connection': 'first'})).json()
        original_wait = hub.library.get('library.media.video_wait')
        await client.post(wait_path, json={'connection': 'second'})
        assert hub.library.get('library.media.video_wait').source.id != original_wait.source.id
        again = (await client.post(submit_path, json={'connection': 'first'})).json()
        assert again == first
        assert hub.library.get('library.media.video_wait').source == original_wait.source
        assert not hub.store.runs()


async def test_large_original_upload_and_preflight_failure_are_distinct_from_uncertain_write(api):
    from fastapi.responses import JSONResponse
    url, hub = api
    remote, requests = FastAPI(), []
    content = b'original-image-fixture' + b'x' * 10_000_000
    original = hub.artifacts.put('large-original.png', content, 'image/png')

    @remote.post('/v1/images/edits')
    async def edit(request: Request):
        body = await request.body()
        assert content in body
        requests.append(len(body))
        if len(requests) == 2:
            return JSONResponse({'error': {'message': 'upstream failed after request received'}}, status_code=500)
        if len(requests) == 3:
            return JSONResponse({'error': {'message': 'request too large'}}, status_code=413)
        return {'data': [{'url': 'https://example.com/edited.png'}]}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        await client.post('/v1/studio/connections', json={'alias': 'large-image', 'base_url': endpoint+'/v1',
                          'model': 'image-fixture', 'capabilities': ['image']})
        hub.library.install('library.media.image_edit', {'connection': 'large-image'})
        step = hub.library.instantiate('library.media.image_edit', {'input': {
            'image': original['id'], 'prompt': 'retouch'}})['step']
        source = hub.development.get('api', step['target'])
        assert source['definition']['max_upload_bytes'] == 50_000_000
        hub.development.save_api({**source['definition'], 'name': 'fixture.small_upload', 'max_upload_bytes': 10_000_000})
        limited = {**step, 'target': 'fixture.small_upload', 'tool_revision': 1}
        failed = hub.submit({'name': 'local rejection', 'steps': [limited]})
        await approve(hub, failed)
        result = await hub.wait(failed)
        assert result['status'] == 'failed', result
        assert '请求未发送' in result['steps'][0]['error'] and '10 MB' in result['steps'][0]['error']
        assert not requests and not result['approvals']
        with hub.store.connect() as db:
            assert db.execute('SELECT status FROM invocations WHERE run_id=?', (failed,)).fetchone()[0] == 'failed'

        succeeded = hub.submit({'name': 'large original', 'steps': [step]})
        await approve(hub, succeeded)
        result = await hub.wait(succeeded)
        assert result['status'] == 'succeeded', result
        assert len(requests) == 1 and hub.artifacts.get(original['id'])[1] == content

        uncertain = hub.submit({'name': 'remote error', 'steps': [step]})
        await approve(hub, uncertain)
        result = await hub.wait(uncertain)
        assert result['status'] == 'needs_attention', result
        assert len(requests) == 2 and result['approvals'][0]['status'] == 'uncertain'

        rejected = hub.submit({'name': 'explicit upload rejection', 'steps': [step]})
        await approve(hub, rejected)
        result = await hub.wait(rejected)
        assert result['status'] == 'failed', result
        assert 'HTTP 413' in result['steps'][0]['error'] and not result['approvals']
        assert len(requests) == 3


async def test_prepare_image_creates_bounded_copy_preserving_original(hub):
    import io
    from PIL import Image
    image = Image.effect_noise((1024, 768), 100).convert('RGB')
    encoded = io.BytesIO()
    image.save(encoded, format='PNG')
    original = hub.artifacts.put('original.png', encoded.getvalue(), 'image/png')
    run = await hub.wait(hub.submit({'name': 'prepare upload copy', 'steps': [
        {'id': 'prepare', 'target': 'attachments.prepare_image', 'input': {
            'artifact_id': original['id'], 'max_bytes': 64000, 'max_side': 512}}]}))
    assert run['status'] == 'succeeded', run
    result = run['steps'][0]['output']
    info, content = hub.artifacts.get(result['artifact']['id'])
    assert info['media_type'] == 'image/jpeg' and info['size'] <= 64000
    assert result['original']['id'] == original['id'] and info['id'] != original['id']
    assert hub.artifacts.get(original['id'])[1] == encoded.getvalue()
    prepared = Image.open(io.BytesIO(content))
    assert prepared.width <= 512 and prepared.height <= 512
    assert abs(prepared.width / prepared.height - 1024 / 768) < .02
    assert not run['approvals'] and run['usage']['model_calls'] == 0
