"""ModelClient: chat model via OpenRouter /chat/completions.

- Model id comes only from the ``EASYAGENT_MODEL`` environment variable (never
  hard-coded: any OpenRouter model id is acceptable; Nemotron verified).
- API key comes only from ``OPENROUTER_API_KEY``. The key is never written to
  any file and never logged.
- ``EASYAGENT_MOCK_LLM=1`` switches to :class:`MockClient`, which replays a
  fixed canned ReAct trajectory for offline smoke tests. All tests must use the
  mock; real paid calls are forbidden in tests.
"""
from __future__ import annotations

import itertools
import logging
import os

import httpx

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/chat/completions"


class LLMError(Exception):
    """Raised for configuration or transport errors of the model client."""


class MockClient:
    """Canned ReAct trajectory: thought -> tool_call -> observation -> done.

    Cycles through a fixed sequence of chat() responses so offline smoke tests
    can exercise the ReAct loop without any network or paid calls.
    """

    _TRAJECTORY = [
        {
            "content": "Thought: I need to check the workspace. I will list files first.",
            "tool_calls": [
                {
                    "id": "mock-call-1",
                    "name": "shell.exec",
                    "arguments": {"command": ["ls", "-la"]},
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        },
        {
            "content": (
                "Thought: Observation received. The workspace looks fine. "
                "No further tool calls are needed; the task is done."
            ),
            "tool_calls": [],
            "usage": {"prompt_tokens": 200, "completion_tokens": 35, "total_tokens": 235},
        },
    ]

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("EASYAGENT_MODEL", "mock-model")
        self._cycle = itertools.cycle(self._TRAJECTORY)
        self.calls = 0

    def chat(self, messages, tools=None) -> dict:
        self.calls += 1
        step = next(self._cycle)
        return {
            "content": step["content"],
            "tool_calls": [dict(tc) for tc in step["tool_calls"]],
            "usage": dict(step["usage"]),
        }


class ModelClient:
    """Chat model via OpenRouter.

    ``chat(messages, tools=None)`` returns ``{"content", "tool_calls", "usage"}``
    where ``tool_calls`` is a list of ``{"id", "name", "arguments"}`` and
    ``usage`` carries token counts (and ``cost_usd`` when reported).
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if os.environ.get("EASYAGENT_MOCK_LLM") == "1":
            raise LLMError(
                "EASYAGENT_MOCK_LLM=1 is set; use MockClient instead of ModelClient."
            )
        model = model or os.environ.get("EASYAGENT_MODEL")
        if not model:
            raise LLMError(
                "EASYAGENT_MODEL is not set. Set it to a Nemotron model id "
                "on OpenRouter, e.g. "
                "export EASYAGENT_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free. "
                "No default model is baked into the code on purpose."
            )
        api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Export it in your environment; "
                "the key is read from the environment only and is never "
                "written to files or logs."
            )
        self.model = model
        self.base_url = base_url or OPENROUTER_URL
        self.timeout = timeout
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://prts.si",
                "X-Title": "EasyAgent",
            },
        )
        # Drop the reference to the key immediately; the header lives only in
        # the httpx client and is never logged by this module.
        del api_key

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            resp = self._client.post(self.base_url, json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenRouter transport error: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # Log status only; never log request/response bodies (may echo keys).
            log.warning("OpenRouter returned HTTP %s", resp.status_code)
            raise LLMError(f"OpenRouter HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("OpenRouter returned non-JSON") from exc
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError("OpenRouter response has no choices[0].message") from exc

        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                import json as _json

                try:
                    args = _json.loads(args or "{}")
                except ValueError:
                    args = {"_raw": args}
            tool_calls.append(
                {"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args}
            )

        usage = data.get("usage") or {}
        return {
            "content": choice.get("content") or "",
            "tool_calls": tool_calls,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cost_usd": usage.get("cost"),
            },
        }

    def close(self):
        self._client.close()


def make_client(model: str | None = None, **kwargs):
    """Factory: MockClient when EASYAGENT_MOCK_LLM=1, else ModelClient."""
    if os.environ.get("EASYAGENT_MOCK_LLM") == "1":
        return MockClient(model)
    return ModelClient(model, **kwargs)
