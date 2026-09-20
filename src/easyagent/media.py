"""Normalize native JSON/base64/binary media into bounded, reusable artifact references."""
import base64
import json

from .artifacts import Artifacts
from .store import encode


def media_result(content, content_type, context, name='response.bin'):
    if not context.store or not content:
        raise ValueError('media response needs a workflow and nonempty content')
    artifacts = Artifacts(context.store)
    mime = content_type.split(';')[0].strip().lower()
    outputs = []

    def save(data, media_type, filename):
        info = artifacts.put(filename, data, media_type, context.run_id)
        outputs.append(info)
        return info

    def media_type(data):
        if data.startswith(b'\x89PNG'):
            return 'image/png', 'png'
        if data.startswith(b'\xff\xd8\xff'):
            return 'image/jpeg', 'jpg'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return 'image/webp', 'webp'
        return 'application/octet-stream', 'bin'

    def walk(value):
        if isinstance(value, list):
            return [walk(v) for v in value]
        if not isinstance(value, dict):
            return value
        value = dict(value)
        if isinstance(value.get('b64_json'), str):
            try:
                binary = base64.b64decode(value.pop('b64_json'), validate=True)
            except ValueError:
                raise ValueError('invalid base64 image response') from None
            kind, ext = media_type(binary)
            value['artifact'] = save(binary, kind, f'image-{len(outputs)+1}.{ext}')
        for key in ('inlineData', 'inline_data'):
            inline = value.get(key)
            if isinstance(inline, dict) and isinstance(inline.get('data'), str):
                kind = inline.get('mimeType', inline.get('mime_type', 'application/octet-stream'))
                if not kind.startswith(('image/', 'audio/', 'video/')):
                    raise ValueError('unsupported inline media type')
                try:
                    binary = base64.b64decode(inline['data'], validate=True)
                except ValueError:
                    raise ValueError('invalid inline media data') from None
                extension = {'image/png': 'png', 'image/jpeg': 'jpg', 'audio/mpeg': 'mp3', 'video/mp4': 'mp4'}.get(kind, 'bin')
                value[key] = {'artifact': save(binary, kind, f'media-{len(outputs)+1}.{extension}')}
        return {k: walk(v) for k, v in value.items()}

    if mime == 'application/json' or mime.endswith('+json'):
        try:
            raw = json.loads(content)
        except (ValueError, UnicodeError):
            raise ValueError('invalid JSON media response') from None
        result = walk(raw)
        if len(encode(result).encode()) > 800_000:
            receipt = artifacts.put('response.json', content, 'application/json', context.run_id)
            return {'result': None, 'result_artifact': receipt, 'artifacts': outputs, 'inline_result': False}
        return {'result': result, 'artifacts': outputs, 'inline_result': True}
    if mime.startswith(('audio/', 'image/', 'video/')) or mime in ('application/octet-stream', 'text/event-stream', 'text/plain'):
        if name == 'response.bin':
            name = {'audio/mpeg': 'speech.mp3', 'audio/wav': 'speech.wav', 'image/png': 'image.png',
                    'video/mp4': 'video.mp4', 'text/event-stream': 'events.sse', 'text/plain': 'response.txt'}.get(mime, name)
        save(content, mime, name)
        return {'result': {}, 'artifacts': outputs, 'inline_result': True}
    raise ValueError('unexpected media response content type')
