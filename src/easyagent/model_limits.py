"""Discover model limits from the connected service; never infer them from model names."""
import asyncio
import json
import re
import time
from urllib.parse import quote

import httpx


def positive(value):
    return value if type(value) is int and 0 < value <= 100_000_000 else None


def read_limits(data):
    result = {}
    for key, names in {
        'context_window': ('context_window', 'context_length', 'max_context_length', 'max_context_tokens'),
        'max_input_tokens': ('max_input_tokens', 'input_token_limit'),
        'max_output_tokens': ('max_output_tokens', 'max_completion_tokens', 'output_token_limit'),
    }.items():
        for row in (data, data.get('limits'), data.get('top_provider'), data.get('capabilities')):
            if isinstance(row, dict):
                value = next((positive(row.get(name)) for name in names if positive(row.get(name))), None)
                if value:
                    result[key] = value
                    break
    return result


class ContextWindowError(RuntimeError):
    retryable = False

    def __init__(self, limit=None):
        self.limit = limit
        super().__init__('模型上下文已满，需要压缩历史后继续。')


def context_error(data):
    if not isinstance(data, dict):
        return None
    error = data.get('error', data)
    if not isinstance(error, dict):
        return None
    message = str(error.get('message', ''))
    match = re.search(r'maximum context length (?:is|of)\s*([\d,]+)\s*tokens', message, re.I)
    recognized = error.get('code') in ('context_length_exceeded', 'context_window_exceeded', 'prompt_too_long')
    if not recognized and not match and not re.search(r'prompt is too long|input exceeds (?:the )?(?:context|maximum)', message, re.I):
        return None
    limit = positive(error.get('max_context_tokens')) or positive(error.get('context_window'))
    if match:
        limit = positive(int(match[1].replace(',', '')))
    return ContextWindowError(limit)


class ModelLimits:
    def __init__(self, provider):
        self.provider = provider
        self.entries = {}
        self.checked = {}
        self.lock = asyncio.Lock()

    def public(self, model):
        return dict(self.entries.get(model, {'source': 'unknown'}))

    async def discover(self, model):
        async with self.lock:
            if time.monotonic() - self.checked.get(model, -1e9) < 3600:
                return self.public(model)
            provider = self.provider
            headers = ({'x-api-key': provider.api_key, 'anthropic-version': '2023-06-01'}
                       if provider.dialect == 'anthropic' else
                       {'Authorization': 'Bearer ' + provider.api_key} if provider.api_key else {})
            found = {}
            try:
                # Metadata only: no probe inference, no billable long prompt, no redirected credentials.
                async with asyncio.timeout(5), httpx.AsyncClient(timeout=3, follow_redirects=False) as client:
                    for path in ('/models/' + quote(model, safe=''), '/models'):
                        async with client.stream('GET', provider.base_url + path, headers=headers) as response:
                            if response.status_code != 200:
                                continue
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 2_000_000:
                                    raise ValueError('model metadata too large')
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            continue
                        row = data if data.get('id') == model else next((r for r in data.get('data', [])
                            if isinstance(r, dict) and r.get('id') == model), {})
                        found.update(read_limits(row))
                        if found.get('context_window') or found.get('max_input_tokens'):
                            break
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
                pass
            previous = self.entries.get(model, {})
            self.entries[model] = {**previous, **found,
                                   'source': 'provider_metadata' if found else previous.get('source', 'unknown')}
            self.checked[model] = time.monotonic()
            return self.public(model)

    def overflow(self, model, limit):
        if limit:
            self.entries[model] = {**self.entries.get(model, {}), 'context_window': limit, 'source': 'provider_error'}

    def observe(self, model, request, usage):
        tokens = usage.get('input_tokens', usage.get('prompt_tokens'))
        if positive(tokens):
            from .store import encode
            size = len(encode({'messages': request.messages or [{'role': 'user', 'content': request.prompt}],
                               'tools': [t.model_dump() for t in request.tools],
                               'schema': request.response_schema}).encode())
            # A conservative measured ratio, not a claimed context-window size.
            ratio = max(.05, min(4, tokens / max(1, size)))
            previous = self.entries.get(model, {})
            self.entries[model] = {**previous, 'source': previous.get('source', 'unknown'),
                                   'tokens_per_byte': max(ratio, previous.get('tokens_per_byte', 0)),
                                   'last_input_tokens': tokens}
