"""hermes_search: free, keyless web search via the DuckDuckGo instant-answer API.

15s timeout. Any failure (network, offline, bad payload) returns an empty
list instead of raising, so the ReAct loop can continue or degrade.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

TOOL_INFO = {
    "name": "hermes_search",
    "description": (
        "Free keyless web search (DuckDuckGo instant answers). Args: query "
        "(string), limit (default 5). Returns [{title, url, snippet}]; "
        "returns [] on any failure, never raises."
    ),
    "trust": "trusted",
    "schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    },
}

_DDG_URL = "https://api.duckduckgo.com/"


def _collect_topics(topics, out, limit):
    for t in topics:
        if len(out) >= limit:
            break
        if isinstance(t, dict) and "Topics" in t:
            _collect_topics(t.get("Topics") or [], out, limit)
        elif isinstance(t, dict) and t.get("FirstURL"):
            out.append(
                {
                    "title": (t.get("Text") or "")[:160],
                    "url": t.get("FirstURL") or "",
                    "snippet": (t.get("Text") or "")[:500],
                }
            )


def run(args: dict, ctx: dict | None = None) -> list[dict]:
    query = (args.get("query") or "").strip()
    limit = max(1, min(20, int(args.get("limit", 5) or 5)))
    if not query:
        return []
    params = urlencode(
        {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    )
    try:
        resp = httpx.get(
            _DDG_URL + "?" + params,
            timeout=15.0,
            headers={"User-Agent": "EasyAgent/0.1 hermes-search"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network/offline/parse: degrade to []
        log.info("hermes_search failed (%s); returning []", type(exc).__name__)
        return []

    out: list[dict] = []
    abstract_url = data.get("AbstractURL")
    if abstract_url:
        out.append(
            {
                "title": (data.get("AbstractSource") or data.get("Heading") or "")[:160],
                "url": abstract_url,
                "snippet": (data.get("AbstractText") or "")[:500],
            }
        )
    _collect_topics(data.get("RelatedTopics") or [], out, limit)
    _collect_topics(data.get("Results") or [], out, limit)
    return out[:limit]
