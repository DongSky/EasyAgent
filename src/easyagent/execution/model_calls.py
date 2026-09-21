"""One model request: hooks, provider selection, budgets, usage and durable receipts."""

import json
import time
import uuid

from jsonschema import ValidationError as SchemaError

from ..contracts import ModelRequest
from ..models import MockProvider, ProviderError
from ..store import encode
from ..retry_policy import MODEL_WAIT, attempt_number, error_info


async def generate(hub, job, request):
    request = request.with_context()
    context = await hub.extensions.dispatch("context.transform", {"messages": request.messages}, job=job)
    request = request.model_copy(update={"messages": context["messages"]})
    transformed = await hub.extensions.dispatch("model.before_request", request.model_dump(), job=job)
    request = ModelRequest.model_validate(transformed)
    alias, visited = request.model, set()
    while True:
        if alias in visited:
            raise ValueError("model fallback cycle detected")
        visited.add(alias)
        binding = hub.extensions.provider_binding(
            alias, hub.extensions.run_snapshot(job)
        ) or hub.models.bindings.get(alias)
        if not binding:
            raise ValueError("unregistered model: " + alias)
        from ..models import HTTPProvider

        if isinstance(binding.provider, HTTPProvider) and request.capability in ("chat", "decision"):
            limits = await binding.provider.limits.discover(binding.model)
            if cap := limits.get("max_output_tokens"):
                request = request.model_copy(
                    update={"max_output_tokens": min(request.max_output_tokens, cap)}
                )
        call_id = uuid.uuid4().hex
        estimated_input = len(encode(request.model_dump()).encode())
        price_known = isinstance(binding.provider, MockProvider) or (
            binding.input_price_per_million is not None and binding.output_price_per_million is not None
        )
        with hub.store.transaction() as db:
            hub.store.assert_owner(db, job)
            # A request's max_output_tokens is a ceiling, not a minimum. Use the
            # remaining allowance instead of rejecting a still-viable continuation.
            remaining, ancestor = request.max_output_tokens, job["run_id"]
            while ancestor:
                spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (ancestor,)).fetchone()[0])
                used = db.execute(
                    "SELECT output_reserved FROM run_usage WHERE run_id=?", (ancestor,)
                ).fetchone()
                cap = spec.get("limits", {}).get("output_tokens")
                if cap is not None:
                    remaining = min(remaining, cap - (used[0] if used else 0))
                parent = db.execute(
                    "SELECT parent_id FROM child_runs WHERE child_id=?", (ancestor,)
                ).fetchone()
                ancestor = parent[0] if parent else None
            if remaining > 0:
                request = request.model_copy(update={"max_output_tokens": remaining})
            max_cost = (
                (binding.input_price_per_million or 0) * estimated_input
                + (binding.output_price_per_million or 0) * request.max_output_tokens
            ) / 1_000_000
            hub.store.reserve(db, job["run_id"], "model_calls")
            hub.store.reserve(db, job["run_id"], "output_reserved", request.max_output_tokens)
            if (request.capability in ("image", "embedding") or request.attachments) and not isinstance(
                binding.provider, MockProvider
            ):
                price_known = False
            hub.store.reserve_cost(db, job["run_id"], max_cost, price_known)
            db.execute(
                "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)",
                (call_id, job["run_id"], job["id"], alias, "started", None, time.time(), None),
            )
            hub.store.event(
                db,
                job["run_id"],
                "model.started",
                {"step": job["id"], "call_id": call_id, "model": alias},
            )
        try:
            effective = request.model_copy(update={"model": alias})
            await hub.extensions.dispatch("message.start", {"call_id": call_id, "role": "assistant"}, job=job)
            from ..model_streaming import MODEL_OBSERVER

            async def observe(delta):
                with hub.store.transaction() as db:
                    hub.store.assert_owner(db, job)
                    hub.store.event(
                        db, job["run_id"], "model.delta", {"step": job["id"], "call_id": call_id, **delta}
                    )
                await hub.extensions.dispatch("model.delta", delta, job=job)
                await hub.extensions.dispatch("message.update", {"call_id": call_id, **delta}, job=job)

            streaming = job["spec"]["input"].get("streaming", False) or request.parameters.get(
                "stream", False
            )
            observer = MODEL_OBSERVER.set(observe if streaming else None)
            wait_settings = MODEL_WAIT.set((attempt_number(job), job.get("retry_state", {})))
            try:
                result = await hub.models.generate(effective, binding=binding)
            finally:
                MODEL_OBSERVER.reset(observer)
                MODEL_WAIT.reset(wait_settings)
            patched = await hub.extensions.dispatch("model.after_response", result.model_dump(), job=job)
            from ..contracts import ModelResult

            result = ModelResult.model_validate(patched)
            await hub.extensions.dispatch(
                "message.end", {"call_id": call_id, "message": result.model_dump()}, job=job
            )
            if request.response_schema and not result.tool_calls:
                from jsonschema import validate

                validate(
                    result.data if result.data is not None else json.loads(result.text),
                    request.response_schema,
                )
        except Exception as exc:
            exc.model_alias = alias
            with hub.store.transaction() as db:
                hub.store.assert_owner(db, job)
                db.execute(
                    "UPDATE model_calls SET status='failed',finished=? WHERE id=?", (time.time(), call_id)
                )
                hub.store.event(
                    db,
                    job["run_id"],
                    "model.failed",
                    {"call_id": call_id, "error_type": type(exc).__name__, "detail": error_info(exc)},
                )
            permanent = (
                isinstance(exc, (ValueError, KeyError, PermissionError, SchemaError))
                or getattr(exc, "retryable", None) is False
                or isinstance(exc, ProviderError)
                and not exc.retryable
            )
            if binding.fallback and not permanent:
                alias = binding.fallback
                continue
            raise
        usage = result.usage
        tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        if not isinstance(tokens, int) or tokens < 0:
            tokens = None
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        mock = bool(usage.get("mock"))
        cost_delta = 0
        if mock:
            tokens = 0
            cost_delta = -max_cost
        elif price_known and tokens is not None and isinstance(input_tokens, int) and input_tokens >= 0:
            actual = (
                (binding.input_price_per_million or 0) * input_tokens
                + (binding.output_price_per_million or 0) * tokens
            ) / 1_000_000
            cost_delta = actual - max_cost
        with hub.store.transaction() as db:
            hub.store.assert_owner(db, job)
            db.execute(
                "UPDATE model_calls SET status='succeeded',usage=?,finished=? WHERE id=?",
                (encode(usage), time.time(), call_id),
            )
            hub.store.settle(
                db,
                job["run_id"],
                tokens - request.max_output_tokens if tokens is not None else 0,
                tokens or 0,
                cost_delta,
                int(not mock and (tokens is None or not price_known)),
            )
            hub.store.event(
                db, job["run_id"], "model.succeeded", {"call_id": call_id, "model": alias, "usage": usage}
            )
            spec = json.loads(db.execute("SELECT spec FROM runs WHERE id=?", (job["run_id"],)).fetchone()[0])
            accounted = db.execute(
                "SELECT output_reserved,cost FROM run_usage WHERE run_id=?", (job["run_id"],)
            ).fetchone()
            output_cap = spec.get("limits", {}).get("output_tokens")
            exceeded = output_cap is not None and accounted[0] > output_cap
            cost_cap = spec.get("limits", {}).get("cost_usd")
            exceeded = exceeded or cost_cap is not None and accounted[1] > cost_cap
        if exceeded:
            raise ValueError("provider usage exceeded the reserved run budget; further execution stopped")
        result.usage["model_alias"] = alias
        return result
