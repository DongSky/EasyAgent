"""file.read / file.write / file.edit.

All paths are constrained inside ``EASYAGENT_WORKSPACE``
(default ``~/workspace/easyagent-rewrite/work``). Paths are resolved
(symlinks included) and any escape outside the workspace is refused.
"""
from __future__ import annotations

import os
from pathlib import Path

TOOL_INFOS = [
    {
        "name": "file.read",
        "description": (
            "Read a text file inside the workspace. Args: path (workspace-"
            "relative or absolute), offset (1-based first line, default 1), "
            "limit (max lines, default 200). Returns {ok, path, lines, "
            "total_lines} or {ok:false, error}."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "default": 1, "minimum": 1},
                "limit": {"type": "integer", "default": 200, "minimum": 1},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file.write",
        "description": (
            "Write content to a file inside the workspace (creates parent "
            "dirs). Args: path, content. Returns {ok, path, bytes}."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "file.edit",
        "description": (
            "Replace exact old_text with new_text in a workspace file. "
            "old_text must occur exactly once. Args: path, old_text, "
            "new_text. Returns {ok, path} or {ok:false, error}."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
]


def workspace_root() -> Path:
    root = os.environ.get(
        "EASYAGENT_WORKSPACE",
        str(Path.home() / "workspace" / "easyagent-rewrite" / "work"),
    )
    p = Path(root).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_inside(path: str) -> Path:
    """Resolve *path* and ensure it stays inside the workspace."""
    root = workspace_root()
    candidate = (root / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise PermissionError(f"path escapes workspace: {path}")
    return candidate


def read(args: dict, ctx: dict | None = None) -> dict:
    try:
        target = resolve_inside(args.get("path", ""))
    except (PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if not target.is_file():
        return {"ok": False, "error": f"not a file: {args.get('path')}"}
    offset = max(1, int(args.get("offset", 1) or 1))
    limit = max(1, int(args.get("limit", 200) or 200))
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    lines = text.splitlines()
    window = lines[offset - 1 : offset - 1 + limit]
    return {
        "ok": True,
        "path": str(target),
        "lines": window,
        "total_lines": len(lines),
        "offset": offset,
    }


def write(args: dict, ctx: dict | None = None) -> dict:
    try:
        target = resolve_inside(args.get("path", ""))
    except (PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    content = args.get("content", "")
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        target.write_bytes(data)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "path": str(target), "bytes": len(data)}


def edit(args: dict, ctx: dict | None = None) -> dict:
    try:
        target = resolve_inside(args.get("path", ""))
    except (PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    old_text = args.get("old_text", "")
    new_text = args.get("new_text", "")
    if not old_text:
        return {"ok": False, "error": "old_text must not be empty"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    count = text.count(old_text)
    if count == 0:
        return {"ok": False, "error": "old_text not found"}
    if count > 1:
        return {"ok": False, "error": f"old_text occurs {count} times; must be unique"}
    try:
        target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "path": str(target)}


def run(args: dict, ctx: dict | None = None) -> dict:
    """Dispatch entry point (registry calls run with the tool's own args)."""
    raise RuntimeError("use file.read / file.write / file.edit handlers directly")
