from __future__ import annotations

import json
from pathlib import Path

from .models import HTTPProvider
from .plugins import load_plugin
from .http_tools import register_http_tool
from .search import register_tinyfish


async def configure(hub, config_path=None):
    if not config_path:
        return
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    for extension in config.get("extensions", []):
        options = dict(extension)
        options["package"] = json.loads((path.parent / options["package"]).read_text(encoding="utf-8"))
        await hub.extensions.install(options)
    for entry in config.get("search", []):
        entry = dict(entry)
        if entry.pop("provider", "tinyfish") != "tinyfish":
            raise ValueError("unknown search provider; use http_tools or a plugin for custom search")
        register_tinyfish(hub, entry)
    for entry in config.get("http_tools", []):
        register_http_tool(hub, entry)
    for entry in config.get("models", []):
        key_env = entry.get("api_key_env")
        key = hub.development.credential(key_env) if key_env else ""
        if key_env and not key:
            raise ValueError(f"set environment variable {key_env} for model {entry['alias']}")
        hub.models.register(
            entry["alias"],
            HTTPProvider(entry["base_url"], key, entry.get("dialect", "chat")),
            entry["model"],
            entry["capabilities"],
            fallback=entry.get("fallback"),
            output_price_per_million=entry.get("output_price_per_million"),
            input_price_per_million=entry.get("input_price_per_million"),
        )
    for plugin in config.get("plugins", []):
        load_plugin(hub.tools, path.parent / plugin)
    for skill_root in config.get("skills", []):
        hub.skills.discover(path.parent / skill_root)
    for server in config.get("mcp", []):
        from .mcp_bridge import MCPConnection
        connection = MCPConnection(**server["connection"])
        await connection.import_tools(hub.tools, server["prefix"], server["permissions"])

    for entry in config.get("node_library", []):
        entry = dict(entry)
        hub.library.install(entry.pop("id"), entry)

    if connection := config.get("model_catalog"):
        await hub.model_catalog.discover(connection)
