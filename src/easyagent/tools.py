from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jsonschema import validate

from .contracts import ToolSpec
from .store import Conflict, encode


class ApprovalRequired(Exception):
    pass


class UncertainEffect(Exception):
    pass


class ToolInputError(ValueError):
    """Rejected before any handler execution; safe to return for model correction."""


class ToolPreparationError(ValueError):
    """Trusted adapter rejected the request before any external transmission."""


class ToolRejectedError(ValueError):
    """The remote endpoint explicitly rejected the upload without performing it."""


def validate_input(arguments, schema):
    from jsonschema import ValidationError

    try:
        validate(arguments, schema)
    except ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "input"
        # Do not echo user values or credential-bearing input into diagnostics.
        raise ToolInputError(
            f"{path}: does not satisfy {exc.validator}; follow the tool input schema"
        ) from exc


class WaitingChildren(Exception):
    """Suspend a tool invocation without consuming another attempt or call budget."""


class WaitingRemote(Exception):
    """Checkpointed read-only polling; release the worker until the next request."""

    def __init__(self, delay):
        self.delay = delay


class WaitingInput(Exception):
    """Suspend until a person answers a durable input request; no attempt is consumed."""


# Pause signals travel through tool handlers unchanged; they are not tool failures.
PAUSE_SIGNALS = (WaitingChildren, WaitingRemote, WaitingInput, ApprovalRequired, UncertainEffect)


def redact_error(text, limit=2000):
    """Bearer tokens, API keys and signed URLs never enter model context through error text."""
    import re

    return re.sub(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|api[_-]?key=[^&\s]+|https?://\S+\?\S+)",
                  "[redacted]", str(text))[:limit]


def tool_failure_observation(exc, *, executed=None, code=None):
    """Describe a failed tool call for the model. Prepared/rejected errors were never sent."""
    not_performed = isinstance(exc, (ToolInputError, ToolPreparationError, ToolRejectedError))
    return {"error": {"code": code or type(exc).__name__, "message": redact_error(exc),
                      "executed": (not not_performed) if executed is None else executed}}


@dataclass
class InvocationContext:
    invocation_id: str
    run_id: str
    step_id: str
    job: dict | None = None
    store: Any = None


Handler = Callable[[dict, InvocationContext], Awaitable[Any]]


class ToolRegistry:
    def __init__(self):
        self.entries: dict[str, tuple[ToolSpec, Handler]] = {}
        self.versions: dict[tuple[str, int], tuple[ToolSpec, Handler]] = {}
        self.latest: dict[str, int] = {}
        self.internal_names: set[str] = set()
        self.catalog_exclusions = None
        self.refresh = None
        self.extensions = None

    def register(self, spec: ToolSpec, handler: Handler):
        if spec.name in self.entries:
            raise ValueError(f"duplicate tool: {spec.name}")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("tool handlers must be async; use a process plugin for blocking code")
        self.entries[spec.name] = (spec, handler)

    def regeneratable_media(self, name, revision=None):
        """Only built-in synchronous image generation may repeat an uncertain paid call.

        This is not an idempotency claim. Generic writes and video submissions keep
        their receipts and reconciliation rules; model-authored adapters cannot opt in.
        """
        if not self.extensions or not name.startswith(('media.', 'builtin_media.')):
            return False
        from urllib.parse import urlsplit
        try:
            definition = self.extensions.hub.development.get('api', name, revision)['definition']
        except KeyError:
            return False
        return (definition.get('method') == 'POST' and definition.get('response_mode') == 'media'
                and urlsplit(definition['url']).path.rstrip('/').endswith(('/images/generations', '/images/edits')))

    def entry(self, name, revision=None):
        if self.refresh:
            self.refresh(name, revision)
        if revision is not None:
            if (name, revision) not in self.versions:
                raise ValueError(f"unknown tool revision: {name}@{revision}")
            return self.versions[name, revision]
        if name not in self.entries:
            raise ValueError(f"unknown tool: {name}")
        return self.entries[name]

    def spec(self, name, revision=None):
        return self.entry(name, revision)[0]

    def revision(self, name):
        self.spec(name)
        return self.latest.get(name)

    @staticmethod
    def job_revision(job, name):
        step = job["spec"]
        if step.get("kind") == "tool" and step.get("target") == name:
            return step.get("tool_revision")
        if (step.get("compensate") or {}).get("target") == name:
            return step["compensate"].get("tool_revision")
        return step.get("input", {}).get("tool_revisions", {}).get(name)

    def catalog(self, *, include_internal=False):
        hidden = self.catalog_exclusions() if self.catalog_exclusions else set()
        return [
            entry[0].model_dump()
            for name, entry in self.entries.items()
            if name not in hidden and (include_internal or name not in self.internal_names)
        ]

    async def invoke(self, store, job, name, arguments, slot, require_approval=False, revision=None):
        revision = revision if revision is not None else self.job_revision(job, name)
        spec, handler = self.entry(name, revision)
        if self.extensions:
            # Persist transformations once so an approval/restart uses identical arguments.
            key = hashlib.sha256(
                encode([job["run_id"], job["id"], slot, name, arguments]).encode()
            ).hexdigest()
            with store.connect() as db:
                cached = db.execute(
                    "SELECT value FROM memory WHERE namespace='extension-tool-inputs' AND key=?", (key,)
                ).fetchone()
            if cached:
                arguments = json.loads(cached[0])
            else:
                transformed = await self.extensions.dispatch(
                    "tool.before_call", {"tool": name, "arguments": arguments}, job=job
                )
                arguments = transformed["arguments"]
                validate_input(arguments, spec.input_schema)
                store.memory_put("extension-tool-inputs", key, arguments, "extension-hooks")
        validate_input(arguments, spec.input_schema)
        arguments_json = encode(arguments)
        identity = encode(
            [job["run_id"], job["id"], slot, name, arguments] + ([revision] if revision is not None else [])
        )
        invocation_id = hashlib.sha256(identity.encode()).hexdigest()
        pause = None
        with store.transaction() as db:
            store.assert_owner(db, job)
            automatic = store.automatic(db, job['run_id'])
            row = db.execute("SELECT * FROM invocations WHERE id=?", (invocation_id,)).fetchone()
            if row and row['status'] in ('started', 'uncertain'):
                receipt = db.execute('SELECT 1 FROM http_receipts WHERE invocation_id=?', (invocation_id,)).fetchone()
                if receipt or (automatic and self.regeneratable_media(name, revision)):
                    db.execute("UPDATE invocations SET status='failed' WHERE id=?", (invocation_id,))
                    row = dict(row) | {'status': 'failed'}
                    store.event(db, job['run_id'], 'media.recovery_started', {
                        'invocation_id': invocation_id, 'reuse_response': bool(receipt),
                        'possible_duplicate_generation': not bool(receipt)})
            if row and row["status"] == "succeeded":
                return json.loads(row["output"])
            if row and row["status"] == "denied":
                raise PermissionError("tool invocation was denied")
            if row and row["status"] == "uncertain":
                pause = UncertainEffect("external result requires reconciliation: " + invocation_id)
            elif row and row["status"] == "started" and spec.effect == "write" and not spec.idempotent:
                db.execute("UPDATE invocations SET status='uncertain' WHERE id=?", (invocation_id,))
                pause = UncertainEffect("a previous write may have completed: " + invocation_id)
            elif (spec.effect == "write" or require_approval) and not automatic and (not row or not row["approved"]):
                if not row:
                    store.reserve(db, job["run_id"], "tool_calls")
                    db.execute(
                        "INSERT INTO invocations(id,run_id,step_id,tool,arguments,status,tool_revision) VALUES(?,?,?,?,?,'approval',?)",
                        (invocation_id, job["run_id"], job["id"], name, arguments_json, revision),
                    )
                    store.event(
                        db,
                        job["run_id"],
                        "tool.approval_required",
                        {"invocation_id": invocation_id, "tool": name},
                    )
                pause = ApprovalRequired(invocation_id)
            else:
                if row:
                    db.execute("UPDATE invocations SET status='started' WHERE id=?", (invocation_id,))
                else:
                    store.reserve(db, job["run_id"], "tool_calls")
                    db.execute(
                        "INSERT INTO invocations(id,run_id,step_id,tool,arguments,status,tool_revision) VALUES(?,?,?,?,?,'started',?)",
                        (invocation_id, job["run_id"], job["id"], name, arguments_json, revision),
                    )
                if not row or row["status"] != "started":
                    if automatic and (spec.effect == 'write' or require_approval):
                        store.event(db, job['run_id'], 'tool.authorized',
                                    {'tool': name, 'invocation_id': invocation_id, 'source': 'automatic_task'})
                    store.event(
                        db, job["run_id"], "tool.started", {"tool": name, "invocation_id": invocation_id}
                    )
        if pause:
            if (
                isinstance(pause, ApprovalRequired)
                and self.extensions
                and self.extensions.hub.backends.binding("approval", job)
            ):
                try:
                    await self.extensions.hub.backends.call(
                        "approval",
                        "present",
                        {
                            "invocation_id": invocation_id,
                            "run_id": job["run_id"],
                            "tool": name,
                            "arguments": arguments,
                        },
                        job,
                    )
                except Exception:
                    # Local durable approval remains available if the alternate display fails.
                    with store.connect() as db:
                        store.event(
                            db, job["run_id"], "approval.transport_failed", {"invocation_id": invocation_id}
                        )
            raise pause
        context = InvocationContext(invocation_id, job["run_id"], job["id"], job, store)
        try:
            result = await handler(arguments, context)
            if self.extensions:
                transformed = await self.extensions.dispatch(
                    "tool.after_result", {"tool": name, "result": result}, job=job
                )
                result = transformed["result"]
            validate(result, spec.output_schema)
            encoded = encode(result)
            if len(encoded.encode()) > 1_000_000:
                raise ValueError("tool output exceeds 1 MB; store artifacts externally")
        except PAUSE_SIGNALS:
            raise
        except BaseException as exc:
            if self.extensions and not isinstance(exc, asyncio.CancelledError):
                try:
                    await self.extensions.dispatch(
                        "tool.error", {"tool": name, "error": type(exc).__name__}, job=job
                    )
                except Exception:
                    pass  # Reporting cannot change the original side-effect classification.
            # Leave non-idempotent started calls unresolved on cancellation or crash.
            not_performed = isinstance(exc, (ToolPreparationError, ToolRejectedError))
            with store.connect() as db:
                received = db.execute('SELECT 1 FROM http_receipts WHERE invocation_id=?', (invocation_id,)).fetchone()
            regenerate = automatic and self.regeneratable_media(name, revision) and not isinstance(exc, asyncio.CancelledError)
            if spec.effect == "write" and not spec.idempotent and not not_performed and not received and not regenerate:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                with store.transaction() as db:
                    store.assert_owner(db, job)
                    db.execute(
                        "UPDATE invocations SET status='uncertain',error=? WHERE id=?",
                        (type(exc).__name__, invocation_id),
                    )
                raise UncertainEffect("write failed after it started; verify the receipt") from exc
            with store.transaction() as db:
                store.assert_owner(db, job)
                if regenerate and not not_performed and not received:
                    store.event(db, job['run_id'], 'media.regeneration_needed', {
                        'invocation_id': invocation_id, 'error_type': type(exc).__name__,
                        'receipt_available': False, 'possible_duplicate_generation': True})
                db.execute(
                    "UPDATE invocations SET status='failed',error=? WHERE id=?",
                    (str(exc) if not_performed else type(exc).__name__, invocation_id),
                )
                if isinstance(exc, ToolPreparationError):
                    store.event(db, job['run_id'], 'tool.not_sent',
                                {'invocation_id': invocation_id, 'reason': str(exc)})
                elif isinstance(exc, ToolRejectedError):
                    store.event(db, job['run_id'], 'tool.rejected',
                                {'invocation_id': invocation_id, 'reason': str(exc)})
            raise
        with store.transaction() as db:
            store.assert_owner(db, job)
            db.execute(
                "UPDATE invocations SET status='succeeded',output=? WHERE id=?", (encoded, invocation_id)
            )
            store.event(db, job["run_id"], "tool.succeeded", {"tool": name, "invocation_id": invocation_id})
        return result

    def approve(self, store, invocation_id, approved):
        with store.transaction() as db:
            row = db.execute("SELECT * FROM invocations WHERE id=?", (invocation_id,)).fetchone()
            if not row:
                raise KeyError(invocation_id)
            if row["status"] != "approval":
                raise Conflict("invocation is no longer awaiting approval")
            run = db.execute("SELECT status FROM runs WHERE id=?", (row["run_id"],)).fetchone()
            if run[0] in ("cancelled", "failed", "succeeded"):
                raise Conflict("run is already terminal")
            step = db.execute(
                "SELECT status FROM steps WHERE run_id=? AND id=?", (row["run_id"], row["step_id"])
            ).fetchone()
            if step[0] != "waiting_approval":
                raise Conflict("step is still pausing; retry approval shortly")
            db.execute(
                "UPDATE invocations SET approved=?,status=? WHERE id=?",
                (int(approved), "ready" if approved else "denied", invocation_id),
            )
            db.execute(
                "UPDATE steps SET status=?,ready_at=0 WHERE run_id=? AND id=?",
                ("queued" if approved else "failed", row["run_id"], row["step_id"]),
            )
            store.event(
                db,
                row["run_id"],
                "tool.approved" if approved else "tool.denied",
                {"invocation_id": invocation_id},
            )
            store.reconcile(db, row["run_id"])

    def reconcile(self, store, invocation_id, output, receipt=None):
        with store.transaction() as db:
            row = db.execute("SELECT * FROM invocations WHERE id=?", (invocation_id,)).fetchone()
            if not row:
                raise KeyError(invocation_id)
            if row["status"] != "uncertain":
                raise Conflict("only uncertain invocations can be reconciled")
            validate(output, self.spec(row["tool"], row["tool_revision"]).output_schema)
            state = db.execute(
                "SELECT status FROM steps WHERE run_id=? AND id=?", (row["run_id"], row["step_id"])
            ).fetchone()[0]
            if state != "needs_attention":
                raise Conflict("step is not awaiting reconciliation")
            db.execute(
                "UPDATE invocations SET status='succeeded',output=? WHERE id=?",
                (encode(output), invocation_id),
            )
            if receipt is not None:
                if not receipt.strip():
                    raise ValueError("a verified receipt is required")
                db.execute(
                    "INSERT OR REPLACE INTO memory VALUES(?,?,?,?,?)",
                    ("receipts", invocation_id, encode(receipt), "human-reconciliation", time.time()),
                )
            db.execute(
                "UPDATE steps SET status='queued',ready_at=0 WHERE run_id=? AND id=?",
                (row["run_id"], row["step_id"]),
            )
            store.event(db, row["run_id"], "tool.reconciled", {"invocation_id": invocation_id})
            store.reconcile(db, row["run_id"])


def register_builtins(registry, store):
    async def echo(args, ctx):
        return args

    async def memory_search(args, ctx):
        if ctx.job["spec"].get("kind") == "agent" and args["namespace"] not in ctx.job["spec"]["input"].get(
            "memory_namespaces", []
        ):
            raise PermissionError("memory namespace not granted to this Agent")
        from .components import digest

        if registry.extensions and registry.extensions.hub.backends.binding("memory", ctx.job):
            result = await registry.extensions.hub.backends.call("memory", "search", args, ctx.job)
            return {"items": [r | {"digest": digest(r["value"])} for r in result["items"]]}
        return {
            "items": [
                r | {"digest": digest(r["value"])}
                for r in store.memory_search(args["namespace"], args.get("query", ""))
            ]
        }

    async def to_text(args, ctx):
        value = args["value"]
        return {"text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)}

    registry.register(
        ToolSpec(
            name="core.to_text",
            description="Convert an object/array to JSON text for a later model prompt; does not execute instructions",
            input_schema={
                "type": "object",
                "properties": {"value": {}},
                "required": ["value"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        to_text,
    )
    registry.register(ToolSpec(name="core.echo", description="Return the supplied JSON object"), echo)
    registry.register(
        ToolSpec(
            name="memory.search",
            description="Search a memory namespace with provenance",
            input_schema={
                "type": "object",
                "properties": {"namespace": {"type": "string"}, "query": {"type": "string"}},
                "required": ["namespace"],
                "additionalProperties": False,
            },
        ),
        memory_search,
    )
