"""Configured HTTP APIs become typed tools; credentials stay outside model context."""
from __future__ import annotations

import json
import base64
import os
import re
import time
from typing import Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import Field, model_validator

from .contracts import Contract, ToolSpec


class Polling(Contract):
    status_path: str = Field(default="status", pattern=r"^[A-Za-z0-9_.-]+$")
    pending: list[str] = Field(min_length=1, max_length=20)
    succeeded: list[str] = Field(min_length=1, max_length=20)
    failed: list[str] = Field(min_length=1, max_length=20)
    interval_seconds: float = Field(default=5, ge=0.05, le=300)
    max_polls: int = Field(default=240, ge=1, le=10000)
    deadline_seconds: float = Field(default=1800, gt=0, le=86400)

    @model_validator(mode="after")
    def disjoint(self):
        statuses = self.pending + self.succeeded + self.failed
        if len(set(statuses)) != len(statuses):
            raise ValueError("polling statuses must be distinct")
        return self


class HTTPTool(Contract):
    name: str
    description: str = Field(min_length=1, max_length=4000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    url: str = Field(min_length=1, max_length=2000)
    input_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False})
    output_schema: dict = Field(default_factory=lambda: {"type": "object"})
    parameter_locations: dict[str, Literal["query", "header", "cookie", "body"]] = Field(default_factory=dict)
    body_parameter: str | None = None
    request_encoding: Literal["json", "form", "multipart"] = "json"
    file_parameters: list[str] = Field(default_factory=list)
    artifact_url_parameters: list[str] = Field(default_factory=list, max_length=40)
    response_mode: Literal["json", "text", "artifact", "media"] = "json"
    artifact_name: str = Field(default="response.bin", min_length=1, max_length=200)
    artifact_media_types: list[str] = Field(default_factory=lambda: ["application/octet-stream"])
    max_response_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000)
    polling: Polling | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    api_key_env: str | None = None
    api_key: str = ""
    auth_location: Literal["header", "query"] = "header"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    effect: Literal["read", "write"] | None = None
    idempotent: bool = False
    timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def valid(self):
        for path in self.artifact_url_parameters:
            if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.(?:[A-Za-z0-9_-]+|\*))*", path):
                raise ValueError("artifact URL paths use dotted fields and optional array wildcards")
        if self.artifact_url_parameters and self.request_encoding != "json":
            raise ValueError("artifact URL parameters require JSON encoding")
        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("API URL must be HTTP(S), without embedded credentials or fragment")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote APIs require HTTPS")
        if any(c in parsed.netloc + parsed.query for c in "{}"):
            raise ValueError("placeholders are supported only in the URL path")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,80}", self.auth_header):
            raise ValueError("invalid auth field name")
        if self.auth_location == "header":
            validate_header(self.auth_header, "")
        if any(c in self.auth_prefix + self.api_key for c in "\r\n"):
            raise ValueError("credentials must not contain line breaks")
        ToolSpec(name=self.name, input_schema=self.input_schema, output_schema=self.output_schema)
        if self.polling and (self.method != "GET" or self.response_mode != "json" or self.effect not in (None, "read")):
            raise ValueError("polling requires a read-only GET JSON endpoint")
        if self.file_parameters and self.request_encoding != "multipart":
            raise ValueError("file parameters require multipart encoding")
        if self.response_mode not in ("artifact", "media") and self.max_response_bytes > 1_000_000:
            raise ValueError("large responses must be saved as artifacts")
        if not self.artifact_media_types or any(not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", m) for m in self.artifact_media_types):
            raise ValueError("artifact media types must be explicit MIME types")
        props = self.input_schema.get("properties", {})
        paths = re.findall(r"\{([^{}]+)\}", parsed.path)
        if any(c in re.sub(r"\{[^{}]+\}", "", parsed.path) for c in "{}"):
            raise ValueError("invalid URL placeholder")
        for field in paths:
            if field not in props or field not in self.input_schema.get("required", []):
                raise ValueError("URL path parameters must be declared and required: " + field)
            if field in self.parameter_locations:
                raise ValueError("path parameters cannot also have another location")
        if set(self.parameter_locations) - props.keys():
            raise ValueError("parameter_locations must reference input properties")
        if self.body_parameter and (self.body_parameter not in props or self.body_parameter in paths or self.body_parameter in self.parameter_locations):
            raise ValueError("body_parameter must be a separate declared input property")
        if self.body_parameter and "body" in self.parameter_locations.values():
            raise ValueError("choose body_parameter or individual body fields")
        if self.method in ("GET", "HEAD") and (self.body_parameter or "body" in self.parameter_locations.values()):
            raise ValueError("GET/HEAD tools cannot declare a request body")
        for name, value in self.headers.items():
            validate_header(name, value)
        for name, location in self.parameter_locations.items():
            if location == "header":
                validate_header(name, "")
            if location in ("query", "header") and location == self.auth_location and name.lower() == self.auth_header.lower():
                raise ValueError("authentication fields cannot be supplied by tool arguments")
        if self.auth_header.lower() in {h.lower() for h in self.headers}:
            raise ValueError("use credential fields for authentication, not static headers")
        return self


def validate_header(name, value):
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(c in value for c in "\r\n"):
        raise ValueError("invalid HTTP header")
    if name.lower() in {"host", "content-length", "transfer-encoding", "connection", "idempotency-key"}:
        raise ValueError("transport headers cannot be configured")


def build_http_tool(definition, validate_arguments=None):
    definition = HTTPTool.model_validate(definition)
    key = os.environ.get(definition.api_key_env, "") if definition.api_key_env else definition.api_key
    if definition.api_key_env and not key:
        raise ValueError("set the configured API key environment variable")
    if any(c in key for c in "\r\n"):
        raise ValueError("credentials must not contain line breaks")
    effect = definition.effect or ("read" if definition.method in ("GET", "HEAD", "OPTIONS") else "write")
    spec = ToolSpec(name=definition.name, description=definition.description, input_schema=definition.input_schema,
                    output_schema=definition.output_schema, effect=effect,
                    idempotent=True if effect == "read" else definition.idempotent)

    async def call(arguments, context):
        poll_state = None
        if definition.polling:
            if not context.store or not context.job:
                raise ValueError("durable polling requires a workflow invocation")
            poll_state = context.job["state"].get("http_polls", {}).get(context.invocation_id, {"count": 0, "started": time.time()})
            policy = definition.polling
            if poll_state["count"] >= policy.max_polls or time.time() - poll_state["started"] >= policy.deadline_seconds:
                raise ValueError("remote task polling budget exhausted")
            poll_state = {**poll_state, "count": poll_state["count"] + 1}
            # Record before issuing the request so crashes cannot reset the budget.
            context.store.checkpoint(context.job, {**context.job["state"], "http_polls": {
                **context.job["state"].get("http_polls", {}), context.invocation_id: poll_state}})
        arguments = dict(arguments)
        if validate_arguments:
            arguments = validate_arguments(arguments)
        path_values = {}
        def replace(match):
            name = match.group(1)
            if name not in path_values:
                path_values[name] = arguments.pop(name)
            value = path_values[name]
            if isinstance(value, (dict, list)) or value is None:
                raise ValueError("path parameters must be scalar")
            if str(value) in (".", ".."):
                raise ValueError("path parameters cannot be traversal segments")
            return quote(str(value), safe="")
        url = re.sub(r"\{([^{}]+)\}", replace, definition.url)
        headers = {"Accept": ", ".join(definition.artifact_media_types) if definition.response_mode == "artifact" else "application/json",
                   "Idempotency-Key": context.invocation_id, **definition.headers}
        # Retain any fixed query parameters already configured in the URL.
        params = list(httpx.URL(url).params.multi_items())
        cookies, body = {}, {}
        payload = arguments.pop(definition.body_parameter, None) if definition.body_parameter else None
        for name, value in arguments.items():
            location = definition.parameter_locations.get(name, "query" if definition.method in ("GET", "HEAD") else "body")
            if location == "query":
                values = value if isinstance(value, list) else [value]
                if any(isinstance(v, (dict, list)) for v in values):
                    raise ValueError("query values must be scalar or a list of scalars")
                params.extend((name, str(v).lower() if isinstance(v, bool) else v) for v in values if v is not None)
            elif location == "header":
                validate_header(name, str(value))
                headers[name] = str(value)
            elif location == "cookie":
                cookies[name] = str(value)
            else:
                body[name] = value
        if definition.body_parameter and body:
            raise ValueError("extra body fields cannot be combined with body_parameter")
        if key:
            credential = definition.auth_prefix + key
            if definition.auth_location == "header":
                headers[definition.auth_header] = credential
            else:
                params = [(k, v) for k, v in params if k != definition.auth_header] + [(definition.auth_header, credential)]
        kwargs = {"params": params}
        if definition.method not in ("GET", "HEAD"):
            request_body = payload if definition.body_parameter else body
            if definition.artifact_url_parameters:
                from .artifacts import Artifacts
                # Convert only declared media URL fields, after resolving workflow references.
                # Store small artifact IDs in workflows; inline bytes only on the outbound wire.
                request_body = json.loads(json.dumps(request_body))
                total_media_bytes = 0
                def inline(value, path):
                    nonlocal total_media_bytes
                    if not path:
                        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                            return value  # Ordinary URLs/data URLs keep native semantics.
                        if not context.store:
                            raise ValueError("media artifact input requires workflow artifact storage")
                        info, content = Artifacts(context.store).get(value)
                        if info['media_type'] not in ('image/png','image/jpeg','image/webp','image/gif',
                                'video/mp4','video/webm','audio/mpeg','audio/wav','audio/ogg'):
                            raise ValueError("unsupported media artifact type")
                        total_media_bytes += len(content)
                        if total_media_bytes > 10_000_000:
                            raise ValueError("inline media artifacts exceed 10 MB")
                        return 'data:'+info['media_type']+';base64,'+base64.b64encode(content).decode()
                    head, *tail = path
                    if head == '*' and isinstance(value, list):
                        return [inline(item, tail) for item in value]
                    if isinstance(value, dict) and head in value:
                        value[head] = inline(value[head], tail)
                    return value
                for path in definition.artifact_url_parameters:
                    request_body = inline(request_body, path.split('.'))
            if definition.request_encoding == "multipart":
                if not isinstance(request_body, dict) or not context.store:
                    raise ValueError("multipart requires an object and workflow artifact storage")
                from .artifacts import Artifacts
                parts, total_bytes = [], 0
                for name, value in request_body.items():
                    if name in definition.file_parameters:
                        for identifier in value if isinstance(value, list) else [value]:
                            if not isinstance(identifier, str):
                                raise ValueError("upload files by artifact id, never host file paths")
                            info, content = Artifacts(context.store).get(identifier)
                            total_bytes += len(content)
                            if total_bytes > 10_000_000:
                                raise ValueError("multipart files exceed 10 MB")
                            parts.append((name, (info["name"], content, info["media_type"])))
                    elif value is not None:
                        parts.append((name, (None, value if isinstance(value, str) else json.dumps(value))))
                kwargs["files"] = parts
            else:
                kwargs["json" if definition.request_encoding == "json" else "data"] = request_body

        def strings(value):
            if isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, str):
                yield value

        sensitive = [key, *headers.values(), *cookies.values(), *strings(path_values)]
        sensitive.extend(strings(request_body if definition.method not in ("GET", "HEAD") else arguments))
        sensitive = sorted({value for value in sensitive if value}, key=len, reverse=True)

        def diagnostic(value, limit=600):
            if value is None:
                return None
            if not isinstance(value, (str, int, float, bool)):
                return "[invalid diagnostic value]"
            value = str(value)
            for secret in sensitive:
                value = value.replace(secret, "[redacted]")
            return re.sub(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]+|https?://\S+)", "[redacted]", value)[:limit]

        try:
            async with httpx.AsyncClient(timeout=definition.timeout_seconds, follow_redirects=False, cookies=cookies) as client:
                async with client.stream(definition.method, url, headers=headers, **kwargs) as response:
                    if context.store:
                        with context.store.transaction() as db:
                            context.store.event(db, context.run_id, "http.response", {
                                "invocation_id": context.invocation_id, "status": response.status_code,
                                "content_type": response.headers.get("content-type", "")[:120],
                                "request_ids": {name: diagnostic(response.headers[name], 160) for name in
                                    ("x-request-id", "x-api-request-id", "x-oneapi-request-id", "x-tt-logid")
                                    if name in response.headers},
                            })
                    if not 200 <= response.status_code < 300:
                        # Keep a bounded, sanitized diagnostic, not arbitrary response bodies or headers.
                        diagnostic_body = bytearray()
                        async for chunk in response.aiter_bytes():
                            diagnostic_body.extend(chunk[:8192-len(diagnostic_body)])
                            if len(diagnostic_body) >= 8192:
                                break
                        try:
                            data = json.loads(diagnostic_body)
                            detail = data.get("error", data) if isinstance(data, dict) else {}
                            detail = detail if isinstance(detail, dict) else {}
                            if context.store:
                                with context.store.transaction() as db:
                                    context.store.event(db, context.run_id, "http.error", {
                                        "invocation_id": context.invocation_id, "status": response.status_code,
                                        "message": diagnostic(detail.get("message", "")),
                                        "code": diagnostic(detail.get("code"), 160),
                                    })
                        except (ValueError, UnicodeError):
                            pass
                        error = RuntimeError if response.status_code in (408, 429) or response.status_code >= 500 else ValueError
                        raise error(f"configured API returned HTTP {response.status_code}")
                    result = bytearray()
                    async for chunk in response.aiter_bytes():
                        result.extend(chunk)
                        if len(result) > definition.max_response_bytes:
                            raise ValueError("API response exceeds configured byte budget")
                    if definition.response_mode == "artifact":
                        from .artifacts import Artifacts
                        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                        if media_type not in definition.artifact_media_types:
                            raise ValueError("unexpected media type from configured API")
                        if not result or not context.store:
                            raise ValueError("binary responses require nonempty content and a workflow invocation")
                        return Artifacts(context.store).put(definition.artifact_name, bytes(result), media_type, context.run_id)
                    if definition.response_mode == "media":
                        from .media import media_result
                        return media_result(bytes(result), response.headers.get("content-type", ""), context,
                                            definition.artifact_name)
                    if definition.response_mode == "text":
                        return {"text": result.decode("utf-8"), "content_type": response.headers.get("content-type", "")}
                    try:
                        data = json.loads(result) if result else {}
                    except (ValueError, UnicodeError):
                        raise ValueError("configured API did not return valid JSON") from None
                    if definition.polling:
                        status = data
                        for part in definition.polling.status_path.split("."):
                            if not isinstance(status, dict) or part not in status:
                                raise ValueError("missing remote task status")
                            status = status[part]
                        visible_status = diagnostic(status, 120)
                        remote_error = data.get('error', {})
                        remote_error = remote_error if isinstance(remote_error, dict) else {}
                        with context.store.transaction() as db:
                            context.store.event(db, context.run_id, "http.poll", {
                                "invocation_id": context.invocation_id, "remote_status": visible_status,
                                "error_code": diagnostic(remote_error.get('code'), 160),
                                "error_message": diagnostic(remote_error.get('message')),
                            })
                        if status in definition.polling.failed:
                            raise ValueError("remote task ended without a result: " + str(visible_status))
                        if status in definition.polling.pending:
                            from .tools import WaitingRemote
                            raise WaitingRemote(definition.polling.interval_seconds)
                        if status not in definition.polling.succeeded:
                            raise ValueError("unknown remote task status: " + str(visible_status))
                    return data
        except httpx.TimeoutException:
            raise RuntimeError("configured API timed out") from None
        except httpx.HTTPError:
            raise RuntimeError("configured API transport failed") from None
    return spec, call


def register_http_tool(hub, definition):
    definition = HTTPTool.model_validate(definition)
    # A restart may load the same configured API that was previously saved in Studio.
    try:
        saved = hub.development.get("api", definition.name)
    except KeyError:
        saved = None
    if saved and export_definition(HTTPTool.model_validate(saved["definition"])) == export_definition(definition):
        return hub.tools.spec(definition.name)
    spec, call = build_http_tool(definition)
    hub.tools.register(spec, call)
    if not hasattr(hub, 'http_definitions'):
        hub.http_definitions = {}
    hub.http_definitions[definition.name] = export_definition(definition)
    return spec


def install_http_tools(app, hub):
    from .openapi_tools import install_openapi_tools
    from .search import install_search_tools

    @app.post("/v1/studio/apis", status_code=201)
    async def add_api(body: HTTPTool):
        saved = hub.development.save_api(body)
        return {**saved, "tool": hub.tools.spec(body.name).model_dump(), "config": {"http_tools": [saved["definition"]]},
                "message": "API 定义及版本已保存，重启自动恢复。密钥不写入数据库；重启后请通过环境变量提供密钥。"}

    @app.get("/v1/studio/apis")
    async def apis():
        latest = {r["id"]: r for r in hub.development.list_versions("api", include_archived=False)}
        return list(latest.values())

    @app.get("/v1/studio/apis/{name}")
    async def api_definition(name: str, revision: int | None = None):
        return hub.development.get("api", name, revision)

    @app.get("/v1/studio/apis/{name}/revisions")
    async def api_revisions(name: str):
        return hub.development.list_versions("api", name)

    @app.put("/v1/studio/apis/{name}")
    async def update_api(name: str, body: HTTPTool, expected_revision: int):
        if name != body.name:
            raise ValueError("API name cannot change during an update")
        return hub.development.save_api(body, expected_revision)

    install_search_tools(app, hub)
    install_openapi_tools(app, hub)


def export_definition(definition):
    result = definition.model_dump(exclude={"api_key"}, exclude_none=True)
    if definition.api_key and not definition.api_key_env:
        result["api_key_env"] = "EAH_" + re.sub(r"[^A-Z0-9]", "_", definition.name.upper()) + "_KEY"
    return result
