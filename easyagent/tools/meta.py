"""fetch_docs / probe / scaffold — capability-gap tooling.

SSRF protection follows the spirit of the original
``capability_research.py::public_url``: only http/https, no credentials in the
URL, every resolved IP must be globally routable (no private / loopback /
link-local ranges, which also covers the 169.254.169.254 cloud-metadata
address), redirect targets re-validated, <=2MB bodies, 20s timeout.

``scaffold`` writes ``plugins/inbox/<name>/{manifest.json, tool.json, impl.py,
fixtures.json}``; ``impl.py`` is generated from the recipe as an httpx-based
``run(args, ctx)`` implementation.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 20.0
USER_AGENT = "EasyAgent/0.1 capability-probe"

TOOL_INFOS = [
    {
        "name": "fetch_docs",
        "description": (
            "Fetch a public documentation URL and return {title, text}. SSRF-"
            "protected (http/https only, no private IPs, <=2MB, 20s). Args: url."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "probe",
        "description": (
            "Sandbox HTTP probe of a public API. Same SSRF protection. Args: "
            "method (GET/POST/PUT/DELETE/HEAD), url, headers?, body?. "
            "Returns {ok, status, data, recipe}."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "default": "GET"},
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "body": {},
            },
            "required": ["url"],
        },
    },
    {
        "name": "scaffold",
        "description": (
            "Scaffold a plugin into plugins/inbox/<name>/ "
            "{manifest.json, tool.json, impl.py, fixtures.json}. impl.py is "
            "generated from the recipe as httpx code. Args: name, description, "
            "recipe {method, url, headers?, body?, parse?}."
        ),
        "trust": "trusted",
        "schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "recipe": {"type": "object"},
            },
            "required": ["name", "description", "recipe"],
        },
    },
]


# ------------------------------------------------------------ SSRF guard

def _via_egress_proxy(url: str) -> bool:
    """True when HTTP traffic is routed through an egress proxy.

    In that case local DNS resolution returns the proxy's internal
    addressing (e.g. 198.18.0.0/15), which tells us nothing about the real
    destination — the proxy itself is the SSRF boundary. Enforcing the
    resolved-IP check would false-positive on every external domain.
    """
    scheme = urlsplit(url).scheme.lower()
    env = os.environ
    if env.get("https_proxy") or env.get("HTTPS_PROXY"):
        return True
    if env.get("all_proxy") or env.get("ALL_PROXY"):
        return True
    if scheme == "http" and (env.get("http_proxy") or env.get("HTTP_PROXY")):
        return True
    return False


def _proxy_url() -> str | None:
    """Egress proxy URL from the environment (may carry credentials)."""
    env = os.environ
    return (env.get("HTTPS_PROXY") or env.get("https_proxy")
            or env.get("ALL_PROXY") or env.get("all_proxy")
            or env.get("HTTP_PROXY") or env.get("http_proxy"))


def _ca_verify():
    """TLS verify target: the egress CA bundle when present.

    httpx only honours SSL_CERT_FILE with trust_env=True, but trust_env
    must stay off (its no_proxy parser crashes on bracketed IPv6 like
    "[::1]"). So we pass the bundle explicitly.
    """
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        path = os.environ.get(key)
        if path and os.path.exists(path):
            return path
    return True


def _make_client() -> httpx.Client:
    """httpx client that survives this sandbox's proxy env.

    httpx's no_proxy parser chokes on bracketed IPv6 entries like ``[::1]``
    (``InvalidURL: Invalid port: ':1]'``), so we bypass trust_env entirely
    and hand it the proxy URL explicitly. Local-only hosts are already
    rejected by _check_public_url, so nothing needs no_proxy bypass.
    """
    return httpx.Client(timeout=TIMEOUT, trust_env=False,
                        proxy=_proxy_url(), verify=_ca_verify())


_LOCAL_NAMES = ("localhost",)


def _check_public_url(url: str) -> str:
    """Validate *url*; return it unchanged or raise ValueError."""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs with credentials are not allowed")
    host = parsed.hostname.lower()
    # Explicit block for cloud metadata endpoints (also non-global, but be explicit).
    if host in ("169.254.169.254", "metadata.google.internal", "metadata.google"):
        raise ValueError("cloud metadata addresses are not allowed")
    # IP literals and local names are checked without DNS, in every mode:
    # with trust_env=False there is no no_proxy bypass, so a direct check
    # here is the SSRF boundary for these.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None and not ip.is_global:
        raise ValueError(f"address {ip} is not publicly routable")
    if host in _LOCAL_NAMES or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError(f"local hostname '{host}' is not allowed")
    if _via_egress_proxy(url):
        # Local DNS view is the proxy's internal addressing; the proxy is
        # the SSRF boundary for resolved names. Skip the resolved-IP check
        # (see _via_egress_proxy).
        return url
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed: {exc}") from exc
    if not infos:
        raise ValueError("hostname did not resolve")
    for info in infos:
        rip = ipaddress.ip_address(info[4][0])
        if not rip.is_global:
            raise ValueError(f"resolved address {rip} is not publicly routable")
    return url


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.parts.append(text)


def _read_bounded(resp: httpx.Response) -> bytes:
    data = bytearray()
    for chunk in resp.iter_bytes():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise ValueError("response exceeds 2MB budget")
    return bytes(data)


def fetch_docs(args: dict, ctx: dict | None = None) -> dict:
    url = args.get("url", "")
    try:
        _check_public_url(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        with _make_client() as client:
            for _ in range(4):
                _check_public_url(url)  # re-validate every redirect hop
                resp = client.get(url, headers={"User-Agent": USER_AGENT},
                                  follow_redirects=False)
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                raw = _read_bounded(resp).decode("utf-8", errors="replace")
                if "html" in ctype:
                    parser = _TextExtractor()
                    parser.feed(raw)
                    text = " ".join(parser.parts)
                else:
                    text = raw
                title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
                title = title_m.group(1).strip()[:200] if title_m else url
                return {
                    "ok": True,
                    "url": url,
                    "title": title,
                    "text": text[:24000],
                    "untrusted_reference": True,
                }
    except Exception as exc:
        log.info("fetch_docs failed (%s)", type(exc).__name__)
        return {"ok": False, "error": f"{type(exc).__name__}"}
    return {"ok": False, "error": "too many redirects"}


def probe(args: dict, ctx: dict | None = None) -> dict:
    method = str(args.get("method", "GET")).upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "HEAD"):
        return {"ok": False, "error": f"method not allowed: {method}"}
    url = args.get("url", "")
    try:
        _check_public_url(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    headers = args.get("headers") or {}
    body = args.get("body")
    if not isinstance(headers, dict):
        return {"ok": False, "error": "headers must be an object"}
    # Never let caller-supplied headers smuggle auth.
    headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("authorization", "proxy-authorization", "cookie")
    }
    try:
        with _make_client() as client:
            _check_public_url(url)
            resp = client.request(
                method, url,
                headers={"User-Agent": USER_AGENT, **headers},
                json=body if isinstance(body, (dict, list)) else None,
                content=body if isinstance(body, str) else None,
            )
            raw = _read_bounded(resp)
            try:
                data = json.loads(raw)
            except ValueError:
                data = raw.decode("utf-8", errors="replace")[:8000]
            ok = 200 <= resp.status_code < 300
            result: dict = {"ok": ok, "status": resp.status_code, "data": data}
            if ok:
                result["recipe"] = {
                    "method": method, "url": url, "headers": headers,
                    "note": "successful probe; use as scaffold recipe",
                }
            return result
    except Exception as exc:
        log.info("probe failed (%s)", type(exc).__name__)
        return {"ok": False, "error": f"{type(exc).__name__}"}


# ------------------------------------------------------------ scaffold

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def plugins_root() -> Path:
    root = os.environ.get(
        "EASYAGENT_PLUGINS",
        str(Path.home() / "workspace" / "easyagent-rewrite" / "plugins"),
    )
    return Path(root).expanduser()


_IMPL_TEMPLATE = '''"""Generated plugin: {name}. Implements run(args, ctx) -> dict."""
from __future__ import annotations

import os

import httpx

_RECIPE = {recipe!r}
_TIMEOUT = 20.0
_MAX_BYTES = 2 * 1024 * 1024


def _make_client():
    # Bypass trust_env: this sandbox's no_proxy contains bracketed IPv6
    # entries ("[::1]") that crash httpx's parser (InvalidURL). Local-only
    # hosts are rejected by the registry's SSRF guard before we get here.
    # httpx only honours SSL_CERT_FILE with trust_env=True, so pass the
    # egress CA bundle explicitly (the proxy MITMs TLS).
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
    """Call the probed HTTP endpoint described by the recipe."""
    method = _RECIPE.get("method", "GET")
    url = _RECIPE["url"]
    headers = dict(_RECIPE.get("headers") or {{}})
    body = args.get("body", _RECIPE.get("body"))
    try:
        with _make_client() as client:
            resp = client.request(method, url, headers=headers, json=body)
            data = resp.content[:_MAX_BYTES]
            try:
                import json as _json
                payload = _json.loads(data)
            except ValueError:
                payload = data.decode("utf-8", errors="replace")
            return {{"ok": 200 <= resp.status_code < 300,
                     "status": resp.status_code, "data": payload}}
    except Exception as exc:  # noqa: BLE001 - plugin boundary
        return {{"ok": False, "error": type(exc).__name__}}
'''


_SHELL_IMPL_TEMPLATE = '''"""Generated plugin: {name}. Replays recorded shell commands."""
from __future__ import annotations

import shlex
import subprocess

_COMMANDS = {commands!r}
_CWD = {cwd!r}
_TIMEOUT = 30.0
_MAX_BYTES = 1_000_000


def run(args: dict, ctx: dict) -> dict:
    """Replay the recorded commands in order; stop on first failure.

    Optional ``args.args`` (list of strings) is appended as extra argv to the
    final command.
    """
    cmds = list(_COMMANDS)
    if isinstance(args, dict):
        extra = args.get("args")
        if isinstance(extra, list) and extra:
            cmds[-1] = cmds[-1] + " " + " ".join(shlex.quote(str(a)) for a in extra)
    steps = []
    for cmd in cmds:
        try:
            proc = subprocess.run(
                shlex.split(cmd), capture_output=True, text=True,
                timeout=_TIMEOUT, cwd=_CWD or None)
        except Exception as exc:  # noqa: BLE001 - plugin boundary
            return {{"ok": False, "error": f"{{type(exc).__name__}}: {{exc}}",
                     "steps": steps}}
        steps.append({{"command": cmd, "rc": proc.returncode,
                       "stdout": (proc.stdout or "")[:_MAX_BYTES],
                       "stderr": (proc.stderr or "")[:_MAX_BYTES]}})
        if proc.returncode != 0:
            return {{"ok": False,
                     "error": f"command failed (rc={{proc.returncode}}): {{cmd}}",
                     "steps": steps}}
    return {{"ok": True, "steps": steps}}
'''


def _scaffold_shell(name: str, description: str, recipe: dict) -> dict:
    """Scaffold a plugin that replays recorded shell commands (macro plugin).

    Recipe: {"kind": "shell", "commands": [...], "cwd": "..."}. No shell=True
    anywhere; commands are split with shlex and run with a timeout. The
    plugin is untrusted and still goes through the full promote gates
    (fixtures, determinism, leak review, judge).
    """
    raw = recipe.get("commands")
    if isinstance(raw, str):
        raw = [raw]
    commands = [str(c).strip() for c in (raw or []) if str(c).strip()][:10]
    if not commands:
        return {"ok": False,
                "error": "shell recipe needs 'commands' (non-empty list)"}
    if any(len(c) > 2000 for c in commands):
        return {"ok": False, "error": "shell command too long (>2000 chars)"}
    cwd = str(recipe.get("cwd") or "")
    dest = plugins_root() / "inbox" / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {
            "api_version": "1",
            "name": name,
            "version": "0.1.0",
            "description": description,
            "trust": "untrusted",
            "timeout_seconds": 120,
            "max_output_bytes": 1_000_000,
        }
        tool = {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {
                    "args": {
                        "type": "array", "items": {"type": "string"},
                        "description": "extra argv appended to the final command",
                    },
                },
            },
        }
        impl = _SHELL_IMPL_TEMPLATE.format(
            name=name, commands=commands, cwd=cwd)
        fixtures = [
            {
                "name": "smoke",
                "args": {},
                "expect": {"ok": True},
            }
        ]
        (dest / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (dest / "tool.json").write_text(
            json.dumps(tool, indent=2, ensure_ascii=False), encoding="utf-8")
        (dest / "impl.py").write_text(impl, encoding="utf-8")
        (dest / "fixtures.json").write_text(
            json.dumps(fixtures, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"scaffold write failed: {exc}"}
    return {"ok": True, "path": str(dest),
            "files": ["manifest.json", "tool.json", "impl.py", "fixtures.json"]}


def scaffold(args: dict, ctx: dict | None = None) -> dict:
    name = args.get("name", "")
    description = args.get("description", "")
    recipe = args.get("recipe") or {}
    if not _NAME_RE.match(name):
        return {"ok": False, "error": "invalid plugin name"}
    if not isinstance(recipe, dict):
        return {"ok": False, "error": "recipe must be an object"}
    if recipe.get("kind") == "shell":
        return _scaffold_shell(name, description, recipe)
    if "url" not in recipe:
        return {"ok": False, "error": "recipe must be an object with at least a url"}
    try:
        _check_public_url(recipe["url"])
    except ValueError as exc:
        return {"ok": False, "error": f"recipe url rejected: {exc}"}

    dest = plugins_root() / "inbox" / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
        manifest = {
            "api_version": "1",
            "name": name,
            "version": "0.1.0",
            "description": description,
            "trust": "untrusted",
            "timeout_seconds": 30,
            "max_output_bytes": 1_000_000,
        }
        tool = {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": {"body": {"description": "optional request body"}},
            },
        }
        safe_recipe = {
            k: recipe.get(k)
            for k in ("method", "url", "headers", "body")
            if recipe.get(k) is not None
        }
        impl = _IMPL_TEMPLATE.format(name=name, recipe=safe_recipe)
        fixtures = [
            {
                "name": "smoke",
                "args": {},
                "expect": {"ok": True},
            }
        ]
        (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        (dest / "tool.json").write_text(json.dumps(tool, indent=2, ensure_ascii=False), encoding="utf-8")
        (dest / "impl.py").write_text(impl, encoding="utf-8")
        (dest / "fixtures.json").write_text(json.dumps(fixtures, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"scaffold write failed: {exc}"}
    return {"ok": True, "path": str(dest), "files": ["manifest.json", "tool.json", "impl.py", "fixtures.json"]}
