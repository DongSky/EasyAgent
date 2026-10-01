"""memory.search / memory.append — long-term learning tools for the agent.

Lets the ReAct loop read and write ``learnings.jsonl`` itself, so capability
recipes discovered during exploration persist across missions.
"""
from __future__ import annotations

TOOL_INFOS = [
    {
        "name": "memory.search",
        "description": (
            "Search past learnings for a query. Returns scored hits "
            "[{ts, text, tags, score}]. Use this FIRST when facing a task "
            "you may have solved before, or when you need a recipe for "
            "building a new tool. Args: query (string), limit (default 5)."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5, "minimum": 1,
                          "maximum": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory.append",
        "description": (
            "Record a learning for future missions. After you successfully "
            "build a new capability (e.g. a promoted plugin), store the "
            "working recipe here so you remember how to do it next time. "
            "Include a 'recipe:' line with actionable details. "
            "Args: text (string), tags (list of strings, optional)."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
        },
    },
    {
        "name": "memory.consolidate",
        "description": (
            "Merge near-duplicate learnings into compact entries so the "
            "memory log stays small and searchable (avoids bloat/forgetting "
            "as learnings accumulate). Deterministic, no LLM. Backs up the "
            "log before merging (atomic rewrite). Args: max_entries (default "
            "200; no-op when the log is at or below this), similarity "
            "(default 0.55), dry_run (default false; when true, report what "
            "would merge without writing)."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "max_entries": {"type": "integer", "default": 200, "minimum": 1},
                "similarity": {"type": "number", "default": 0.55,
                               "minimum": 0.0, "maximum": 1.0},
                "dry_run": {"type": "boolean", "default": False},
            },
        },
    },
]


def _memory_module():
    try:
        from easyagent import memory as mem
        return mem
    except Exception:
        return None


def search(args: dict, ctx: dict | None = None) -> dict:
    mem = _memory_module()
    if mem is None:
        return {"ok": False, "error": "easyagent.memory unavailable"}
    query = str(args.get("query", ""))
    try:
        limit = int(args.get("limit", 5) or 5)
    except (TypeError, ValueError):
        limit = 5
    try:
        hits = mem.search(query, limit=max(1, min(20, limit)))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "hits": hits}


def append(args: dict, ctx: dict | None = None) -> dict:
    mem = _memory_module()
    if mem is None:
        return {"ok": False, "error": "easyagent.memory unavailable"}
    text = str(args.get("text", ""))
    if not text.strip():
        return {"ok": False, "error": "empty learning refused"}
    tags = args.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    try:
        entry = mem.append_learning(text, [str(t) for t in tags])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "ts": entry.get("ts")}


def consolidate(args: dict, ctx: dict | None = None) -> dict:
    mem = _memory_module()
    if mem is None:
        return {"ok": False, "error": "easyagent.memory unavailable"}
    try:
        max_entries = int(args.get("max_entries", 200) or 200)
    except (TypeError, ValueError):
        max_entries = 200
    try:
        similarity = float(args.get("similarity", 0.55))
    except (TypeError, ValueError):
        similarity = 0.55
    try:
        result = mem.consolidate(
            max_entries=max(1, max_entries),
            similarity=min(1.0, max(0.0, similarity)),
            dry_run=bool(args.get("dry_run", False)))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **result}
