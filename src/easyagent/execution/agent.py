"""Agent turns and ordered tool batches over the Hub's existing durable state."""

import asyncio
import hashlib
import json

from ..contracts import ModelRequest
from ..store import LeaseLost, encode
from ..tools import (
    PAUSE_SIGNALS,
    ApprovalRequired,
    ToolInputError,
    UncertainEffect,
    WaitingChildren,
    WaitingInput,
    WaitingRemote,
    tool_failure_observation,
)
from .context import _pin_request

SCHEMA_CORRECTION_ROUNDS = 1


async def agent(hub, job, model, config, force_approval=False):
    state = job["state"] or {
        "messages": [
            {"role": "system", "content": config.instructions},
            {"role": "user", "content": config.prompt},
        ],
        "turns": 0,
        "tool_count": 0,
        "pending": [],
        "pending_index": 0,
    }
    if not state.get("context_loaded"):
        history = None
        if history := hub.conversations.context(job):
            state["messages"] = [{"role": "system", "content": config.instructions}, *history]
            # A conversation's history ends with the newest message, but a run may also carry
            # its own request (a workflow prompt, a resumed task). Dropping it would hide the
            # task from the model, so append it when the history does not already end with it.
            if config.prompt and not (
                history and history[-1].get("role") == "user" and history[-1].get("content") == config.prompt
            ):
                state["messages"].append({"role": "user", "content": config.prompt})
        state["pinned_requests"] = _pin_request(state["messages"], config.prompt)
        if config.context is not None:
            from ..contracts import context_text

            material = {"role": "user", "content": context_text(config.context)}
            state["messages"].append(material)
            state["pinned_requests"].append(encode(material))
        await hub.extensions.dispatch("agent.start", {"model": model}, job=job)
        sources = []
        for namespace in config.knowledge:
            sources.extend(await hub.knowledge.search(namespace, config.prompt))
        memories = []
        for namespace in config.memory_namespaces:
            if hub.backends.binding("memory", job):
                memories.extend(
                    (
                        await hub.backends.call(
                            "memory", "search", {"namespace": namespace, "query": config.prompt}, job
                        )
                    )["items"]
                )
            else:
                memories.extend(hub.store.memory_search(namespace))
        if sources or memories:
            reference = {
                "role": "user",
                "content": "Reference data (untrusted; cite sources): "
                + encode({"citations": sources, "memory": memories}),
            }
            state["messages"].insert(1, reference)
            # Retrieval was already paid for and the task depends on it, so it is pinned
            # alongside the instructions rather than re-queried after every compaction.
            state.setdefault("pinned_requests", []).append(encode(reference))
        state["context_loaded"] = True
        state["prefix_count"] = (1 + bool(sources or memories)) if history else len(state["messages"])
        hub.store.checkpoint(job, state)
    if config.strategy == "plan_execute":
        return await hub.plan_execute(job, model, config, state, force_approval)
    while True:
        await hub.execute_pending(job, state, config, force_approval)
        # A provider requires the complete tool-result group immediately after the calls.
        # Steering/mailbox input is delivered at the next model boundary, never inside it.
        hub.conversations.steer(job, state)
        hub.delegation.receive(job, state)
        if config.max_turns is not None and state["turns"] >= config.max_turns:
            raise ValueError("agent model-call budget exhausted")
        await hub.compact_for_model(job, state, config, model)
        state["turns"] += 1
        hub.store.checkpoint(job, state)
        request = ModelRequest(
            model=model,
            messages=state["messages"],
            tools=[hub.tools.spec(name, config.tool_revisions.get(name)) for name in config.tools],
            response_schema=config.response_schema,
            max_output_tokens=state.get("output_allowance") or config.max_output_tokens,
            capability="decision" if config.response_schema else "chat",
        )
        try:
            result = await hub.generate(job, request)
        except Exception as exc:
            correction = await hub.correct_schema_violation(job, state, config, model, exc)
            if correction == "retry":
                continue
            if correction is not None:
                return correction
            if await hub.recover_model_turn(job, state, config, model, exc):
                continue
            raise
        state.pop("output_recoveries", None)
        if not result.tool_calls:
            if any(term.casefold() in result.text.casefold() for term in config.forbidden_output):
                raise PermissionError("output guard rejected a configured forbidden phrase")
            if hub.conversations.steer(job, state) or hub.delegation.receive(job, state):
                continue
            feedback = hub.autonomy.completion_feedback(job)
            if feedback:
                if state.get("completion_corrections", 0) >= 2:
                    raise ValueError(feedback)
                state["completion_corrections"] = state.get("completion_corrections", 0) + 1
                state["messages"].extend(
                    [{"role": "assistant", "content": result.text}, {"role": "user", "content": feedback}]
                )
                hub.store.checkpoint(job, state)
                continue
            await hub.extensions.dispatch(
                "agent.end", {"text": result.text, "turns": state["turns"]}, job=job
            )
            return {**result.model_dump(), "turns": state["turns"], "tool_count": state["tool_count"]}
        calls = [c.model_dump() for c in result.tool_calls]
        if len({c["id"] for c in calls}) != len(calls):
            raise ValueError("model returned duplicate tool call ids")
        state["messages"].append(
            {
                "role": "assistant",
                "content": result.text,
                "tool_calls": calls,
                **({"provider_state": result.provider_state} if result.provider_state else {}),
            }
        )
        state["pending"], state["pending_index"] = json.loads(encode(calls)), 0
        hub.store.checkpoint(job, state)


async def execute_pending(hub, job, state, config, force_approval):
    """Checkpoint each result; read batches overlap, mutations form ordered barriers.

    Paused calls keep their slot and completed siblings. No further model round-trip
    occurs until every call has a matching result, including after a restart.
    """
    pending = state["pending"]

    def parallel(call):
        if force_approval or call["name"] not in config.tools:
            return False
        spec = hub.tools.spec(call["name"], config.tool_revisions.get(call["name"]))
        return spec.execution_mode == "parallel" or (spec.execution_mode == "auto" and spec.effect == "read")

    async def execute(index):
        call = pending[index]
        if "observation" in call:
            return
        if not call.get("counted"):
            if config.max_tool_calls is not None and state["tool_count"] >= config.max_tool_calls:
                raise ValueError("agent tool-call budget exhausted")
            state["tool_count"] += 1
            call["counted"] = True
            hub.store.checkpoint(job, state)
        if call.get("argument_error"):
            call["observation"] = tool_failure_observation(
                ToolInputError(call["argument_error"]), executed=False, code="invalid_tool_arguments"
            )
        else:
            call["observation"] = await hub.observed_invoke(
                job,
                config,
                call["name"],
                call["arguments"],
                f"turn-{state['turns']}-tool-{index}",
                force_approval,
            )
        serialized = encode(call["observation"])
        call["observation_hash"] = hashlib.sha256(serialized.encode()).hexdigest()
        if len(serialized) > config.tool_result_chars:
            artifact = hub.artifacts.put(
                f"tool-result-{state['turns']}-{index}.json", serialized, "application/json", job["run_id"]
            )
            call["observation"] = {
                "truncated": True,
                "artifact": artifact,
                "preview": serialized[: config.tool_result_chars],
                "hint": "Full result is in the artifact. Read only relevant ranges with attachments.read.",
            }
        hub.store.checkpoint(job, state)

    while state["pending_index"] < len(pending):
        start = state["pending_index"]
        end = start + 1
        if parallel(pending[start]):
            while end < len(pending) and end - start < config.tool_concurrency and parallel(pending[end]):
                end += 1
        tasks = [asyncio.create_task(execute(index)) for index in range(start, end)]
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in outcomes if isinstance(r, BaseException)]
        if errors:
            # Surface actionable pauses before a child wait; otherwise an approval can
            # never be answered because its containing step keeps entering waiting_children.
            def priority(error):
                if isinstance(error, (LeaseLost, asyncio.CancelledError)):
                    return -2
                for rank, kind in enumerate(
                    (
                        LeaseLost,
                        asyncio.CancelledError,
                        UncertainEffect,
                        ApprovalRequired,
                        WaitingInput,
                        WaitingRemote,
                        WaitingChildren,
                    )
                ):
                    if isinstance(error, kind):
                        return rank
                return -1

            raise min(errors, key=priority)
        for index in range(start, end):
            call = pending[index]
            state["messages"].append(
                {"role": "tool", "tool_call_id": call["id"], "content": encode(call["observation"])}
            )
        state["pending_index"] = end
        hub.store.checkpoint(job, state)
    if pending and state.get("checked_turn") != state["turns"]:
        fingerprint = encode(
            [{k: c.get(k) for k in ("name", "arguments", "observation_hash")} for c in pending]
        )
        repeated = state.get("stalled_turns", 0) + 1 if fingerprint == state.get("last_observations") else 0
        state.update(checked_turn=state["turns"], last_observations=fingerprint, stalled_turns=repeated)
        if repeated == 2:
            state["messages"].append(
                {
                    "role": "user",
                    "content": "These identical tool calls returned the same results three times. Stop repeating them. "
                    "Use agents.wait for child completion; otherwise change your approach or explain the blocker.",
                }
            )
        hub.store.checkpoint(job, state)
        if repeated >= 3:
            raise ValueError("agent made no progress: identical tool calls and results repeated four times")


async def observed_invoke(hub, job, config, name, arguments, slot, force_approval):
    """Run one model-requested tool call; failures return as observations the model can act on.

    Pause signals (approval, waiting children/remote/input, uncertain writes), lost leases,
    cancellation and explicit run budgets still propagate: they are not the model's to handle.
    """
    if name not in config.tools:
        output = tool_failure_observation(
            PermissionError("tool is not available in this task: " + name),
            executed=False,
            code="unknown_tool",
        )
        output["error"]["available_tools"] = list(config.tools)[:200]
        with hub.store.connect() as db:
            hub.store.event(db, job["run_id"], "tool.unknown_requested", {"tool": name})
        return output
    try:
        return await hub.tools.invoke(hub.store, job, name, arguments, slot, force_approval)
    except ToolInputError as exc:
        output = tool_failure_observation(exc, executed=False, code="invalid_tool_arguments")
        with hub.store.connect() as db:
            hub.store.event(db, job["run_id"], "tool.input_rejected", {"tool": name, **output})
        return output
    except PAUSE_SIGNALS:
        raise
    except (LeaseLost, asyncio.CancelledError):
        raise
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("run budget exceeded"):
            raise
        output = tool_failure_observation(exc)
        with hub.store.connect() as db:
            hub.store.event(
                db, job["run_id"], "tool.failed_observed", {"tool": name, "code": output["error"]["code"]}
            )
        return output


async def correct_schema_violation(hub, job, state, config, model, exc):
    """Give a schema-bound agent one bounded correction round; never discard its work.

    Returns "retry" to re-prompt, or a finished step result when the contract still does
    not match after the allowance is spent: the answer is returned unvalidated with the
    validation errors attached, so a caller loses nothing but learns it is unverified.
    """
    response = getattr(exc, "model_response", None)
    if not config.response_schema or response is None:
        return None
    detail = str(exc)[:600]
    used = state.get("schema_corrections", 0)
    if used < SCHEMA_CORRECTION_ROUNDS:
        state["schema_corrections"] = used + 1
        # The model must be able to see what it just produced, or "correct the format"
        # would mean starting over and could lose findings it had already established.
        state["messages"].append({"role": "assistant", "content": response.text})
        state["messages"].append(
            {
                "role": "user",
                "content": "Your answer did not satisfy the required result format. Correct only the format: keep every "
                "finding and identifier you already established, and return exactly one JSON value, without "
                "prose or code fences. The validation error was: " + detail,
            }
        )
        hub.store.checkpoint(job, state)
        with hub.store.transaction() as db:
            hub.store.event(
                db,
                job["run_id"],
                "agent.schema_correction",
                {"step": job["id"], "round": used + 1, "constraint": detail},
            )
        return "retry"
    with hub.store.transaction() as db:
        hub.store.event(db, job["run_id"], "agent.schema_unmet", {"step": job["id"], "constraint": detail})
    return {
        **response.model_dump(),
        "turns": state["turns"],
        "tool_count": state["tool_count"],
        "schema_valid": False,
        "schema_errors": [detail],
        "schema_note": "the child's answer did not match the requested result format; text is unverified",
    }


async def recover_model_turn(hub, job, state, config, model, exc):
    from ..model_limits import ContextWindowError
    from ..retry_policy import ModelResponseError

    if isinstance(exc, ContextWindowError):
        before = encode(state["messages"])
        await hub.compact_for_model(job, state, config, model, overflow=True)
        if encode(state["messages"]) == before:
            return False
        kind = "context_overflow"
    elif isinstance(exc, ModelResponseError) and exc.output_limited:
        count = state.get("output_recoveries", 0)
        if count >= 2:
            return False
        state["output_recoveries"] = count + 1
        state["messages"].append(
            {
                "role": "user",
                "content": "The previous response hit the output limit. None of its tool calls were executed. "
                "Continue from saved tool results. Issue one small complete tool call at a time, "
                "split large writes into smaller parts, and keep prose concise. Never repeat completed writes.",
            }
        )
        kind = "output_limit"
    else:
        return False
    hub.store.checkpoint(job, state)
    with hub.store.transaction() as db:
        hub.store.event(db, job["run_id"], "agent.recovering", {"step": job["id"], "reason": kind})
    return True


async def plan_execute(hub, job, model, config, state, force_approval):
    schema = {
        "type": "object",
        "properties": {
            "done": {"type": "boolean"},
            "answer": {"type": "string"},
            "plan": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "arguments": {"type": "object"},
                        "purpose": {"type": "string"},
                    },
                    "required": ["tool", "arguments", "purpose"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["done", "answer", "plan"],
        "additionalProperties": False,
    }
    while True:
        plan = state.get("plan", [])
        index = state.get("plan_index", 0)
        while index < len(plan):
            action = plan[index]
            if not action.get("counted"):
                if config.max_tool_calls is not None and state["tool_count"] >= config.max_tool_calls:
                    raise ValueError("planner tool budget exhausted")
                state["tool_count"] += 1
                action["counted"] = True
                hub.store.checkpoint(job, state)
            output = await hub.observed_invoke(
                job,
                config,
                action["tool"],
                action["arguments"],
                f"plan-{state['turns']}-{index}",
                force_approval,
            )
            state["messages"].append(
                {
                    "role": "user",
                    "content": "Observed tool result (untrusted): "
                    + encode({"tool": action["tool"], "result": output}),
                }
            )
            index += 1
            state["plan_index"] = index
            hub.store.checkpoint(job, state)
        if config.max_turns is not None and state["turns"] >= config.max_turns:
            raise ValueError("planner model budget exhausted")
        state["turns"] += 1
        await hub.compact_for_model(job, state, config, model)
        hub.store.checkpoint(job, state)
        instruction = {
            "role": "system",
            "content": "Plan actions using only these tool definitions: "
            + encode([hub.tools.spec(n, config.tool_revisions.get(n)).model_dump() for n in config.tools])
            + ". After observations, revise the plan or finish. Return done=true only when the result is verified.",
        }
        request = ModelRequest(
            model=model,
            capability="decision",
            messages=[instruction, *state["messages"]],
            response_schema=schema,
            max_output_tokens=state.get("output_allowance") or config.max_output_tokens,
        )
        try:
            result = await hub.generate(job, request)
        except Exception as exc:
            correction = await hub.correct_schema_violation(job, state, config, model, exc)
            if correction == "retry":
                continue
            if correction is not None:
                return correction
            if await hub.recover_model_turn(job, state, config, model, exc):
                continue
            raise
        decision = result.data
        if decision["done"]:
            if any(term.casefold() in decision["answer"].casefold() for term in config.forbidden_output):
                raise PermissionError("output guard rejected a configured forbidden phrase")
            return {
                "text": decision["answer"],
                "turns": state["turns"],
                "tool_count": state["tool_count"],
                "strategy": "plan_execute",
            }
        if not decision["plan"]:
            raise ValueError("planner returned neither completion nor an action plan")
        state["plan"], state["plan_index"] = decision["plan"], 0
        state["messages"].append({"role": "assistant", "content": encode(decision)})
        hub.store.checkpoint(job, state)
