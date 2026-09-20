"""Built-in media nodes from the exercised image/edit and asynchronous video protocols.

Definitions ship without an endpoint or credential. Binding a saved connection creates
ordinary versioned HTTP nodes; it never submits generation or uploads a file.
"""
import hashlib

from .http_tools import HTTPTool
from .models import HTTPProvider
from .node_recipes import obj
from .store import Conflict

IMAGE_MODEL = 'gpt-image-2.5-flare'
VIDEO_MODEL = 'doubao-seedance-2-5-260628'


def definitions():
    def text(title):
        return {'type': 'string', 'minLength': 1, 'title': title}
    image = obj({'model': text('模型 ID'), 'prompt': text('图片描述'),
                 'size': {'type': 'string', 'title': '尺寸'}, 'quality': {'type': 'string', 'title': '质量'},
                 'n': {'type': 'integer', 'minimum': 1, 'maximum': 10, 'title': '数量'}}, ['model', 'prompt'])
    edits = obj({**image['properties'], 'image': text('原图文件 ID'), 'mask': text('蒙版文件 ID（可选）')},
                ['model', 'prompt', 'image'])
    video = obj({'model': text('视频模型 ID'), 'content': {'type': 'array', 'minItems': 1, 'title': '提示词与参考媒体',
        'items': {'oneOf': [obj({'type': {'const': 'text'}, 'text': text('动作描述')}),
                           obj({'type': {'const': 'image_url'}, 'image_url': obj({'url': text('图片 HTTPS 地址或文件 ID')}),
                                'role': {'enum': ['first_frame', 'last_frame', 'reference_image']}}, ['type', 'image_url'])]}},
        'duration': {'type': 'integer', 'minimum': 4, 'maximum': 30, 'title': '时长（秒）'},
        'ratio': {'type': 'string', 'title': '比例（首帧动画使用 adaptive）'},
        'generate_audio': {'type': 'boolean', 'title': '生成音频（以服务实际返回为准）'}}, ['model', 'content'])
    rows = [
        ('image_generate', '文字生成图片', '/v1/images/generations', image,
         '根据提示词生成图片；返回 result 和 artifacts。Base64 图片自动保存为新文件，URL 响应保留服务链接。',
         {'model': IMAGE_MODEL, 'prompt': '', 'size': '1024x1024', 'quality': 'medium', 'n': 1}),
        ('image_edit', '参考图生成 / 编辑图片', '/v1/images/edits', edits,
         '原图 image 使用上传文件的 artifact ID；以 multipart 传递原图，返回 result 和 artifacts，保留原图。',
         {'model': IMAGE_MODEL, 'prompt': '', 'image': '', 'size': '1024x1024', 'quality': 'medium', 'n': 1}),
        ('video_submit', '生成视频 · 提交任务', '/api/v3/contents/generations/tasks', video,
         '文字或参考图生视频。content 接收 text 与 image_url；首帧动画使用 role=first_frame、ratio=adaptive。'
         '返回 result.id（兼容服务可能为 result.data.task_id）；将实际任务 ID 传给视频等待节点，不重复提交。',
         {'model': VIDEO_MODEL, 'content': [{'type': 'text', 'text': ''}], 'duration': 4, 'ratio': 'adaptive', 'generate_audio': False}),
        ('video_wait', '生成视频 · 等待结果', '/api/v3/contents/generations/tasks/{id}', obj({'id': text('视频任务 ID')}),
         '持久查询已有任务，支持 pending / queued / running；成功返回 content.video_url。重启继续查询，不重新生视频。'
         '结果链接可能过期；服务是否实际返回音轨以回执为准。', {'id': ''}),
        ('image_upload', '上传参考图为 HTTPS 地址', '/openapi/v2/image/upload', obj({'image': text('原图文件 ID')}),
         '将原图 artifact ID 以 multipart 上传，返回 result.Resp.img_url。用于不接受内联原图的视频服务；'
         '这是独立的兼容上传协议，所选服务必须提供此路径。', {'image': ''}),
    ]
    return {f'library.media.{key}': {'id': f'library.media.{key}', 'title': title, 'path': path,
        'description': desc, 'input_schema': schema, 'defaults': defaults,
        'category': '默认媒体节点', 'component_type': 'node', 'builtin_media': True,
        'docs': ['https://github.com/DongSky/EasyAgent/blob/main/docs/DEFAULT_MEDIA_NODES.md'],
        'capability': 'image_edit' if key == 'image_edit' else 'image' if key == 'image_generate' else 'video',
        'protocol': '图像生成兼容协议' if key.startswith('image_') and key != 'image_upload' else
                    '异步视频任务协议' if key.startswith('video_') else '参考图上传兼容协议'}
        for key, title, path, schema, desc, defaults in rows}


def install(hub, identifier, options):
    from .node_library import PublishComponent

    recipe = definitions()[identifier]
    if not options.connection:
        raise ValueError('请选择已保存的模型服务连接；节点定义已内置，无需填写接口 schema')
    binding = hub.models.bindings.get(options.connection)
    if not binding or not isinstance(binding.provider, HTTPProvider) or binding.provider.dialect == 'anthropic':
        raise ValueError('请选择使用兼容 HTTP 接口的已连接服务')
    if options.endpoint or options.api_key or options.api_key_env:
        raise ValueError('默认媒体节点使用已保存连接的地址和加密凭证，请在模型与服务中管理')
    provider = binding.provider
    from .model_catalog import gateway_root
    root = gateway_root(provider.base_url)
    name = 'builtin_media.' + hashlib.sha256((options.connection + ':' + identifier).encode()).hexdigest()[:20]
    credential = name + '.key'
    defaults = dict(recipe['defaults'])
    if 'model' in defaults:
        defaults['model'] = options.model or (binding.model if 'image' in binding.capabilities and 'image_' in identifier else defaults['model'])
    is_wait = identifier.endswith('video_wait')
    multipart = identifier.endswith(('image_edit', 'image_upload'))
    description = recipe['description'] + (' 默认模型：' + defaults['model'] + '。' if 'model' in defaults else '')
    definition = HTTPTool(name=name, description=description, url=root + recipe['path'],
        method='GET' if is_wait else 'POST', input_schema=recipe['input_schema'],
        request_encoding='multipart' if multipart else 'json', file_parameters=['image', 'mask'] if multipart else [],
        artifact_url_parameters=['content.*.image_url.url'] if identifier.endswith('video_submit') else [],
        response_mode='json' if is_wait else 'media', max_response_bytes=1_000_000 if is_wait else 10_000_000,
        timeout_seconds=120, effect='read' if is_wait else 'write', idempotent=is_wait,
        api_key_env=credential if provider.api_key else None,
        polling={'status_path': 'status', 'pending': ['pending', 'queued', 'running'], 'succeeded': ['succeeded'],
                 'failed': ['failed', 'cancelled', 'expired'], 'interval_seconds': 10,
                 'max_polls': 180, 'deadline_seconds': 1800} if is_wait else None)
    try:
        existing = hub.library.get(identifier)
    except KeyError:
        existing = None
    if existing and not existing.source.id.startswith('builtin_media.'):
        raise Conflict('默认媒体节点名称已被其他组件使用，请保留原定义并使用另一个工作区')
    try:
        previous = hub.development.get('api', name)
    except KeyError:
        previous = None
    with hub.store.connect() as db:
        revoked = previous and db.execute("SELECT 1 FROM memory WHERE namespace='disabled-model-adapters' AND key=?",
                                         (f"{name}@{previous['revision']}",)).fetchone()
    changed = not previous or previous['definition'] != definition.model_dump() or revoked
    if provider.api_key:
        try:
            old_key = hub.connections.secret(credential)
        except ValueError:
            old_key = None
        if old_key != provider.api_key:
            hub.connections.put_secret(credential, provider.api_key)
    saved = hub.development.put('api', name, definition.model_dump(), previous['revision'] if previous else 0) if changed else previous
    hub.store.memory_put('model-media-bindings', f"{name}@{saved['revision']}",
                         {'connection': options.connection, 'api': name, 'revision': saved['revision'],
                          'credential': credential, 'component': identifier}, 'operator')
    hub.development.set_archived('api', name, False)
    hub.development.refresh_api(name)
    if identifier.endswith('video_submit'):
        # Keep the paired query on the same service even after an independent rebind.
        # Waiting remains a separate node; setup never uploads or generates media.
        install(hub, 'library.media.video_wait', options)
    if (existing and existing.source.id == name and existing.source.revision == saved['revision']
            and existing.defaults == defaults):
        return existing
    manifest = hub.library.publish(PublishComponent(id=identifier, kind='api', source_id=name,
        source_revision=saved['revision'], title=recipe['title'], description=description, defaults=defaults,
        expected_revision=existing.revision if existing else 0), docs=recipe['docs'], validation='protocol_integration')
    hub.development.set_archived('component', identifier, False)
    return manifest
