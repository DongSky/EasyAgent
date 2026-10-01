"""Builtin tool registry: registration helper + first-party tool table.

Each builtin exposes ``run(args, ctx) -> dict`` (same convention as plugin
``impl.py``). Trust levels: ``shell.exec`` is ``untrusted``; everything else
is ``trusted``.
"""
from __future__ import annotations

from . import files, meta, search, shell

_HANDLERS = {
    "shell.exec": shell.run,
    "file.read": files.read,
    "file.write": files.write,
    "file.edit": files.edit,
    "hermes_search": search.run,
    "fetch_docs": meta.fetch_docs,
    "probe": meta.probe,
    "scaffold": meta.scaffold,
}

_INFOS = [shell.TOOL_INFO, *files.TOOL_INFOS, search.TOOL_INFO, *meta.TOOL_INFOS]


def builtin_tools() -> list[dict]:
    """Return the builtin tool table: [{name, description, trust, schema, run}]."""
    infos = {info["name"]: info for info in _INFOS}
    table = []
    for name, handler in _HANDLERS.items():
        info = infos[name]
        table.append(
            {
                "name": name,
                "description": info["description"],
                "trust": info["trust"],
                "schema": info["schema"],
                "run": handler,
            }
        )
    return table


def register_builtins(registry) -> list[str]:
    """Register all builtins into a registry with a ``register(name, handler,
    trust=...)`` method (e.g. easyagent.registry.ToolRegistry). Returns names.
    """
    names = []
    for tool in builtin_tools():
        registry.register_tool(
            tool["name"], tool["run"],
            trust=tool["trust"], description=tool["description"],
            schema=tool.get("schema"),
        )
        names.append(tool["name"])
    return names
