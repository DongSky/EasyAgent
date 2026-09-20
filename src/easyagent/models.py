from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

import httpx
from jsonschema import ValidationError, validate

from .contracts import ModelRequest, ModelResult, ToolCall


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest, model: str) -> ModelResult: ...


class ProviderError(RuntimeError):
    def __init__(self, status):
        self.status = status
        self.retryable = status in (408, 429) or status >= 500
        super().__init__(f"model provider HTTP {status}")


@dataclass
class ModelBinding:
    provider: ModelProvider
    model: str
    capabilities: set[str]
    fallback: str | None = None
    output_price_per_million: float | None = None
    input_price_per_million: float | None = None


class ModelRegistry:
    def __init__(self):
        self.bindings: dict[str, ModelBinding] = {}
        self.attachment_loader = None

    def register(self, alias, provider, model, capabilities, fallback=None, output_price_per_million=None, input_price_per_million=None):
        if alias in self.bindings:
            raise ValueError(f"model alias already registered: {alias}")
        self.bindings[alias] = ModelBinding(provider, model, set(capabilities), fallback, output_price_per_million, input_price_per_million)

    async def generate(self, request: ModelRequest, binding=None):
        binding = binding or self.bindings.get(request.model)
        if not binding:
            raise ValueError(f"unknown model alias: {request.model}")
        if request.capability not in binding.capabilities:
            raise ValueError(f"{request.model} does not support {request.capability}")
        if request.attachments:
            if not self.attachment_loader:
                raise ValueError("attachment storage is not connected")
            request = self.attachment_loader(request, binding)
        result = await binding.provider.generate(request, binding.model)
        if request.response_schema and not result.tool_calls:
            try:
                if result.data is None:
                    result.data = json.loads(result.text)
                validate(result.data, request.response_schema)
            except (ValidationError, json.JSONDecodeError) as exc:
                # Keep the invalid draft available to bounded compiler repair, not as
                # a successful result or a raw payload in ordinary error messages.
                exc.model_response = result
                raise
        return result

    def catalog(self):
        return [{"alias": a, "model": b.model, "capabilities": sorted(b.capabilities)}
                for a, b in self.bindings.items()]


class MockProvider:
    """Deterministic development provider. Explicitly not a language/image model."""
    async def generate(self, request, model):
        if request.capability == "image":
            # One-pixel PNG; demos label this as a protocol fixture, never a generated illustration.
            png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII="
            return ModelResult(images=[{"b64_json": png, "mime_type": "image/png", "mock": True}])
        if request.capability == "embedding":
            digest = hashlib.sha256(request.prompt.encode()).digest()
            return ModelResult(embeddings=[[v / 255 for v in digest[:8]]], usage={"mock": True})
        latest = request.messages[-1] if request.messages else {"content": request.prompt}
        text = str(latest.get("content", ""))
        if request.tools and latest.get("role") != "tool":
            try:
                action = json.loads(text)
            except (ValueError, TypeError):
                action = {}
            if "tool" in action:
                return ModelResult(tool_calls=[ToolCall(id="mock-call", name=action["tool"],
                                                        arguments=action.get("arguments", {}))])
        if request.response_schema:
            # Test callers provide a JSON value to validate; do not fabricate model decisions.
            return ModelResult(text=text, data=json.loads(text), usage={"mock": True})
        return ModelResult(text=text, usage={"mock": True})


class HTTPProvider:
    """Provider adapters with bounded time and response bytes; keys never enter workflow state."""
    def __init__(self, base_url, api_key="", dialect="chat", timeout=120, max_bytes=16_000_000):
        if dialect not in ("chat", "responses", "anthropic"):
            raise ValueError("dialect must be chat, responses or anthropic")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dialect = dialect
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def post(self, path, payload):
        from .model_streaming import MODEL_OBSERVER, fold_sse
        streaming = path in ('/chat/completions', '/responses', '/messages') and (MODEL_OBSERVER.get() is not None or payload.get('stream'))
        if streaming:
            payload = {**payload, 'stream': True}
            if path == '/chat/completions':
                payload['stream_options'] = {'include_usage': True}
        headers = {"content-type": "application/json"}
        if self.dialect == "anthropic":
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        elif self.api_key:
            headers["authorization"] = "Bearer " + self.api_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", self.base_url + path, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    # Provider responses may echo secrets or private content; do not persist the body.
                    raise ProviderError(response.status_code)
                if streaming:
                    if 'text/event-stream' not in response.headers.get('content-type', ''):
                        raise ValueError('provider did not return an SSE stream')
                    return await fold_sse(response, self.dialect, self.max_bytes)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ValueError("model response exceeds byte budget")
        return json.loads(body)

    @staticmethod
    def wire_names(request):
        # Dots are permitted locally, but not by every provider's tool-name grammar.
        mapping = {f"tool_{index}": tool.name for index, tool in enumerate(request.tools)}
        return mapping, {v: k for k, v in mapping.items()}

    async def generate(self, request, model):
        if request.capability not in ("chat", "decision", "image", "embedding"):
            raise ValueError("this HTTP adapter does not implement capability " + request.capability)
        if request.capability == "image":
            if self.dialect == "anthropic":
                raise ValueError("Anthropic Messages does not implement image generation")
            payload = {**request.parameters, "model": model, "prompt": request.prompt, "n": request.parameters.get("n", 1)}
            for key in ("size", "quality", "background", "output_format"):
                if key in request.parameters:
                    payload[key] = request.parameters[key]
            data = await self.post("/images/generations", payload)
            return ModelResult(images=data["data"], usage=data.get("usage", {}))
        if request.capability == "embedding":
            if self.dialect == "anthropic":
                raise ValueError("Anthropic Messages does not implement embeddings")
            data = await self.post("/embeddings", {"model": model, "input": request.prompt})
            return ModelResult(embeddings=[d["embedding"] for d in data["data"]], usage=data.get("usage", {}))
        if set(request.parameters) & {"tools", "tool_choice", "max_tokens", "max_completion_tokens", "max_output_tokens", "messages", "input", "model"}:
            raise ValueError("model parameters cannot override managed tools, messages or budgets")
        messages = request.messages or [{"role": "user", "content": request.prompt}]
        forward, reverse = self.wire_names(request)
        if self.dialect == "responses":
            items = []
            for m in messages:
                if m["role"] == "tool":
                    items.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
                elif m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        items.append({"type": "function_call", "call_id": tc["id"],
                                      "name": reverse[tc["name"]], "arguments": json.dumps(tc["arguments"])})
                else:
                    items.append({"role": m["role"], "content": m["content"]})
            payload = {**request.parameters, "model": model, "input": items, "max_output_tokens": request.max_output_tokens,
                       "store": False}
            if request.tools:
                payload["tools"] = [{"type": "function", "name": reverse[t.name], "description": t.description,
                                     "parameters": t.input_schema, "strict": False} for t in request.tools]
            if request.response_schema:
                payload["text"] = {"format": {"type": "json_schema", "name": "result", "strict": False,
                                              "schema": request.response_schema}}
            data = await self.post("/responses", payload)
            if data.get("status") in ("incomplete", "failed", "cancelled"):
                raise ValueError("provider did not complete the response")
            calls, text = [], []
            for item in data.get("output", []):
                if item["type"] == "function_call":
                    calls.append(ToolCall(id=item["call_id"], name=forward.get(item["name"], item["name"]),
                                          arguments=json.loads(item["arguments"])))
                elif item["type"] == "message":
                    text.extend(c["text"] for c in item["content"] if c["type"] == "output_text")
            return ModelResult(text="\n".join(text), tool_calls=calls, usage=data.get("usage", {}))
        if self.dialect == "anthropic":
            return await self.anthropic(request, model, messages, forward, reverse)
        wire = []
        for m in messages:
            item = {k: v for k, v in m.items() if k in ("role", "content", "tool_call_id")}
            if m.get("tool_calls"):
                item["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": reverse[c["name"]], "arguments": json.dumps(c["arguments"])}} for c in m["tool_calls"]]
            wire.append(item)
        payload = {**request.parameters, "model": model, "messages": wire, "max_tokens": request.max_output_tokens}
        if request.tools:
            payload["tools"] = [{"type": "function", "function": {"name": reverse[t.name],
                "description": t.description, "parameters": t.input_schema}} for t in request.tools]
        if request.response_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "result", "schema": request.response_schema, "strict": False}}
        data = await self.post("/chat/completions", payload)
        if data["choices"][0].get("finish_reason") == "length":
            raise ValueError("model output was truncated by its token limit")
        result = data["choices"][0]["message"]
        return ModelResult(text=result.get("content") or "", usage=data.get("usage", {}), tool_calls=[
            ToolCall(id=c["id"], name=forward.get(c["function"]["name"], c["function"]["name"]),
                     arguments=json.loads(c["function"]["arguments"])) for c in result.get("tool_calls", [])])

    async def anthropic(self, request, model, messages, forward, reverse):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if request.response_schema:
            system += "\nReturn only JSON matching this schema: " + json.dumps(request.response_schema)
        wire = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = m["role"]
            if role == "tool":
                role = "user"
                content = [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]
            elif m.get("tool_calls"):
                content = [{"type": "tool_use", "id": c["id"], "name": reverse[c["name"]], "input": c["arguments"]}
                           for c in m["tool_calls"]]
            else:
                content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            if wire and wire[-1]["role"] == role:
                wire[-1]["content"].extend(content)
            else:
                wire.append({"role": role, "content": content})
        payload = {**request.parameters, "model": model, "system": system, "messages": wire, "max_tokens": request.max_output_tokens}
        if request.tools:
            payload["tools"] = [{"name": reverse[t.name], "description": t.description, "input_schema": t.input_schema}
                                for t in request.tools]
        data = await self.post("/messages", payload)
        return ModelResult(text="\n".join(c["text"] for c in data["content"] if c["type"] == "text"),
            tool_calls=[ToolCall(id=c["id"], name=forward.get(c["name"], c["name"]), arguments=c["input"])
                        for c in data["content"] if c["type"] == "tool_use"], usage=data.get("usage", {}))
