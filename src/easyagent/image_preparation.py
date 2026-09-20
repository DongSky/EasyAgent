"""Create a bounded upload copy locally while preserving the original artifact."""
import asyncio
import io
from importlib.util import find_spec

from .contracts import ToolSpec


def install(hub):
    if find_spec('PIL') is None:
        return

    def prepare(args, ctx):
        from PIL import Image, ImageOps, UnidentifiedImageError
        original, content = hub.artifacts.get(args['artifact_id'])
        try:
            with Image.open(io.BytesIO(content)) as source:
                if source.width * source.height > 50_000_000 or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('仅支持不超过 5000 万像素的单帧图片；原图未修改。')
                image = ImageOps.exif_transpose(source).convert('RGBA')
                original_size = image.size
                canvas = Image.new('RGBA', image.size, 'white')
                canvas.alpha_composite(image)
                image = canvas.convert('RGB')
        except UnidentifiedImageError as exc:
            raise ValueError('附件不是可解码的图片；原图未修改。') from exc
        image.thumbnail((args.get('max_side', 3072),) * 2, Image.Resampling.LANCZOS)
        for _ in range(8):
            for quality in (95, 90, 85, 80):
                output = io.BytesIO()
                image.save(output, format='JPEG', quality=quality, optimize=True)
                if output.tell() <= args.get('max_bytes', 1_000_000):
                    artifact = hub.artifacts.put('upload-copy.jpg', output.getvalue(), 'image/jpeg', ctx.run_id)
                    return {'original': original, 'artifact': artifact, 'width': image.width, 'height': image.height,
                            'original_width': original_size[0], 'original_height': original_size[1], 'quality': quality,
                            'note': '仅生成用于上传的 JPEG 副本，原图完整保留；透明区域使用白色背景。'}
            image.thumbnail((max(1, int(image.width * .8)), max(1, int(image.height * .8))), Image.Resampling.LANCZOS)
        raise ValueError('无法在限定次数内生成满足大小要求的副本；原图未修改。')

    async def call(args, ctx):
        return await asyncio.to_thread(prepare, args, ctx)

    hub.tools.register(ToolSpec(name='attachments.prepare_image', effect='local', idempotent=True,
        description='Create a smaller JPEG upload copy from an image artifact locally. Preserves the original; '
        'returns original and artifact metadata (use artifact.id for image APIs). Default max_side=3072, '
        'max_bytes=1000000. Use before editing large images or after an HTTP 413 rejection. '
        'This resizes/compresses only; it does not perform artistic retouching. Transparent areas become white.',
        input_schema={'type': 'object', 'properties': {'artifact_id': {'type': 'string'},
            'max_bytes': {'type': 'integer', 'minimum': 64000, 'maximum': 10_000_000},
            'max_side': {'type': 'integer', 'minimum': 256, 'maximum': 8192}},
            'required': ['artifact_id'], 'additionalProperties': False}), call)
