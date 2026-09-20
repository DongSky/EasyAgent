"""Planner output errors are corrected before verification or business execution."""
import asyncio
import copy

import pytest
from pydantic import ValidationError

from easyagent.assistant_builder import build_status, start_build
from easyagent.contracts import ModelResult, Step
from easyagent.runtime import Hub
from test_autonomous_build import assistant


def draft():
    return {'workflow': {'name': '修图前的数据准备', 'steps': [
        {'id': 'prepare', 'kind': 'transform', 'input': {'prompt': '自然重打光'}},
        {'id': 'collect', 'kind': 'transform', 'depends_on': ['prepare'],
         'input': {'prompt': {'$ref': 'prepare.prompt'}}, 'timeout_seconds': 60}]},
        'explanation': '保留步骤之间的数据连接。', 'questions': []}


class InvalidFirst:
    def __init__(self, problem, always=False):
        self.problem, self.always, self.calls = problem, always, []

    async def generate(self, request, model):
        self.calls.append(copy.deepcopy(request))
        result = draft()
        if len(self.calls) == 1 or self.always:
            if self.problem == 'json':
                return ModelResult(text='{"workflow":')
            if self.problem == 'timeout':
                result['workflow']['steps'][1]['timeout_seconds'] = 13800
            elif self.problem == 'nested_timeout':
                result['workflow']['steps'][1] = {'id': 'collect', 'kind': 'foreach', 'input': {'items': []},
                    'body': {'name': 'child', 'steps': [
                        {'id': 'echo', 'kind': 'transform', 'timeout_seconds': 13800}]}}
            elif self.problem == 'graph':
                result['workflow']['steps'][1]['depends_on'] = ['missing']
        return ModelResult(data=result)


@pytest.mark.parametrize('problem', ['timeout', 'nested_timeout', 'graph', 'json'])
async def test_initial_plan_repairs_invalid_output_before_verification(hub, problem):
    model = InvalidFirst(problem)
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('准备自然修图提示词。')
    run = await hub.wait(start_build(hub, 'repair-output', body)['id'])
    assert run['status'] == 'succeeded', run
    assert [s['status'] for s in run['steps']] == ['succeeded', 'succeeded']
    assert build_status(hub, 'repair-output', body)['status'] == 'ready'
    assert len(model.calls) == 2 and run['usage']['model_calls'] == 2
    corrected = model.calls[1]
    assert corrected.response_schema == model.calls[0].response_schema
    assert corrected.messages[1] == model.calls[0].messages[1]
    assert corrected.messages[-2]['role'] == 'assistant'
    assert 'failed validation' in corrected.messages[-1]['content']
    if problem == 'timeout':
        assert '13800' in corrected.messages[-2]['content']
        assert 'maximum=3600' in corrected.messages[-1]['content']
        assert 'workflow.steps.1.timeout_seconds' in corrected.messages[-1]['content']
    with pytest.raises(ValidationError):
        Step(id='still_bounded', timeout_seconds=13800)
    assert not hub.code.list()


async def test_invalid_plan_stops_after_three_calls_without_publishing(hub):
    model = InvalidFirst('timeout', always=True)
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('保留任务但不要执行无效规划。')
    run = await hub.wait(start_build(hub, 'never-valid', body)['id'])
    assert run['status'] == 'failed'
    assert len(model.calls) == 3 and run['usage']['model_calls'] == 3
    assert '3 次校验' in run['steps'][0]['error']
    assert 'maximum=3600' in run['steps'][0]['error']
    assert len(run['steps'][0]['error']) < 500
    assert run['steps'][1]['status'] == 'skipped'
    assert not hub.development.workflows() and not hub.code.list()


async def test_compiler_keeps_correction_context_across_restart(hub):
    entered = asyncio.Event()

    class Interrupted(InvalidFirst):
        async def generate(self, request, model):
            result = await super().generate(request, model)
            if len(self.calls) == 2:
                entered.set()
                await asyncio.Event().wait()
            return result

    model = Interrupted('timeout')
    hub.models.register('planner', model, 'fixture', ['decision'])
    body = assistant('重启后继续纠正规划。')
    identifier = start_build(hub, 'durable-repair', body)['id']
    await asyncio.wait_for(entered.wait(), 5)
    await hub.stop()
    restored = Hub(hub.store.path, poll_seconds=.01)
    restored.models.register('planner', model, 'fixture', ['decision'])
    await restored.start()
    try:
        run = await restored.wait(identifier)
        assert run['status'] == 'succeeded', run
        assert build_status(restored, 'durable-repair', body)['status'] == 'ready'
        assert len(model.calls) == 3
        assert model.calls[-1].messages == model.calls[-2].messages
        assert 'maximum=3600' in model.calls[-1].messages[-1]['content']
    finally:
        await restored.stop()
