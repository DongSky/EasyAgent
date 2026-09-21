"""Draft first, connect later: durable setup state, original artifacts and real rebinding."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from playwright.async_api import async_playwright, expect

from conftest import live_server
from easyagent.api import create_app
from easyagent.runtime import Hub
from easyagent.studio import install_studio


@pytest.fixture(autouse=True)
def compile_engine(hub):
    # These fixtures speak the BuildDraft compiler protocol; the autonomous operator has its own tests.
    hub.autonomy.configure({'engine': 'compile'})


async def phase(hub, conversation, wanted):
    async with asyncio.timeout(30):
        while True:
            await hub.conversations.tick()
            current = hub.conversations.get(conversation)
            if current['turns'][-1].get('task', {}).get('phase') == wanted:
                return current
            if current['turns'][-1]['status'] == 'failed':
                raise AssertionError(current['turns'][-1])
            await asyncio.sleep(.02)


def pending_provider():
    app, calls = FastAPI(), []

    @app.post('/chat/completions')
    async def reply(request: Request):
        body = await request.json()
        content = json.loads(body['messages'][-1]['content'])
        calls.append(content)
        title = body['response_format']['json_schema']['schema']['title']
        if title == 'DispatchDecision':
            draft = {'action': 'create', 'title': '图片任务', 'message': '先创建图片流程'}
        elif not any(m['alias'] == 'photos' for m in content['models']):
            draft = {'workflow': {'name': '图片草稿', 'steps': [
                {'id': 'edit', 'kind': 'model', 'target': 'pending_picture', 'input': {'prompt': '编辑原图'}}]},
                'explanation': '已创建流程草稿：读取原图、编辑、保存。请接入支持原图输入的图片编辑服务。', 'questions': [],
                'required_connections': [{'id': 'picture', 'capability': 'image_edit', 'title': '支持原图编辑的图片模型',
                                          'reason': '需要接收上传原图并返回编辑后的图片；只支持文字生图的接口不够。'}]}
        else:
            # Assert actual discovery of the newly connected service and the saved draft.
            assert content['current_workflow']['steps'][0]['target'] == 'pending_picture'
            edits = [t for t in content['available_tools'] if t['name'].endswith('.edits')]
            assert edits
            draft = {'workflow': {'name': '原图编辑', 'steps': [
                {'id': 'edit', 'kind': 'foreach', 'input': {'items': {'$ref': '$input.attachment_ids'}},
                 'body': {'name': '逐张编辑', 'steps': [{'id': 'call', 'target': edits[0]['name'], 'input': {
                     'body': {'model': 'gpt-image-2', 'prompt': '调整光线', 'image': {'$ref': '$input.item'}}}}]}}]},
                     'explanation': '已绑定实际图片编辑接口', 'questions': [], 'required_connections': []}
        return {'choices': [{'message': {'content': json.dumps(draft, ensure_ascii=False)}}]}

    return app, calls


async def test_no_models_keeps_task_and_attachments_across_restart(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        artifact = (await client.post('/v1/artifacts/upload?name=notes.txt', content=b'original material',
                                    headers={'Content-Type': 'text/plain'})).json()
        conversation = (await client.post('/v1/conversations', json={'workspace': True})).json()
        cid = conversation['id']
        body = {'text': '根据原始材料创建流程', 'attachments': [artifact['id']], 'idempotency_key': 'first'}
        turn = (await client.post(f'/v1/conversations/{cid}/messages', json=body)).json()
        result = await phase(hub, cid, 'waiting_connections')
        state = result['turns'][0]['task']
        assert result['turns'][0]['status'] == 'waiting_connections' and not state['runs']
        assert state['required_connections'][0]['capability'] == 'decision'
        assert state['attachments'][0]['id'] == artifact['id']
        assert not state['can_resume']
        assert (await client.post(f'/v1/conversations/{cid}/messages', json=body)).json()['id'] == turn['id']
    await hub.stop()
    restored = Hub(hub.store.path, poll_seconds=.01)
    await restored.start()
    app = create_app(restored, manage_workers=False)
    install_studio(app, restored)
    remote = FastAPI()
    calls = []

    @remote.post('/chat/completions')
    async def reply(request: Request):
        content = json.loads((await request.json())['messages'][-1]['content'])
        calls.append(content)
        assert content['attachments'][0]['id'] == artifact['id']
        assert content['request'] == body['text']
        return {'choices': [{'message': {'content': json.dumps({'action': 'reply', 'message': '继续处理原材料'})}}]}

    try:
        async with live_server(app) as origin, live_server(remote) as endpoint, httpx.AsyncClient(base_url=origin, timeout=30) as client:
            assert restored.conversations.get(cid)['turns'][0]['task']['phase'] == 'waiting_connections'
            await client.post('/v1/studio/connections', json={'alias': 'planner', 'base_url': endpoint, 'model': 'text'})
            assert not calls  # Adding a connection alone does not execute saved requests.
            assert (await client.get('/v1/conversations/'+cid)).json()['turns'][0]['task']['can_resume']
            results = await asyncio.gather(*[client.post(f'/v1/conversations/{cid}/resume-connections') for _ in range(3)])
            assert sum(r.json()['resumed'] for r in results) == 1
            result = await phase(restored, cid, 'answered')
            assert result['turns'][0]['id'] == turn['id'] and len(calls) == 1
            assert result['turns'][0]['task']['model'] == 'planner'
    finally:
        await restored.stop()


async def test_media_blueprint_rebinds_new_connection_without_losing_original(api):
    url, hub = api
    remote, calls = pending_provider()
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        await client.post('/v1/studio/connections', json={'alias': 'planner', 'base_url': endpoint, 'model': 'text'})
        artifact = (await client.post('/v1/artifacts/upload?name=original.png', content=b'original-image-fixture',
                                    headers={'Content-Type': 'image/png'})).json()
        cid = (await client.post('/v1/conversations', json={'workspace': True})).json()['id']
        await client.post(f'/v1/conversations/{cid}/messages', json={'text': '帮我编辑原图', 'attachments': [artifact['id']]})
        waiting = await phase(hub, cid, 'waiting_connections')
        state = waiting['turns'][0]['task']
        assert state['blueprint']['steps'][0]['target'] == 'pending_picture'
        assert state['required_connections'][0]['capability'] == 'image_edit'
        assert not hub.development.workflows()  # Blueprint cannot be accidentally executed or exported as ready.
        count = len(calls)
        assert not (await client.post(f'/v1/conversations/{cid}/resume-connections')).json()['resumed']
        assert len(calls) == count
        await client.post('/v1/studio/connections', json={'alias': 'photos', 'base_url': endpoint,
                          'model': 'gpt-image-2', 'capabilities': ['image']})
        assert (await client.post(f'/v1/conversations/{cid}/resume-connections')).json()['resumed']
        async with asyncio.timeout(30):
            while True:
                await hub.conversations.tick()
                c = hub.conversations.get(cid)
                task = c['turns'][0]['task']
                if task['phase'] == 'executing' and hub.store.run(task['run_id'])['status'] == 'waiting_approval':
                    break
                assert c['turns'][0]['status'] != 'failed', task
                await asyncio.sleep(.02)
        run = hub.store.run(task['run_id'])
        assert run['spec']['inputs']['attachment_ids'] == [artifact['id']]
        # A true external edit remains subject to the existing approval, no billable edit in this test.
        assert all('pending_picture' not in json.dumps(w['workflow']) for w in hub.development.workflows())
        assert len(c['turns']) == 1


async def test_assistant_can_be_created_before_any_model(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        assistant = (await client.post('/v1/studio/assistants', json={'name': '未来的助手', 'purpose': '整理文本',
                     'construction': 'automatic', 'model': 'auto'})).json()
        path = '/v1/studio/assistants/'+assistant['id']
        first = await client.post(path+'/build')
        assert first.status_code == 202 and first.json()['status'] == 'waiting_connections'
        pending = (await client.get(path+'/workflow')).json()
        assert pending['required_connections'][0]['capability'] == 'decision'
        assert (await client.post(path+'/run', json={'message': 'hello'})).status_code == 409
        restored = Hub(hub.store.path)
        from easyagent.assistant_builder import build_status
        assert build_status(restored, assistant['id'], {k:v for k,v in assistant.items() if k!='id'})['status'] == 'waiting_connections'


async def test_browser_return_from_setup_resumes_original_turn(api):
    url, hub = api
    remote = FastAPI()

    @remote.post('/chat/completions')
    async def reply(request: Request):
        data = await request.json()
        assert '保留这个需求' in data['messages'][-1]['content']
        return {'choices': [{'message': {'content': json.dumps({'action': 'reply', 'message': '已使用新连接继续原需求'})}}]}

    async with live_server(remote) as endpoint, async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(url+'/#conversations')
            await page.locator('#workspaceMessage').fill('保留这个需求')
            await page.locator('#conversations [data-send]').click()
            await expect(page.locator('[data-setup]')).to_contain_text('用于理解需求和编排流程的大语言模型')
            await page.reload()
            await expect(page.locator('[data-setup]')).to_contain_text('需求和附件已保留')
            await page.locator('[data-setup-connect]').click()
            await page.locator('[data-add-model]').click()
            form = page.locator('#connectionForm')
            await form.locator('[name=alias]').fill('new-planner')
            await form.locator('[name=base_url]').fill(endpoint)
            await form.locator('[name=model]').fill('text')
            await page.locator('#saveConnectionOnly').click()
            await expect(page.locator('#connectionDialog')).not_to_be_visible()
            await page.locator('[data-group=conversations]').click()
            await expect(page.locator('.chat-result-message')).to_contain_text('已使用新连接继续原需求', timeout=30000)
            assert len(hub.conversations.list()) == 1
            assert len(hub.conversations.get(hub.conversations.list()[0]['id'])['turns']) == 1
            assert not errors
        finally:
            await browser.close()


async def test_follow_up_preserves_waiting_material_and_cancel_does_not_resume(api):
    url, hub = api
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        artifact = (await client.post('/v1/artifacts/upload?name=source.txt', content=b'keep this',
                                    headers={'Content-Type': 'text/plain'})).json()
        cid = (await client.post('/v1/conversations', json={'workspace': True})).json()['id']
        await client.post(f'/v1/conversations/{cid}/messages', json={'text': '原始要求', 'attachments': [artifact['id']]})
        await client.post(f'/v1/conversations/{cid}/messages', json={'text': '再补充一个约束'})
        waiting = await phase(hub, cid, 'waiting_connections')
        assert waiting['turns'][0]['status'] == 'cancelled'
        assert waiting['turns'][1]['task']['attachment_ids'] == [artifact['id']]
        with hub.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM conversation_jobs WHERE turn_id=?', (waiting['turns'][1]['id'],)).fetchone()[0])
        assert state['material_text'] == '原始要求\n补充：再补充一个约束'
        assert (await client.post(f'/v1/conversations/{cid}/interrupt')).json()['interrupted']
        await client.post('/v1/studio/connections', json={'alias': 'new', 'base_url': 'http://127.0.0.1:1', 'model': 'local'})
        assert not (await client.post(f'/v1/conversations/{cid}/resume-connections')).json()['resumed']
        assert not hub.store.runs()


async def test_browser_assistant_setup_keeps_saved_requirement(api):
    url, hub = api
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        try:
            await page.goto(url+'/#create')
            await page.locator('#assistantName').fill('先建好的助手')
            await page.locator('#purpose').fill('把输入保存为文件')
            await page.locator('#buildAssistant').click()
            await expect(page.locator('#assistantPlan')).to_contain_text('助手已创建 · 等待连接模型或服务')
            await expect(page.locator('#savedLabel')).to_have_text('助手与需求已保存，等待连接')
            await expect(page.locator('#tryBtn')).to_be_disabled()
            await page.locator('#setupAssistantConnection').click()
            await expect(page.locator('#connections')).to_be_visible()
            assert len(hub.store.memory_search('studio-assistants')) == 1
        finally:
            await browser.close()


async def test_conceptual_draft_keeps_parallel_steps_without_inventing_api_schemas(api):
    url, hub = api
    remote = FastAPI()
    calls = []
    requirement = {'id': 'image', 'capability': 'image', 'title': '图片生成模型', 'reason': '需要生成图片'}
    steps = [
        {'id': 'research', 'title': '整理材料', 'description': '整理用户输入', 'depends_on': [], 'requires': []},
        {'id': 'left', 'title': '制作方案一', 'description': '基于材料生成第一张图', 'depends_on': ['research'], 'requires': ['image']},
        {'id': 'right', 'title': '制作方案二', 'description': '基于材料生成第二张图', 'depends_on': ['research'], 'requires': ['image']},
        {'id': 'collect', 'title': '汇总图片', 'description': '收集两张图', 'depends_on': ['left', 'right'], 'requires': []}]

    @remote.post('/chat/completions')
    async def reply(request: Request):
        calls.append(await request.json())
        return {'choices': [{'message': {'content': json.dumps({'workflow': None, 'explanation': '步骤已规划，等待生图服务',
            'questions': [], 'required_connections': [requirement], 'planned_steps': steps if len(calls)>1 else []})}}]}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        await client.post('/v1/studio/connections', json={'alias': 'planner', 'model': 'text', 'base_url': endpoint})
        body = {'name': '双方案', 'purpose': '做两张图', 'model': 'planner', 'construction': 'automatic'}
        assistant = (await client.post('/v1/studio/assistants', json=body)).json()
        path = '/v1/studio/assistants/'+assistant['id']
        build = (await client.post(path+'/build')).json()
        assert (await hub.wait(build['id']))['status'] == 'succeeded'
        result = (await client.get(path+'/workflow')).json()
        assert result['status'] == 'waiting_connections'
        assert result['planned_steps'] == steps and result['workflow'] is None
        assert not hub.development.workflows()
        assert len(calls) == 2
        assert (await client.get(path+'/workflow.json')).status_code == 409


async def test_queued_follow_up_continues_when_previous_turn_waits_for_setup(api):
    url, hub = api
    remote = FastAPI()
    entered, release = asyncio.Event(), asyncio.Event()

    @remote.post('/chat/completions')
    async def reply(request: Request):
        body = await request.json()
        title = body['response_format']['json_schema']['schema']['title']
        if title == 'DispatchDecision':
            entered.set()
            await release.wait()
            result = {'action': 'create', 'title': '带约束的图片任务', 'message': '创建草稿'}
        else:
            result = {'workflow': None, 'explanation': '等待图片能力', 'questions': [],
                      'required_connections': [{'id': 'image', 'capability': 'image', 'title': '生图模型', 'reason': '需要生成图像'}],
                      'planned_steps': [{'id': 'make', 'title': '按约束生成图片', 'description': '使用原始材料和后续约束',
                                         'depends_on': [], 'requires': ['image']}]}
        return {'choices': [{'message': {'content': json.dumps(result)}}]}

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        await client.post('/v1/studio/connections', json={'alias': 'planner', 'model': 'text', 'base_url': endpoint})
        cid = (await client.post('/v1/conversations', json={'workspace': True})).json()['id']
        await client.post(f'/v1/conversations/{cid}/messages', json={'text': '原始任务'})
        await asyncio.wait_for(entered.wait(), 30)
        await client.post(f'/v1/conversations/{cid}/messages', json={'text': '补充约束'})
        release.set()
        waiting = await phase(hub, cid, 'waiting_connections')
        assert waiting['turns'][0]['status'] == 'cancelled'
        with hub.store.connect() as db:
            state = json.loads(db.execute('SELECT state FROM conversation_jobs WHERE turn_id=?', (waiting['turns'][1]['id'],)).fetchone()[0])
        assert state['material_text'] == '原始任务\n补充：补充约束'
        assert state['planned_steps'][0]['title'] == '按约束生成图片'
