"""Three real client processes share durable media workflows over local HTTP fixtures."""
import asyncio
import base64
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
from fastapi import FastAPI, Request, Response

from conftest import live_server
from easyagent.api import create_app
from easyagent.studio import install_studio
from easyagent_client import HubClient
from examples.getting_started.media.setup import IMAGE_MODEL, VIDEO_MODEL, install

ROOT = Path(__file__).resolve().parents[2]


async def command(*args, env=None, resume_paused=False):
    for _ in range(8 if resume_paused else 1):
        code, out, err = await process_output(*args, env=env)
        if code == 0:
            return out.decode()
        if resume_paused and b'waiting_approval' in err:
            await asyncio.sleep(.05)
            continue
        pytest.fail(err.decode() + '\n' + out.decode(), pytrace=False)
    pytest.fail('Demo did not resume after fixture approvals', pytrace=False)


async def process_output(*args, env=None):
    process = await asyncio.create_subprocess_exec(*map(str, args), cwd=ROOT, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 180)
        return process.returncode, out, err
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize('reference_mode,task_id_path', [('url-upload', 'result.id'), ('inline', 'result.data.task_id')])
async def test_three_language_media_demos_share_runs_and_download_without_token_leak(hub, monkeypatch, tmp_path, reference_mode, task_id_path):
    if not shutil.which('node') or not shutil.which('cargo'):
        pytest.skip('Node and Cargo are required for the three-language integration acceptance')
    monkeypatch.setenv('OPENAI_API_KEY', 'media-fixture-secret')
    remote = FastAPI()
    counts = dict(images=0, videos=0, uploads=0, polls=0, downloads=0)
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII=')
    video_bytes = b'explicit-local-video-protocol-fixture'

    @remote.get('/v1/models')
    async def models():
        return {'data': [m for m in hub.model_catalog.models if m['id'] in (IMAGE_MODEL, VIDEO_MODEL)]}

    @remote.get('/api/pricing')
    async def pricing():
        return {'supported_endpoint': hub.model_catalog.endpoints}

    @remote.post('/v1/images/edits')
    async def image(request: Request):
        assert request.headers['authorization'] == 'Bearer media-fixture-secret'
        body = await request.body()
        assert png in body and IMAGE_MODEL.encode() in body
        counts['images'] += 1
        return {'data': [{'b64_json': base64.b64encode(png).decode()}]}

    @remote.post('/openapi/v2/image/upload')
    async def upload(request: Request):
        assert request.headers['authorization'] == 'Bearer media-fixture-secret'
        assert png in await request.body()
        counts['uploads'] += 1
        return {'Resp': {'img_url': 'https://example.test/reference.png'}}

    @remote.post('/api/v3/contents/generations/tasks')
    async def animate(request: Request):
        assert request.headers['authorization'] == 'Bearer media-fixture-secret'
        body = await request.json()
        assert body['model'] == VIDEO_MODEL and body['ratio'] == 'adaptive' and body['duration'] == 4
        reference = body['content'][1]['image_url']['url']
        if reference_mode == 'url-upload':
            assert reference == 'https://example.test/reference.png'
        else:
            assert base64.b64decode(reference.split(',', 1)[1]) == png
        counts['videos'] += 1
        return {'id': 'sdk-media'} if task_id_path == 'result.id' else {'data': {'task_id': 'sdk-media'}}

    @remote.get('/api/v3/contents/generations/tasks/{identifier}')
    async def poll(identifier: str, request: Request):
        assert identifier == 'sdk-media'
        assert request.headers['authorization'] == 'Bearer media-fixture-secret'
        counts['polls'] += 1
        return {'status': 'pending'} if counts['polls'] == 1 else {'status': 'succeeded', 'content': {'video_url': endpoint + '/result.mp4'}}

    @remote.get('/result.mp4')
    async def download(request: Request):
        assert 'authorization' not in request.headers
        counts['downloads'] += 1
        return Response(video_bytes, media_type='video/mp4')

    app = create_app(hub, token='private-hub-token', manage_workers=False)
    install_studio(app, hub)
    async with live_server(remote) as endpoint, live_server(app) as url:
        async with HubClient(url, 'private-hub-token') as client:
            installed = await install(client, base_url=endpoint + '/v1', reference_mode=reference_mode,
                                      task_id_path=task_id_path, poll_interval=.05)
        assert counts['images'] == counts['videos'] == 0  # Setup is not generation.
        tools = {name for info in installed.values() for name in info['tools']}

        async def approve_fixture_calls():
            while True:
                for run in hub.store.runs():
                    for invocation in hub.store.run(run['id'])['approvals']:
                        if invocation['status'] == 'approval' and invocation['tool'] in tools:
                            hub.tools.approve(hub.store, invocation['id'], True)
                await asyncio.sleep(.02)

        approver = asyncio.create_task(approve_fixture_calls())
        reference = tmp_path / 'reference.png'
        reference.write_bytes(png)
        # SDK subprocesses receive only toolchain settings and this fixture's
        # Hub credential. Never copy unrelated personal model credentials.
        env = {k: v for k, v in os.environ.items() if k.upper() in (
            'PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'SYSTEMROOT', 'WINDIR', 'USERPROFILE', 'LOCALAPPDATA',
            'APPDATA', 'CARGO_HOME', 'RUSTUP_HOME', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'LIB', 'LIBPATH', 'INCLUDE',
            'VSCMD_ARG_TGT_ARCH', 'VCTOOLSINSTALLDIR', 'VSINSTALLDIR', 'VCINSTALLDIR', 'WINDOWSSDKDIR',
            'WINDOWSSDKVERSION', 'UNIVERSALCRTSDKDIR', 'UCRTVERSION')}
        env.update(EAH_URL=url, EAH_TOKEN='private-hub-token', EAH_DEMO_KEY='three-languages-one-operation')
        try:
            results = await asyncio.gather(
                command(sys.executable, ROOT/'examples/getting_started/media/python_demo.py', reference, tmp_path/'python', env=env, resume_paused=True),
                command('node', ROOT/'examples/getting_started/media/javascript_demo.mjs', reference, tmp_path/'javascript', env=env, resume_paused=True),
                command('cargo', 'run', '--quiet', '--manifest-path', ROOT/'sdk/rust/Cargo.toml', '--example', 'media', '--', reference, tmp_path/'rust', env=env, resume_paused=True),
            )
        finally:
            approver.cancel()
            await asyncio.gather(approver, return_exceptions=True)
        summaries = [json.loads(result.strip().splitlines()[-1]) for result in results]
        assert len({r['run_id'] for r in summaries}) == 1
        async with HubClient(url, 'private-hub-token') as client:
            result = await client.run_handle(summaries[0]['run_id']).result()
            assert any(a['media_type'] == 'image/png' for a in result.artifacts)  # Includes child artifacts.
            assert len((await client.run(result.id))['children']) == 2
        for language in ('python', 'javascript', 'rust'):
            assert (tmp_path/language/'expression.png').read_bytes() == png
            assert (tmp_path/language/'animation.mp4').read_bytes() == video_bytes
        assert counts == {'images': 1, 'videos': 1, 'uploads': int(reference_mode=='url-upload'), 'polls': 2, 'downloads': 3}
