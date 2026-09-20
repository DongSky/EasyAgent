# SPDX-FileCopyrightText: 2026 EasyAgent contributors
# SPDX-License-Identifier: Apache-2.0
"""One-time media workflow setup. Configures nodes, but does not generate media."""
import argparse
import asyncio
import json
from copy import deepcopy

from easyagent_client import HubClient

IMAGE_MODEL = 'gpt-image-2.5-flare'
VIDEO_MODEL = 'doubao-seedance-2-5-260628'


async def install(client, *, base_url=None, api_key_env='OPENAI_API_KEY', image_model=IMAGE_MODEL,
                  video_model=VIDEO_MODEL, reference_mode='url-upload', task_id_path='result.id',
                  video_url_path='content.video_url', upload_operation='pixverse.post.openapi_v2_image_upload',
                  upload_field='image', upload_url_path='result.Resp.img_url', poll_interval=10, replace=False):
    if base_url:
        await client.request('POST', '/v1/studio/model-catalog/discover', {'base_url': base_url, 'api_key_env': api_key_env})

    async def node(operation, model=None, **options):
        row = await client.request('POST', '/v1/studio/model-catalog/nodes', {'operation_id': operation, 'model': model, **options})
        return deepcopy(row['step'])

    draw = await node('v1.post.v1_images_edits', image_model,
                      defaults={'body': {'model': image_model, 'size': '1024x1024', 'quality': 'medium', 'n': 1}})
    draw.update(id='draw', max_attempts=1)
    draw['input']['body'].update(image={'$ref': '$input.reference_image'}, prompt={'$ref': '$input.prompt'})
    schema = {'type': 'object', 'properties': {'reference_image': {'type': 'string', 'minLength': 1},
              'prompt': {'type': 'string', 'minLength': 1}}, 'required': ['reference_image', 'prompt'], 'additionalProperties': False}
    image = {'name': '参考图生成表情', 'steps': [draw], 'metadata': {
        'description': '使用参考图片生成一张新表情；返回命名产物 image。',
        'input_schema': schema, 'outputs': {'image': {'$ref': 'draw.artifacts.0'}},
        'step_labels': {'draw': '生成表情图片'}}}

    steps, reference = [], {'$ref': '$input.reference_image'}
    if reference_mode == 'url-upload':
        upload = await node(upload_operation)
        upload.update(id='upload', max_attempts=1, input={'body': {upload_field: reference}})
        steps.append(upload)
        reference = {'$ref': 'upload.' + upload_url_path}
    animate = await node('seedance.post.api_v3_contents_generations_tasks', video_model)
    animate.update(id='animate', max_attempts=1, depends_on=['upload'] if steps else [], input={'body': {
        'model': video_model, 'duration': {'$ref': '$input.duration'}, 'ratio': 'adaptive', 'generate_audio': False,
        'content': [{'type': 'text', 'text': {'$ref': '$input.prompt'}},
                    {'type': 'image_url', 'image_url': {'url': reference}, 'role': 'first_frame'}]}})
    wait = await node('volc.get.api_v3_contents_generations_tasks_id', polling={
        'status_path': 'status', 'pending': ['pending', 'queued', 'running'], 'succeeded': ['succeeded'],
        'failed': ['failed', 'cancelled', 'expired'], 'interval_seconds': poll_interval,
        'max_polls': 180, 'deadline_seconds': 1800})
    wait.update(id='wait', depends_on=['animate'], input={'id': {'$ref': 'animate.' + task_id_path}})
    video_schema = deepcopy(schema)
    video_schema['properties']['duration'] = {'type': 'integer', 'minimum': 1, 'maximum': 15}
    video = {'name': '图片生成动画', 'inputs': {'duration': 4}, 'steps': [*steps, animate, wait], 'metadata': {
        'description': '将参考图片生成短动画并等待完成；返回命名视频地址 video。',
        'input_schema': video_schema,
        'outputs': {'video': {'$ref': 'wait.' + video_url_path}, 'task_id': {'$ref': 'animate.' + task_id_path}},
        'step_labels': {'upload': '上传参考图片', 'animate': '生成动画', 'wait': '等待动画完成'}}}
    installed = {}
    async def save(identifier, workflow):
        revision = 0
        if replace:
            response = await client.http.get('/v1/workflows/' + identifier)
            if response.status_code != 404:
                response.raise_for_status()
                revision = response.json()['revision']
        saved = await client.request('PUT', f'/v1/workflows/{identifier}?expected_revision={revision}', workflow)
        installed[identifier] = {'revision': saved['revision'], 'tools': [s['target'] for s in workflow['steps'] if s.get('target')]}
    await save('media.image', image)
    await save('media.video', video)
    await save('media.expression_video', {
        'name': '参考图生成表情与动画',
        'inputs': {'image_prompt': '根据参考图画一个可爱的 Q 版挥手表情，保留角色特征。',
                   'video_prompt': '让图中的角色轻轻挥手，保持外貌和背景不变，固定镜头。', 'duration': 4},
        'metadata': {
            'description': '上传一张参考图，生成表情图片并制作短动画；返回 image 和 video。',
            'input_schema': {'type': 'object', 'properties': {
                'reference_image': {'type': 'string', 'minLength': 1}, 'image_prompt': {'type': 'string', 'minLength': 1},
                'video_prompt': {'type': 'string', 'minLength': 1}, 'duration': {'type': 'integer', 'minimum': 1, 'maximum': 15}},
                'required': ['reference_image'], 'additionalProperties': False},
            'outputs': {'image': {'$ref': 'image.outputs.0.image'}, 'video': {'$ref': 'video.outputs.0.video'}},
            'step_labels': {'image': '生成表情图片', 'video': '生成动画'}},
        'steps': [
            {'id': 'image', 'kind': 'subworkflow', 'workflow_ref': {'id': 'media.image', 'revision': installed['media.image']['revision']},
             'input': {'reference_image': {'$ref': '$input.reference_image'}, 'prompt': {'$ref': '$input.image_prompt'}}},
            {'id': 'video', 'kind': 'subworkflow', 'workflow_ref': {'id': 'media.video', 'revision': installed['media.video']['revision']},
             'depends_on': ['image'], 'input': {'reference_image': {'$ref': 'image.outputs.0.image.id'},
                 'prompt': {'$ref': '$input.video_prompt'}, 'duration': {'$ref': '$input.duration'}}}]})
    return installed


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', help='Omit to reuse the model catalog connection saved in Studio')
    parser.add_argument('--api-key-env', default='OPENAI_API_KEY', help='Secret reference in the server, not the SDK process')
    parser.add_argument('--image-model', default=IMAGE_MODEL)
    parser.add_argument('--video-model', default=VIDEO_MODEL)
    parser.add_argument('--reference-mode', choices=['url-upload', 'inline'], default='url-upload')
    parser.add_argument('--task-id-path', default='result.id', help='For alternate receipts, use result.data.task_id')
    parser.add_argument('--video-url-path', default='content.video_url')
    parser.add_argument('--upload-operation', default='pixverse.post.openapi_v2_image_upload')
    parser.add_argument('--upload-field', default='image')
    parser.add_argument('--upload-url-path', default='result.Resp.img_url')
    parser.add_argument('--replace', action='store_true', help='Explicitly publish new versions of existing media workflows')
    async with HubClient.from_env() as client:
        print(json.dumps(await install(client, **vars(parser.parse_args())), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
