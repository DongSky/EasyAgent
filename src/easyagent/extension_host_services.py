"""Typed host services for session-aware extension commands. Capabilities remain explicit."""

from .contracts import ToolSpec


def install_host_services(hub):
    def session(ctx):
        metadata = hub.store.run(ctx.run_id)["spec"].get("metadata", {})
        identifier = metadata.get("extension_session") or metadata.get("conversation")
        if not identifier:
            raise PermissionError("select a conversation before calling this command")
        return identifier

    async def read(args, ctx):
        return hub.conversations.get(session(ctx))

    async def send(args, ctx):
        return await hub.conversations.send(session(ctx), {**args, "idempotency_key": ctx.invocation_id})

    async def interrupt(args, ctx):
        return await hub.conversations.interrupt(session(ctx))

    async def fork(args, ctx):
        return await hub.conversations.fork(session(ctx), args)

    async def compact(args, ctx):
        identifier = session(ctx)
        hub.store.memory_put(
            "compaction-requests",
            ctx.invocation_id,
            {"conversation": identifier, "options": args},
            "extension",
        )
        return {"queued": True, "request_id": ctx.invocation_id}

    async def configure(args, ctx):
        return await hub.conversations.configure(session(ctx), args)

    async def resources(args, ctx):
        return hub.extensions.resources()

    from .sessions import ConversationInput, ConversationFork, ConversationCompact, ConversationSettings

    definitions = [
        ("session.read", read, {"type": "object", "additionalProperties": False}),
        ("session.send", send, ConversationInput.model_json_schema()),
        ("session.interrupt", interrupt, {"type": "object", "additionalProperties": False}),
        ("session.fork", fork, ConversationFork.model_json_schema()),
        ("session.compact", compact, ConversationCompact.model_json_schema()),
        ("session.configure", configure, ConversationSettings.model_json_schema()),
        ("resources", resources, {"type": "object", "additionalProperties": False}),
    ]
    hub.extension_host_services = set()
    for name, handler, schema in definitions:
        name = "host." + name
        hub.extension_host_services.add(name)
        hub.tools.internal_names.add(name)
        hub.tools.register(
            ToolSpec(
                name=name,
                description="Extension host service " + name,
                input_schema=schema,
                effect="local",
                idempotent=True,
            ),
            handler,
        )
