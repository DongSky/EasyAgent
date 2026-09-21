"""Real HTTP + durable runtime integration; model replies are explicit local protocol fixtures."""
import asyncio
import base64
import io
import json
import zipfile

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.contracts import ToolSpec
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub


async def settled(hub, conversation, *, status=None, timeout=20):
    async with asyncio.timeout(timeout):
        while True:
            await hub.conversations.tick()
            current = hub.conversations.get(conversation)
            if status and current['active_run'] and hub.store.run(current['active_run'])['status'] == status:
                return current
            if not status and current['turns'] and all(t['status'] in ('succeeded', 'failed', 'cancelled', 'steered') for t in current['turns']):
                return current
            await asyncio.sleep(.02)


def provider_app(route, compile_flow=None):
    remote = FastAPI()
    calls = []

    @remote.post('/chat/completions')
    async def reply(request: Request):
        body = await request.json()
        calls.append(body)
        title = body.get('response_format', {}).get('json_schema', {}).get('schema', {}).get('title')
        if title == 'DispatchDecision':
            result = await route(json.loads(body['messages'][-1]['content']))
        elif title in ('Draft', 'BuildDraft'):
            result = {'workflow': compile_flow, 'explanation': '读取上传材料，然后保存结果。', 'questions': []}
        else:
            return {'choices': [{'message': {'content': '附件已按原始格式交给接口。'}}]}
        return {'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)}}]}
    return remote, calls


def document_flow():
    return {'name': '通知整理', 'inputs': {'message': ''},
            'metadata': {'description': '读取一份通知文档，保存整理后的材料', 'step_labels': {'read': '读取材料', 'save': '保存结果'},
                         'input_schema': {'type': 'object', 'properties': {'message': {'type': 'string'}, 'reference_artifact': {'type': 'string'}}, 'required': ['reference_artifact'], 'additionalProperties': False}},
            'steps': [{'id': 'read', 'target': 'attachments.read', 'input': {'artifact_id': {'$ref': '$input.reference_artifact'}}},
                      {'id': 'save', 'kind': 'artifact', 'depends_on': ['read'], 'input': {'name': '通知.txt', 'content': {'$ref': 'read.text'}}}]}


async def test_router_sees_nested_pinned_tools_and_shared_input_wiring(api):
    url, hub = api
    body = {'name': 'process one', 'steps': [{'id': 'copy', 'target': 'core.echo',
        'input': {'file': {'$ref': '$input.item'}, 'prompt': {'$ref': '$input.edit_prompt'}}}]}
    hub.development.save_workflow('process-one', body)
    flow = {'name': '批量处理', 'steps': [{'id': 'batch', 'kind': 'foreach',
        'input': {'items': {'$ref': '$input.attachment_ids'}, 'edit_prompt': {'$ref': '$input.message'}},
        'workflow_ref': {'id': 'process-one', 'revision': 1}}]}
    hub.development.save_workflow('batch', flow)
    hub.development.save_workflow('process-one', {'name': 'changed', 'steps': [
        {'id': 'different', 'kind': 'transform'}]}, 1)
    artifact = hub.artifacts.put('original.txt', b'original bytes', 'text/plain')

    async def route(context):
        candidate = context['catalog'][0]
        assert candidate['key'] == 'batch@1'
        batch = candidate['steps'][0]
        assert batch['input']['edit_prompt'] == {'$ref': '$input.message'}
        inner = batch['children'][0]
        assert inner['id'] == 'copy' and inner['target'] == 'core.echo'
        assert inner['input']['prompt'] == {'$ref': '$input.edit_prompt'}
        assert inner['description'] == hub.tools.spec('core.echo').description
        return {'action': 'use', 'candidate': candidate['key'], 'confidence': 1,
                'inputs': {}, 'message': '使用已配置的子流程节点。'}

    remote, calls = provider_app(route)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('router', HTTPProvider(endpoint), 'fixture', ['decision'])
        conversation = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'router'})).json()
        response = await client.post(f"/v1/conversations/{conversation['id']}/messages", json={
            'text': '保持自然', 'attachments': [artifact['id']], 'intent': 'workflow', 'workflow': 'batch@1'})
        assert response.status_code == 202
        completed = await settled(hub, conversation['id'])
        task = completed['turns'][-1]['task']
        assert task['phase'] == 'completed', task
        result = hub.store.run(task['run_id'])['steps'][0]['output']['results'][0]['copy']
        assert result == {'file': artifact['id'], 'prompt': '保持自然'}
        assert len(calls) == 1


async def test_match_attachments_pinned_version_idempotency_and_restore(api):
    url, hub = api
    first = hub.development.save_workflow('notice', document_flow())
    entered, release = asyncio.Event(), asyncio.Event()

    async def route(context):
        entered.set()
        await release.wait()
        assert context['attachments'][0]['excerpt'] == '周五下午三点前提交回执。'
        return {'action': 'use', 'candidate': context['catalog'][0]['key'], 'confidence': .96,
                'inputs': {'reference_artifact': context['attachments'][0]['id']}, 'message': '使用通知整理流程。'}

    remote, calls = provider_app(route)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('route-fixture', HTTPProvider(endpoint, ''), 'fixture', ['chat', 'decision'])
        upload = (await client.post('/v1/artifacts/upload?name=notice.txt', content='周五下午三点前提交回执。'.encode(), headers={'Content-Type': 'text/plain'})).json()
        c = (await client.post('/v1/conversations', json={'workspace': True, 'title': '整理通知'})).json()
        payload = {'text': '整理这份通知', 'attachments': [upload['id']], 'idempotency_key': 'one-request'}
        sent = await client.post(f"/v1/conversations/{c['id']}/messages", json=payload)
        assert sent.status_code == 202, sent.text
        await asyncio.wait_for(entered.wait(), 5)
        altered = document_flow()
        altered['steps'][-1]['input']['content'] = 'NEW VERSION MUST NOT RUN'
        hub.development.save_workflow('notice', altered, first['revision'])
        release.set()
        result = await settled(hub, c['id'])
        task = result['turns'][0]['task']
        assert task['phase'] == 'completed', result
        assert task['selected']['revision'] == 1
        assert len(task['runs']) == 2
        run = hub.store.run(task['run_id'])
        output = run['steps'][-1]['output']
        assert hub.artifacts.get(output['id'])[1].decode() == '周五下午三点前提交回执。'
        assert run['spec']['inputs']['reference_artifact'] == upload['id']
        repeat = await client.post(f"/v1/conversations/{c['id']}/messages", json=payload)
        assert repeat.json()['id'] == sent.json()['id'] and len(calls) == 1
        assert (await client.post(f"/v1/conversations/{c['id']}/messages", json={**payload, 'text': 'different'})).status_code == 409
        restored = Hub(hub.store.path)
        assert restored.conversations.get(c['id'])['turns'][0]['task']['selected']['revision'] == 1
        assert (await client.get('/v1/conversations')).json()[0]['workspace']


async def test_ambiguous_match_clarifies_and_explicit_choice_still_requires_inputs(api):
    url, hub = api
    hub.development.save_workflow('notice', document_flow())

    async def route(context):
        return {'action': 'use', 'candidate': context['catalog'][0]['key'], 'confidence': .2, 'message': '你是想整理通知吗？'}

    remote, _ = provider_app(route)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('router', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        c = (await client.post('/v1/conversations', json={'workspace': True})).json()
        await client.post(f"/v1/conversations/{c['id']}/messages", json={'text': '帮我处理一下'})
        result = await settled(hub, c['id'])
        assert result['turns'][0]['task']['phase'] == 'clarification'
        assert result['turns'][0]['task']['choices'][0]['key'] == 'notice@1'
        assert len(result['turns'][0]['task']['runs']) == 1
        await client.post(f"/v1/conversations/{c['id']}/messages", json={'text': '就是通知整理', 'intent': 'workflow', 'workflow': 'notice@1'})
        result = await settled(hub, c['id'])
        assert result['turns'][-1]['task']['phase'] == 'clarification'
        assert 'reference_artifact' in result['messages'][-1]['content']
        assert not hub.artifacts.list()


async def test_zero_code_build_save_run_and_reuse_with_document(api):
    url, hub = api
    flow = {'name': '读取文档', 'steps': [
        {'id': 'read', 'target': 'attachments.read', 'input': {'artifact_id': {'$ref': '$input.attachment_ids.0'}}},
        {'id': 'save', 'kind': 'artifact', 'depends_on': ['read'], 'input': {'name': 'summary.txt', 'content': {'$ref': 'read.text'}}}]}

    async def route(context):
        return {'action': 'use', 'candidate': context['catalog'][0]['key'], 'confidence': .99, 'message': '复用刚刚创建的流程。'}

    remote, calls = provider_app(route, flow)
    hub.autonomy.configure({'engine': 'compile'})  # This fixture speaks the BuildDraft compiler protocol.
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('planner', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        upload = (await client.post('/v1/artifacts/upload?name=meeting.md', content=b'# Meeting\nBring the signed form.', headers={'Content-Type': 'text/markdown'})).json()
        c = (await client.post('/v1/conversations', json={'workspace': True})).json()
        await client.post(f"/v1/conversations/{c['id']}/messages", json={'text': '做一个读取文档并保存的流程', 'intent': 'create', 'attachments': [upload['id']]})
        result = await settled(hub, c['id'])
        task = result['turns'][0]['task']
        assert task['phase'] == 'completed', result
        assert task['selected']['id'].startswith('assistant-chat-')
        assert len(task['runs']) == 2
        catalog = (await client.get('/v1/conversations/workflow-catalog')).json()
        assert any(x['key'] == task['selected']['key'] for x in catalog)
        await client.post(f"/v1/conversations/{c['id']}/messages", json={'text': '再处理这份文档', 'attachments': [upload['id']]})
        result = await settled(hub, c['id'])
        assert result['turns'][-1]['task']['selected'] == task['selected']
        assert len(calls) == 2  # One compilation, one dispatch; no fictional extra model calls.


async def test_workspace_queue_approval_restart_and_cancel(tmp_path):
    effects = []
    spec = ToolSpec(name='fixture.send', effect='write', idempotent=False)

    async def write(args, ctx):
        effects.append(args)
        return {'receipt': 'sent-once'}

    async def route(context):
        if context['intent'] == 'chat':
            return {'action': 'reply', 'message': '后续问题已收到。'}
        return {'action': 'use', 'candidate': 'send@1', 'confidence': 1, 'message': '准备发送，执行前需要确认。'}

    remote, calls = provider_app(route)
    async with live_server(remote) as endpoint:
        hub = Hub(tmp_path/'restart.db', poll_seconds=.01)
        hub.models.register('router', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        hub.tools.register(spec, write)
        hub.development.save_workflow('send', {'name': '发送回执', 'steps': [{'id': 'send', 'target': 'fixture.send'}]})
        await hub.start()
        c = await hub.conversations.create({'workspace': True})
        await hub.conversations.send(c['id'], {'text': '发送回执'})
        awaiting = await settled(hub, c['id'], status='waiting_approval')
        assert effects == []
        run_id = awaiting['active_run']
        await hub.conversations.send(c['id'], {'text': '然后回复我', 'intent': 'chat'})
        # An old pending conversation still advances beyond the 200-row history page.
        for i in range(205):
            await hub.conversations.create({'workspace': True, 'title': f'Later conversation {i}'})
        assert c['id'] not in {item['id'] for item in hub.conversations.list()}
        await hub.stop()
        restored = Hub(tmp_path/'restart.db', poll_seconds=.01)
        restored.models.register('router', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        restored.tools.register(spec, write)
        await restored.start()
        try:
            current = restored.store.run(run_id)
            restored.tools.approve(restored.store, current['approvals'][0]['id'], True)
            completed = await settled(restored, c['id'])
            assert all(t['status'] == 'succeeded' for t in completed['turns']), completed
            assert effects == [{}] and len(calls) == 2
            await restored.conversations.send(c['id'], {'text': '再次发送回执'})
            await settled(restored, c['id'], status='waiting_approval')
            await restored.conversations.interrupt(c['id'])
            assert restored.conversations.get(c['id'])['turns'][-1]['status'] == 'cancelled'
            assert effects == [{}]
        finally:
            await restored.stop()


async def test_oversized_dispatch_context_clarifies_before_model_call(api):
    url, hub = api
    flow = document_flow()
    flow['metadata']['description'] = 'x' * 181_000
    hub.development.save_workflow('large-catalog', flow)

    async def unexpected(context):
        raise AssertionError('Oversized routing must not call the provider')

    remote, calls = provider_app(unexpected)
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('router', HTTPProvider(endpoint, ''), 'fixture', ['decision', 'chat'])
        conversation = (await client.post('/v1/conversations', json={'workspace': True})).json()
        await client.post(f"/v1/conversations/{conversation['id']}/messages", json={'text': '整理通知'})
        completed = await settled(hub, conversation['id'])
        assert completed['turns'][0]['task']['phase'] == 'clarification'
        assert completed['turns'][0]['task']['runs'] == []
        assert calls == []


@pytest.mark.parametrize('dialect', ['chat', 'responses', 'anthropic'])
async def test_model_images_are_translated_at_provider_boundary(hub, dialect):
    image = hub.artifacts.put('sample.png', base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII='), 'image/png')
    remote = FastAPI()
    seen = []

    @remote.post('/{path:path}')
    async def respond(path, request: Request):
        body = await request.json()
        seen.append(body)
        if dialect == 'responses':
            return {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'image received'}]}]}
        if dialect == 'anthropic':
            return {'content': [{'type': 'text', 'text': 'image received'}]}
        return {'choices': [{'message': {'content': 'image received'}}]}

    async with live_server(remote) as endpoint:
        hub.models.register('vision', HTTPProvider(endpoint, '', dialect), 'fixture', ['chat'])
        run = await hub.wait(hub.submit({'name': 'read image', 'steps': [{'id': 'read', 'kind': 'model', 'target': 'vision', 'input': {'prompt': 'Read this image', 'attachments': [image['id']]}}]}))
        assert run['status'] == 'succeeded', run
        assert 'base64' not in json.dumps(run['spec'])
        blocks = seen[0].get('messages', seen[0].get('input'))[-1]['content']
        assert blocks[-1]['type'] == {'chat': 'image_url', 'responses': 'input_image', 'anthropic': 'image'}[dialect]


async def test_document_extraction_audio_video_and_missing_media(hub):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 100 Td (Submit before Friday) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    pdf = io.BytesIO()
    writer.write(pdf)
    pdf_file = hub.artifacts.put('notice.pdf', pdf.getvalue(), 'application/pdf')
    assert 'Submit before Friday' in hub.attachments.read(pdf_file['id'])['text']
    word = io.BytesIO()
    with zipfile.ZipFile(word, 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Meeting at three</w:t></w:r></w:p></w:body></w:document>')
    doc = hub.artifacts.put('meeting.docx', word.getvalue(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
    assert hub.attachments.read(doc['id'])['text'] == 'Meeting at three'
    async def unused(context):
        return {}
    remote, seen = provider_app(unused)
    async with live_server(remote) as endpoint:
        hub.models.register('media-reader', HTTPProvider(endpoint, ''), 'fixture', ['chat', 'video_input'])
        for name, mime, kind in [('sample.wav', 'audio/wav', 'input_audio'), ('sample.mp4', 'video/mp4', 'video_url')]:
            file = hub.artifacts.put(name, b'protocol-media-fixture', mime)
            run = await hub.wait(hub.submit({'name': name, 'steps': [{'id': 'read', 'kind': 'model', 'target': 'media-reader', 'input': {'prompt': 'Inspect media', 'attachments': [file['id']]}}]}))
            assert run['status'] == 'succeeded', run
            assert seen[-1]['messages'][-1]['content'][0]['type'] == kind
    with pytest.raises(KeyError):
        await hub.conversations.send((await hub.conversations.create({'workspace': True}))['id'], {'text': 'process', 'attachments': ['missing']})
