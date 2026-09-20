"""Real HTTP servers and durable workflow execution; media bytes are protocol fixtures."""
import asyncio
import copy
import json

import httpx
import pytest
from fastapi import FastAPI, Request, Response

from conftest import live_server
from easyagent.components import CodePackage, digest
from easyagent.runtime import Hub
from easyagent.store import Conflict


async def until(hub, run_id, predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            run = hub.store.run(run_id)
            if predicate(run):
                return run
            await asyncio.sleep(.01)


async def approve_all(hub, run_id):
    run = await hub.wait(run_id)
    assert run['status'] == 'waiting_approval', run
    for approval in run['approvals']:
        hub.tools.approve(hub.store, approval['id'], True)


async def test_library_general_decision_reuse_publish_and_platform_contracts(api, monkeypatch, tmp_path):
    url, hub = api
    remote = FastAPI()
    requests = []
    @remote.post('/decide')
    async def decide(request: Request):
        assert request.headers['authorization'] == 'Bearer protocol-only-key'
        body = await request.json()
        requests.append(body)
        choices = body['questions']['result']['criteria']
        choice = 'support' if body['state'] == '退货请求' else 'urgent'
        return {'model': 'jev-fixture', 'usage': {}, 'answers': {'result': {
            'type': 'choice', 'choice': choice, 'confidence': .95,
            'probabilities': {k: .95 if k == choice else .05 for k in choices}}}}
    monkeypatch.setenv('TYPESAFE_API_KEY', 'protocol-only-key')
    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        created = await client.post('/v1/library/library.typesafe.evaluate/install', json={'endpoint': endpoint+'/decide'})
        assert created.status_code == 201, created.text
        manifest = created.json()
        assert manifest['requirements']['credential_refs'] == ['TYPESAFE_API_KEY']
        rows = (await client.get('/v1/library')).json()
        assert 'protocol-only-key' not in json.dumps(rows)
        assert any(r['id'] == 'library.decision.classify_receipt' and r['available'] for r in rows)
        consumers = []
        for state, choices in [('退货请求', {'support': '售后', 'sales': '销售'}), ('设备过热', {'urgent': '需要立即处理', 'normal': '正常'})]:
            instance = await client.post('/v1/library/library.decision.classify_receipt/instantiate', json={'input': {
                'state': state, 'criteria': choices, 'instructions': '按类别判断', 'filename': 'decision.json'}})
            assert instance.status_code == 200, instance.text
            workflow = {'name': state, 'steps': [instance.json()['step']]}
            consumers.append(workflow)
            run = await hub.wait(hub.submit(workflow))
            assert run['status'] == 'succeeded', run
            result = run['steps'][0]['output']['results'][0]
            assert result['decision']['choice'] == ('support' if state == '退货请求' else 'urgent')
            artifact = result['receipt']
            assert not hub.store.run(run['children'][0]['id'])['children']
            meta, content = hub.artifacts.get(artifact['id'])
            assert json.loads(content)['answers']['result']['choice'] == ('support' if state == '退货请求' else 'urgent')
            assert meta['media_type'] == 'application/json'
        assert requests[0]['questions']['result']['criteria'] != requests[1]['questions']['result']['criteria']
        # Publishing an ordinary saved API creates a reusable manifest with exact source digest.
        definition = copy.deepcopy(hub.development.get('api', 'library.typesafe.evaluate')['definition'])
        definition['description'] += ' v2'
        hub.development.save_api(definition, 1)
        pinned = await hub.wait(hub.submit(consumers[0]))
        assert pinned['status'] == 'succeeded'
        child = hub.store.run(pinned['children'][0]['id'])
        assert child['steps'][0]['spec']['tool_revision'] == 1
        with pytest.raises(KeyError):
            hub.development.get('workflow', 'library.decision.classify_receipt', 0)
        resolved = await client.post('/v1/library/library.decision.classify_receipt/resolve', json={'profiles': [
            {'id': 'iphone', 'platform': 'ios', 'capabilities': ['workflow.v1']},
            {'id': 'my-pc', 'platform': 'linux', 'location': 'remote', **{k: v for k, v in manifest['requirements'].items()
                if k in ('capabilities', 'network_origins', 'credential_refs', 'memory_mb')}}]})
        assert resolved.json()['selected'] is None
        assert any('remote execution' in r for r in resolved.json()['candidates'][1]['reasons'])
        # Same supported manifest does not claim that an iOS runtime is installed.
        raw = (await client.get('/v1/library/library.decision.classify_receipt')).json()
        profiles = [{'id': 'my-pc', 'platform': 'linux', 'location': 'remote',
                     **{k: v for k, v in raw['requirements'].items() if k != 'platforms'}}]
        plan = (await client.post('/v1/library/library.decision.classify_receipt/resolve', json={
            'allow_remote': True, 'profiles': profiles})).json()
        assert plan['selected'] == 'my-pc' and plan['execution_started'] is False
        # Contracts describe code candidates; no shell/VM is launched by validation.
        package = CodePackage(id='example.compute', revision=1, language='python', entrypoint='main.py',
            source_digests={'main.py': digest('print(1)')}, dependency_lock_digest=digest({}),
            requirements={'capabilities': ['python.sandbox']}, integration_scenarios=['compute.json'])
        assert package.state == 'draft'
        with pytest.raises(ValueError):
            CodePackage.model_validate(package.model_dump() | {'entrypoint': '../main.py', 'source_digests': {'../main.py': digest('x')}})
        # Immutable dependency digest is actually checked at instantiation.
        with hub.store.transaction() as db:
            db.execute("UPDATE definition_versions SET body=? WHERE kind='api' AND id=? AND revision=1",
                       (json.dumps(definition), 'library.typesafe.evaluate'))
        with pytest.raises(Conflict, match='digest mismatch'):
            hub.library.instantiate('library.decision.classify_receipt')


async def test_media_submission_polling_restart_and_binary_artifact(tmp_path, monkeypatch):
    counts = {'submit': 0, 'poll': 0, 'speech': 0}
    remote = FastAPI()
    ready = False
    @remote.post('/v1/text_to_video')
    async def submit(request: Request):
        assert request.headers['authorization'] == 'Bearer media-fixture-key'
        assert request.headers['x-runway-version'] == '2024-11-06'
        body = await request.json()
        assert body['model'] == 'gen4.5' and body['duration'] == 2
        counts['submit'] += 1
        return {'id': 'fixture-task'}
    @remote.get('/v1/tasks/{identifier}')
    async def status(identifier: str):
        counts['poll'] += 1
        assert identifier == 'fixture-task'
        return {'id': identifier, 'status': 'SUCCEEDED' if ready else 'RUNNING',
                **({'output': ['https://example.com/protocol-fixture.mp4']} if ready else {})}
    audio = b'ID3\x04\x00\x00protocol-fixture-not-generated-audio'
    @remote.post('/speech/{voice_id}')
    async def speech(voice_id: str, request: Request):
        assert voice_id == 'test-voice' and request.headers['xi-api-key'] == 'audio-fixture-key'
        assert request.query_params['output_format'] == 'mp3_44100_128'
        assert (await request.json())['text'] == '协议测试'
        counts['speech'] += 1
        return Response(audio, media_type='audio/mpeg')
    monkeypatch.setenv('RUNWAYML_API_SECRET', 'media-fixture-key')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'audio-fixture-key')
    async with live_server(remote) as endpoint:
        hub = Hub(tmp_path/'media.db', concurrency=1, poll_seconds=.01)
        hub.library.install('library.runway.video', {'endpoint': endpoint+'/v1/text_to_video'})
        # Keep fixture polling short; production recipe respects the documented five-second interval.
        wait = hub.development.get('api', 'library.runway.wait')['definition']
        wait['polling']['interval_seconds'] = .1
        hub.development.save_api(wait, 1)
        submit_step = hub.library.instantiate('library.runway.video', {'step_id': 'submit', 'input': {
            'promptText': 'Test', 'model': 'gen4.5', 'ratio': '1280:720', 'duration': 2}})['step']
        run_id = hub.submit({'name': 'Durable media task', 'steps': [submit_step,
            {'id': 'wait', 'target': 'library.runway.wait', 'tool_revision': 2, 'depends_on': ['submit'],
             'input': {'id': {'$ref': 'submit.id'}}}]})
        await hub.start()
        await approve_all(hub, run_id)
        await until(hub, run_id, lambda r: r['steps'][1]['status'] == 'waiting_remote')
        # One worker remains free while an external job is running.
        other = await hub.wait(hub.submit({'name': 'Other work', 'steps': [{'id': 'ok', 'target': 'core.echo'}]}))
        assert other['status'] == 'succeeded'
        await hub.stop()
        ready = True
        restored = Hub(tmp_path/'media.db', concurrency=1, poll_seconds=.01)
        await restored.start()
        try:
            complete = await restored.wait(run_id)
            assert complete['status'] == 'succeeded', complete
            assert counts['submit'] == 1 and counts['poll'] >= 2
            assert complete['usage']['tool_calls'] == 2
            assert complete['steps'][1]['attempts'] == 1
            assert complete['steps'][1]['output']['output'] == ['https://example.com/protocol-fixture.mp4']
            restored.library.install('library.elevenlabs.speech', {'endpoint': endpoint+'/speech/{voice_id}'})
            step = restored.library.instantiate('library.elevenlabs.speech', {'input': {
                'voice_id': 'test-voice', 'text': '协议测试', 'model_id': 'eleven_multilingual_v2', 'output_format': 'mp3_44100_128'}})['step']
            audio_run = restored.submit({'name': 'Speech artifact', 'steps': [step]})
            await approve_all(restored, audio_run)
            result = await restored.wait(audio_run)
            assert result['status'] == 'succeeded', result
            info, content = restored.artifacts.get(result['steps'][0]['output']['id'])
            assert content == audio and info['media_type'] == 'audio/mpeg' and counts['speech'] == 1
            assert 'media-fixture-key' not in json.dumps(restored.store.run(run_id))
            assert 'audio-fixture-key' not in json.dumps(result)
        finally:
            await restored.stop()


async def test_polling_failure_cancel_budgets_and_bad_media(hub):
    remote = FastAPI()
    counts = {}
    @remote.get('/job/{mode}')
    async def status(mode: str):
        counts[mode] = counts.get(mode, 0) + 1
        return {'status': {'fail': 'FAILED', 'unknown': 'WHAT'}.get(mode, 'RUNNING'),
                'error': {'code': 'invalid_input', 'message': 'bad reference sk-testsecret https://example.test/private'}}
    @remote.get('/bad-media')
    async def bad_media():
        return Response(b'<html>not audio</html>', media_type='text/html')
    async with live_server(remote) as endpoint:
        definition = {'name': 'fixture.wait', 'description': 'Fixture task status', 'url': endpoint+'/job/{mode}',
            'input_schema': {'type': 'object', 'properties': {'mode': {'type': 'string'}}, 'required': ['mode']},
            'polling': {'pending': ['RUNNING'], 'succeeded': ['SUCCEEDED'], 'failed': ['FAILED'],
                        'interval_seconds': .05, 'max_polls': 3, 'deadline_seconds': 5}}
        hub.development.save_api(definition)
        for mode in ['fail', 'unknown', 'budget']:
            run = await hub.wait(hub.submit({'name': mode, 'steps': [{'id': 'wait', 'target': 'fixture.wait', 'input': {'mode': mode}}]}))
            assert run['status'] == 'failed', run
            assert counts[mode] == (3 if mode == 'budget' else 1)
            events = [e['payload'] for e in hub.store.events(run['id']) if e['kind'] == 'http.poll']
            assert events[-1]['remote_status'] == {'fail': 'FAILED', 'unknown': 'WHAT'}.get(mode, 'RUNNING')
            assert events[-1]['error_code'] == 'invalid_input'
            assert all(secret not in json.dumps(events) for secret in ['sk-testsecret', 'example.test'])
            if mode == 'unknown':
                assert 'WHAT' in run['steps'][0]['error']
        run_id = hub.submit({'name': 'cancel', 'steps': [{'id': 'wait', 'target': 'fixture.wait', 'input': {'mode': 'cancel'}}]})
        await until(hub, run_id, lambda r: r['steps'][0]['status'] == 'waiting_remote')
        hub.store.cancel(run_id)
        count = counts['cancel']
        await asyncio.sleep(.2)
        assert counts['cancel'] == count and hub.store.run(run_id)['status'] == 'cancelled'
        hub.development.save_api({'name': 'fixture.audio', 'description': 'Reject non audio', 'url': endpoint+'/bad-media',
            'response_mode': 'artifact', 'artifact_media_types': ['audio/mpeg']})
        failed = await hub.wait(hub.submit({'name': 'bad content type', 'steps': [{'id': 'audio', 'target': 'fixture.audio'}]}))
        assert failed['status'] == 'failed' and not hub.artifacts.list(failed['id'])
        # A read-only poll config cannot be attached to a task-creation write.
        with pytest.raises(ValueError, match='read-only'):
            hub.development.save_api(definition | {'name': 'fixture.invalid', 'method': 'POST'})


async def test_three_sdks_instantiate_and_execute_library_components(api):
    import os
    from easyagent.client import HubClient
    from test_interop import subprocess_output
    url, hub = api
    async with HubClient(url) as client:
        assert any(row['id'] == 'library.json.receipt' for row in await client.library())
        instance = await client.instantiate('library.json.receipt', inputs={'value': {'sdk': 'python'}, 'filename': 'python.json'})
        run = await client.submit({'name': 'Python library consumer', 'steps': [instance['step']]})
        assert (await client.wait(run['id']))['status'] == 'succeeded'
    code = """import {HubClient} from './sdk/javascript/index.js';
const hub=new HubClient(process.env.EAH_URL);
const c=await hub.instantiate('library.json.receipt',{input:{value:{sdk:'js'},filename:'js.json'}});
const r=await hub.submit({name:'JS library consumer',steps:[c.step]});
console.log(JSON.stringify(await hub.wait(r.id)));"""
    result = json.loads(await subprocess_output(['node', '--input-type=module', '-e', code], {**os.environ, 'EAH_URL': url}))
    assert result['status'] == 'succeeded'
    rust = json.loads(await subprocess_output(['cargo', 'run', '--quiet', '--manifest-path', 'sdk/rust/Cargo.toml',
                                               '--example', 'component'], {**os.environ, 'EAH_URL': url}, timeout=240))
    assert rust['status'] == 'succeeded'
