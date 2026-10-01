"""plugin.promote / plugin.merge / plugin.prune — self-improvement tooling.

- ``plugin.promote``: run an inbox plugin's fixtures offline; on success move
  it to ``plugins/active/`` and register it, so the new tool is callable in
  the *next* ReAct iteration (the loop rebuilds the tool list every turn).
  Failures stay in the inbox with a rejection record.
- ``plugin.merge``: merge two shell-macro plugins into one inbox plugin
  (commands concatenated + deduplicated); the result still needs promote.
- ``plugin.prune``: RRSI pruning of failing/stale plugins (dry-run default).
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
    {
        "name": "plugin.merge",
        "description": (
            "Merge two shell-macro plugins into one inbox plugin: recorded "
            "commands are concatenated and deduplicated. Use when the bypass "
            "counter (or you) created near-duplicate macros and the tool list "
            "is getting bloated. The merged plugin still needs plugin.promote "
            "to pass the full gates. Args: name_a, name_b, new_name."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "name_a": {"type": "string"},
                "name_b": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["name_a", "name_b", "new_name"],
        },
    },
    {
        "name": "plugin.prune",
        "description": (
            "RRSI pruning: demote active plugins that fail too often "
            "(failure_rate >= max_failure_rate over >= min_calls calls) or "
            "were never called within stale_days. Builtin tools are never "
            "pruned. Dry-run by default. Args: dry_run (bool, default true), "
            "min_calls (default 5), max_failure_rate (default 0.5), "
            "stale_days (default 30)."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean"},
                "min_calls": {"type": "integer"},
                "max_failure_rate": {"type": "number"},
                "stale_days": {"type": "number"},
            },
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


def merge(args: dict, ctx: dict | None = None) -> dict:
    reg = _REGISTRY
    if reg is None:
        return {"ok": False, "error": "no registry bound to plugin tools"}
    try:
        return dict(reg.merge_plugins(
            str(args.get("name_a", "")).strip(),
            str(args.get("name_b", "")).strip(),
            str(args.get("new_name", "")).strip(),
        ))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def prune(args: dict, ctx: dict | None = None) -> dict:
    reg = _REGISTRY
    if reg is None:
        return {"ok": False, "error": "no registry bound to plugin tools"}
    try:
        return dict(reg.prune(
            dry_run=bool(args.get("dry_run", True)),
            min_calls=int(args.get("min_calls", 5)),
            max_failure_rate=float(args.get("max_failure_rate", 0.5)),
            stale_days=float(args.get("stale_days", 30)),
        ))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
