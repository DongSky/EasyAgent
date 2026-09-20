"""Reviewable OpenAPI 3 JSON/form imports. Unsupported protocols fail explicitly."""
from __future__ import annotations

import copy
import re

from pydantic import Field, ValidationError

from .contracts import Contract
from .http_tools import HTTPTool, build_http_tool, export_definition


class OpenAPIImport(Contract):
    document: dict
    prefix: str = Field(default="api", pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,30}$")
    server_url: str | None = None
    operations: list[str] = Field(default_factory=list, max_length=100)
    api_key: str = ""
    api_key_env: str | None = None


def definitions(request: OpenAPIImport):
    doc = request.document
    if not str(doc.get("openapi", "")).startswith("3."):
        raise ValueError("provide an OpenAPI 3.0/3.1 document")
    resolved_nodes = 0
    def resolve(value, chain=()):
        nonlocal resolved_nodes
        resolved_nodes += 1
        if resolved_nodes > 20000:
            raise ValueError("OpenAPI expanded document exceeds 20000 nodes; import fewer operations")
        if len(chain) > 40:
            raise ValueError("OpenAPI reference depth exceeds 40")
        if isinstance(value, list):
            return [resolve(v, chain) for v in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            if not ref.startswith("#/") or ref in chain:
                raise ValueError("only non-recursive local OpenAPI references are supported")
            target = doc
            try:
                for key in ref[2:].split("/"):
                    target = target[key.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                raise ValueError("unresolved OpenAPI reference") from None
            return resolve({**target, **{k: v for k, v in value.items() if k != "$ref"}}, (*chain, ref))
        result = {k: resolve(v, chain) for k, v in value.items()}
        if result.pop("nullable", False) and isinstance(result.get("type"), str):
            result["type"] = [result["type"], "null"]
        return result

    available, errors = [], []
    seen = set()
    for path, item in doc.get("paths", {}).items():
        for method, operation in item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                continue
            op_id = operation.get("operationId") or method + "_" + re.sub(r"\W+", "_", path).strip("_")
            if op_id in seen:
                raise ValueError("OpenAPI operation IDs must be unique")
            seen.add(op_id)
            if request.operations and op_id not in request.operations:
                continue
            try:
                servers = operation.get("servers", item.get("servers", doc.get("servers", [])))
                base = request.server_url or (servers[0]["url"] if servers else "")
                if not base or "{" in base or not path.startswith("/"):
                    raise ValueError("supply an absolute server_url (server variables are not expanded)")
                props, required, locations = {}, [], {}
                parameters = {}
                for raw in item.get("parameters", []) + operation.get("parameters", []):
                    param = resolve(raw)
                    parameters[(param["name"], param["in"])] = param
                for (name, location), param in parameters.items():
                    if name in props:
                        raise ValueError("same-name parameters in different locations need manual configuration")
                    if location not in ("path", "query", "header", "cookie"):
                        raise ValueError("unsupported parameter location")
                    schema = param.get("schema", {})
                    if schema.get("type") in ("object", "array"):
                        if location != "query" or schema.get("type") != "array" or param.get("style", "form") != "form" or param.get("explode", True) is not True:
                            raise ValueError("only scalar parameters and exploded query arrays are supported")
                    elif param.get("style", "simple" if location in ("path", "header") else "form") not in ("simple", "form"):
                        raise ValueError("unsupported parameter serialization")
                    props[name] = schema
                    if param.get("required") or location == "path":
                        required.append(name)
                    if location != "path":
                        locations[name] = location
                body_parameter, encoding = None, "json"
                if "requestBody" in operation:
                    body = resolve(operation["requestBody"])
                    content = body.get("content", {})
                    media = next((m for m in ("application/json", "application/x-www-form-urlencoded") if m in content), None)
                    if not media:
                        raise ValueError("only JSON and URL-encoded request bodies are supported; use a plugin for uploads")
                    body_parameter = "body"
                    if body_parameter in props:
                        raise ValueError("body parameter name collision")
                    props[body_parameter] = content[media].get("schema", {})
                    if body.get("required"):
                        required.append(body_parameter)
                    encoding = "json" if media == "application/json" else "form"
                responses = operation.get("responses", {})
                response = resolve(next((v for k, v in responses.items() if str(k).startswith("2")), responses.get("default", {})))
                content = response.get("content", {})
                response_mode, output = "json", {}
                if content:
                    if "application/json" in content:
                        output = content["application/json"].get("schema", {})
                    elif "text/plain" in content:
                        response_mode, output = "text", {"type": "object", "properties": {"text": {"type": "string"}, "content_type": {"type": "string"}}, "required": ["text"]}
                    else:
                        raise ValueError("only JSON and text responses are supported")
                auth = {}
                security = operation.get("security", doc.get("security", []))
                if security:
                    if len(security) != 1 or len(security[0]) > 1:
                        raise ValueError("choose one authentication scheme using manual configuration")
                    if security[0]:
                        name = next(iter(security[0]))
                        scheme = resolve(doc.get("components", {}).get("securitySchemes", {}).get(name, {}))
                        if scheme.get("type") == "apiKey" and scheme.get("in") in ("header", "query"):
                            auth = {"auth_header": scheme["name"], "auth_prefix": "", "auth_location": scheme["in"]}
                        elif scheme.get("type") == "http" and scheme.get("scheme", "").lower() in ("bearer", "basic"):
                            auth = {"auth_prefix": "Basic " if scheme["scheme"].lower() == "basic" else "Bearer "}
                        elif scheme.get("type") in ("oauth2", "openIdConnect"):
                            auth = {"auth_prefix": "Bearer "}  # Caller supplies a current access token.
                        else:
                            raise ValueError("unsupported authentication scheme; use manual configuration or a plugin")
                definition = HTTPTool(name=request.prefix + "." + re.sub(r"[^A-Za-z0-9_.-]", "_", op_id),
                    description=(operation.get("description") or operation.get("summary") or op_id)[:4000],
                    method=method.upper(), url=base.rstrip("/") + path, parameter_locations=locations,
                    input_schema={"type": "object", "properties": props, "required": required, "additionalProperties": False},
                    output_schema=output, body_parameter=body_parameter, request_encoding=encoding, response_mode=response_mode,
                    api_key=request.api_key if auth else "", api_key_env=request.api_key_env if auth else None, **auth)
                if auth and not (request.api_key or request.api_key_env):
                    # Preview remains useful without credentials; import requires them.
                    auth["needs_credential"] = True
                available.append({"operation_id": op_id, "definition": definition, "needs_credential": bool(auth)})
            except (ValueError, KeyError, TypeError) as exc:
                message = "; ".join(e["msg"] for e in exc.errors()) if isinstance(exc, ValidationError) else str(exc)
                errors.append({"operation_id": op_id, "error": message})
    missing = set(request.operations) - seen
    if missing:
        raise ValueError("unknown OpenAPI operation IDs: " + ", ".join(sorted(missing)))
    return available, errors


def install_openapi_tools(app, hub):
    @app.post("/v1/studio/apis/openapi/preview")
    async def preview(body: OpenAPIImport):
        rows, errors = definitions(body)
        return {"operations": [{"id": row["operation_id"], "name": row["definition"].name,
            "method": row["definition"].method, "description": row["definition"].description,
            "needs_credential": row["needs_credential"]} for row in rows], "unsupported": errors}

    @app.post("/v1/studio/apis/openapi", status_code=201)
    async def add(body: OpenAPIImport):
        if not body.operations:
            raise ValueError("select operation IDs from the preview before importing")
        rows, errors = definitions(body)
        if errors:
            raise ValueError("selected operations are unsupported: " + str(errors))
        if any(r["needs_credential"] for r in rows) and not (body.api_key or body.api_key_env):
            raise ValueError("provide a credential or environment variable for authenticated operations")
        tools = [build_http_tool(copy.deepcopy(r["definition"])) for r in rows]
        names = [spec.name for spec, _ in tools]
        if len(set(names)) != len(names) or any(n in hub.tools.entries for n in names):
            raise ValueError("duplicate tool name; change the prefix or selection")
        # All selected definitions validated before the registry is mutated.
        for spec, handler in tools:
            hub.tools.register(spec, handler)
        return {"tools": [spec.model_dump() for spec, _ in tools], "config": {"http_tools": [export_definition(r["definition"]) for r in rows]}, "message": f"已接入 {len(tools)} 个 API，当前服务进程内有效。"}
