"""The documented search -> image DAG over real local HTTP, without paid services."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI, Request
from jsonschema.exceptions import ValidationError

from conftest import live_server
from easyagent import Call, Module, Runtime, Sequential, Subflow, node
from easyagent_client import RunStopped
from examples.getting_started.media.research_image import ResearchImage, save_result
from test_media_sdk_demos import process_output

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'examples/getting_started/media/research_image.py'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII=')


def provider_fixture(*, writer_started=None, release_writer=None, empty=False):
    app, received = FastAPI(), {'search': [], 'writer': [], 'image': []}

    @app.get('/search')
    async def search(request: Request):
        assert request.headers['x-api-key'] == 'fixture-search'
        received['search'].append(dict(request.query_params))
        query = request.query_params['query']
        return {'query': query, 'total_results': 0 if empty else 1, 'page': 0,
                'results': [] if empty else [{'position':1, 'site_name':'fixture', 'title':'Character appearance',
                    'snippet':'The reference character wears a green jacket and a hat.',
                    'url':'https://example.test/character'}]}

    @app.post('/v1/chat/completions')
    async def writer(request: Request):
        assert request.headers['authorization'] == 'Bearer fixture-model'
        body = await request.json()
        received['writer'].append(body)
        brief = json.loads(body['messages'][-1]['content'])
        assert brief['requested_style'] == 'watercolor'
        assert brief['subject'] == 'reference character'
        assert brief['source_snippets'][0]['url'] == 'https://example.test/character'
        if writer_started:
            writer_started.set()
            await asyncio.wait_for(release_writer.wait(), 10)
        return {'choices':[{'message':{'content':'Watercolor portrait, green jacket, hat, waving.'}}]}

    @app.post('/v1/images/edits')
    async def image(request: Request):
        assert request.headers['authorization'] == 'Bearer fixture-model'
        body = await request.body()
        assert 'multipart/form-data' in request.headers['content-type']
        assert PNG in body and b'Watercolor portrait' in body
        assert b'gpt-image-2.5-flare' in body and b'1536x1024' in body
        received['image'].append(body)
        return {'data':[{'b64_json':base64.b64encode(PNG).decode()}]}

    return app, received


def config_file(tmp_path, endpoint):
    config = json.loads((EXAMPLE.parent/'research_image.config.example.json').read_text())
    config['search'][0]['endpoint'] = endpoint + '/search'
    config['models'][0]['base_url'] = endpoint + '/v1'
    config['models'][0]['model'] = 'fixture-text'
    config['http_tools'][0]['url'] = endpoint + '/v1/images/edits'
    path = tmp_path/'config.json'
    path.write_text(json.dumps(config))
    return path


async def test_research_image_multiple_inputs_concurrent_fork_join_approval_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('TINYFISH_API_KEY', 'fixture-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-model')
    started, release = asyncio.Event(), asyncio.Event()
    remote, calls = provider_fixture(writer_started=started, release_writer=release)
    graph = ResearchImage().workflow()
    assert len(graph.steps) == 8
    # Image uses prompt plus two independent root inputs. Notes do not depend on the model or image.
    assert graph.steps[4].depends_on == ['step_4']
    assert graph.steps[4].input['image'] == {'$ref':'$input.reference_image'}
    assert graph.steps[4].input['size'] == {'$ref':'$input.size'}
    assert graph.steps[5].depends_on == ['step_2']
    assert graph.steps[7].depends_on == ['step_4', 'step_5', 'step_7']
    database = tmp_path/'run.db'
    async with live_server(remote) as endpoint:
        config = config_file(tmp_path, endpoint)
        async with Runtime(database, config=config, timeout=20) as runtime:
            reference = runtime.hub.artifacts.put('reference.png', PNG, 'image/png')
            task = asyncio.create_task(runtime.arun(ResearchImage(), keywords='reference character',
                reference_image=reference['id'], style='watercolor', size='1536x1024'))
            try:
                await asyncio.wait_for(started.wait(), 5)
                # The writer is blocked by a gate; the other branch must still save its result.
                async with asyncio.timeout(5):
                    while not (run := runtime.hub.store.runs()) or not any(
                        a['name']=='sources.md' for a in runtime.hub.artifacts.list(run[0]['id'])):
                        await asyncio.sleep(.01)
                state = runtime.hub.store.run(run[0]['id'])
                assert state['steps'][3]['status'] == 'running'
                assert state['steps'][6]['status'] == 'succeeded'
                assert state['steps'][7]['status'] == 'queued'  # Join cannot run yet.
                assert not calls['image']
                release.set()
                with pytest.raises(RunStopped) as stopped:
                    await task
                paused = stopped.value.state
                assert paused['status'] == 'waiting_approval' and not calls['image']
                runtime.hub.tools.approve(runtime.hub.store, paused['approvals'][0]['id'], True)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with Runtime(database, config=config) as restored:
            restored.bind(ResearchImage())
            result = await restored.aresume(paused['id'])
            assert result.value['image']['media_type'] == 'image/png'
            files = save_result(restored, result, tmp_path/'output')
            assert Path(files['image']).read_bytes() == PNG
            assert 'https://example.test/character' in Path(files['sources']).read_text()
            assert not restored.hub.store.run(result.id)['children']
            assert len(calls['search']) == len(calls['writer']) == len(calls['image']) == 1


async def test_research_image_cli_export_and_run_resume(tmp_path):
    remote, calls = provider_fixture()
    env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','TMPDIR','TMP','TEMP','SYSTEMROOT','WINDIR')}
    env.update(TINYFISH_API_KEY='fixture-search', OPENAI_API_KEY='fixture-model')
    export = tmp_path/'graph.json'
    code, out, err = await process_output(sys.executable, EXAMPLE, '--export', export, env=env)
    assert code == 0, err.decode()
    assert len(json.loads(export.read_text())['steps']) == 8 and not calls['search']
    reference = tmp_path/'reference.png'
    reference.write_bytes(PNG)
    database = tmp_path/'cli.db'
    async with live_server(remote) as endpoint:
        config = config_file(tmp_path, endpoint)
        code, out, err = await process_output(sys.executable, EXAMPLE, '--config', config, '--database', database,
            '--keywords','reference character','--reference',reference,'--style','watercolor','--size','1536x1024', env=env)
        assert code == 3, err.decode()
        state = json.loads(out)
        assert state['status'] == 'waiting_approval' and not calls['image']
        code, out, err = await process_output(sys.executable, '-m', 'easyagent', 'approve',state['approvals'][0]['id'],
            '--yes','--database',database, env=env)
        assert code == 0, err.decode()
        code, out, err = await process_output(sys.executable, EXAMPLE, '--config',config,'--database',database,
            '--resume',state['id'],'--output',tmp_path/'download', env=env)
        assert code == 0, err.decode()
        assert Path(json.loads(out)['image']).read_bytes() == PNG
        assert len(calls['search']) == len(calls['writer']) == len(calls['image']) == 1


async def test_empty_search_and_invalid_inputs_stop_before_paid_generation(tmp_path, monkeypatch):
    monkeypatch.setenv('TINYFISH_API_KEY', 'fixture-search')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-model')
    remote, calls = provider_fixture(empty=True)
    async with live_server(remote) as endpoint:
        async with Runtime(tmp_path/'empty.db', config=config_file(tmp_path, endpoint)) as runtime:
            with pytest.raises(ValidationError):
                await runtime.arun(ResearchImage(), keywords='subject', reference_image='unused', size='invalid')
            with pytest.raises(TypeError, match='reference_image'):
                await runtime.arun(ResearchImage(), keywords='subject')
            assert not calls['search']
            with pytest.raises(RunStopped) as stopped:
                await runtime.arun(ResearchImage(), keywords='subject', reference_image='unused')
            assert stopped.value.status == 'failed'
            assert 'No usable search sources' in str(stopped.value.state['errors'])
            assert not calls['writer'] and not calls['image']


async def test_full_object_refs_and_multiple_outputs_across_nodes_and_subflows(tmp_path):
    @node
    def arguments(left: str, right: str) -> dict:
        return {'left':left, 'right':right}

    class Forward(Module):
        def forward(self, left: str, right: str):
            # An entire symbolic object is passed to a tool, not merely one field.
            return Call('core.echo')(arguments(left, right))

    class Child(Module):
        def forward(self, inputs: dict):
            return Call('core.echo')(inputs)

    class Parent(Module):
        def forward(self, left: str, right: str):
            return Subflow(Child())(arguments(left, right))

    async with Runtime(tmp_path/'objects.db') as runtime:
        for workflow in (Forward(), Parent()):
            result = await runtime.arun(workflow, 'left value', right='right value')
            assert result.value == {'left':'left value','right':'right value'}
        # Scalars cannot silently become tool argument dictionaries.
        @node
        def scalar(value: str) -> str:
            return value
        with pytest.raises(RunStopped) as stopped:
            await runtime.arun(Sequential(scalar, Call('core.echo')), 'scalar')
        assert 'object of named arguments' in str(stopped.value.state['errors'])
    with pytest.raises(TypeError, match='Module.forward'):
        Sequential(Call('core.echo'), arguments).workflow()
