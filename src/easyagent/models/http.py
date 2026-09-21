"""HTTP model protocols: Chat Completions, Responses and Anthropic Messages."""

import json

import httpx

from ..contracts import ModelResult, ToolCall
from ..retry_policy import ModelResponseError
from .registry import ProviderError


class HTTPProvider:
    """Provider adapters with cancellable waits; keys never enter workflow state."""

    def __init__(self, base_url, api_key="", dialect="chat", timeout=None, max_bytes=16_000_000):
        if dialect not in ("chat", "responses", "anthropic"):
            raise ValueError("dialect must be chat, responses or anthropic")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dialect = dialect
        self.timeout = timeout
        self.max_bytes = max_bytes
        from ..model_limits import ModelLimits

        self.limits = ModelLimits(self)

    async def post(self, path, payload):
        from ..retry_policy import model_timeout, retry_after

        # Only reasoning/text calls receive longer waits; never change media write retry semantics.
        wait = (
            model_timeout(self.timeout)
            if path in ("/chat/completions", "/responses", "/messages")
            else self.timeout
        )
        try:
            return await self._post(path, payload, wait, retry_after)
        except (httpx.TransportError, ProviderError) as exc:
            exc.wait_seconds = wait
            raise

    async def _post(self, path, payload, wait, parse_retry_after):
        from .streaming import MODEL_OBSERVER, fold_sse

        streaming = path in ("/chat/completions", "/responses", "/messages") and (
            MODEL_OBSERVER.get() is not None or payload.get("stream")
        )
        if streaming:
            payload = {**payload, "stream": True}
            if path == "/chat/completions":
                payload["stream_options"] = {"include_usage": True}
        headers = {"content-type": "application/json"}
        if self.dialect == "anthropic":
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        elif self.api_key:
            headers["authorization"] = "Bearer " + self.api_key
        connection_wait = min(20, wait) if wait is not None else 20
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(wait, connect=connection_wait, pool=connection_wait)
        ) as client:
            async with client.stream("POST", self.base_url + path, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    if response.status_code in (400, 413):
                        from ..model_limits import context_error

                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 64000:
                                break
                        if len(body) <= 64000:
                            try:
                                overflow = context_error(json.loads(body))
                            except (ValueError, TypeError):
                                overflow = None
                            if overflow:
                                self.limits.overflow(payload["model"], overflow.limit)
                                raise overflow
                    # Provider responses may echo secrets or private content; do not persist the body.
                    raise ProviderError(
                        response.status_code, parse_retry_after(response.headers.get("retry-after"))
                    )
                if streaming:
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise ValueError("provider did not return an SSE stream")
                    return await fold_sse(response, self.dialect, self.max_bytes)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise ValueError("model response exceeds byte budget")
        return json.loads(body)

    @staticmethod
    def tool_call(identifier, name, arguments):
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(parsed, dict):
                raise ValueError("arguments must be an object")
            return ToolCall(id=identifier, name=name, arguments=parsed)
        except (ValueError, TypeError):
            return ToolCall(
                id=identifier,
                name=name,
                arguments={},
                argument_error="Tool arguments must be a complete JSON object. Correct this call.",
                raw_arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
            )

    @staticmethod
    def wire_names(request):
        # Dots are permitted locally, but not by every provider's tool-name grammar.
        names = list(
            dict.fromkeys(
                [t.name for t in request.tools]
                + [c["name"] for m in request.messages for c in m.get("tool_calls", [])]
            )
        )
        mapping = {f"tool_{index}": name for index, name in enumerate(names)}
        return mapping, {v: k for k, v in mapping.items()}

    async def generate(self, request, model):
        if request.capability in ("chat", "decision"):
            limits = await self.limits.discover(model)
            if cap := limits.get("max_output_tokens"):
                request = request.model_copy(
                    update={"max_output_tokens": min(request.max_output_tokens, cap)}
                )
        result = await self._generate(request, model)
        self.limits.observe(model, request, result.usage)
        return result

    async def _generate(self, request, model):
        if request.capability not in ("chat", "decision", "image", "embedding"):
            raise ValueError("this HTTP adapter does not implement capability " + request.capability)
        if request.capability == "image":
            if self.dialect == "anthropic":
                raise ValueError("Anthropic Messages does not implement image generation")
            payload = {
                **request.parameters,
                "model": model,
                "prompt": request.prompt,
                "n": request.parameters.get("n", 1),
            }
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
        if set(request.parameters) & {
            "tools",
            "tool_choice",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "messages",
            "input",
            "model",
        }:
            raise ValueError("model parameters cannot override managed tools, messages or budgets")
        messages = request.messages or [{"role": "user", "content": request.prompt}]
        forward, reverse = self.wire_names(request)
        if self.dialect == "responses":
            items = []
            for m in messages:
                saved = m.get("provider_state", {})
                if m["role"] == "tool":
                    items.append(
                        {"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]}
                    )
                elif saved.get("dialect") == "responses" and saved.get("model") == model:
                    for original in saved["output"]:
                        item = dict(original)
                        if item.get("type") == "function_call":
                            item["name"] = reverse[item["name"]]
                        items.append(item)
                elif m.get("tool_calls"):
                    if m.get("content"):
                        items.append({"role": m["role"], "content": m["content"]})
                    for tc in m["tool_calls"]:
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": tc["id"],
                                "name": reverse[tc["name"]],
                                "arguments": tc.get("raw_arguments") or json.dumps(tc["arguments"]),
                            }
                        )
                else:
                    items.append({"role": m["role"], "content": m["content"]})
            payload = {
                **request.parameters,
                "model": model,
                "input": items,
                "max_output_tokens": request.max_output_tokens,
                "store": False,
            }
            payload.setdefault("include", ["reasoning.encrypted_content"])
            if request.tools:
                payload["tools"] = [
                    {
                        "type": "function",
                        "name": reverse[t.name],
                        "description": t.description,
                        "parameters": t.input_schema,
                        "strict": False,
                    }
                    for t in request.tools
                ]
            if request.response_schema:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "result",
                        "strict": False,
                        "schema": request.response_schema,
                    }
                }
            data = await self.post("/responses", payload)
            if data.get("status") in ("incomplete", "failed", "cancelled"):
                raise ModelResponseError(
                    (data.get("incomplete_details") or {}).get("reason")
                    or (data.get("error") or {}).get("code")
                    or data["status"]
                )
            calls, text = [], []
            for item in data.get("output", []):
                if item["type"] == "function_call":
                    calls.append(
                        self.tool_call(
                            item["call_id"], forward.get(item["name"], item["name"]), item["arguments"]
                        )
                    )
                elif item["type"] == "message":
                    text.extend(c["text"] for c in item["content"] if c["type"] == "output_text")
            continuation = [
                {**item, "name": forward.get(item["name"], item["name"])}
                if item.get("type") == "function_call"
                else item
                for item in data.get("output", [])
            ]
            return ModelResult(
                text="\n".join(text),
                tool_calls=calls,
                usage=data.get("usage", {}),
                provider_state={"dialect": "responses", "model": model, "output": continuation}
                if calls
                else {},
            )
        if self.dialect == "anthropic":
            return await self.anthropic(request, model, messages, forward, reverse)
        wire = []
        for m in messages:
            item = {k: v for k, v in m.items() if k in ("role", "content", "tool_call_id")}
            saved = m.get("provider_state", {})
            if saved.get("dialect") == "chat" and saved.get("model") == model:
                item.update(saved.get("continuation", {}))
            if m.get("tool_calls"):
                item["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": reverse[c["name"]],
                            "arguments": c.get("raw_arguments") or json.dumps(c["arguments"]),
                        },
                    }
                    for c in m["tool_calls"]
                ]
            wire.append(item)
        payload = {
            **request.parameters,
            "model": model,
            "messages": wire,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": reverse[t.name],
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": request.response_schema, "strict": False},
            }
        data = await self.post("/chat/completions", payload)
        if data["choices"][0].get("finish_reason") == "length":
            raise ModelResponseError("length")
        result = data["choices"][0]["message"]
        continuation = {k: result[k] for k in ("reasoning_content", "reasoning_details") if k in result}
        return ModelResult(
            text=result.get("content") or "",
            usage=data.get("usage", {}),
            provider_state={"dialect": "chat", "model": model, "continuation": continuation}
            if continuation
            else {},
            tool_calls=[
                self.tool_call(
                    c["id"],
                    forward.get(c["function"]["name"], c["function"]["name"]),
                    c["function"]["arguments"],
                )
                for c in result.get("tool_calls", [])
            ],
        )

    async def anthropic(self, request, model, messages, forward, reverse):
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        if request.response_schema:
            system += "\nReturn only JSON matching this schema: " + json.dumps(request.response_schema)
        wire = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = m["role"]
            saved = m.get("provider_state", {})
            if role == "tool":
                role = "user"
                content = [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}]
            elif saved.get("dialect") == "anthropic" and saved.get("model") == model:
                content = [
                    {**c, "name": reverse[c["name"]]} if c["type"] == "tool_use" else dict(c)
                    for c in saved["content"]
                ]
            elif m.get("tool_calls"):
                content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                content += [
                    {"type": "tool_use", "id": c["id"], "name": reverse[c["name"]], "input": c["arguments"]}
                    for c in m["tool_calls"]
                ]
            else:
                content = (
                    m["content"]
                    if isinstance(m["content"], list)
                    else [{"type": "text", "text": m["content"]}]
                )
            if wire and wire[-1]["role"] == role:
                wire[-1]["content"].extend(content)
            else:
                wire.append({"role": role, "content": content})
        payload = {
            **request.parameters,
            "model": model,
            "system": system,
            "messages": wire,
            "max_tokens": request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [
                {"name": reverse[t.name], "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        data = await self.post("/messages", payload)
        if data.get("stop_reason") == "max_tokens":
            raise ModelResponseError("max_output_tokens")
        return ModelResult(
            text="\n".join(c["text"] for c in data["content"] if c["type"] == "text"),
            provider_state={
                "dialect": "anthropic",
                "model": model,
                "content": [
                    {**c, "name": forward.get(c["name"], c["name"])} if c["type"] == "tool_use" else c
                    for c in data["content"]
                ],
            }
            if any(c["type"] == "tool_use" for c in data["content"])
            else {},
            tool_calls=[
                self.tool_call(c["id"], forward.get(c["name"], c["name"]), c["input"])
                for c in data["content"]
                if c["type"] == "tool_use"
            ],
            usage=data.get("usage", {}),
        )
