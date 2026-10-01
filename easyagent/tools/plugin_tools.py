"""plugin.promote — hot-update entrypoint for the agent itself.

Runs an inbox plugin's fixtures offline; on success moves it to
``plugins/active/`` and registers it, so the new tool is callable in the
*next* ReAct iteration (the loop rebuilds the tool list every turn).
Failures stay in the inbox with a rejection record.
"""
from __future__ import annotations

TOOL_INFOS = [
    {
        "name": "plugin.promote",
        "description": (
            "Promote a plugin from plugins/inbox/<name>/ to active: runs its "
            "fixtures.json offline; on success the tool is hot-loaded and "
            "usable immediately. Use after scaffold (or after writing "
            "impl.py by hand) to finish building a new capability. "
            "Args: name (string)."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
]

_REGISTRY = None


def set_registry(registry) -> None:
    """Bind the runner's registry (called by MissionRunner.__init__)."""
    global _REGISTRY
    _REGISTRY = registry


def promote(args: dict, ctx: dict | None = None) -> dict:
    reg = _REGISTRY
    if reg is None:
        return {"ok": False, "error": "no registry bound to plugin tools"}
    name = str(args.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "plugin name required"}
    try:
        return dict(reg.promote(name))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
