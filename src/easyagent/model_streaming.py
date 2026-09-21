"""SSE folding preserves provider terminal/usage semantics and streams text deltas."""

import json
from contextvars import ContextVar

MODEL_OBSERVER = ContextVar("eah_model_observer", default=None)


async def fold_sse(response, dialect, max_bytes):
    text, calls, blocks, usage, continuation = "", {}, {}, {}, {}
    complete, final, finish = False, None, None
    total, lines = 0, []

    async def event(raw):
        nonlocal text, usage, complete, final, finish
        if raw == "[DONE]":
            complete = True
            return
        obj = json.loads(raw)
        delta = ""
        if dialect == "responses":
            kind = obj.get("type")
            if kind == "response.output_text.delta":
                delta = obj.get("delta", "")
            elif kind in ("response.output_item.added", "response.output_item.done"):
                blocks[obj["output_index"]] = dict(obj["item"])
            elif kind == "response.function_call_arguments.delta":
                block = blocks[obj["output_index"]]
                block["arguments"] = block.get("arguments", "") + obj.get("delta", "")
            elif kind == "response.function_call_arguments.done":
                blocks[obj["output_index"]]["arguments"] = obj["arguments"]
            elif kind == "response.completed":
                final, complete = obj["response"], True
            elif kind in ("response.failed", "response.incomplete", "error"):
                from .retry_policy import ModelResponseError
                result = obj.get('response') or obj
                raise ModelResponseError((result.get('incomplete_details') or {}).get('reason')
                                         or (result.get('error') or {}).get('code'))
        elif dialect == "anthropic":
            kind = obj.get("type")
            if kind == "message_start":
                usage.update(obj["message"].get("usage", {}))
            elif kind == "content_block_start":
                blocks[obj["index"]] = {**obj["content_block"], "_json": ""}
            elif kind == "content_block_delta":
                value = obj["delta"]
                block = blocks[obj["index"]]
                if value["type"] == "text_delta":
                    delta = value["text"]
                    block["text"] = block.get("text", "") + delta
                elif value["type"] == "input_json_delta":
                    block["_json"] += value["partial_json"]
                elif value["type"] == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + value["thinking"]
                elif value["type"] == "signature_delta":
                    block["signature"] = block.get("signature", "") + value["signature"]
            elif kind == "message_delta":
                usage.update(obj.get("usage", {}))
                if obj.get("delta", {}).get("stop_reason") == "max_tokens":
                    from .retry_policy import ModelResponseError
                    raise ModelResponseError('max_output_tokens')
            elif kind == "message_stop":
                complete = True
            elif kind == "error":
                raise ValueError("provider stream failed")
        else:
            usage.update(obj.get("usage") or {})
            for choice in obj.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                value = choice.get("delta", {})
                if value.get("reasoning_content"):
                    continuation["reasoning_content"] = continuation.get("reasoning_content", "") + value["reasoning_content"]
                delta += value.get("content") or ""
                finish = choice.get("finish_reason") or finish
                for call in value.get("tool_calls", []):
                    target = calls.setdefault(
                        call["index"],
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if call.get("id"):
                        target["id"] = call["id"]
                    for name in ("name", "arguments"):
                        target["function"][name] += call.get("function", {}).get(name, "")
            text += delta
        if delta and (observer := MODEL_OBSERVER.get()):
            await observer({"type": "text_delta", "text": delta})

    async for line in response.aiter_lines():
        total += len(line.encode())
        if total > max_bytes:
            raise ValueError("model stream exceeds byte budget")
        if not line:
            if lines:
                await event("\n".join(lines))
                lines = []
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip())
    if lines:
        await event("\n".join(lines))
    if not complete:
        raise ValueError("model stream ended without a terminal event")
    if dialect == "responses":
        if final is None:
            raise ValueError("Responses stream missing completed response")
        if not final.get("output") and blocks:
            final = {**final, "output": [blocks[i] for i in sorted(blocks)]}
        return final
    if dialect == "anthropic":
        content = []
        for _, block in sorted(blocks.items()):
            partial = block.pop("_json")
            if partial:
                block["input"] = json.loads(partial)
            content.append(block)
        return {"content": content, "usage": usage}
    return {
        "choices": [
            {"message": {"content": text, "tool_calls": [calls[i] for i in sorted(calls)], **continuation}, "finish_reason": finish}
        ],
        "usage": usage,
    }
