"""Small, bounded file tools for agents; edits match the original file atomically."""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .contracts import ToolSpec
from .local_files import read_file, workspace_path
from .tools import ToolPreparationError

MAX_TEXT = 2_000_000
MAX_OUTPUT = 24_000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text_file(path):
    if path.stat().st_size > MAX_TEXT:
        raise ValueError("Text file exceeds 2 MB; use terminal for bulk processing")
    data = read_file(path)
    if b"\0" in data:
        raise ValueError("Binary file; use an attachment or terminal tool")
    return data, data.decode("utf-8")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".eah-edit-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(hub):
    locks = {}

    def path_for(name, directory=False):
        if directory and name == ".":
            # Validate the configured workspace through the same authority check.
            candidate, _ = workspace_path(hub, ".eah-path-check")
            return candidate.parent
        return workspace_path(hub, name)[0]

    async def read(args, ctx):
        path = path_for(args["path"])
        data, content = await asyncio.to_thread(text_file, path)
        lines = content.splitlines(keepends=True)
        start, limit = args.get("offset", 1) - 1, args.get("limit", 200)
        selected, size = [], 0
        for line in lines[start:start + limit]:
            if size + len(line) > MAX_OUTPUT:
                if not selected:
                    raise ValueError("One line exceeds the output limit; use files.grep or terminal for that line")
                break
            selected.append(line)
            size += len(line)
        following = start + len(selected)
        return {"path": args["path"], "content": "".join(selected), "offset": start + 1,
                "next_offset": following + 1 if following < len(lines) else None,
                "total_lines": len(lines), "sha256": digest(data)}

    async def mutate(args, ctx, edit=False):
        try:
            path = path_for(args["path"])
            async with locks.setdefault(str(path), asyncio.Lock()):
                old_bytes, original = await asyncio.to_thread(text_file, path) if path.exists() else (b"", "")
                if edit and not path.exists():
                    raise ValueError("File does not exist; use files.write to create it")
                if args.get("expected_sha256") and digest(old_bytes) != args["expected_sha256"]:
                    raise ValueError("File changed since it was read; read it again before editing")
                if edit:
                    replacements = []
                    for item in args["edits"]:
                        before = item["old_text"]
                        pos = original.find(before)
                        if pos < 0 or original.find(before, pos + 1) >= 0:
                            raise ValueError("old_text must match exactly once in the original file")
                        replacements.append((pos, pos + len(before), item["new_text"]))
                    replacements.sort()
                    if any(a[1] > b[0] for a, b in zip(replacements, replacements[1:])):
                        raise ValueError("Edits overlap; merge them into one replacement")
                    content = original
                    for start, end, replacement in reversed(replacements):
                        content = content[:start] + replacement + content[end:]
                else:
                    content = args["content"]
                data = content.encode("utf-8")
                if len(data) > MAX_TEXT:
                    raise ValueError("Text file exceeds 2 MB")
                patch = "".join(difflib.unified_diff(original.splitlines(True), content.splitlines(True),
                                                  fromfile=args["path"], tofile=args["path"]))
        except (ValueError, OSError, UnicodeError) as exc:
            raise ToolPreparationError(str(exc)) from exc
        # Keep write failures distinct from preflight rejection for uncertain-write recovery.
        async with locks.setdefault(str(path), asyncio.Lock()):
            current = await asyncio.to_thread(read_file, path) if path.exists() else b""
            if current != old_bytes:
                raise ToolPreparationError("File changed during edit; read it again")
            await asyncio.to_thread(atomic_write, path, data)
        return {"path": args["path"], "bytes": len(data), "sha256": digest(data),
                "diff": patch[:MAX_OUTPUT], "diff_truncated": len(patch) > MAX_OUTPUT}

    async def write(args, ctx):
        return await mutate(args, ctx)

    async def edit(args, ctx):
        return await mutate(args, ctx, edit=True)

    def list_files(root, pattern, hidden, limit):
        found = []
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", ".venv")
                             and (hidden or not d.startswith(".")) and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                file = Path(directory) / name
                relative = file.relative_to(root).as_posix()
                if (not hidden and name.startswith(".")) or file.is_symlink():
                    continue
                if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern):
                    found.append(relative)
                    if len(found) > limit:
                        return found[:limit], True
        return found, False

    async def find(args, ctx):
        root = path_for(args.get("path", "."), directory=True)
        if not root.is_dir():
            raise ValueError("files.find needs a directory")
        files, truncated = await asyncio.to_thread(list_files, root, args.get("glob", "*"),
                                                   args.get("hidden", False), args.get("limit", 200))
        return {"path": args.get("path", "."), "files": files, "truncated": truncated}

    async def grep(args, ctx):
        root = path_for(args.get("path", "."), directory=True)
        limit, matches = args.get("limit", 100), []
        binary = shutil.which("rg")
        if not binary:
            if not args.get("literal", True):
                raise ValueError("Regex search requires ripgrep; install rg or use literal=true")
            names, cut = await asyncio.to_thread(list_files, root, args.get("glob", "*"), False, 10000) if root.is_dir() else ([root.name], False)
            for name in names:
                try:
                    _, content = await asyncio.to_thread(text_file, root / name if root.is_dir() else root)
                except (ValueError, OSError, UnicodeError):
                    continue
                for line_number, line in enumerate(content.splitlines(), 1):
                    if args["pattern"] in line:
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
                        matches.append({"path": name, "line": line_number, "text": line[:200]})
            return {"matches": matches, "truncated": cut}
        argv = [binary, "--json", "--max-columns", "2000", "--glob", "!.git/**", "--glob", "!node_modules/**",
                "--glob", "!.venv/**"]
        if args.get("literal", True):
            argv.append("--fixed-strings")
        if args.get("glob"):
            argv.extend(["--glob", args["glob"]])
        argv.extend(["--", args["pattern"], "." if root.is_dir() else root.name])
        process = await asyncio.create_subprocess_exec(*argv, cwd=root if root.is_dir() else root.parent,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_TEXT + 10000)
        truncated = False
        try:
            async with asyncio.timeout(20):
                while line := await process.stdout.readline():
                    event = json.loads(line)
                    if event["type"] != "match":
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                    data = event["data"]
                    matches.append({"path": data["path"].get("text", ""), "line": data["line_number"],
                                    "text": data["lines"].get("text", "")[:200].rstrip("\n")})
                if not truncated:
                    error = await process.stderr.read(4000)
                    if await process.wait() not in (0, 1):
                        raise ValueError(error.decode(errors="replace"))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return {"matches": matches, "truncated": truncated}

    path = {"type": "string", "minLength": 1, "description": "Workspace-relative path"}
    checksum = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    location = {"path": path, "glob": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200}}
    definitions = [
        ("read", read, "Read UTF-8 text with one-based line offset, bounded output and next_offset. Read before editing.",
         {"path": path, "offset": {"type": "integer", "minimum": 1},
          "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, ["path"], "read"),
        ("write", write, "Create or replace a UTF-8 file atomically. Prefer edit for existing files; expected_sha256 detects concurrent changes.",
         {"path": path, "content": {"type": "string", "maxLength": MAX_TEXT}, "expected_sha256": checksum}, ["path", "content"], "write"),
        ("edit", edit, "Apply unique, non-overlapping exact text replacements against the ORIGINAL file in one atomic write. Returns a diff. No partial edits on mismatch.",
         {"path": path, "expected_sha256": checksum, "edits": {"type": "array", "minItems": 1, "maxItems": 100,
          "items": {"type": "object", "properties": {"old_text": {"type": "string", "minLength": 1},
                    "new_text": {"type": "string"}}, "required": ["old_text", "new_text"], "additionalProperties": False}}},
         ["path", "edits"], "write"),
        ("find", find, "Find workspace files by glob. Skips symlinks, .git, node_modules and .venv; bounded results.",
         {**location, "hidden": {"type": "boolean"}}, [], "read"),
        ("grep", grep, "Search file contents with ripgrep, returning paths, line numbers and bounded excerpts. literal=true by default; false enables regex.",
         {**location, "pattern": {"type": "string", "minLength": 1, "maxLength": 2000}, "literal": {"type": "boolean", "default": True}},
         ["pattern"], "read"),
    ]
    for name, handler, description, properties, required, effect in definitions:
        hub.tools.register(ToolSpec(name="files." + name, description=description, effect=effect,
            idempotent=effect == "read", input_schema={"type": "object", "properties": properties,
                "required": required, "additionalProperties": False}), handler)
