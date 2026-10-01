"""Generated plugin: image_gen. Implements run(args, ctx) -> dict."""
from __future__ import annotations

import os
import urllib.parse
from pathlib import Path

import httpx

_TIMEOUT = 60.0
_MAX_BYTES = 10 * 1024 * 1024


def _make_client():
    env = os.environ
    proxy = (env.get("HTTPS_PROXY") or env.get("https_proxy")
             or env.get("ALL_PROXY") or env.get("all_proxy")
             or env.get("HTTP_PROXY") or env.get("http_proxy"))
    verify = True
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = env.get(key)
        if path and os.path.exists(path):
            verify = path
            break
    return httpx.Client(timeout=_TIMEOUT, trust_env=False,
                        proxy=proxy, verify=verify)


def run(args: dict, ctx: dict) -> dict:
    """Generate image from prompt and save to file."""
    prompt = args.get("prompt", "")
    output_path = args.get("output_path", "/tmp/easyagent-debug/cat_moon.png")

    if not prompt:
        return {"ok": False, "error": "Missing required argument: prompt"}

    # URL encode the prompt
    encoded_prompt = urllib.parse.quote(prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true"

    try:
        with _make_client() as client:
            resp = client.get(url)
            if resp.status_code >= 300:
                return {"ok": False, "error": f"HTTP {resp.status_code}", "status": resp.status_code}

            image_data = resp.content[:_MAX_BYTES]

            # Ensure output directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

            # Save image to file
            with open(output_path, "wb") as f:
                f.write(image_data)

            return {"ok": True, "path": output_path, "size": len(image_data)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
