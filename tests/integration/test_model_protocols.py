"""Protocol conformance uses real local HTTP, without charging every model in the directory."""
import base64
import json
from importlib.resources import files
import re

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from conftest import live_server
from easyagent.contracts import ModelRequest
from easyagent.http_tools import build_http_tool
from easyagent.tools import InvocationContext


SNAPSHOT = json.loads(files('easyagent').joinpath('data/model_protocols.json').read_text(encoding='utf-8'))
NATIVE_FAMILIES = sorted({op['family'] for op in SNAPSHOT['operations'] if '*' not in op['path']})


@pytest.fixture
async def native_catalog(hub, monkeypatch):
    # These route contracts invoke handlers directly; idle workflow workers would
    # only add database polling. The workflow approval case starts them explicitly.
    await hub.stop()
    snapshot = hub.model_catalog.snapshot
    remote, received = FastAPI(), []
    @remote.get('/v1/models')
    async def models(request: Request):
        received.append((request.method, '/v1/models', request.headers.get('content-type'), await request.body()))
        return {'data': snapshot['models']}
    @remote.get('/api/pricing')
    async def pricing():
        return {'supported_endpoint': snapshot['endpoint_types']}
    @remote.api_route('/{path:path}', methods=['GET', 'POST', 'PATCH', 'PUT', 'DELETE'])
    async def native(path: str, request: Request):
        assert request.headers['authorization'] == 'Bearer fixture-only'
        body = await request.body()
        received.append((request.method, '/'+path, request.headers.get('content-type'), body))
        if path == 'v1/audio/speech':
            return Response(b'ID3-protocol-fixture', media_type='audio/mpeg')
        if path == 'v1/images/generations':
            return {'data': [{'b64_json': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()}]}
        return {'method': request.method, 'path': '/'+path, 'received': bool(body)}
    monkeypatch.setenv('TEST_PROTOCOL_KEY', 'fixture-only')
    async with live_server(remote) as endpoint:
        catalog = await hub.model_catalog.discover({'base_url': endpoint+'/v1', 'api_key_env': 'TEST_PROTOCOL_KEY'})
        assert catalog['model_count'] == 343 and catalog['missing_protocol_count'] == 22
        assert all(o['schema_status'] == 'documented' for m in catalog['models'] for o in m['operations'])
        assert 'fixture-only' not in json.dumps(catalog)
        received.clear()
        yield catalog, received


@pytest.mark.parametrize('family', NATIVE_FAMILIES)
async def test_catalog_concrete_native_routes(hub, native_catalog, family):
    # Keep complete route coverage within the watchdog on slower platforms.
    _, received = native_catalog
    media = hub.artifacts.put('input.wav', b'RIFF-fixture', 'audio/wav')
    run_id = hub.submit({'name': 'Protocol fixtures', 'steps': [{'id':'receipt', 'target':'core.echo'}]})
    all_concrete = [op for op in hub.model_catalog.operations.values() if '*' not in op['path']]
    assert len(all_concrete) >= 300
    assert {op['family'] for op in all_concrete} == set(NATIVE_FAMILIES)
    concrete = [op for op in all_concrete if op['family'] == family]
    assert concrete
    for index, op in enumerate(concrete):
        definition, defaults, _ = hub.model_catalog.native_definition({'operation_id': op['id']})
        args = {name: 'fixture' for name in re.findall(r'\{([^}]+)\}', op['path'])}
        if definition.body_parameter:
            args['body'] = {'prompt': 'protocol fixture'}
            for key in definition.file_parameters:
                args['body'][key] = media['id']
        _, handler = build_http_tool(definition)
        output = await handler(args, InvocationContext(str(index), run_id, 'receipt', store=hub.store))
        assert output['inline_result'] is True
        expected_path = re.sub(r'\{[^}]+\}', 'fixture', op['path'])
        assert received[-1][:2] == (op['method'], expected_path)
        if definition.request_encoding == 'multipart' and definition.body_parameter:
            assert received[-1][2].startswith('multipart/form-data; boundary=')
            if definition.file_parameters:
                assert b'RIFF-fixture' in received[-1][3]
        if op['path'] in ('/v1/images/generations','/v1/audio/speech'):
            assert output['artifacts']
            info, data = hub.artifacts.get(output['artifacts'][0]['id'])
            assert data and info['media_type'] in ('image/png','audio/mpeg')
    assert len(received) == len(concrete)


async def test_catalog_reference_upload_media_and_installed_operation(hub, native_catalog):
    catalog, received = native_catalog
    run_id = hub.submit({'name': 'Media protocol fixtures', 'steps': [{'id': 'receipt', 'target': 'core.echo'}]})
    # Select the model-specific oneOf branch before identifying binary fields.
    # Otherwise reference images are silently sent as text artifact identifiers.
    reference = hub.artifacts.put('reference.png', b'\x89PNG\r\n\x1a\nreference-bytes', 'image/png')
    definition, defaults, _ = hub.model_catalog.native_definition({
        'operation_id': 'v1.post.v1_images_edits', 'model': 'gpt-image-2.5-flare'})
    assert 'image' in definition.file_parameters
    assert definition.input_schema['properties']['body']['properties']['image']['format'] == 'binary'
    _, handler = build_http_tool(definition)
    await handler({'body': {**defaults['body'], 'prompt': 'chibi', 'n': 1, 'image': reference['id']}},
                  InvocationContext('reference-upload', run_id, 'receipt', store=hub.store))
    assert b'filename="reference.png"' in received[-1][3]
    assert b'reference-bytes' in received[-1][3]
    assert reference['id'].encode() not in received[-1][3]
    assert b'gpt-image-2.5-flare' in received[-1][3]
    definition, defaults, _ = hub.model_catalog.native_definition({
        'operation_id': 'seedance.post.api_v3_contents_generations_tasks', 'model': 'doubao-seedance-2-5-260628'})
    _, handler = build_http_tool(definition)
    arguments={'body': {**defaults['body'], 'content':[
        {'type':'image_url','image_url':{'url':reference['id']},'role':'first_frame'}]}}
    await handler(arguments, InvocationContext('video-reference',run_id,'receipt',store=hub.store))
    sent=json.loads(received[-1][3])
    data_url=sent['content'][0]['image_url']['url']
    assert base64.b64decode(data_url.split(',',1)[1]) == b'\x89PNG\r\n\x1a\nreference-bytes'
    assert arguments['body']['content'][0]['image_url']['url'] == reference['id']
    # Model ids with '/' stay in the model field or an encoded path placeholder.
    gemini = next(m for m in catalog['models'] if m['id'] == 'gemini-3.1-flash-tts-preview')
    route = next(r for r in gemini['operations'] if r['protocol'] == 'gemini')
    definition, defaults, _ = hub.model_catalog.native_definition({'operation_id': route['operation_id'], 'model': gemini['id']})
    assert defaults['model'] == gemini['id']
    assert 'key' not in definition.input_schema['properties']
    # Native interfaces still work through normal Hub approval and persisted invocation handling.
    row = hub.model_catalog.install_operation({'operation_id': next(o['id'] for o in hub.model_catalog.operations.values() if o['path']=='/v1/audio/speech'),
        'model': 'tts-1', 'defaults': {'body': {'input': 'hello', 'voice': 'alloy'}}})
    await hub.start()
    created = hub.submit({'name': 'Native voice via workflow', 'steps': [row['step']]})
    approval = await hub.wait(created)
    assert approval['status'] == 'waiting_approval'
    hub.tools.approve(hub.store, approval['approvals'][0]['id'], True)
    result = await hub.wait(created)
    assert result['status'] == 'succeeded', result
    assert result['steps'][0]['output']['artifacts'][0]['media_type'] == 'audio/mpeg'
    revised = hub.model_catalog.install_operation({'operation_id': row['operation']['id'],
        'model': 'tts-1', 'defaults': {'body': {'input': 'updated text', 'voice': 'alloy'}}})
    assert revised['manifest']['revision'] == row['manifest']['revision'] + 1
    assert revised['manifest']['source']['revision'] == row['manifest']['source']['revision']
    reused = hub.library.instantiate(revised['manifest']['id'])['step']
    assert reused['input']['body']['input'] == 'updated text'


async def test_catalog_text_dialects_preserve_model_ids_and_parameters(api, monkeypatch):
    url, hub = api
    remote, received = FastAPI(), []
    @remote.get('/v1/models')
    async def models():
        return {'data': [
            {'id':'test-reasoner', 'model_type':'对话', 'supported_endpoint_types':['openai','openai-response']},
            {'id':'test-chat', 'model_type':'对话', 'supported_endpoint_types':['openai']},
            {'id':'test-native', 'model_type':'对话', 'supported_endpoint_types':['anthropic']}]}
    @remote.get('/api/pricing')
    async def pricing():
        return {'supported_endpoint': hub.model_catalog.snapshot['endpoint_types']}
    @remote.post('/v1/{dialect}')
    async def native(dialect: str, request: Request):
        body = await request.json()
        received.append(body)
        if dialect == 'responses':
            assert body['reasoning'] == {'effort':'low'} and body['store'] is False
            return {'output':[{'type':'message','content':[{'type':'output_text','text':'ok'}]}]}
        if dialect == 'messages':
            assert request.headers['anthropic-version'] == '2023-06-01'
            return {'content':[{'type':'text','text':'ok'}]}
        raise AssertionError(dialect)
    @remote.post('/v1/chat/completions')
    async def chat(request: Request):
        body = await request.json()
        received.append(body)
        assert body['temperature'] == .2
        return {'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]}
    monkeypatch.setenv('TEST_PROTOCOL_KEY', 'fixture-key')
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url, timeout=30) as client:
        discover = await client.post('/v1/studio/model-catalog/discover', json={'base_url':endpoint+'/v1', 'api_key_env':'TEST_PROTOCOL_KEY'})
        assert discover.status_code == 200
        for name, params in [('test-reasoner', {'reasoning':{'effort':'low'}}), ('test-chat', {'temperature':.2}), ('test-native', {})]:
            connection = await client.post('/v1/studio/model-catalog/connect', json={'model':name})
            assert connection.status_code == 200, connection.text
            result = await hub.models.generate(ModelRequest(model=name, prompt='hello', parameters=params))
            assert result.text == 'ok' and received[-1]['model'] == name
        assert (await client.post('/v1/studio/model-catalog/connect', json={'model':'test-chat','dialect':'anthropic'})).status_code == 422


async def test_native_http_error_receipt_redacts_credentials_and_inputs(hub):
    remote = FastAPI()
    @remote.post('/reject')
    async def reject(request: Request):
        return Response(json.dumps({'error': {'code': 'invalid_size', 'message':
            'Unsupported size. private-prompt fixture-secret sk-othersecret https://example.test/private'}}),
            status_code=422, media_type='application/json',
            headers={'x-api-request-id': 'trace-fixture-42', 'x-request-id': 'fixture-secret'})
    async with live_server(remote) as endpoint:
        _, handler = build_http_tool({'name':'media.reject', 'description':'diagnostics fixture',
            'url':endpoint+'/reject', 'method':'POST', 'api_key':'fixture-secret'})
        run_id=hub.submit({'name':'diagnostic record', 'steps':[{'id':'receipt','target':'core.echo'}]})
        with pytest.raises(ValueError, match='HTTP 422'):
            await handler({'prompt':'private-prompt'}, InvocationContext('failure',run_id,'receipt',store=hub.store))
        events=hub.store.events(run_id)
        encoded=json.dumps(events)
        assert 'Unsupported size.' in encoded and 'http.error' in encoded
        assert 'invalid_size' in encoded and 'trace-fixture-42' in encoded
        assert all(secret not in encoded for secret in ('private-prompt','fixture-secret','sk-othersecret','example.test'))
