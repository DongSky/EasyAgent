"""Expose an installed extension as a model provider."""

from ..contracts import ModelResult
from ..store import encode


class ExtensionProvider:
    def __init__(self, host, package, contribution):
        self.host, self.package, self.contribution = host, package, contribution

    async def generate(self, request, model):
        if self.contribution.stream_handler:
            from ..model_streaming import MODEL_OBSERVER

            cursor = None
            total = 0
            try:
                for _ in range(4096):
                    page = await self.host.call(
                        self.package,
                        self.contribution.stream_handler,
                        {
                            "request": request.model_dump() if cursor is None else None,
                            "model": model,
                            "cursor": cursor,
                        },
                    )
                    if not isinstance(page, dict):
                        raise ValueError("invalid model stream page")
                    total += len(encode(page).encode())
                    if total > 4_000_000:
                        raise ValueError("model stream exceeds byte limit")
                    for delta in page.get("deltas", []):
                        if not isinstance(delta, dict) or delta.get("type") not in (
                            "text_delta",
                            "tool_delta",
                            "usage",
                        ):
                            raise ValueError("invalid model delta")
                        if observer := MODEL_OBSERVER.get():
                            await observer(delta)
                    if page.get("done"):
                        return ModelResult.model_validate(page["result"])
                    cursor = page.get("cursor")
                    if not isinstance(cursor, str) or not cursor:
                        raise ValueError("model stream missing continuation cursor")
                raise ValueError("model stream did not finish")
            finally:
                if cursor and self.contribution.cancel_handler:
                    await self.host.call(self.package, self.contribution.cancel_handler, {"cursor": cursor})
        result = await self.host.call(
            self.package, self.contribution.handler, {"request": request.model_dump(), "model": model}
        )
        return ModelResult.model_validate(result)
