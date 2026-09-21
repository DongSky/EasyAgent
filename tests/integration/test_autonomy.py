"""Autonomous operator: observe → act → build → delegate → remember inside one durable agent step.

Models here are explicit scripted fixtures that speak native tool calls; they prove the runtime
contract (tool errors become observations, grants are issued automatically, artifacts persist),
not model quality.
"""
import asyncio
import json
import re

import httpx
import pytest

from easyagent.contracts import ModelResult, ToolCall, ToolSpec
from test_workspace_chat import settled

CSV_SOURCE = ('function handle(r) {if(r.method.startsWith("lifecycle."))return {result:{}};'
              'const totals={};for(const line of r.params.csv.split("\\n").slice(1)){'
              'if(!line.trim())continue;const [name,amount]=line.split(",");'
              'totals[name]=(totals[name]||0)+Number(amount);}return {result:{totals}};}')


def namespace_of(request):
    match = re.search(r"'(op_[0-9a-f]{12})_'", request.messages[0]['content'])
    assert match, 'operator instructions must tell the model its code namespace'
    return match.group(1)


def observations(request):
    return [json.loads(m['content']) for m in request.messages if m['role'] == 'tool']


class Scripted:
    """Replays tool calls in order; each step may inspect prior observations."""

    def __init__(self, steps, final='完成。', reflection=None, dispatch=None):
        self.steps, self.final, self.reflection, self.dispatch = steps, final, reflection, dispatch
        self.requests, self.children = [], []

    async def generate(self, request, model):
        self.requests.append(request)
        title = (request.response_schema or {}).get('title')
        if title == 'DispatchDecision':
            return ModelResult(data=self.dispatch(json.loads(request.messages[-1]['content'])))
        if title == 'Reflection':
            return ModelResult(data=self.reflection or {'memory_notes': [], 'skill': None, 'summary': ''})
        if request.messages[0]['content'].startswith('You are a delegated sub-agent'):
            self.children.append(request)
            return ModelResult(text='child verified: ' + request.messages[-1]['content'][:60], usage={'mock': True})
        seen = observations(request)
        if len(seen) < len(self.steps):
            step = self.steps[len(seen)]
            name, arguments = step(request, seen) if callable(step) else step
            return ModelResult(tool_calls=[ToolCall(id=f'call-{len(seen)}', name=name, arguments=arguments)])
        text = self.final(request, seen) if callable(self.final) else self.final
        return ModelResult(text=text, usage={'mock': True})


async def test_operator_builds_node_saves_workflow_remembers_and_delegates(api, monkeypatch):
    from easyagent import capability_research
    url, hub = api
    reads = []

    async def search(query):
        return {'results': [{'url': 'https://docs.example.test/csv', 'title': 'CSV totals'}]}

    async def document(url):
        reads.append(url)
        return {'url': url, 'text': 'Sum the amount column per customer.', 'untrusted_reference': True}

    monkeypatch.setattr(capability_research, 'public_search', search)
    monkeypatch.setattr(capability_research, 'fetch_document', document)

    def code(request, seen):
        ns = namespace_of(request)
        name = ns + '_csv.aggregate'
        return 'code.create', {'manifest': {'id': ns + '_csv', 'revision': 1, 'title': '按客户汇总 CSV', 'tools': [
            {'handler': 'aggregate', 'spec': {'name': name, 'description': 'Aggregate CSV amounts by customer',
             'input_schema': {'type': 'object', 'properties': {'csv': {'type': 'string'}}, 'required': ['csv']},
             'output_schema': {'type': 'object', 'properties': {'totals': {'type': 'object'}}, 'required': ['totals']}}}]},
            'files': {'extension.js': CSV_SOURCE},
            'scenarios': [{'tool': name, 'input': {'csv': 'customer,amount\nAlice,2\nAlice,3'}, 'expected': {'totals': {'Alice': 5}}}]}

    def dispatch(context):
        return {'action': 'use', 'candidate': 'csv-totals@1', 'confidence': 1, 'inputs': {}, 'message': '复用已保存的汇总流程。'}

    model = Scripted([
        ('web.search', {'query': 'csv aggregate by customer'}),
        ('web.read', {'url': 'https://docs.example.test/csv'}),
        code,
        lambda request, seen: ('code.test', {'id': seen[2]['id']}),
        lambda request, seen: ('code.publish', {'id': seen[2]['id']}),
        lambda request, seen: ('tools.call', {'name': seen[4]['tools'][0], 'arguments': {'csv': 'customer,amount\nAlice,2\nAlice,3'}}),
        ('memory.put', {'namespace': 'user', 'key': 'csv-format', 'value': 'customer,amount header'}),
        lambda request, seen: ('workflows.save', {'id': 'csv-totals', 'description': '按客户汇总 CSV 金额', 'workflow': {
            'name': 'CSV 汇总', 'steps': [{'id': 'aggregate', 'target': seen[4]['tools'][0],
                                          'input': {'csv': {'$ref': '$input.message'}}}]}}),
        ('agents.spawn', {'goal': 'Double-check that Alice totals 5', 'model': 'planner', 'tools': ['web.read', 'code.create']}),
        lambda request, seen: ('agents.wait', {'id': seen[8]['id']}),
        ('workflows.run', {'id': 'csv-totals', 'revision': 1,
                           'inputs': {'message': 'customer,amount\nAlice,2\nAlice,3'}}),
    ], final=lambda request, seen: '已完成：Alice 合计 ' + str(seen[5]['totals']['Alice']) + '；流程已保存为 ' + seen[7]['key'],
       reflection={'memory_notes': [{'namespace': 'tasks', 'key': 'csv-approach', 'value': 'aggregate with the published node'}],
                   'skill': {'name': 'csv-totals', 'description': 'Sum CSV amounts per customer', 'body': '1. Publish the aggregate node.\n2. Save the workflow.\n3. Verify totals.'},
                   'summary': 'built a node'}, dispatch=dispatch)
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    async with httpx.AsyncClient(base_url=url, timeout=30) as client:
        conversation = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'planner'})).json()
        path = f"/v1/conversations/{conversation['id']}/messages"
        response = await client.post(path, json={'text': 'customer,amount\nAlice,2\nAlice,3 — 按客户汇总，并保存成可复用流程',
                                                 'intent': 'create', 'execution': 'automatic'})
        assert response.status_code == 202
        # This chain publishes code, delegates, executes a workflow, and reflects.
        # Verify its results without turning slow CI storage into a 20-second SLA.
        completed = await settled(hub, conversation['id'], timeout=60)
        task = completed['turns'][-1]['task']
        assert task['phase'] == 'completed', task
        assert task['engine'] == 'operator'
        assert completed['messages'][-1]['content'].startswith('已完成：Alice 合计 5')
        run = hub.store.run(task['run_id'])
        assert run['status'] == 'succeeded' and run['execution'] == 'automatic' and not run['approvals']
        ns = namespace_of(model.requests[1])
        assert hub.library.get(ns + '_csv.aggregate').component_type == 'node'
        assert reads == ['https://docs.example.test/csv']
        assert hub.store.memory_search('user', 'csv-format')[0]['value'] == 'customer,amount header'
        assert task['selected'] == {'key': 'csv-totals@1', 'id': 'csv-totals', 'revision': 1, 'title': 'CSV 汇总'}
        assert any(c['key'] == 'csv-totals@1' for c in hub.chat.public_catalog())
        assert [c['status'] for c in run['children']] == ['succeeded', 'succeeded'] and model.children
        child = next(hub.store.run(c['id']) for c in run['children'] if hub.store.run(c['id'])['steps'][0]['id'] == 'agent')
        assert child['spec']['steps'][0]['input']['code_development'] == {'namespace': ns}
        record = hub.store.memory_search('tasks', run['id'])[0]['value']
        assert record['saved_workflows'] == ['csv-totals@1'] and record['published_code'] and 'code.publish' in record['tools']
        reflection = await hub.wait(task['reflection_run'])
        assert reflection['status'] == 'succeeded', reflection
        assert hub.store.memory_search('tasks', 'csv-approach')[0]['value'] == 'aggregate with the published node'
        skill = hub.skill_packages.get('learned-csv-totals')
        assert 'Publish the aggregate node' in skill['package']['files']['SKILL.md']
        assert any(s['name'] == 'learned-csv-totals' for s in hub.skills.catalog())
        # The saved workflow is now matched and executed directly, without another autonomous run.
        before = len(model.requests)
        await client.post(path, json={'text': 'customer,amount\nBob,7', 'execution': 'automatic'})
        again = await settled(hub, conversation['id'])
        result = again['turns'][-1]['task']
        assert result['phase'] == 'completed' and result['selected']['key'] == 'csv-totals@1', result
        assert hub.store.run(result['run_id'])['steps'][0]['output'] == {'totals': {'Bob': 7}}
        assert len(model.requests) == before + 1


async def test_tool_failures_are_observations_and_the_operator_self_corrects(hub):
    calls = []

    async def flaky(args, ctx):
        calls.append(args['value'])
        if args['value'] == 'bad':
            raise RuntimeError('bearer sk-secret-token-1234567890 rejected: value must be good')
        return {'ok': args['value']}

    hub.tools.register(ToolSpec(name='fixture.flaky', input_schema={
        'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}), flaky)

    def corrected(request, seen):
        error = seen[0]['error']
        assert error['code'] == 'RuntimeError' and error['executed'] is True
        assert 'sk-secret' not in error['message'] and 'value must be good' in error['message']
        return 'fixture.flaky', {'value': 'good'}

    def unknown(request, seen):
        assert seen[1] == {'ok': 'good'}
        return 'nope.tool', {}

    def invalid(request, seen):
        assert seen[2]['error']['code'] == 'unknown_tool' and 'fixture.flaky' in seen[2]['error']['available_tools']
        return 'fixture.flaky', {'wrong': 1}

    model = Scripted([('fixture.flaky', {'value': 'bad'}), corrected, unknown, invalid],
                     final=lambda request, seen: 'recovered after ' + seen[3]['error']['code'])
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    c = await hub.conversations.create({'workspace': True, 'model': 'planner'})
    await hub.conversations.send(c['id'], {'text': '试一下工具', 'execution': 'automatic'})
    done = await settled(hub, c['id'])
    task = done['turns'][-1]['task']
    assert task['phase'] == 'completed', task
    assert done['messages'][-1]['content'] == 'recovered after invalid_tool_arguments'
    assert calls == ['bad', 'good']
    kinds = [e['kind'] for e in hub.store.events(task['run_id'])]
    assert 'tool.failed_observed' in kinds and 'tool.unknown_requested' in kinds and 'tool.input_rejected' in kinds


async def test_steer_message_reaches_the_running_operator(hub):
    gate = asyncio.Event()

    async def slow(args, ctx):
        await gate.wait()
        return {'done': True}

    hub.tools.register(ToolSpec(name='fixture.slow'), slow)
    steered = []

    def finish(request, seen):
        steered.extend(m['content'] for m in request.messages if m['role'] == 'user' and '改成英文' in m['content'])
        return '已按补充要求改成英文'

    model = Scripted([('fixture.slow', {})], final=finish)
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    c = await hub.conversations.create({'workspace': True, 'model': 'planner'})
    await hub.conversations.send(c['id'], {'text': '慢慢处理', 'execution': 'automatic'})
    async with asyncio.timeout(10):
        while not hub.chat.working_turn(c['id']):
            await hub.conversations.tick()
            await asyncio.sleep(.02)
    with pytest.raises(ValueError):
        await hub.conversations.send((await hub.conversations.create({'workspace': True, 'model': 'planner'}))['id'],
                                     {'text': 'x', 'mode': 'steer'})
    extra = await hub.conversations.send(c['id'], {'text': '另外，结果改成英文', 'mode': 'steer'})
    gate.set()
    done = await settled(hub, c['id'])
    assert steered and done['turns'][0]['task']['phase'] == 'completed', done['turns']
    assert next(t for t in done['turns'] if t['id'] == extra['id'])['status'] == 'steered'
    assert done['messages'][-1]['content'] == '已按补充要求改成英文'
    run = hub.store.run(done['turns'][0]['task']['run_id'])
    pins = [json.loads(p) for p in run['steps'][0]['state']['pinned_requests']]
    assert {'role': 'user', 'content': '另外，结果改成英文'} in pins


async def test_ask_user_pauses_and_resumes_with_the_answer(api):
    url, hub = api
    model = Scripted([('task.ask_user', {'question': '出发城市是哪里？'})],
                     final=lambda request, seen: '出发城市：' + seen[0]['answer']['answer'])
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    async with httpx.AsyncClient(base_url=url) as client:
        c = (await client.post('/v1/conversations', json={'workspace': True, 'model': 'planner'})).json()
        await client.post(f"/v1/conversations/{c['id']}/messages", json={'text': '帮我订票', 'execution': 'automatic'})
        waiting = await settled(hub, c['id'], status='waiting_input')
        run = hub.store.run(waiting['active_run'])
        assert run['input_requests'][0]['prompt'] == '出发城市是哪里？'
        answer = await client.post('/v1/inputs/' + run['input_requests'][0]['id'], json={'answer': '香港'})
        assert answer.status_code == 200, answer.text
        done = await settled(hub, c['id'])
        assert done['turns'][-1]['task']['phase'] == 'completed'
        assert done['messages'][-1]['content'] == '出发城市：香港'


async def test_missing_service_is_recorded_and_the_task_waits_for_setup(hub):
    model = Scripted([('task.request_connection', {'capability': 'image_edit', 'title': '支持原图编辑的图片模型',
                                                   'reason': '需要接收原图并返回编辑结果。'})],
                     final='请先连接一个支持原图编辑的图片模型，我会继续完成修图。')
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    c = await hub.conversations.create({'workspace': True, 'model': 'planner'})
    await hub.conversations.send(c['id'], {'text': '把这张照片压暗背景', 'execution': 'automatic'})
    async with asyncio.timeout(10):
        while hub.conversations.get(c['id'])['turns'][-1]['status'] != 'waiting_connections':
            await hub.conversations.tick()
            await asyncio.sleep(.02)
    task = hub.conversations.get(c['id'])['turns'][-1]['task']
    assert task['phase'] == 'waiting_connections' and task['required_connections'][0]['capability'] == 'image_edit'
    assert task['message'].startswith('请先连接')


async def test_engine_selection_prefers_operator_for_tool_calling_models(hub):
    class Decision:
        async def generate(self, request, model):
            return ModelResult(data={})
    hub.models.register('planner', Decision(), 'fixture', ['decision'])
    hub.models.register('chatty', Decision(), 'fixture', ['decision', 'chat'])
    assert hub.autonomy.engine_for('planner') == 'compile'
    assert hub.autonomy.engine_for('chatty') == 'operator'
    hub.autonomy.configure({'engine': 'compile'})
    assert hub.autonomy.engine_for('chatty') == 'compile'
    hub.autonomy.configure({'engine': 'operator'})
    assert hub.autonomy.engine_for('planner') == 'operator'
    toolkit = hub.autonomy.toolkit()
    assert {'code.create', 'agents.spawn', 'memory.put', 'skills.save', 'workflows.save', 'web.search',
            'task.ask_user'} <= set(toolkit)
    assert not any(n.startswith(('development.', 'host.')) for n in toolkit) and 'agents.reply' not in toolkit


async def test_browser_shows_operator_activity_and_saved_workflow(api):
    from playwright.async_api import async_playwright, expect
    url, hub = api
    model = Scripted([
        ('memory.put', {'namespace': 'user', 'key': 'greeting', 'value': 'prefers concise answers'}),
        ('workflows.save', {'id': 'echo-note', 'description': '原样保存输入', 'workflow': {
            'name': '保存笔记', 'steps': [{'id': 'save', 'kind': 'artifact',
                                          'input': {'name': 'note.txt', 'content': {'$ref': '$input.message'}}}]}}),
    ], final='已记住你的偏好，并保存了「保存笔记」流程。')
    hub.models.register('planner', model, 'fixture', ['chat', 'decision'])
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page(viewport={'width': 1280, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url + '/#conversations')
            await page.locator('#conversations [data-model]').select_option('planner')
            await page.locator('#workspaceMessage').fill('记住我喜欢简洁回答，并做一个保存笔记的流程')
            await page.locator('#conversations [data-send]').click()
            card = page.locator('.chat-turn').first
            await expect(card.locator('.chat-task-heading')).to_contain_text('处理完成', timeout=15000)
            await expect(card.locator('.chat-task-heading')).to_contain_text('保存笔记')
            activity = card.locator('[data-activity]')
            await expect(activity).to_be_visible()
            await expect(activity.locator('summary')).to_contain_text('2 次工具调用')
            await expect(activity.locator('.chat-activity-row')).to_contain_text(['记住一条内容', '已保存流程 echo-note v1'])
            await expect(card.locator('[data-reply]')).to_contain_text('已记住你的偏好')
            await expect(card.locator('[data-edit]')).to_be_visible()
            assert not errors, errors
        finally:
            await browser.close()


async def test_prompt_orders_stable_material_before_volatile(hub):
    """A changing tail must not invalidate the cached prefix of the operator prompt."""
    from easyagent.skill_packages import builtin_skills

    hub.skill_packages.install({"package": builtin_skills()[0]["package"], "expected_revision": 0})
    skill = next(s["name"] for s in hub.skills.catalog())

    class Model:
        async def generate(self, request, model):
            return ModelResult(text="ok", usage={"mock": True})

    hub.models.register("planner", Model(), "fixture", ["chat", "decision"])
    hub.development.save_workflow(
        "saved-one", {"name": "已保存的流程", "steps": [{"id": "e", "target": "core.echo", "input": {"text": "hi"}}]}
    )

    conversation, turn = {"id": "c1"}, {"id": "t1", "text": "做个任务"}
    state = {"material_text": "做个任务", "attachments": []}
    step = hub.autonomy.agent_step(conversation, turn, state, "planner")
    instructions = step["input"]["instructions"]
    assert "Principles:" in instructions and "Toolkit guide" in instructions
    # The workflow catalogue changes whenever a workflow is saved, so it goes last.
    assert instructions.index("Saved workflows") > instructions.index("Toolkit guide")

    workflow = {"name": "ordered", "steps": [{"id": "a", "kind": "agent", "target": "planner", "input": {
        "prompt": "p", "instructions": "BASE-INSTRUCTIONS", "skill_access": [skill]}}]}
    merged = hub.prepare(workflow).steps[0].input["instructions"]
    assert merged.index("BASE-INSTRUCTIONS") < merged.index("Available skills")
