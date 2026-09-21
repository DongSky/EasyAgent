from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

from jsonschema import ValidationError as SchemaError

from .contracts import AgentConfig, ModelRequest, Step, Workflow
from .artifacts import Artifacts
from .knowledge import Knowledge
from .scheduling import Scheduler
from .evolution import Evolution
from .models import MockProvider, ModelRegistry, ProviderError
from .skills import SkillRegistry
from .store import LeaseLost, Store, encode
from .retry_policy import MODEL_WAIT, attempt_number, error_info, retry_delay
from .tools import (  # noqa: F401  (WaitingInput is re-exported for goals.py and SDK callers)
    PAUSE_SIGNALS,
    ApprovalRequired,
    ToolRegistry,
    ToolInputError,
    UncertainEffect,
    WaitingChildren,
    WaitingInput,
    WaitingRemote,
    register_builtins,
    tool_failure_observation,
)

logger = logging.getLogger(__name__)


class ConditionSkipped(Exception):
    pass


def resolve(value, outputs):
    if isinstance(value, dict):
        if "$ref" in value:
            head, *parts = value["$ref"].split(".")
            result = outputs[head]
            for part in parts:
                result = result[int(part)] if isinstance(result, list) else result[part]
            return result
        return {k: resolve(v, outputs) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, outputs) for v in value]
    return value


def named_outputs(run):
    """Stable workflow outputs shared by HTTP clients and parent workflows."""
    from jsonschema import Draft202012Validator
    mapping = run['spec']['metadata'].get('outputs', {})
    if not isinstance(mapping, dict):
        raise ValueError('workflow metadata.outputs must be an object of named output references')
    try:
        outputs = resolve(mapping, {'$input': run['spec']['inputs'], **{s['id']: s['output'] for s in run['steps']}})
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError('workflow output mapping could not be resolved; inspect metadata.outputs') from exc
    if schema := run['spec']['metadata'].get('output_schema'):
        Draft202012Validator(schema).validate(outputs)
    return outputs


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


# One correction round: enough to fix a formatting slip, bounded so a noncompliant model
# cannot loop. Taken from Hermes' delegate contract ("exactly one bounded correction round").
SCHEMA_CORRECTION_ROUNDS = 1

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


class Hub:
    def __init__(
        self, database: str | Path = ".eah/hub.db", *, concurrency=4, lease_seconds=30, poll_seconds=0.1,
        run_roots: set[str] | None = None,
    ):
        if not 1 <= concurrency <= 64 or lease_seconds < 0.15:
            raise ValueError("invalid concurrency or lease duration")
        self.store = Store(database)
        self.run_roots = run_roots
        self.tools = ToolRegistry()
        register_builtins(self.tools, self.store)
        self.models = ModelRegistry()
        self.models.register(
            "mock", MockProvider(), "deterministic-fixture", {"chat", "decision", "image", "embedding"}
        )
        self.knowledge = Knowledge(self.store, self.models)
        self.artifacts = Artifacts(self.store)
        self.scheduler = Scheduler(self)
        self.skills = SkillRegistry()
        from .skill_packages import SkillPackages

        self.skill_packages = SkillPackages(self)
        self.evolution = Evolution(self.store, self.models, self.tools, self)
        self.concurrency, self.lease_seconds, self.poll_seconds = concurrency, lease_seconds, poll_seconds
        self.workers: list[asyncio.Task] = []
        from .development import RuntimeDevelopment

        self.development = RuntimeDevelopment(self)
        from .node_library import NodeLibrary

        self.library = NodeLibrary(self)
        from .model_catalog import ModelCatalog

        self.model_catalog = ModelCatalog(self)
        from .extensions import ExtensionHost

        self.extensions = ExtensionHost(self)
        self.tools.extensions = self.extensions
        from .sessions import Conversations

        self.conversations = Conversations(self)
        from .delegation import Delegation

        self.delegation = Delegation(self)
        from .learning import Learning

        self.learning = Learning(self)
        from .connections import Connections

        self.connections = Connections(self)
        from .search import SearchConnections

        self.search_connections = SearchConnections(self)
        self._mcp = None
        from .code_development import CodeDevelopment

        self.code = CodeDevelopment(self)
        from .maintenance import Maintenance

        self.maintenance = Maintenance(self)
        from .extension_host_services import install_host_services

        install_host_services(self)
        from .gateway import Gateway

        self.gateway = Gateway(self)
        from .memory_management import MemoryManagement

        self.memory_management = MemoryManagement(self)
        from .goals import GoalController

        self.goals = GoalController(self)
        from .backends import BackendRegistry

        self.backends = BackendRegistry(self)
        from .execution_backends import LocalExecution

        self.execution = LocalExecution(self)
        from .workspace_files import install as install_workspace_files

        install_workspace_files(self)
        from .voice import Voice

        self.voice = Voice(self)
        from .attachments import Attachments
        from .workspace_chat import WorkspaceChat

        self.attachments = Attachments(self)
        self.chat = WorkspaceChat(self)
        from .build_capabilities import BuildCapabilities
        self.build_capabilities = BuildCapabilities(self)
        from .autonomy import Autonomy
        self.autonomy = Autonomy(self)

    @property
    def mcp(self):
        if self._mcp is None:
            try:
                from .mcp_manager import ManagedMCP
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.split('.')[0] == 'mcp':
                    raise RuntimeError('MCP requires: pip install "easyagent[mcp]"') from exc
                raise
            self._mcp = ManagedMCP(self)
        return self._mcp

    def available_tools(self):
        rows = self.tools.catalog()
        settings = self.execution.settings()
        bindings = self.backends.snapshot()
        unavailable = {
            "backend." + kind
            for kind in ("browser", "terminal")
            if kind not in bindings and not getattr(settings, kind + "_enabled")
        }
        if not self.voice.settings() and "media" not in bindings:
            unavailable.update({"voice.transcribe", "voice.speak", "backend.media"})
        if "channel" not in bindings:
            unavailable.add("backend.channel")
        if not settings.terminal_enabled:
            unavailable.update({"attachments.import_file", "attachments.export_file"})
            unavailable.update(t["name"] for t in rows if t["name"].startswith("files."))
        return [t for t in rows if t["name"] not in unavailable]

    def submit(self, workflow: Workflow | dict, idempotency_key=None, parent=None, parent_job=None, *, execution='confirm'):
        workflow = self.prepare(workflow)
        if schema := workflow.metadata.get("component_input_schema"):
            from jsonschema import validate

            validate(workflow.inputs, schema)
        return self.store.submit(workflow, idempotency_key, parent, parent_job, execution=execution)

    def prepare(self, workflow: Workflow | dict, *, _depth=0):
        """Validate and snapshot a workflow without creating or executing a run."""
        if _depth > 8:
            raise ValueError("subworkflow nesting exceeds 8 levels")
        workflow = Workflow.model_validate(workflow).model_copy(deep=True)
        if hasattr(self, "backends") and (bindings := self.backends.snapshot()):
            workflow.metadata.setdefault("backends", bindings)
        if hasattr(self, "voice") and (voice_settings := self.voice.settings()):
            workflow.metadata.setdefault("voice_settings", voice_settings)
        if self.extensions.active:
            workflow.metadata.setdefault("extensions", self.extensions.snapshot())
        for binding in workflow.metadata.get("backends", {}).values():
            workflow.metadata.setdefault("extensions", {})[binding["extension"]] = binding["revision"]
        for name, revision in workflow.metadata.get("extensions", {}).items():
            if (name, revision) not in self.extensions.packages:
                raise ValueError("required extension version is not installed: " + name)
        for step in workflow.steps:
            if step.kind == "tool":
                self.tools.spec(step.target, step.tool_revision)
                if step.tool_revision is None:
                    step.tool_revision = self.tools.revision(step.target)
            elif (
                step.kind in ("agent", "model")
                and step.target not in self.models.bindings
                and not self.extensions.provider_binding(step.target, workflow.metadata.get("extensions", {}))
            ):
                raise ValueError("unregistered model alias: " + step.target)
            if step.kind == "agent":
                prompt_ref = step.input.get("prompt") if isinstance(step.input.get("prompt"), dict) else None
                config = AgentConfig.model_validate(
                    {**step.input, "prompt": ""} if prompt_ref else step.input
                )
                if config.policy:
                    version = self.evolution.active(config.policy)
                    policy = version["body"]
                    if not set(config.tools).issubset(policy["tools"]):
                        raise PermissionError("requested tools exceed active policy")
                    config.instructions = policy["instructions"]
                    step.target = policy["model"]
                    workflow.metadata.setdefault("policy_versions", {})[step.id] = version["id"]
                    config.policy = None
                for name in config.tools:
                    self.tools.spec(name, config.tool_revisions.get(name))
                    if name not in config.tool_revisions and (revision := self.tools.revision(name)):
                        config.tool_revisions[name] = revision
                if config.development:
                    self.development.prepare_grant(config.development)
                elif any(name.startswith("development.") for name in config.tools):
                    raise PermissionError("development tools require an explicit development grant")
                if not config.code_development and any(name.startswith("code.") for name in config.tools):
                    raise PermissionError("code development tools require an explicit namespace grant")
                if config.delegation:
                    grant = config.delegation
                    if not set(grant.models).issubset(self.models.bindings):
                        raise ValueError("unknown delegated model")
                    for name in grant.tools:
                        self.tools.spec(name)
                    # Grant limits apply to this node's delegated children. RunLimits is
                    # the separate shared budget for the entire workflow tree.
                elif any(name.startswith("agents.") for name in config.tools):
                    raise PermissionError("agent delegation tools require a grant")
                if "skills.save" in config.tools and not config.skill_namespace:
                    raise PermissionError("skill writes require a namespace grant")
                for skill in config.skill_access:
                    config.skill_resources[skill] = self.skills.snapshot(skill)
                if config.skill_access:
                    metadata = {s["name"]: s for s in self.skills.catalog()}
                    # Added last: a skill catalogue changes whenever someone authors a skill, so it
                    # must not sit in front of the instructions a provider cache can reuse.
                    config.instructions += (
                        "\n\nAvailable skills (read the full file with skills.read when a task matches): "
                        + encode(
                            [
                                {"name": s, "description": metadata[s]["description"]}
                                for s in config.skill_access
                            ]
                        )
                    )
                    config.skill_access = []
                for skill in config.skills:
                    config.skill_resources[skill] = self.skills.snapshot(skill)
                    config.instructions += "\n\nSkill " + skill + ":\n" + self.skills.load(skill)
                # Snapshot the selected instructions. Later skill edits cannot alter an existing run.
                config.skills = []
                step.input = config.model_dump()
                if prompt_ref:
                    step.input["prompt"] = prompt_ref
            if step.kind in ("foreach", "subworkflow"):
                if step.workflow_ref:
                    saved = self.development.get("workflow", step.workflow_ref.id, step.workflow_ref.revision)
                    step.workflow_ref.revision = saved["revision"]
                    step.body = saved["workflow"]
                if not step.body:
                    raise ValueError("composite steps need body or workflow_ref")
                step.body = self.prepare(step.body, _depth=_depth + 1).model_dump()
            if step.compensate:
                name = step.compensate["target"]
                self.tools.spec(name, step.compensate.get("tool_revision"))
                if "tool_revision" not in step.compensate and (revision := self.tools.revision(name)):
                    step.compensate["tool_revision"] = revision
        return workflow

    async def start(self):
        if not self.workers:
            if self._mcp is not None or self.store.memory_search("managed-mcp", limit=1):
                await self.mcp.restore()
            self.workers = [
                asyncio.create_task(self.worker(), name=f"eah-worker-{i}") for i in range(self.concurrency)
            ]
            if self.run_roots is None:
                self.workers.append(asyncio.create_task(self.delivery_loop(), name="eah-delivery"))
                self.workers.append(asyncio.create_task(self.schedule_loop(), name="eah-scheduler"))
                self.workers.append(asyncio.create_task(self.maintenance.loop(), name="eah-maintenance"))

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        if self._mcp is not None:
            await self._mcp.close()
        await self.extensions.close()
        await self.execution.close()
        for connection in getattr(self.tools, "mcp_connections", []):
            await connection.close()

    async def wait(self, run_id, timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                run = self.store.run(run_id)
                if run["status"] in (
                    "succeeded",
                    "failed",
                    "cancelled",
                    "waiting_approval",
                    "waiting_input",
                    "needs_attention",
                ):
                    return run
                await asyncio.sleep(self.poll_seconds)

    async def worker(self):
        while True:
            try:
                job = self.store.claim(self.lease_seconds, self.run_roots)
                if not job:
                    await asyncio.sleep(self.poll_seconds)
                    continue
                await self.run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A bad job must not silently kill the long-lived worker.
                logger.exception("worker iteration failed")
                await asyncio.sleep(self.poll_seconds)

    async def schedule_loop(self):
        while True:
            try:
                self.scheduler.tick()
                await self.conversations.tick()
                self.gateway.replies()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(max(0.1, self.poll_seconds))

    async def delivery_loop(self):
        while True:
            try:
                await self.connections.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("delivery worker iteration failed")
            await asyncio.sleep(max(0.1, self.poll_seconds))

    async def heartbeat(self, job):
        while True:
            await asyncio.sleep(self.lease_seconds / 3)
            self.store.heartbeat(job, self.lease_seconds)

    async def run_job(self, job):
        work = asyncio.create_task(self.execute(job))
        pulse = asyncio.create_task(self.heartbeat(job))
        try:
            timeout = job['spec']['timeout_seconds']
            if (job['spec']['kind'] == 'tool' and
                    self.tools.regeneratable_media(job['spec']['target'], job['spec'].get('tool_revision'))):
                with self.store.connect() as db:
                    if self.store.automatic(db, job['run_id']):
                        timeout = None
            retry = job.get('retry_state', {})
            if 'step_timeout' in retry:
                if retry['step_timeout'] is None:
                    timeout = None
                elif timeout is not None:
                    timeout = max(timeout, retry['step_timeout'])
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait({work, pulse}, return_when=asyncio.FIRST_COMPLETED)
                if pulse in done:
                    pulse.result()
                output = await work
            incomplete = job["spec"]["kind"] == "goal" and output.get("goal_status") != "completed"
            self.store.finish(
                job,
                "failed" if incomplete else "succeeded",
                output,
                error=output.get("reason") if incomplete else None,
            )
            try:
                await self.extensions.dispatch(
                    "workflow.after_step", {"step": job["id"], "output": output}, job=job
                )
            except Exception:
                logger.exception("extension observer failed after durable step completion")
        except asyncio.CancelledError:
            raise
        except LeaseLost:
            pass
        except ConditionSkipped:
            self.store.finish(job, "skipped", error="condition did not match")
        except WaitingChildren:
            self.store.finish(job, "waiting_children", delay=0.1)
        except WaitingRemote as exc:
            self.store.finish(job, "waiting_remote", delay=exc.delay)
        except WaitingInput:
            self.store.finish(job, "waiting_input")
        except ApprovalRequired as exc:
            self.store.finish(job, "waiting_approval", error=str(exc))
        except UncertainEffect as exc:
            self.store.finish(job, "needs_attention", error=str(exc))
        except Exception as exc:
            # Cancel before inspecting invocation state, so an in-flight non-idempotent
            # write is never rescheduled by the ordinary transient-error path.
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            uncertain = False
            with self.store.transaction() as db:
                self.store.assert_owner(db, job)
                for row in db.execute(
                    "SELECT id,tool,tool_revision FROM invocations WHERE run_id=? AND step_id=? AND status='started'",
                    (job["run_id"], job["id"]),
                ).fetchall():
                    spec = self.tools.spec(row["tool"], row["tool_revision"])
                    if spec.effect == "write" and not spec.idempotent:
                        db.execute("UPDATE invocations SET status='uncertain' WHERE id=?", (row["id"],))
                        uncertain = True
            if uncertain:
                self.store.finish(job, "needs_attention", error="write interrupted; verify its receipt")
            else:
                fatal = isinstance(exc, (ValueError, KeyError, PermissionError, SchemaError))
                fatal = fatal or isinstance(exc, ProviderError) and not exc.retryable
                attempt = attempt_number(job)
                info = error_info(exc)
                fatal = fatal or info['category'] == 'configuration' or getattr(exc, 'retryable', None) is False
                retry = not fatal and attempt < job["spec"]["max_attempts"]
                if not fatal and not retry and self.tools.regeneratable_media(job['spec'].get('target', ''), job['spec'].get('tool_revision')):
                    with self.store.connect() as db:
                        retry = self.store.automatic(db, job['run_id'])
                if not retry and job["spec"].get("compensate") and not job["state"].get("compensation"):
                    self.store.checkpoint(
                        job,
                        {
                            **job["state"],
                            "compensation": job["spec"]["compensate"],
                            "original_error": type(exc).__name__,
                        },
                    )
                    self.store.finish(
                        job, "retrying", error="original operation failed; compensation scheduled"
                    )
                    return
                if not retry:
                    with self.store.connect() as db:
                        children = [
                            r[0]
                            for r in db.execute(
                                "SELECT child_id FROM child_runs WHERE parent_id=? AND step_id=?",
                                (job["run_id"], job["id"]),
                            )
                        ]
                    for identifier in children:
                        self.store.cancel(identifier)
                self.store.finish(
                    job,
                    "retrying" if retry else "failed",
                    error=(info['message'] if info['category'] != 'execution' else f"{type(exc).__name__}: {str(exc)[:500]}"),
                    delay=retry_delay(info, attempt) if retry else 0,
                    retry_state={**job.get('retry_state', {}), 'error': info, 'attempt': attempt,
                                 'max_attempts': job['spec']['max_attempts'], 'scheduled': retry},
                )
        finally:
            work.cancel()
            pulse.cancel()
            await asyncio.gather(work, pulse, return_exceptions=True)

    async def execute(self, job):
        spec = Step.model_validate(job["spec"])
        await self.extensions.dispatch("workflow.before_step", {"step": spec.model_dump()}, job=job)
        if attempt_number(job) > spec.max_attempts and not job["state"].get("compensation"):
            with self.store.connect() as db:
                regenerate = (self.store.automatic(db, job['run_id'])
                              and self.tools.regeneratable_media(spec.target, spec.tool_revision))
            if not regenerate:
                raise ValueError("step recovery/attempt budget exhausted")
        outputs = self.store.execution_inputs(job['run_id'])
        if spec.when and resolve({"$ref": spec.when["source"]}, outputs) != spec.when["equals"]:
            raise ConditionSkipped()
        literal_fields = (
            {"response_schema", "development"}
            if spec.kind in ("model", "agent")
            else {"schema"}
            if spec.kind == "input"
            else set()
        )
        arguments = (
            spec.input
            if spec.kind == "goal"
            else resolve(spec.input, outputs)
            if set(spec.input) == {"$ref"}
            else {k: v if k in literal_fields else resolve(v, outputs) for k, v in spec.input.items()}
        )
        if not isinstance(arguments, dict):
            raise ValueError("step input must resolve to an object of named arguments")
        if job["state"].get("compensation"):
            action = job["state"]["compensation"]
            await self.tools.invoke(
                self.store, job, action["target"], action.get("input", {}), "compensation"
            )
            raise ValueError("original operation failed; compensation completed")
        if spec.kind == "transform":
            return arguments
        if spec.kind == "input":
            from jsonschema import Draft202012Validator

            schema = arguments.get("schema", {"type": "object"})
            Draft202012Validator.check_schema(schema)
            identifier = job["run_id"] + "-" + job["id"]
            with self.store.transaction() as db:
                self.store.assert_owner(db, job)
                row = db.execute("SELECT * FROM input_requests WHERE id=?", (identifier,)).fetchone()
                if row and row["status"] == "answered":
                    return json.loads(row["output"])
                if not row:
                    db.execute(
                        "INSERT INTO input_requests VALUES(?,?,?,?,?,'waiting',NULL)",
                        (
                            identifier,
                            job["run_id"],
                            job["id"],
                            arguments.get("prompt", "Please provide the missing information"),
                            encode(schema),
                        ),
                    )
                    self.store.event(
                        db,
                        job["run_id"],
                        "input.requested",
                        {"id": identifier, "prompt": arguments.get("prompt")},
                    )
            raise WaitingInput()
        if spec.kind == "approval":
            return await self.tools.invoke(self.store, job, "core.echo", arguments, "approval", True)
        if spec.kind == "retrieve":
            return {"citations": await self.knowledge.search(**arguments)}
        if spec.kind == "artifact":
            content = arguments["content"]
            if not isinstance(content, str):
                content = encode(content)
            return self.artifacts.put(
                arguments.get("name", spec.id + ".txt"),
                content,
                arguments.get("media_type", "text/plain"),
                job["run_id"],
            )
        if spec.kind in ("foreach", "subworkflow"):
            return await self.composite(job, spec, arguments)
        if spec.kind == "goal":
            return await self.goals.execute(job, arguments)
        if spec.kind == "tool":
            result = await self.tools.invoke(
                self.store, job, spec.target, arguments, "step", spec.requires_approval
            )
            if spec.target == 'backend.terminal' and isinstance(result, dict) and result.get('exit_code', 0) != 0:
                self.store.checkpoint(job, {**job['state'], 'terminal_result': result})
                raise ValueError('terminal command failed; exit_code=' + str(result['exit_code'])
                                 + '; stderr=' + str(result.get('stderr', ''))[-1200:])
            return result
        if spec.kind == "model":
            if spec.requires_approval:
                await self.tools.invoke(
                    self.store,
                    job,
                    "core.echo",
                    {"model": spec.target, "input": arguments},
                    "model-approval",
                    True,
                )
            result = await self.generate(job, ModelRequest(model=spec.target, **arguments))
            return result.model_dump()
        return await self.agent(
            job, spec.target, AgentConfig.model_validate(arguments), spec.requires_approval
        )

    async def agent(self, job, model, config, force_approval=False):
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
            if history := self.conversations.context(job):
                state["messages"] = [{"role": "system", "content": config.instructions}, *history]
                # A conversation's history ends with the newest message, but a run may also carry
                # its own request (a workflow prompt, a resumed task). Dropping it would hide the
                # task from the model, so append it when the history does not already end with it.
                if config.prompt and not (history and history[-1].get("role") == "user"
                                          and history[-1].get("content") == config.prompt):
                    state["messages"].append({"role": "user", "content": config.prompt})
            state["pinned_requests"] = _pin_request(state["messages"], config.prompt)
            await self.extensions.dispatch("agent.start", {"model": model}, job=job)
            sources = []
            for namespace in config.knowledge:
                sources.extend(await self.knowledge.search(namespace, config.prompt))
            memories = []
            for namespace in config.memory_namespaces:
                if self.backends.binding("memory", job):
                    memories.extend(
                        (
                            await self.backends.call(
                                "memory", "search", {"namespace": namespace, "query": config.prompt}, job
                            )
                        )["items"]
                    )
                else:
                    memories.extend(self.store.memory_search(namespace))
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
            self.store.checkpoint(job, state)
        if config.strategy == "plan_execute":
            return await self.plan_execute(job, model, config, state, force_approval)
        while True:
            await self.execute_pending(job, state, config, force_approval)
            # A provider requires the complete tool-result group immediately after the calls.
            # Steering/mailbox input is delivered at the next model boundary, never inside it.
            self.conversations.steer(job, state)
            self.delegation.receive(job, state)
            if config.max_turns is not None and state["turns"] >= config.max_turns:
                raise ValueError("agent model-call budget exhausted")
            await self.compact_for_model(job, state, config, model)
            state["turns"] += 1
            self.store.checkpoint(job, state)
            request = ModelRequest(
                    model=model,
                    messages=state["messages"],
                    tools=[self.tools.spec(name, config.tool_revisions.get(name)) for name in config.tools],
                    response_schema=config.response_schema,
                    max_output_tokens=state.get("output_allowance") or config.max_output_tokens,
                    capability="decision" if config.response_schema else "chat",
                )
            try:
                result = await self.generate(job, request)
            except Exception as exc:
                correction = await self.correct_schema_violation(job, state, config, model, exc)
                if correction == "retry":
                    continue
                if correction is not None:
                    return correction
                if await self.recover_model_turn(job, state, config, model, exc):
                    continue
                raise
            state.pop("output_recoveries", None)
            if not result.tool_calls:
                if any(term.casefold() in result.text.casefold() for term in config.forbidden_output):
                    raise PermissionError("output guard rejected a configured forbidden phrase")
                if self.conversations.steer(job, state) or self.delegation.receive(job, state):
                    continue
                feedback = self.autonomy.completion_feedback(job)
                if feedback:
                    if state.get("completion_corrections", 0) >= 2:
                        raise ValueError(feedback)
                    state["completion_corrections"] = state.get("completion_corrections", 0) + 1
                    state["messages"].extend([{"role": "assistant", "content": result.text},
                                              {"role": "user", "content": feedback}])
                    self.store.checkpoint(job, state)
                    continue
                await self.extensions.dispatch(
                    "agent.end", {"text": result.text, "turns": state["turns"]}, job=job
                )
                return {**result.model_dump(), "turns": state["turns"], "tool_count": state["tool_count"]}
            calls = [c.model_dump() for c in result.tool_calls]
            if len({c["id"] for c in calls}) != len(calls):
                raise ValueError("model returned duplicate tool call ids")
            state["messages"].append({"role": "assistant", "content": result.text, "tool_calls": calls,
                                     **({"provider_state": result.provider_state} if result.provider_state else {})})
            state["pending"], state["pending_index"] = json.loads(encode(calls)), 0
            self.store.checkpoint(job, state)

    async def execute_pending(self, job, state, config, force_approval):
        """Checkpoint each result; read batches overlap, mutations form ordered barriers.

        Paused calls keep their slot and completed siblings. No further model round-trip
        occurs until every call has a matching result, including after a restart.
        """
        pending = state["pending"]

        def parallel(call):
            if force_approval or call["name"] not in config.tools:
                return False
            spec = self.tools.spec(call["name"], config.tool_revisions.get(call["name"]))
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
                self.store.checkpoint(job, state)
            if call.get("argument_error"):
                call["observation"] = tool_failure_observation(
                    ToolInputError(call["argument_error"]), executed=False, code="invalid_tool_arguments")
            else:
                call["observation"] = await self.observed_invoke(
                    job, config, call["name"], call["arguments"], f"turn-{state['turns']}-tool-{index}", force_approval)
            serialized = encode(call["observation"])
            call["observation_hash"] = hashlib.sha256(serialized.encode()).hexdigest()
            if len(serialized) > config.tool_result_chars:
                artifact = self.artifacts.put(f"tool-result-{state['turns']}-{index}.json", serialized,
                                              "application/json", job["run_id"])
                call["observation"] = {"truncated": True, "artifact": artifact,
                    "preview": serialized[:config.tool_result_chars],
                    "hint": "Full result is in the artifact. Read only relevant ranges with attachments.read."}
            self.store.checkpoint(job, state)

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
                    for rank, kind in enumerate((LeaseLost, asyncio.CancelledError, UncertainEffect,
                                                  ApprovalRequired, WaitingInput, WaitingRemote, WaitingChildren)):
                        if isinstance(error, kind):
                            return rank
                    return -1
                raise min(errors, key=priority)
            for index in range(start, end):
                call = pending[index]
                state["messages"].append({"role": "tool", "tool_call_id": call["id"],
                                          "content": encode(call["observation"])})
            state["pending_index"] = end
            self.store.checkpoint(job, state)
        if pending and state.get("checked_turn") != state["turns"]:
            fingerprint = encode([{k: c.get(k) for k in ("name", "arguments", "observation_hash")} for c in pending])
            repeated = state.get("stalled_turns", 0) + 1 if fingerprint == state.get("last_observations") else 0
            state.update(checked_turn=state["turns"], last_observations=fingerprint, stalled_turns=repeated)
            if repeated == 2:
                state["messages"].append({"role": "user", "content":
                    "These identical tool calls returned the same results three times. Stop repeating them. "
                    "Use agents.wait for child completion; otherwise change your approach or explain the blocker."})
            self.store.checkpoint(job, state)
            if repeated >= 3:
                raise ValueError("agent made no progress: identical tool calls and results repeated four times")

    async def observed_invoke(self, job, config, name, arguments, slot, force_approval):
        """Run one model-requested tool call; failures return as observations the model can act on.

        Pause signals (approval, waiting children/remote/input, uncertain writes), lost leases,
        cancellation and explicit run budgets still propagate: they are not the model's to handle.
        """
        if name not in config.tools:
            output = tool_failure_observation(
                PermissionError("tool is not available in this task: " + name), executed=False,
                code="unknown_tool")
            output["error"]["available_tools"] = list(config.tools)[:200]
            with self.store.connect() as db:
                self.store.event(db, job["run_id"], "tool.unknown_requested", {"tool": name})
            return output
        try:
            return await self.tools.invoke(self.store, job, name, arguments, slot, force_approval)
        except ToolInputError as exc:
            output = tool_failure_observation(exc, executed=False, code="invalid_tool_arguments")
            with self.store.connect() as db:
                self.store.event(db, job["run_id"], "tool.input_rejected", {"tool": name, **output})
            return output
        except PAUSE_SIGNALS:
            raise
        except (LeaseLost, asyncio.CancelledError):
            raise
        except Exception as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("run budget exceeded"):
                raise
            output = tool_failure_observation(exc)
            with self.store.connect() as db:
                self.store.event(db, job["run_id"], "tool.failed_observed",
                                 {"tool": name, "code": output["error"]["code"]})
            return output

    async def compact_for_model(self, job, state, config, model, *, overflow=False):
        from .models import HTTPProvider
        binding = self.extensions.provider_binding(model, self.extensions.run_snapshot(job)) or self.models.bindings.get(model)
        limits = await binding.provider.limits.discover(binding.model) if binding and isinstance(binding.provider, HTTPProvider) else {}
        chars = config.context_chars  # Optional legacy caller cap; there is no fixed default model window.
        output = min(config.max_output_tokens, limits.get('max_output_tokens', config.max_output_tokens))
        available = limits.get('max_input_tokens')
        if window := limits.get('context_window'):
            # A large requested allowance must not starve the prompt on a small window.
            output = min(output, max(1, window // 4))
            available = min(available or window, window - output)
        state['output_allowance'] = output
        if available is not None:
            ratio = limits.get('tokens_per_byte', 1)
            overhead = len(encode({'tools': [self.tools.spec(n, config.tool_revisions.get(n)).model_dump()
                                            for n in config.tools], 'schema': config.response_schema}).encode())
            # Convert service tokens to a conservative character bound, accounting for CJK and tool schemas.
            wire = encode(state['messages'])
            bytes_per_char = len(wire.encode()) / max(1, len(wire))
            detected = max(1, int((available * .8 / ratio - overhead) / bytes_per_char))
            chars = min(chars, detected) if chars else detected
        if overflow:
            reduced = max(1, int(len(encode(state['messages'])) * .65))
            chars = min(chars, reduced) if chars else reduced
        if chars is not None:
            await self.compact_context(job, state, chars)

    async def correct_schema_violation(self, job, state, config, model, exc):
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
            state["messages"].append({"role": "user", "content":
                "Your answer did not satisfy the required result format. Correct only the format: keep every "
                "finding and identifier you already established, and return exactly one JSON value, without "
                "prose or code fences. The validation error was: " + detail})
            self.store.checkpoint(job, state)
            with self.store.transaction() as db:
                self.store.event(db, job["run_id"], "agent.schema_correction",
                                 {"step": job["id"], "round": used + 1, "constraint": detail})
            return "retry"
        with self.store.transaction() as db:
            self.store.event(db, job["run_id"], "agent.schema_unmet",
                             {"step": job["id"], "constraint": detail})
        return {**response.model_dump(), "turns": state["turns"], "tool_count": state["tool_count"],
                "schema_valid": False, "schema_errors": [detail],
                "schema_note": "the child's answer did not match the requested result format; text is unverified"}

    async def recover_model_turn(self, job, state, config, model, exc):
        from .model_limits import ContextWindowError
        from .retry_policy import ModelResponseError
        if isinstance(exc, ContextWindowError):
            before = encode(state['messages'])
            await self.compact_for_model(job, state, config, model, overflow=True)
            if encode(state['messages']) == before:
                return False
            kind = 'context_overflow'
        elif isinstance(exc, ModelResponseError) and exc.output_limited:
            count = state.get('output_recoveries', 0)
            if count >= 2:
                return False
            state['output_recoveries'] = count + 1
            state['messages'].append({'role': 'user', 'content':
                'The previous response hit the output limit. None of its tool calls were executed. '
                'Continue from saved tool results. Issue one small complete tool call at a time, '
                'split large writes into smaller parts, and keep prose concise. Never repeat completed writes.'})
            kind = 'output_limit'
        else:
            return False
        self.store.checkpoint(job, state)
        with self.store.transaction() as db:
            self.store.event(db, job['run_id'], 'agent.recovering', {'step': job['id'], 'reason': kind})
        return True

    async def compact_context(self, job, state, limit):
        messages = state["messages"]
        if len(encode(messages)) <= limit:
            return
        pinned = _protected(state, messages)
        if self.backends.binding("context", job):
            prefix = _prefix_end(messages, pinned)
            tail_start = max(prefix, len(messages) - 4)
            while tail_start > prefix and messages[tail_start].get("role") == "tool":
                tail_start -= 1
            cooldown = state.get("compaction_cooldown") or {}
            # A summariser that just failed is not worth retrying this turn: repeating a broken
            # call every turn pays its cost forever. Back off further each time it fails again.
            if tail_start > prefix and _free(messages, prefix, tail_start, pinned) \
                    and time.time() >= cooldown.get("until", 0):
                # Fail open: a broken summariser must never be worse than not installing one, so
                # a failure or an unusable result falls through to plain deletion below.
                try:
                    compressed = await self.backends.call(
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
                    self.store.checkpoint(job, state)
                    with self.store.connect() as db:
                        self.store.event(db, job["run_id"], "context.compaction_failed",
                                         {"step": job["id"], "error": type(exc).__name__,
                                          "retry_after_seconds": delay, "attempt": strikes})
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
            with self.store.transaction() as db:
                self.store.assert_owner(db, job)
                self.store.event(
                    db,
                    job["run_id"],
                    "context.compacted",
                    {"step": job["id"], "removed_messages": original - len(messages)},
                )
            self.store.checkpoint(job, state)

    async def generate(self, job, request):
        context = await self.extensions.dispatch("context.transform", {"messages": request.messages}, job=job)
        request = request.model_copy(update={"messages": context["messages"]})
        transformed = await self.extensions.dispatch("model.before_request", request.model_dump(), job=job)
        request = ModelRequest.model_validate(transformed)
        alias, visited = request.model, set()
        while True:
            if alias in visited:
                raise ValueError("model fallback cycle detected")
            visited.add(alias)
            binding = self.extensions.provider_binding(
                alias, self.extensions.run_snapshot(job)
            ) or self.models.bindings.get(alias)
            if not binding:
                raise ValueError("unregistered model: " + alias)
            from .models import HTTPProvider
            if isinstance(binding.provider, HTTPProvider) and request.capability in ('chat', 'decision'):
                limits = await binding.provider.limits.discover(binding.model)
                if cap := limits.get('max_output_tokens'):
                    request = request.model_copy(update={'max_output_tokens': min(request.max_output_tokens, cap)})
            call_id = uuid.uuid4().hex
            estimated_input = len(encode(request.model_dump()).encode())
            price_known = isinstance(binding.provider, MockProvider) or (
                binding.input_price_per_million is not None and binding.output_price_per_million is not None
            )
            with self.store.transaction() as db:
                self.store.assert_owner(db, job)
                # A request's max_output_tokens is a ceiling, not a minimum. Use the
                # remaining allowance instead of rejecting a still-viable continuation.
                remaining, ancestor = request.max_output_tokens, job['run_id']
                while ancestor:
                    spec = json.loads(db.execute('SELECT spec FROM runs WHERE id=?', (ancestor,)).fetchone()[0])
                    used = db.execute('SELECT output_reserved FROM run_usage WHERE run_id=?', (ancestor,)).fetchone()
                    cap = spec.get('limits', {}).get('output_tokens')
                    if cap is not None:
                        remaining = min(remaining, cap - (used[0] if used else 0))
                    parent = db.execute('SELECT parent_id FROM child_runs WHERE child_id=?', (ancestor,)).fetchone()
                    ancestor = parent[0] if parent else None
                if remaining > 0:
                    request = request.model_copy(update={'max_output_tokens': remaining})
                max_cost = (
                    (binding.input_price_per_million or 0) * estimated_input
                    + (binding.output_price_per_million or 0) * request.max_output_tokens
                ) / 1_000_000
                self.store.reserve(db, job["run_id"], "model_calls")
                self.store.reserve(db, job["run_id"], "output_reserved", request.max_output_tokens)
                if (request.capability in ("image", "embedding") or request.attachments) and not isinstance(
                    binding.provider, MockProvider
                ):
                    price_known = False
                self.store.reserve_cost(db, job["run_id"], max_cost, price_known)
                db.execute(
                    "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?)",
                    (call_id, job["run_id"], job["id"], alias, "started", None, time.time(), None),
                )
                self.store.event(
                    db,
                    job["run_id"],
                    "model.started",
                    {"step": job["id"], "call_id": call_id, "model": alias},
                )
            try:
                effective = request.model_copy(update={"model": alias})
                await self.extensions.dispatch(
                    "message.start", {"call_id": call_id, "role": "assistant"}, job=job
                )
                from .model_streaming import MODEL_OBSERVER

                async def observe(delta):
                    with self.store.transaction() as db:
                        self.store.assert_owner(db, job)
                        self.store.event(
                            db, job["run_id"], "model.delta", {"step": job["id"], "call_id": call_id, **delta}
                        )
                    await self.extensions.dispatch("model.delta", delta, job=job)
                    await self.extensions.dispatch("message.update", {"call_id": call_id, **delta}, job=job)

                streaming = job["spec"]["input"].get("streaming", False) or request.parameters.get(
                    "stream", False
                )
                observer = MODEL_OBSERVER.set(observe if streaming else None)
                wait_settings = MODEL_WAIT.set((attempt_number(job), job.get('retry_state', {})))
                try:
                    result = await self.models.generate(effective, binding=binding)
                finally:
                    MODEL_OBSERVER.reset(observer)
                    MODEL_WAIT.reset(wait_settings)
                patched = await self.extensions.dispatch("model.after_response", result.model_dump(), job=job)
                from .contracts import ModelResult

                result = ModelResult.model_validate(patched)
                await self.extensions.dispatch(
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
                with self.store.transaction() as db:
                    self.store.assert_owner(db, job)
                    db.execute(
                        "UPDATE model_calls SET status='failed',finished=? WHERE id=?", (time.time(), call_id)
                    )
                    self.store.event(
                        db,
                        job["run_id"],
                        "model.failed",
                        {"call_id": call_id, "error_type": type(exc).__name__, "detail": error_info(exc)},
                    )
                permanent = (
                    isinstance(exc, (ValueError, KeyError, PermissionError, SchemaError))
                    or getattr(exc, 'retryable', None) is False
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
            with self.store.transaction() as db:
                self.store.assert_owner(db, job)
                db.execute(
                    "UPDATE model_calls SET status='succeeded',usage=?,finished=? WHERE id=?",
                    (encode(usage), time.time(), call_id),
                )
                self.store.settle(
                    db,
                    job["run_id"],
                    tokens - request.max_output_tokens if tokens is not None else 0,
                    tokens or 0,
                    cost_delta,
                    int(not mock and (tokens is None or not price_known)),
                )
                self.store.event(
                    db, job["run_id"], "model.succeeded", {"call_id": call_id, "model": alias, "usage": usage}
                )
                spec = json.loads(
                    db.execute("SELECT spec FROM runs WHERE id=?", (job["run_id"],)).fetchone()[0]
                )
                accounted = db.execute(
                    "SELECT output_reserved,cost FROM run_usage WHERE run_id=?", (job["run_id"],)
                ).fetchone()
                output_cap = spec.get('limits', {}).get('output_tokens')
                exceeded = output_cap is not None and accounted[0] > output_cap
                cost_cap = spec.get("limits", {}).get("cost_usd")
                exceeded = exceeded or cost_cap is not None and accounted[1] > cost_cap
            if exceeded:
                raise ValueError("provider usage exceeded the reserved run budget; further execution stopped")
            result.usage["model_alias"] = alias
            return result

    async def composite(self, job, spec, arguments):
        items = arguments.get("items", []) if spec.kind == "foreach" else [arguments]
        if not isinstance(items, list) or len(items) > 100:
            raise ValueError("foreach needs an array of at most 100 items")
        if job["state"].get("child_count") is None:
            with self.store.connect() as db:
                parent = job["run_id"]
                depth = 0
                while row := db.execute(
                    "SELECT parent_id FROM child_runs WHERE child_id=?", (parent,)
                ).fetchone():
                    depth += 1
                    parent = row[0]
                if depth >= 8:
                    raise ValueError("subworkflow nesting exceeds 8 levels")
            for index, item in enumerate(items):
                child = Workflow.model_validate(spec.body).model_copy(deep=True)
                child.inputs.update({**{k: v for k, v in arguments.items() if k != "items"},
                                     "item": item, "index": index} if spec.kind == "foreach" else item)
                self.submit(
                    child, f"child:{job['run_id']}:{job['id']}:{index}", (job["run_id"], job["id"], index)
                )
            self.store.checkpoint(job, {"child_count": len(items)})
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT child_id FROM child_runs WHERE parent_id=? AND step_id=? ORDER BY slot",
                    (job["run_id"], job["id"]),
                )
            ]
        children = [self.store.run(identifier) for identifier in ids]
        if any(r["status"] in ("failed", "cancelled") for r in children):
            for child in children:
                self.store.cancel(child["id"])
            raise ValueError("child workflow failed; inspect child run events")
        if any(r["status"] != "succeeded" for r in children):
            raise WaitingChildren()
        return {
            "results": [{s["id"]: s["output"] for s in child["steps"]} for child in children],
            "outputs": [named_outputs(child) for child in children],
            "runs": ids,
        }

    async def plan_execute(self, job, model, config, state, force_approval):
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
                    self.store.checkpoint(job, state)
                output = await self.observed_invoke(
                    job, config, action["tool"], action["arguments"], f"plan-{state['turns']}-{index}", force_approval
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
                self.store.checkpoint(job, state)
            if config.max_turns is not None and state["turns"] >= config.max_turns:
                raise ValueError("planner model budget exhausted")
            state["turns"] += 1
            await self.compact_for_model(job, state, config, model)
            self.store.checkpoint(job, state)
            instruction = {
                "role": "system",
                "content": "Plan actions using only these tool definitions: "
                + encode(
                    [self.tools.spec(n, config.tool_revisions.get(n)).model_dump() for n in config.tools]
                )
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
                result = await self.generate(job, request)
            except Exception as exc:
                correction = await self.correct_schema_violation(job, state, config, model, exc)
                if correction == "retry":
                    continue
                if correction is not None:
                    return correction
                if await self.recover_model_turn(job, state, config, model, exc):
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
            self.store.checkpoint(job, state)
