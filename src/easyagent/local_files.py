"""Built-in artifact downloads, workspace exchange, and real image decoding."""
from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import os
from pathlib import Path
import stat
from importlib.util import find_spec

from .contracts import ToolSpec
from .public_download import LIMIT, download


def inspect_image(content):
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(content)) as source:
            if source.width * source.height > 50_000_000 or getattr(source, "n_frames", 1) != 1:
                raise ValueError("仅支持不超过 5000 万像素的单帧图片。")
            source.verify()
        with Image.open(io.BytesIO(content)) as source:
            source.load()  # Header metadata alone does not prove the pixel data is valid.
            return {"width": source.width, "height": source.height, "format": source.format,
                    "media_type": Image.MIME.get(source.format, "application/octet-stream"),
                    "frames": 1, "decoded": True, "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()}
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("文件不是完整、可解码的图片，未通过核验。") from exc


def workspace_path(hub, name):
    settings = hub.execution.settings()
    if not settings.terminal_enabled or not settings.workspace:
        raise PermissionError("请在本机执行设置中启用工作目录；无需 API Key。")
    root = Path(settings.workspace).resolve()
    path = Path(name)
    if path.is_absolute() or not name or path.drive or ".." in path.parts:
        raise ValueError("文件路径必须相对于工作目录，不能包含上级目录。")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise PermissionError("文件路径超出工作目录。")
    return resolved, str(path)


def read_file(path):
    # Reject special files before opening (e.g. FIFO), then check the actual descriptor.
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("只能导入普通文件。")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError("只能导入不超过 50 MB 的普通文件。")
        content = stream.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise ValueError("文件超过 50 MB。")
    return content


def install(hub):
    images = find_spec("PIL") is not None
    artifact_schema = {"type": "object", "properties": {
        "id": {"type": "string"}, "name": {"type": "string"}, "media_type": {"type": "string"},
        "size": {"type": "integer"}, "digest": {"type": "string"},
    }, "required": ["id", "name", "media_type", "size", "digest"]}
    output = {"type": "object", "properties": {"artifact": artifact_schema,
        "image": {"type": ["object", "null"], "properties": {
            "width": {"type": "integer"}, "height": {"type": "integer"}, "decoded": {"type": "boolean"},
            "format": {"type": "string"}, "sha256": {"type": "string"},
        }}}, "required": ["artifact", "image"]}

    def save(content, name, mime, ctx, require_image=False):
        image = None
        if require_image or mime.startswith("image/"):
            if not images:
                raise ValueError('图片核验需要安装 easyagent[media]；桌面版已内置。')
            image = inspect_image(content)
            mime = image["media_type"]
        artifact = hub.artifacts.put(name, content, mime, ctx.run_id)
        return {"artifact": artifact, "image": image}

    async def fetch(args, ctx):
        content, mime = await download(args["url"], args.get("max_bytes", LIMIT))
        # Do not use server filenames or signed URL query strings as local filenames.
        name = args.get("name") or "download" + (mimetypes.guess_extension(mime) or ".bin")
        return await asyncio.to_thread(save, content, name, mime, ctx, args.get("require_image", True))

    async def inspect(args, ctx):
        def work():
            artifact, content = hub.artifacts.get(args["artifact_id"])
            return {"artifact": artifact, "image": inspect_image(content)}
        return await asyncio.to_thread(work)

    async def import_file(args, ctx):
        def work():
            path, _ = workspace_path(hub, args["path"])
            content = read_file(path)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return save(content, path.name, mime, ctx, args.get("require_image", False))
        return await asyncio.to_thread(work)

    async def export_file(args, ctx):
        def work():
            path, relative = workspace_path(hub, args["path"])
            artifact, content = hub.artifacts.get(args["artifact_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                if hashlib.sha256(read_file(path)).hexdigest() != artifact["digest"]:
                    raise ValueError("目标文件已有不同内容，请使用另一个文件名。") from None
            return {"artifact": artifact, "path": relative, "size": len(content), "sha256": artifact["digest"]}
        return await asyncio.to_thread(work)

    def register(name, description, properties, required, handler, schema=output):
        hub.tools.register(ToolSpec(name="attachments." + name, description=description,
            effect="local", idempotent=True,
            input_schema={"type": "object", "properties": properties, "required": required,
                          "additionalProperties": False}, output_schema=schema), handler)

    identifier = {"type": "string", "minLength": 1}
    path = {"type": "string", "minLength": 1, "maxLength": 500,
            "description": "File path relative to the configured local workspace"}
    register("download", "Download a public HTTP(S) URL as a durable artifact; no API key or terminal setup needed. "
        "Preserves original bytes, maximum 50 MB, validates/pins public IPs on each redirect. "
        "require_image defaults to true: fully decode a single-frame image (up to 50 million pixels), return image.width/height "
        "and artifact.id. Use require_image=false for other files. No login/cookies or private network access.",
        {"url": {"type": "string", "minLength": 1, "maxLength": 8000},
         "name": {"type": "string", "minLength": 1, "maxLength": 200},
         "max_bytes": {"type": "integer", "minimum": 1, "maximum": LIMIT},
         "require_image": {"type": "boolean", "default": True}}, ["url"], fetch)
    if images:
        register("inspect_image", "Actually decode and verify an existing single-frame image artifact (up to 50 million pixels). "
            "Returns artifact and image.width, image.height, image.format, image.sha256 and image.decoded=true. "
            "Rejects corrupt images. Does not modify the original or perform semantic visual recognition.",
            {"artifact_id": identifier}, ["artifact_id"], inspect)
    register("import_file", "Import a real file produced in the local workspace into durable attachments (maximum 50 MB). "
        "Returns artifact.id; image files are decoded and verified. Original bytes are preserved. "
        "Set require_image=true to demand image verification even without a recognized filename extension.",
        {"path": path, "require_image": {"type": "boolean", "default": False}}, ["path"], import_file)
    register("export_file", "Copy an existing artifact's original bytes into the local workspace for terminal/Python processing. "
        "Returns the workspace-relative path; never overwrites a different existing file.",
        {"artifact_id": identifier, "path": path}, ["artifact_id", "path"], export_file,
        {"type": "object", "properties": {"artifact": artifact_schema, "path": path,
            "size": {"type": "integer"}, "sha256": {"type": "string"}},
         "required": ["artifact", "path", "size", "sha256"]})
