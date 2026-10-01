"""hermes_search: free, keyless web search via DuckDuckGo.

Primary: instant-answer API (api.duckduckgo.com). Fallback: lite HTML
endpoint (lite.duckduckgo.com) parsed for result links, because the
instant-answer API returns empty RelatedTopics/Results for most queries.

Transport uses urllib (stdlib): it honours the environment's egress proxy,
whereas httpx chokes on some proxy/no_proxy forms (InvalidURL on
bracketed IPv6 no_proxy entries).

15s timeout per attempt. Any failure (network, offline, bad payload,
bot challenge) returns an empty list instead of raising, so the ReAct
loop can continue or degrade.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

TOOL_INFO = {
    "name": "hermes_search",
    "description": (
        "Free keyless web search (DuckDuckGo; instant-answer API with "
        "lite-HTML fallback). Args: query (string), limit (default 5). "
        "Returns [{title, url, snippet}]; returns [] on any failure, "
        "never raises."
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
_LITE_URL = "https://lite.duckduckgo.com/lite/"
_UA = "EasyAgent/0.1 hermes-search"
_LITE_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _http_get_text(url: str, user_agent: str, timeout: float = 15.0) -> str:
    """GET url, return decoded body; raise on non-200."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return resp.read().decode("utf-8", errors="replace")


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


def _lite_search(query: str, limit: int) -> list[dict]:
    """Fallback: scrape lite.duckduckgo.com result links + snippets."""
    try:
        html = _http_get_text(
            _LITE_URL + "?" + urllib.parse.urlencode({"q": query}), _LITE_UA
        )
    except Exception as exc:
        log.info("hermes_search lite failed (%s); returning []", type(exc).__name__)
        return []

    links: list[tuple[str, str]] = []  # (title, url)
    for m in re.finditer(r"<a\s+([^>]*?)>([^<]*)</a>", html):
        attrs, text = m.group(1), m.group(2).strip()
        if "result-link" not in attrs or not text:
            continue
        href_m = re.search(r'href="([^"]+)"', attrs)
        if not href_m:
            continue
        href = href_m.group(1)
        uddg = re.search(r"[?&]uddg=([^&]+)", href)
        url = urllib.parse.unquote(uddg.group(1)) if uddg else href
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith("http"):
            continue
        if "duckduckgo.com/y.js" in url or "ad_domain=" in url:
            continue  # skip ad wrappers (uddg is url-encoded in href)
        links.append((text, url))
        if len(links) >= limit:
            break

    snippets = [
        re.sub(r"<[^>]+>", "", s).strip()
        for s in re.findall(r"<td class='result-snippet'>(.*?)</td>", html, re.S)
    ]
    return [
        {
            "title": title[:160],
            "url": url,
            "snippet": (snippets[i][:500] if i < len(snippets) else ""),
        }
        for i, (title, url) in enumerate(links)
    ]


def run(args: dict, ctx: dict | None = None) -> list[dict]:
    query = (args.get("query") or "").strip()
    limit = max(1, min(20, int(args.get("limit", 5) or 5)))
    if not query:
        return []
    params = urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"}
    )
    try:
        data = json.loads(_http_get_text(_DDG_URL + "?" + params, _UA))
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
    if not out:
        # instant-answer API is empty for most queries; fall back to lite HTML
        out = _lite_search(query, limit)
    return out[:limit]
