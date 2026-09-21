"""Pinned user intent, context limits and compaction of complete message groups."""

import asyncio
import time

from ..store import encode


def _pin_request(messages, prompt):
    """Pin every user instruction by identity, not by index.

    Instructions are the intent everything else is derived from and cannot be reconstructed
    from the work that followed: summarizing "use the existing retry helper, do not add
    another" is how an agent later does exactly what it was told not to. Assistant narration
    and tool output are safe to drop; the person's own words are not. A standalone run that
    was initialized from its prompt pins that message too.
    """
    pins = [encode(m) for m in messages if m.get("role") == "user"]
    if not pins and prompt:
        pins.append(encode({"role": "user", "content": prompt}))
    return pins


def _protected(state, messages):
    """Indices compaction must not delete: pinned instructions and the conversation's anchors.

    Both lists already hold encoded messages (they are compared byte-for-byte against the
    messages in the current list), so they must not be encoded a second time.
    """
    pinned = set(state.get("pinned_requests", ()))
    pinned.update(state.get("context_pins", ()))
    return {index for index, message in enumerate(messages) if encode(message) in pinned}


def _prefix_end(messages, pinned):
    """Length of the leading run preserved verbatim: system prompt, reference data, request.

    Only this contiguous head is fixed. Pinned messages further in are stepped over by the
    deletion loop rather than anchoring everything between them, so the assistant narration
    sitting between two user instructions is still compactable.
    """
    end = 0
    while end < len(messages) and (end in pinned or messages[end].get("role") == "system"):
        end += 1
    return end


# Escalating backoff for a summariser that keeps failing (60s, then 300s, then 900s).
COMPACTION_COOLDOWN_SECONDS = (60, 300, 900)

COMPACTION_INSTRUCTIONS = """Write a handoff summary for the agent that continues this task from the same
saved state. Keep every user instruction and constraint verbatim in meaning; they are the intent
everything else derives from and cannot be reconstructed from the work that followed. Summarise
what was done, what was decided and why, what is still open, and the exact identifiers needed to
continue (artifact ids, file paths, workflow keys, receipts). Drop narration and repeated output.
Write plain Markdown under these headings, omitting any that are empty:

## Goal
## Constraints
## Progress (done / in progress / blocked)
## Key decisions
## Next steps
## Critical context"""

SUMMARY_PREFIX = "Prior context summary (continuation of the same task; reference only): "


def _for_summary(messages):
    """Bound each tool result before summarising it.

    A single command or page can dominate the payload and crowd out the instructions the
    summary exists to preserve, so long results are truncated with an explicit marker.
    """
    limit = 2000
    bounded = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "tool" and isinstance(content, str) and len(content) > limit:
            content = content[:limit] + f"\n… [truncated {len(content) - limit} characters]"
        bounded.append({**{k: v for k, v in message.items() if k != "provider_state"}, "content": content})
    return bounded


def _free(messages, start, end, pinned):
    """A group is deletable only when it holds no protected message and is not the live turn."""
    if end <= start or end >= len(messages):
        return False
    return not any(index in pinned for index in range(start, end))


async def compact_for_model(hub, job, state, config, model, *, overflow=False):
    from ..models import HTTPProvider

    binding = hub.extensions.provider_binding(
        model, hub.extensions.run_snapshot(job)
    ) or hub.models.bindings.get(model)
    limits = (
        await binding.provider.limits.discover(binding.model)
        if binding and isinstance(binding.provider, HTTPProvider)
        else {}
    )
    chars = config.context_chars  # Optional legacy caller cap; there is no fixed default model window.
    output = min(config.max_output_tokens, limits.get("max_output_tokens", config.max_output_tokens))
    available = limits.get("max_input_tokens")
    if window := limits.get("context_window"):
        # A large requested allowance must not starve the prompt on a small window.
        output = min(output, max(1, window // 4))
        available = min(available or window, window - output)
    state["output_allowance"] = output
    if available is not None:
        ratio = limits.get("tokens_per_byte", 1)
        overhead = len(
            encode(
                {
                    "tools": [
                        hub.tools.spec(n, config.tool_revisions.get(n)).model_dump() for n in config.tools
                    ],
                    "schema": config.response_schema,
                }
            ).encode()
        )
        # Convert service tokens to a conservative character bound, accounting for CJK and tool schemas.
        wire = encode(state["messages"])
        bytes_per_char = len(wire.encode()) / max(1, len(wire))
        detected = max(1, int((available * 0.8 / ratio - overhead) / bytes_per_char))
        chars = min(chars, detected) if chars else detected
    if overflow:
        reduced = max(1, int(len(encode(state["messages"])) * 0.65))
        chars = min(chars, reduced) if chars else reduced
    if chars is not None:
        await hub.compact_context(job, state, chars)


async def compact_context(hub, job, state, limit):
    messages = state["messages"]
    if len(encode(messages)) <= limit:
        return
    pinned = _protected(state, messages)
    if hub.backends.binding("context", job):
        prefix = _prefix_end(messages, pinned)
        tail_start = max(prefix, len(messages) - 4)
        while tail_start > prefix and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        cooldown = state.get("compaction_cooldown") or {}
        # A summariser that just failed is not worth retrying this turn: repeating a broken
        # call every turn pays its cost forever. Back off further each time it fails again.
        if (
            tail_start > prefix
            and _free(messages, prefix, tail_start, pinned)
            and time.time() >= cooldown.get("until", 0)
        ):
            # Fail open: a broken summariser must never be worse than not installing one, so
            # a failure or an unusable result falls through to plain deletion below.
            try:
                compressed = await hub.backends.call(
                    "context",
                    "compact",
                    {
                        "messages": _for_summary(messages[prefix:tail_start]),
                        "max_chars": max(1000, limit // 3),
                        "instructions": COMPACTION_INSTRUCTIONS,
                    },
                    job,
                )
                summary = compressed["summary"].strip() if isinstance(compressed, dict) else ""
                state.pop("compaction_cooldown", None)
            except (ValueError, KeyError, PermissionError, asyncio.TimeoutError, RuntimeError) as exc:
                summary = ""
                strikes = cooldown.get("strikes", 0) + 1
                delay = COMPACTION_COOLDOWN_SECONDS[min(strikes - 1, len(COMPACTION_COOLDOWN_SECONDS) - 1)]
                state["compaction_cooldown"] = {"until": time.time() + delay, "strikes": strikes}
                hub.store.checkpoint(job, state)
                with hub.store.connect() as db:
                    hub.store.event(
                        db,
                        job["run_id"],
                        "context.compaction_failed",
                        {
                            "step": job["id"],
                            "error": type(exc).__name__,
                            "retry_after_seconds": delay,
                            "attempt": strikes,
                        },
                    )
            if summary:
                state["messages"] = (
                    messages[:prefix]
                    + [{"role": "user", "content": SUMMARY_PREFIX + summary}]
                    + messages[tail_start:]
                )
                messages = state["messages"]
    # Preserve the original instructions and the run's own request. Remove complete
    # historical assistant/tool groups, never orphan provider tool call ids, and never
    # drop a message the task depends on (pinned) or the newest turn's evidence.
    original = len(messages)
    pinned = _protected(state, messages)
    start = _prefix_end(messages, pinned)
    while len(encode(messages)) > limit and start < len(messages):
        if start in pinned or messages[start].get("role") == "system":
            start += 1
            continue
        end = start + 1
        while end < len(messages) and messages[end].get("role") == "tool":
            end += 1
        if end >= len(messages) or any(index in pinned for index in range(start, end)):
            # Never consume the live turn, and never split a pinned instruction from
            # the work it governs: step over this group instead of deleting it.
            start = end
            continue
        del messages[start:end]
        pinned = _protected(state, messages)
        # The cursor does not move: the next message now occupies this index, and it may
        # itself be deletable. Advancing here is what keeps the loop from re-scanning.
    if len(encode(messages)) > limit:
        raise ValueError("context budget exhausted; reduce document or tool output size")
    if len(messages) != original:
        with hub.store.transaction() as db:
            hub.store.assert_owner(db, job)
            hub.store.event(
                db,
                job["run_id"],
                "context.compacted",
                {"step": job["id"], "removed_messages": original - len(messages)},
            )
        hub.store.checkpoint(job, state)
