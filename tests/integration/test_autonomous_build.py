"""New tasks create executable capabilities; models here are explicit protocol fixtures."""
import base64
import json

import httpx
import pytest
from fastapi import FastAPI, Request

from conftest import live_server
from easyagent.assistant_builder import build_status, start_build
from easyagent.contracts import ModelResult, ToolSpec
from easyagent.models import HTTPProvider
from easyagent.runtime import Hub


def assistant(purpose):
    return {'construction': 'automatic', 'name': '新任务', 'purpose': purpose,
            'model': 'planner', 'limits': {}}


class CSVBuilder:
    def __init__(self):
        self.namespace = None
        self.calls = []

    async def generate(self, request, model):
        context = json.loads(next(m['content'] for m in request.messages
                                  if m['role'] == 'user' and m['content'].lstrip().startswith('{')))
        self.calls.append(request)
        if request.response_schema['title'] == 'DispatchDecision':
            return ModelResult(data={'action': 'use', 'candidate': context['catalog'][0]['key'],
                                     'confidence': 1, 'inputs': {}, 'message': '复用已验证的汇总流程。'})
        if request.response_schema['title'] == 'IndependentChecks':
            assert 'draft' not in context and 'files' not in context
            name = context['tools'][0]['name']
            return ModelResult(data={'scenarios': [
                {'tool': name, 'input': {'csv': 'customer,amount\nAlice,2\nAlice,3\nBob,4'},
                 'expected': {'totals': {'Alice': 5, 'Bob': 4}}},
                {'tool': name, 'input': {'csv': 'customer,amount'}, 'expected': {'totals': {}}}]})
        self.namespace = context['code_namespace']
        name = self.namespace + '.aggregate'
        revision = context.get('revision', context.get('code_revision', 1))
        source = ('function handle(r) {if(r.method.startsWith("lifecycle."))return {result:{}};'
                  'const totals={};for(const line of r.params.csv.split("\\n").slice(1)){'
                  'if(!line.trim())continue;const [name,amount]=line.split(",");'
                  'totals[name]=(totals[name]||0)+Number(amount);}return {result:{totals}};}')
        if revision == 1:
            source = 'function handle(r){return {result:{totals:{}}};}'
        code = {'manifest': {'id': self.namespace, 'revision': revision, 'title': '按客户汇总 CSV',
                 'tools': [{'handler': 'aggregate', 'spec': {'name': name, 'description': 'Aggregate CSV amounts by customer',
                     'input_schema': {'type': 'object', 'properties': {'csv': {'type': 'string'}}, 'required': ['csv']},
                     'output_schema': {'type': 'object', 'properties': {'totals': {'type': 'object'}}, 'required': ['totals']}}}]},
                'files': {'extension.js': source},
                'scenarios': [{'tool': name, 'input': {'csv': 'customer,amount'}, 'expected': {'totals': {}}}]}
        return ModelResult(data={'workflow': {'name': 'CSV 汇总', 'steps': [
            {'id': 'aggregate', 'target': name, 'tool_revision': revision,
             'input': {'csv': {'$ref': '$input.message'}}}]},
            'explanation': '创建节点、验证边界输入，再汇总 CSV。', 'questions': [], 'code_candidate': code})


async def test_new_node_is_independently_tested_repaired_persisted_and_reused(hub):
    model = CSVBuilder()
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('Parse customer,amount CSV and sum amounts for each customer. Empty CSV gives empty totals.')
    run = await hub.wait(start_build(hub, 'csv', body)['id'])
    assert run['status'] == 'succeeded', run
    plan = build_status(hub, 'csv', body)
    assert plan['status'] == 'ready', plan
    assert [r['passed'] for r in plan['development']] == [False, True]
    repair = json.loads(model.calls[-1].messages[1]['content'])
    failed = next(r for r in repair['report']['results'] if not r['passed'])
    assert failed['input']['csv'] and failed['expected'] == {'totals': {'Alice': 5, 'Bob': 4}}
    assert failed['output'] == {'totals': {}}
    assert run['usage']['model_calls'] == 3
    assert len(hub.code.list()) == 2
    assert next(c for c in hub.code.list() if c['status'] == 'published')['package']['manifest']['revision'] == 2
    skill = hub.skill_packages.get(model.namespace.replace('_', '-'))
    assert 'references/checks.json' in skill['package']['files']
    # This is a single executable node, not a disguised nested workflow.
    assert plan['workflow']['steps'][0]['kind'] == 'tool'
    assert hub.library.get(model.namespace + '.aggregate').component_type == 'node'
    restored = Hub(hub.store.path, poll_seconds=.01)
    await restored.start()
    try:
        workflow = plan['workflow'] | {'inputs': {'message': 'customer,amount\nCarol,1.5\nCarol,2.5'}}
        result = await restored.wait(restored.submit(workflow))
        assert result['status'] == 'succeeded', result
        assert result['steps'][0]['output'] == {'totals': {'Carol': 4}}
        assert len(model.calls) == 3
    finally:
        await restored.stop()


async def test_rebuild_publishes_new_revision_without_mutating_saved_consumers(hub):
    model = CSVBuilder()
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('Aggregate CSV by customer.')
    await hub.wait(start_build(hub, 'rebuild', body)['id'])
    original = build_status(hub, 'rebuild', body)
    assert original['workflow']['steps'][0]['tool_revision'] == 2
    await hub.wait(start_build(hub, 'rebuild', body)['id'])
    rebuilt = build_status(hub, 'rebuild', body)
    assert rebuilt['status'] == 'ready', rebuilt
    assert rebuilt['workflow']['steps'][0]['tool_revision'] == 3
    assert original['workflow']['steps'][0]['tool_revision'] == 2
    run = await hub.wait(hub.submit(original['workflow'] | {'inputs': {'message': 'customer,amount\nAlice,4'}}))
    assert run['status'] == 'succeeded'
    assert run['steps'][0]['output'] == {'totals': {'Alice': 4}}


async def test_chat_creates_missing_node_finishes_task_and_reuses_it(api):
    from test_workspace_chat import settled
    url, hub = api
    model = CSVBuilder()
    hub.models.register('planner', model, 'fixture', ['decision'])
    async with httpx.AsyncClient(base_url=url) as client:
        conversation = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'planner'})).json()
        path = f"/v1/conversations/{conversation['id']}/messages"
        response = await client.post(path, json={'text': 'customer,amount\nAlice,2\nAlice,3', 'intent': 'create'})
        assert response.status_code == 202
        completed = await settled(hub, conversation['id'])
        task = completed['turns'][-1]['task']
        assert task['phase'] == 'completed', completed
        assert hub.store.run(task['run_id'])['steps'][0]['output'] == {'totals': {'Alice': 5}}
        published = len(hub.code.list())
        await client.post(path, json={'text': 'customer,amount\nBob,7', 'intent': 'workflow',
                                     'workflow': task['selected']['key']})
        again = await settled(hub, conversation['id'])
        result = again['turns'][-1]['task']
        assert result['phase'] == 'completed', again
        assert hub.store.run(result['run_id'])['steps'][0]['output'] == {'totals': {'Bob': 7}}
        assert len(hub.code.list()) == published


async def test_failed_generated_code_is_never_published(hub):
    class AlwaysBroken(CSVBuilder):
        async def generate(self, request, model):
            result = await super().generate(request, model)
            if code := result.data.get('code_candidate'):
                code['files']['extension.js'] = 'function handle(r){return {result:{totals:{}}};}'
            return result
    hub.models.register('planner', AlwaysBroken(), 'fixture', ['decision'])
    body = assistant('Sum CSV amounts by customer.')
    body['limits'] = {'model_calls': 6}
    run = await hub.wait(start_build(hub, 'broken', body)['id'])
    assert run['status'] == 'failed', run
    plan = build_status(hub, 'broken', body)
    assert plan['status'] == 'failed'
    assert run['usage']['model_calls'] == 6
    assert len(hub.code.list()) > 3
    assert all(c['status'] == 'failed' for c in hub.code.list())
    assert not hub.extensions.active and not hub.skill_packages.list()


@pytest.mark.parametrize('problem', ['too_many', 'invalid_output'])
async def test_invalid_test_generation_retries_tests_before_changing_code(hub, problem):
    class ExcessChecks(CSVBuilder):
        check_calls = 0

        async def generate(self, request, model):
            result = await super().generate(request, model)
            if request.response_schema['title'] == 'IndependentChecks':
                self.check_calls += 1
                if self.check_calls == 1:
                    if problem == 'too_many':
                        result.data['scenarios'] *= 7
                    else:
                        result.data['scenarios'][0]['expected'] = None
            return result
    model = ExcessChecks()
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('Aggregate CSV by customer.')
    run = await hub.wait(start_build(hub, 'checks', body)['id'])
    assert run['status'] == 'succeeded', run
    plan = build_status(hub, 'checks', body)
    assert plan['status'] == 'ready', plan
    assert model.check_calls == 2
    assert [r['passed'] for r in plan['development']] == [False, True]


async def test_repair_can_correct_pure_code_effect_without_changing_interfaces(hub):
    class WrongEffect(CSVBuilder):
        async def generate(self, request, model):
            result = await super().generate(request, model)
            if code := result.data.get('code_candidate'):
                if code['manifest']['revision'] == 1:
                    code['manifest']['tools'][0]['spec']['effect'] = 'local'
            return result
    model = WrongEffect()
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('Aggregate CSV by customer.')
    run = await hub.wait(start_build(hub, 'effects', body)['id'])
    assert run['status'] == 'succeeded', run
    plan = build_status(hub, 'effects', body)
    assert plan['status'] == 'ready', plan
    assert [(r['attempt'], r['passed']) for r in plan['development']] == [(2, True)]
    assert hub.tools.entries[model.namespace + '.aggregate'][0].effect == 'read'
    assert sum(r.response_schema['title'] == 'IndependentChecks' for r in model.calls) == 1


async def test_chat_repairs_actual_failure_without_repeating_completed_write(api):
    from test_workspace_chat import settled
    url, hub = api
    effects = []

    async def write(args, ctx):
        effects.append(args)
        return {'receipt': 'real-local-write'}

    async def calculate(args, ctx):
        if args['value'] != 42:
            raise ValueError('Expected the answer to be 42')
        return {'value': 42}

    hub.tools.register(ToolSpec(name='fixture.write', effect='write', idempotent=False), write)
    hub.tools.register(ToolSpec(name='fixture.calculate'), calculate)

    class RepairBuilder:
        async def generate(self, request, model):
            context = json.loads(request.messages[-1]['content'])
            repaired = bool(context.get('execution_feedback'))
            return ModelResult(data={'workflow': {'name': '计算任务', 'steps': [
                {'id': 'write', 'target': 'fixture.write', 'input': {'value': 'once'}},
                {'id': 'calculate', 'target': 'fixture.calculate', 'depends_on': ['write'],
                 'max_attempts': 1, 'input': {'value': 42 if repaired else 0}}]},
                'explanation': '依据失败结果修正输入。', 'questions': []})

    hub.models.register('planner', RepairBuilder(), 'fixture', ['decision'])
    async with httpx.AsyncClient(base_url=url) as client:
        conversation = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'planner'})).json()
        await client.post(f"/v1/conversations/{conversation['id']}/messages", json={'text': '先保存一次，再计算答案', 'intent': 'create'})
        waiting = await settled(hub, conversation['id'], status='waiting_approval')
        pending = hub.store.run(waiting['active_run'])
        hub.tools.approve(hub.store, pending['approvals'][0]['id'], True)
        done = await settled(hub, conversation['id'])
        task = done['turns'][-1]['task']
        assert task['phase'] == 'completed', task
        assert hub.store.run(task['run_id'])['steps'][-1]['output'] == {'value': 42}
        assert effects == [{'value': 'once'}]


async def test_connected_image_model_gets_edit_node_without_user_schema(api):
    url, hub = api
    remote = FastAPI()
    original = b'original-image-fixture'
    edited = b'edited-image-fixture'
    requests = []

    @remote.post('/v1/images/edits')
    async def edit(request: Request):
        body = await request.body()
        assert original in body and b'gpt-image-2' in body
        assert request.headers['authorization'] == 'Bearer image-fixture-key'
        requests.append(body)
        return {'data': [{'b64_json': base64.b64encode(edited).decode()}]}

    class EditBuilder:
        async def generate(self, request, model):
            context = json.loads(request.messages[-1]['content'])
            assert 'image-fixture-key' not in request.model_dump_json()
            edit = next(t for t in context['available_tools'] if t['name'].endswith('.edits'))
            return ModelResult(data={'workflow': {'name': '自然修图', 'steps': [
                {'id': 'edit', 'target': edit['name'], 'input': {'body': {
                    'model': 'gpt-image-2', 'image': {'$ref': '$input.attachment_ids.0'},
                    'prompt': {'$ref': '$input.message'}, 'n': 1}}}]},
                'explanation': '直接编辑原图并保留原件。', 'questions': []})

    async with live_server(remote) as endpoint, httpx.AsyncClient(base_url=url) as client:
        hub.models.register('planner', EditBuilder(), 'fixture', ['decision'])
        hub.models.register('image', HTTPProvider(endpoint+'/v1', 'image-fixture-key'), 'gpt-image-2', ['image'])
        upload = (await client.post('/v1/artifacts/upload?name=portrait.png', content=original,
                                   headers={'Content-Type': 'image/png'})).json()
        body = assistant('自然重打光，压暗背景，保留皮肤纹理，提升自然饱和度。')
        run = await hub.wait(start_build(hub, 'photo', body)['id'])
        assert run['status'] == 'succeeded', run
        plan = build_status(hub, 'photo', body)
        assert plan['status'] == 'ready', plan
        assert not requests
        flow = plan['workflow'] | {'inputs': {'message': body['purpose'], 'attachment_ids': [upload['id']]}}
        pending = await hub.wait(hub.submit(flow))
        assert pending['status'] == 'waiting_approval' and not requests
        hub.tools.approve(hub.store, pending['approvals'][0]['id'], True)
        result = await hub.wait(pending['id'])
        assert result['status'] == 'succeeded', result
        artifact = result['steps'][0]['output']['artifacts'][0]
        assert hub.artifacts.get(artifact['id'])[1] == edited
        assert hub.artifacts.get(upload['id'])[1] == original
        assert len(requests) == 1
        assert 'image-fixture-key' not in json.dumps(result)


@pytest.mark.parametrize('invalid_headers', [False, True])
async def test_missing_api_searches_reads_docs_and_authored_adapter_runs(api, monkeypatch, invalid_headers):
    from easyagent import capability_research
    url, hub = api
    remote = FastAPI()
    searches, reads, calls = [], [], []

    @remote.get('/convert')
    async def convert(value: int):
        calls.append(value)
        return {'value': value * 2}

    async def search(query):
        searches.append(query)
        return {'results': [{'url': 'https://docs.example.test/conversion', 'title': 'Conversion API'}]}

    async def document(url):
        reads.append(url)
        return {'url': url, 'text': 'GET /convert?value=integer returns JSON {value: integer}, twice the input.',
                'untrusted_reference': True}

    monkeypatch.setattr(capability_research, 'public_search', search)
    monkeypatch.setattr(capability_research, 'fetch_document', document)

    class DiscoverBuilder:
        repaired = False

        async def generate(self, request, model):
            context = json.loads(request.messages[-1]['content'])
            if request.response_schema['title'] == 'BuildEdits':
                assert 'generated definitions cannot supply credentials or headers' in context['feedback']
                self.repaired = True
                return ModelResult(data={'edits': [{'op': 'set', 'path': ['api_candidates', 0, 'definition', 'headers'],
                                                   'value': {}}], 'done': True})
            if 'research_evidence' not in context:
                return ModelResult(data={'workflow': None, 'questions': [], 'explanation': '查找转换接口文档。',
                    'research_queries': ['conversion API documentation']})
            name = context['code_namespace'] + '.convert'
            return ModelResult(data={'workflow': {'name': '使用新接口', 'steps': [
                {'id': 'convert', 'target': name, 'input': {'value': 21}}]}, 'explanation': '已阅读接口文档并创建适配节点。',
                'questions': [], 'api_candidates': [{'source_url': reads[0], 'service': 'service', 'definition': {
                    'name': name, 'description': 'Double input using documented API', 'url': endpoint+'/convert',
                    'headers': {'Accept': 'application/json'} if invalid_headers else {},
                    'input_schema': {'type': 'object', 'properties': {'value': {'type': 'integer'}}, 'required': ['value']}}}]})

    async with live_server(remote) as endpoint:
        provider = DiscoverBuilder()
        hub.models.register('planner', provider, 'fixture', ['decision'])
        hub.models.register('service', HTTPProvider(endpoint), 'fixture-service', ['chat'])
        body = assistant('调用转换服务计算 21 的两倍')
        run = await hub.wait(start_build(hub, 'discovery', body)['id'])
        assert run['status'] == 'succeeded', run
        plan = build_status(hub, 'discovery', body)
        assert plan['status'] == 'ready', plan
        result = await hub.wait(hub.submit(plan['workflow']))
        assert result['steps'][0]['output'] == {'value': 42}
        assert searches and reads and calls == [21]
        assert provider.repaired == invalid_headers
