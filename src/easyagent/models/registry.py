from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from jsonschema import ValidationError, validate

from ..contracts import ModelRequest, ModelResult, ToolCall


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest, model: str) -> ModelResult: ...


class ProviderError(RuntimeError):
    def __init__(self, status, retry_after=None):
        self.status = status
        self.retryable = status in (408, 429) or status >= 500
        self.retry_after = retry_after
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

    def register(
        self,
        alias,
        provider,
        model,
        capabilities,
        fallback=None,
        output_price_per_million=None,
        input_price_per_million=None,
    ):
        if alias in self.bindings:
            raise ValueError(f"model alias already registered: {alias}")
        self.bindings[alias] = ModelBinding(
            provider, model, set(capabilities), fallback, output_price_per_million, input_price_per_million
        )

    async def generate(self, request: ModelRequest, binding=None):
        request = request.with_context()
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
        return [
            {"alias": a, "model": b.model, "capabilities": sorted(b.capabilities)}
            for a, b in self.bindings.items()
        ]


class MockProvider:
    """Deterministic development provider. Explicitly not a language/image model."""

    async def generate(self, request, model):
        if request.capability == "image":
            # One-pixel PNG; demos label this as a protocol fixture, never a generated illustration.
            png = (
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aV1kAAAAASUVORK5CYII="
            )
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
                return ModelResult(
                    tool_calls=[
                        ToolCall(id="mock-call", name=action["tool"], arguments=action.get("arguments", {}))
                    ]
                )
        if request.response_schema:
            # Test callers provide a JSON value to validate; do not fabricate model decisions.
            return ModelResult(text=text, data=json.loads(text), usage={"mock": True})
        return ModelResult(text=text, usage={"mock": True})
