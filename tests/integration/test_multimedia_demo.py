"""Full demo over real local HTTP; media/model outputs here are protocol fixtures."""
import base64
from argparse import Namespace

import pytest
from fastapi import FastAPI, Request, Response

from conftest import live_server
from examples.demos.multimedia_workflow import IMAGE_MODEL, VIDEO_MODEL, main


@pytest.mark.parametrize('reference_mode', ['inline', 'url-upload'])
async def test_multimedia_demo_artifact_to_video_export_and_resume(api, monkeypatch, tmp_path, reference_mode):
    url, hub = api
    counts = {'images': 0, 'videos': 0, 'downloads': 0, 'uploads': 0, 'polls': 0}
    remote = FastAPI()
    png = b'\x89PNG\r\n\x1a\nprotocol-fixture'
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-secret')

    @remote.get('/v1/models')
    async def models():
        return {'data': [m for m in hub.model_catalog.models if m['id'] in (IMAGE_MODEL, VIDEO_MODEL)]}

    @remote.get('/api/pricing')
    async def pricing():
        return {'supported_endpoint': hub.model_catalog.endpoints}

    @remote.post('/v1/images/edits')
    async def edit(request: Request):
        content = await request.body()
        assert png in content and IMAGE_MODEL.encode() in content
        assert b'name="format"' not in content
        counts['images'] += 1
        return {'data': [{'b64_json': base64.b64encode(png).decode()}]}

    @remote.post('/api/v3/contents/generations/tasks')
    async def video(request: Request):
        body = await request.json()
        assert body['model'] == VIDEO_MODEL and body['duration'] == 4
        # Official Seedance 2.5 contract: first-frame tasks must use adaptive ratio.
        assert body['content'][1]['role'] == 'first_frame'
        assert body['ratio'] == 'adaptive'
        assert request.headers['authorization'] == 'Bearer fixture-secret'
        image = body['content'][1]['image_url']['url']
        if reference_mode == 'inline':
            assert base64.b64decode(image.split(',', 1)[1]) == png
        else:
            assert image == 'https://example.test/reference.png'
        counts['videos'] += 1
        return {'data': {'task_id': 'media-fixture'}} if reference_mode == 'inline' else {'id': 'media-fixture'}

    @remote.post('/openapi/v2/image/upload')
    async def upload(request: Request):
        assert request.headers['authorization'] == 'Bearer fixture-secret'
        assert png in await request.body()
        counts['uploads'] += 1
        return {'ErrCode': 0, 'Resp': {'img_url': 'https://example.test/reference.png'}}

    @remote.get('/api/v3/contents/generations/tasks/{identifier}')
    async def poll(identifier: str):
        assert identifier == 'media-fixture'
        counts['polls'] += 1
        if counts['polls'] == 1:
            return {'status': 'pending'}
        return {'status': 'succeeded', 'content': {'video_url': endpoint + '/result.mp4'}}

    @remote.get('/result.mp4')
    async def download(request: Request):
        assert 'authorization' not in request.headers
        counts['downloads'] += 1
        return Response(b'video-protocol-fixture', media_type='video/mp4')

    reference = tmp_path / 'reference.png'
    reference.write_bytes(png)
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('fixture prompt', encoding='utf-8')
    async with live_server(remote) as endpoint:
        args = Namespace(hub=url, hub_token='', base_url=endpoint+'/v1', reference=str(reference),
                         image_prompt=str(prompt), video_prompt=str(prompt), output=str(tmp_path/'output'),
                         approve_generation=True, video_reference_mode=reference_mode,
                         video_upload_operation='pixverse.post.openapi_v2_image_upload',
                         video_upload_file_field='image', video_upload_url_path='result.Resp.img_url')
        await main(args)
        await main(args)  # A rerun resumes the recorded runs; it never submits generation again.
    assert counts == {'images': 1, 'videos': 1, 'downloads': 1,
                      'uploads': int(reference_mode == 'url-upload'), 'polls': 2}
    assert (tmp_path/'output/expression.png').read_bytes() == png
    assert (tmp_path/'output/animation.mp4').read_bytes() == b'video-protocol-fixture'
    export = (tmp_path/'output/complete-workflow.json').read_text(encoding='utf-8')
    assert 'draw.artifacts.0.id' in export
    assert ('animate.result.data.task_id' if reference_mode == 'inline' else 'animate.result.id') in export
    if reference_mode == 'url-upload':
        assert 'upload_image.result.Resp.img_url' in export
    assert 'fixture-secret' not in export and 'data:image/' not in export
