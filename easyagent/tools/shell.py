"""shell.exec: run a command with timeout-kill and bounded output.

Ideas borrowed from the original ``backends.py`` terminal.execute and
``plugins.py`` bounded_read: subprocess timeout kills the process, output is
truncated to a byte budget, empty commands are refused. Default is
``shell=False``; shell interpretation requires explicit ``use_shell=true``.
"""
from __future__ import annotations

import shlex
import subprocess

TOOL_INFO = {
    "name": "shell.exec",
    "description": (
        "Execute a command. Args: command (string or argv list), timeout_s "
        "(default 30), max_output (bytes, default 65536), use_shell (default "
        "false; set true only when shell features are needed). Empty commands "
        "are refused. Output beyond max_output is truncated. "
        "Returns {ok, exit_code, stdout, stderr, truncated}."
    ),
    "trust": "untrusted",
    "schema": {
        "type": "object",
        "properties": {
            "command": {
                "description": "command string or argv list",
            },
            "timeout_s": {"type": "number", "default": 30, "minimum": 1, "maximum": 600},
            "max_output": {"type": "integer", "default": 65536, "minimum": 1024},
            "use_shell": {"type": "boolean", "default": False},
            "cwd": {"type": "string", "description": "working directory (absolute)"},
        },
        "required": ["command"],
    },
}


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    cut = data[:limit].decode("utf-8", errors="ignore")
    return cut + f"\n...[truncated, {len(data) - limit} more bytes]", True


def run(args: dict, ctx: dict | None = None) -> dict:
    command = args.get("command")
    timeout_s = float(args.get("timeout_s", 30) or 30)
    max_output = int(args.get("max_output", 65536) or 65536)
    use_shell = bool(args.get("use_shell", False))
    cwd = args.get("cwd")

    if command is None or (isinstance(command, str) and not command.strip()) or (
        isinstance(command, list) and not command
    ):
        return {"ok": False, "error": "empty command refused"}

    if use_shell:
        if not isinstance(command, str):
            return {"ok": False, "error": "use_shell=true requires a command string"}
        popen_args: str | list = command
        shell = True
    else:
        if isinstance(command, str):
            popen_args = shlex.split(command)
        elif isinstance(command, list) and all(isinstance(c, str) for c in command):
            popen_args = command
        else:
            return {"ok": False, "error": "command must be a string or list of strings"}
        if not popen_args:
            return {"ok": False, "error": "empty command refused"}
        shell = False

    try:
        proc = subprocess.Popen(
            popen_args,
            shell=shell,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"failed to start process: {exc}"}

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except Exception:
            stdout, stderr = "", ""
        timed_out = True

    stdout, t1 = _truncate(stdout or "", max_output)
    stderr, t2 = _truncate(stderr or "", max_output)
    return {
        "ok": (proc.returncode == 0) and not timed_out,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": t1 or t2,
        "timed_out": timed_out,
    }
