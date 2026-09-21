"""Portable extension packages, digests and signature validation."""

import base64
from pathlib import PurePosixPath
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from ..components import digest
from ..extension_contracts import ExtensionManifest, ExtensionPackage


def build_package(manifest, files, *, publisher=None, signing_key=None):
    body = {
        "format": "easyagent.extension.v1",
        "manifest": ExtensionManifest.model_validate(manifest).model_dump(),
        "files": files,
        "publisher": publisher,
    }
    checksum = digest(body)
    signature = base64.b64encode(signing_key.sign(checksum.encode())).decode() if signing_key else None
    return ExtensionPackage(**body, digest=checksum, signature=signature)


def validate_package(raw, publishers):
    package = ExtensionPackage.model_validate(raw)
    if digest(package.model_dump(exclude={"digest", "signature"})) != package.digest:
        raise ValueError("extension package digest mismatch")
    if len(package.model_dump_json().encode()) > 1_500_000 or len(package.files) > 100:
        raise ValueError("extension package exceeds size/file limit")
    for name, content in package.files.items():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or str(path) != name
            or not name
            or "\x00" in content
        ):
            raise ValueError("extension files must be portable relative text paths")
    manifest = package.manifest
    if manifest.entrypoint not in package.files:
        raise ValueError("extension entrypoint missing")
    for view in manifest.views:
        if view.entrypoint and view.entrypoint not in package.files:
            raise ValueError("UI entrypoint missing")
    for name, value in manifest.lock.items():
        if name not in package.files or digest(package.files[name]) != value:
            raise ValueError("dependency lock/file digest mismatch")
    if package.signature:
        if not package.publisher or package.publisher not in publishers:
            raise PermissionError("unknown extension publisher")
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(publishers[package.publisher])).verify(
                base64.b64decode(package.signature), package.digest.encode()
            )
        except Exception as exc:
            raise PermissionError("invalid extension signature") from exc
    return package
