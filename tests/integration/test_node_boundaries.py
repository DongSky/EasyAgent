"""Public node/subworkflow boundaries, persistence, and local session ownership."""
import copy
import json

import httpx
import pytest

from easyagent import Agent, Node, Runtime, Sequential, Subflow, node
from easyagent.component_packages import export_package, import_package
from easyagent.nodes import NodeDefinition
from easyagent.runtime import Hub
from easyagent_client import RunStopped


def words(text):
    return [word.casefold() for word in text.split()]


@node
def summarize(text: str) -> dict:
    tokens = words(text)
    return {'text': ' '.join(tokens), 'count': len(tokens)}


@node
def describe(value: dict) -> str:
    return f"{value['text']} ({value['count']})"


async def test_node_composes_operations_while_only_explicit_subflow_creates_child(tmp_path):
    assert isinstance(summarize, Node)
    single = summarize.workflow()
    assert len(single.steps) == 1 and single.steps[0].kind == 'tool'
    flat = Sequential(summarize, describe)
    nested = Sequential(Subflow(flat), Agent('mock'))
    assert [s.kind for s in flat.workflow().steps] == ['tool', 'tool']
    assert [s.kind for s in nested.workflow().steps] == ['subworkflow', 'agent']
    assert len(nested.workflow().steps[0].body['steps']) == 2
    async with Runtime(tmp_path/'nodes.db') as runtime:
        result = await runtime.arun(flat, '  Hello WORLD ')
        assert result.value == 'hello world (2)'
        assert not runtime.hub.store.run(result.id)['children']
        result = await runtime.arun(nested, '  Hello WORLD ')
        assert result.value == 'hello world (2)'
        children = runtime.hub.store.run(result.id)['children']
        assert len(children) == 1
        assert len(runtime.hub.store.run(children[0]['id'])['steps']) == 2


async def test_child_node_approval_survives_restart_and_requires_original_code(tmp_path):
    calls = []
    @node(effect='write')
    async def send(text: str) -> str:
        calls.append(text)
        return text.upper()
    flow = Subflow(Sequential(send, Agent('mock')))
    database = tmp_path/'approval.db'
    async with Runtime(database) as runtime:
        with pytest.raises(RunStopped) as stopped:
            await runtime.arun(flow, 'hello')
        state = stopped.value.state
        assert state['status'] == 'waiting_approval' and calls == []
        runtime.hub.tools.approve(runtime.hub.store, state['approvals'][0]['id'], True)
    async with Runtime(database) as runtime:
        with pytest.raises(ValueError, match='original @tool'):
            await runtime.aresume(state['id'])
        runtime.bind(flow)
        assert (await runtime.aresume(state['id'])).value == 'HELLO'
        assert calls == ['hello']


async def test_local_runtime_only_claims_selected_tree_and_skips_global_services(tmp_path, monkeypatch):
    database = tmp_path/'shared.db'
    owner = Hub(database)
    queued = owner.submit({'name': 'other script', 'steps': [{'id':'other', 'target':'core.echo'}]})
    expired = owner.submit({'name': 'expired other script', 'limits': {'wall_time_seconds': 1},
                           'steps': [{'id':'other', 'target':'core.echo'}]})
    with owner.store.transaction() as db:
        db.execute('UPDATE runs SET created=0 WHERE id=?', (expired,))
    async with Runtime(database) as runtime:
        async def forbidden():
            pytest.fail('local sessions must not deliver unrelated workspace messages')
        monkeypatch.setattr(runtime.hub.connections, 'tick', forbidden)
        result = await runtime.arun(Subflow(Sequential(summarize, describe)), 'hello')
        assert result.value == 'hello (1)'
        names = {task.get_name() for task in runtime.hub.workers}
        assert not names & {'eah-delivery', 'eah-scheduler', 'eah-maintenance'}
        for identifier in (queued, expired):
            assert runtime.hub.store.run(identifier)['status'] == 'queued'
            assert not any(e['kind'] == 'step.started' for e in runtime.hub.store.events(identifier))


async def test_library_node_is_one_step_and_packaged_without_workflow(api, tmp_path):
    url, hub = api
    async with httpx.AsyncClient(base_url=url) as client:
        initial = (await client.get('/v1/library')).json()
        assert next(r for r in initial if r['id']=='library.json.receipt')['component_type'] == 'node'
        item = (await client.post('/v1/library/library.json.receipt/instantiate', json={
            'step_id':'save', 'input':{'value':{'message':'hello'}}})).json()
        assert item['component_type'] == 'node'
        assert item['step']['kind'] == 'artifact'
        result = await hub.wait(hub.submit({'name':'receipt', 'steps':[item['step']]}))
        assert result['status'] == 'succeeded' and result['children'] == []
        assert json.loads(hub.artifacts.get(result['steps'][0]['output']['id'])[1]) == {'message':'hello'}
        source = (await client.get('/v1/library/sources')).json()
        assert next(r for r in source if r['id']=='library.json.receipt')['kind'] == 'node'
    package = export_package(hub, 'library.json.receipt').model_dump()
    assert {r['kind'] for r in package['definitions']} == {'node', 'component'}
    target = Hub(tmp_path/'imported.db', poll_seconds=.01)
    import_package(target, {'package':package})
    await target.start()
    try:
        item = target.library.instantiate('library.json.receipt', {'input':{'value':[1,2]}})
        result = await target.wait(target.submit({'name':'imported', 'steps':[item['step']]}))
        assert result['status'] == 'succeeded' and result['children'] == []
    finally:
        await target.stop()


async def test_save_publish_node_validation_versioning_and_symbolic_inputs(api):
    url, hub = api
    definition = {'step':{'id':'clean', 'kind':'transform', 'input':{'name':{'$ref':'$input.person.name'}}},
                  'input_schema':{'type':'object', 'properties':{'person':{'type':'object'}}, 'required':['person']}}
    async with httpx.AsyncClient(base_url=url) as client:
        saved = await client.post('/v1/library/nodes', json={'id':'example.clean', 'definition':definition})
        assert saved.status_code == 201, saved.text
        published = await client.post('/v1/library/publish', json={'id':'example.clean', 'kind':'node',
            'source_id':'example.clean', 'source_revision':1, 'title':'Clean', 'description':'Select name'})
        assert published.status_code == 201, published.text
        instance = hub.library.instantiate('example.clean', {'input':{'person':{'$ref':'source.person'}}})
        step = instance['step']
        assert step['input']['name'] == {'$ref':'source.person.name'}
        step['depends_on'] = ['source']
        result = await hub.wait(hub.submit({'name':'references', 'steps':[
            {'id':'source', 'target':'core.echo', 'input':{'person':{'name':'Alice'}}}, step]}))
        assert result['status'] == 'succeeded' and result['steps'][1]['output'] == {'name':'Alice'}
        newer = copy.deepcopy(definition)
        newer['step']['input'] = {'name':'updated'}
        response = await client.post('/v1/library/nodes', json={'id':'example.clean', 'definition':newer, 'expected_revision':1})
        assert response.status_code == 201
        assert hub.library.instantiate('example.clean', {'input':{'person':{'name':'original'}}})['step']['input'] == {'name':'original'}
        for step in ({'id':'bad','kind':'subworkflow','body':{'name':'hidden','steps':[{'id':'echo','target':'core.echo'}]}},
                     {'id':'bad','depends_on':['outside']}, {'id':'bad','body':{}},
                     {'id':'bad','input':{'value':{'$ref':'outside.value'}}}):
            response = await client.post('/v1/library/nodes', json={'id':'bad.node','definition':{'step':step}})
            assert response.status_code == 422, response.text


async def test_saved_workflows_remain_subworkflows_even_with_only_one_step(hub):
    hub.library.saved_workflow('old.receipt', 'Legacy receipt', {'type':'object'}, {},
        [{'id':'echo','target':'core.echo','input':{'value':'legacy'}}], 'One-step legacy workflow')
    item = hub.library.instantiate('old.receipt')
    assert item['component_type'] == 'subworkflow' and item['step']['kind'] == 'subworkflow'
    result = await hub.wait(hub.submit({'name':'legacy', 'steps':[item['step']]}))
    assert result['status'] == 'succeeded' and len(result['children']) == 1


async def test_node_cannot_accidentally_launch_nested_runtime(tmp_path):
    @node
    async def invalid(text: str) -> str:
        return await Agent('mock').acall(text)
    async with Runtime(tmp_path/'invalid.db') as runtime:
        with pytest.raises(RunStopped) as stopped:
            await runtime.arun(invalid, 'hello')
        assert 'ordinary Python helpers' in str(stopped.value.state['errors'])
        assert not runtime.hub.store.run(stopped.value.run_id)['children']


def test_node_definition_preserves_literal_schema_refs():
    schema = {'$defs': {'Name': {'type':'string'}}, '$ref':'#/$defs/Name'}
    definition = NodeDefinition(step={'id':'input','kind':'input','input':{'schema':schema}})
    assert definition.instantiate('ask', {}).input['schema'] == schema


async def test_builtin_upgrade_keeps_pinned_legacy_workflow_and_old_manifest(tmp_path):
    database = tmp_path/'legacy.db'
    hub = Hub(database)
    identifier = 'library.json.receipt'
    schema = {'type':'object', 'properties': {'value': {}, 'filename': {'type':'string','minLength':1,'title':'文件名'}},
              'required':['value','filename'], 'additionalProperties':False}
    hub.library.saved_workflow(identifier, '保存 JSON 回执', schema, {'value':{},'filename':'receipt.json'},
        [{'id':'receipt','kind':'artifact','input':{'name':{'$ref':'$input.filename'},
          'content':{'$ref':'$input.value'},'media_type':'application/json'}}], 'legacy')
    old_workflow = hub.development.get('workflow', identifier, 1)
    restored = Hub(database, poll_seconds=.01)
    assert restored.library.get(identifier).revision == 2
    assert restored.library.get(identifier).source.kind == 'node'
    assert restored.library.get(identifier, 1).source.kind == 'workflow'
    assert restored.development.get('workflow', identifier, 1) == old_workflow
    again = Hub(database)
    assert again.library.get(identifier).revision == 2
    await restored.start()
    try:
        for revision, children in [(1, 1), (2, 0)]:
            item = restored.library.instantiate(identifier, {'revision':revision, 'input':{'value':{'version':revision}}})
            run = await restored.wait(restored.submit({'name':'pinned', 'steps':[item['step']]}))
            assert run['status'] == 'succeeded' and len(run['children']) == children
    finally:
        await restored.stop()


async def test_library_browser_distinguishes_node_from_subworkflow(api):
    from playwright.async_api import async_playwright, expect
    url, hub = api
    hub.library.install_local()
    hub.library.saved_workflow('example.flow', '示例子工作流', {'type':'object'}, {},
        [{'id':'echo','target':'core.echo'}], '保留独立步骤')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={'width':1280,'height':900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(url+'/#workflow')
            await page.locator('.node-library > summary').click()
            await expect(page.locator('#nodeLibraryList .library-card')).to_have_count(8)
            await page.locator('#libraryType').select_option('subworkflow')
            await expect(page.locator('#nodeLibraryList .library-card')).to_have_count(1)
            await expect(page.locator('#nodeLibraryList')).to_contain_text('示例子工作流')
            await page.locator('#libraryType').select_option('node')
            await expect(page.locator('#nodeLibraryList .library-card')).to_have_count(7)
            await page.locator('#librarySearch').fill('回执')
            await expect(page.locator('#nodeLibraryList .library-card')).to_have_count(1)
            await page.locator('[data-library-id="library.json.receipt"]').click()
            await expect(page.locator('#graphEditor .graph-node')).to_contain_text(['保存结果'])
            await page.locator('#publishLibrary').click()
            await expect(page.locator('#componentPublishDialog')).to_be_visible()
            options = await page.locator('#componentSource option').all_text_contents()
            workflow_index = next(i for i, text in enumerate(options) if '示例子工作流' in text)
            await page.locator('#componentSource').select_option(str(workflow_index))
            await expect(page.locator('#componentSourceType')).to_contain_text('保存为：子工作流')
            await page.locator('#closeComponentPublish').click()
            await page.set_viewport_size({'width':390,'height':844})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert errors == []
        finally:
            await browser.close()


async def test_node_template_pins_api_and_supports_whole_input_binding(hub, tmp_path):
    api = hub.development.save_api({'name':'example.read','description':'Versioned read',
        'url':'http://127.0.0.1:1/one', 'input_schema':{'type':'object'}})
    saved = hub.library.save_node({'id':'example.node', 'definition':{
        'step':{'id':'call','target':'example.read','input':{'$ref':'$input.arguments'}},
        'defaults':{'arguments':{}}, 'input_schema':{'type':'object'}}})
    hub.library.publish({'id':'example.node','kind':'node','source_id':'example.node',
        'source_revision':saved['revision'], 'title':'Pinned API','description':'Calls the saved version'})
    hub.development.save_api({**api['definition'],'url':'http://127.0.0.1:1/two'},1)
    step = hub.library.instantiate('example.node', {'input':{'arguments':{'hello':'world'}}})['step']
    assert step['tool_revision'] == 1 and step['input'] == {'hello':'world'}
    symbolic = hub.library.instantiate('example.node', {'input':{'arguments':{'$ref':'source'}}})['step']
    assert symbolic['input'] == {'$ref':'source'}
    package = export_package(hub, 'example.node').model_dump()
    assert {r['kind'] for r in package['definitions']} == {'api','node','component'}
    target = Hub(tmp_path/'pinned.db')
    import_package(target, {'package':package})
    assert target.development.get('api','example.read')['definition']['url'].endswith('/one')
