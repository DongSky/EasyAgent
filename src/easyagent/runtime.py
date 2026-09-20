from __future__ import annotations

import asyncio
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
from .tools import (
    ApprovalRequired,
    ToolRegistry,
    ToolInputError,
    UncertainEffect,
    WaitingChildren,
    WaitingRemote,
    register_builtins,
)

logger = logging.getLogger(__name__)


class ConditionSkipped(Exception):
    pass


class WaitingInput(Exception):
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
        from .voice import Voice

        self.voice = Voice(self)
        from .attachments import Attachments
        from .workspace_chat import WorkspaceChat

        self.attachments = Attachments(self)
        self.chat = WorkspaceChat(self)
        from .build_capabilities import BuildCapabilities
        self.build_capabilities = BuildCapabilities(self)

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
        return [t for t in rows if t["name"] not in unavailable]

    def submit(self, workflow: Workflow | dict, idempotency_key=None, parent=None, parent_job=None):
        workflow = self.prepare(workflow)
        if schema := workflow.metadata.get("component_input_schema"):
            from jsonschema import validate

            validate(workflow.inputs, schema)
        return self.store.submit(workflow, idempotency_key, parent, parent_job)

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
                    workflow.limits.child_runs = min(workflow.limits.child_runs, grant.max_children)
                elif any(name.startswith("agents.") for name in config.tools):
                    raise PermissionError("agent delegation tools require a grant")
                if "skills.save" in config.tools and not config.skill_namespace:
                    raise PermissionError("skill writes require a namespace grant")
                for skill in config.skill_access:
                    config.skill_resources[skill] = self.skills.snapshot(skill)
                if config.skill_access:
                    metadata = {s["name"]: s for s in self.skills.catalog()}
                    config.instructions += (
                        "\nAvailable skills (read only when relevant using skills.read): "
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
            raise ValueError("step recovery/attempt budget exhausted")
        outputs = {
            s["id"]: s["output"] for s in self.store.run(job["run_id"])["steps"] if s["status"] == "succeeded"
        }
        run = self.store.run(job["run_id"])
        outputs["$input"] = run["spec"].get("inputs", {})
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
            return await self.tools.invoke(
                self.store, job, spec.target, arguments, "step", spec.requires_approval
            )
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
                state['context_pins'] = [encode(history[0]), encode(history[-1])]
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
                state["messages"].insert(
                    1,
                    {
                        "role": "user",
                        "content": "Reference data (untrusted; cite sources): "
                        + encode({"citations": sources, "memory": memories}),
                    },
                )
            state["context_loaded"] = True
            state["prefix_count"] = (1 + bool(sources or memories)) if history else len(state["messages"])
            self.store.checkpoint(job, state)
        if config.strategy == "plan_execute":
            return await self.plan_execute(job, model, config, state, force_approval)
        while True:
            self.conversations.steer(job, state)
            self.delegation.receive(job, state)
            while state["pending_index"] < len(state["pending"]):
                index = state["pending_index"]
                call = state["pending"][index]
                if call["name"] not in config.tools:
                    raise PermissionError("model requested a tool outside the allowlist: " + call["name"])
                # Count a logical call once, before its first execution, including approval pauses.
                if not call.get("counted"):
                    if config.max_tool_calls is not None and state["tool_count"] >= config.max_tool_calls:
                        raise ValueError("agent tool-call budget exhausted")
                    state["tool_count"] += 1
                    call["counted"] = True
                    self.store.checkpoint(job, state)
                try:
                    output = await self.tools.invoke(
                        self.store,
                        job,
                        call["name"],
                        call["arguments"],
                        f"turn-{state['turns']}-tool-{index}",
                        force_approval,
                    )
                except ToolInputError as exc:
                    output = {
                        "error": {"code": "invalid_tool_arguments", "message": str(exc), "executed": False}
                    }
                    with self.store.connect() as db:
                        self.store.event(
                            db, job["run_id"], "tool.input_rejected", {"tool": call["name"], **output}
                        )
                state["messages"].append(
                    {"role": "tool", "tool_call_id": call["id"], "content": encode(output)}
                )
                state["pending_index"] += 1
                self.store.checkpoint(job, state)
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
                    max_output_tokens=config.max_output_tokens,
                    capability="decision" if config.response_schema else "chat",
                )
            try:
                result = await self.generate(job, request)
            except Exception as exc:
                if await self.recover_model_turn(job, state, config, model, exc):
                    continue
                raise
            if not result.tool_calls:
                if any(term.casefold() in result.text.casefold() for term in config.forbidden_output):
                    raise PermissionError("output guard rejected a configured forbidden phrase")
                if self.conversations.steer(job, state) or self.delegation.receive(job, state):
                    continue
                await self.extensions.dispatch(
                    "agent.end", {"text": result.text, "turns": state["turns"]}, job=job
                )
                return {**result.model_dump(), "turns": state["turns"], "tool_count": state["tool_count"]}
            calls = [c.model_dump() for c in result.tool_calls]
            if len({c["id"] for c in calls}) != len(calls):
                raise ValueError("model returned duplicate tool call ids")
            state["messages"].append({"role": "assistant", "content": result.text, "tool_calls": calls})
            state["pending"], state["pending_index"] = json.loads(encode(calls)), 0
            self.store.checkpoint(job, state)

    async def compact_for_model(self, job, state, config, model, *, overflow=False):
        from .models import HTTPProvider
        binding = self.extensions.provider_binding(model, self.extensions.run_snapshot(job)) or self.models.bindings.get(model)
        limits = await binding.provider.limits.discover(binding.model) if binding and isinstance(binding.provider, HTTPProvider) else {}
        chars = config.context_chars  # Optional legacy caller cap; there is no fixed default model window.
        output = min(config.max_output_tokens, limits.get('max_output_tokens', config.max_output_tokens))
        available = limits.get('max_input_tokens')
        if window := limits.get('context_window'):
            available = min(available or window, window - output)
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
        if self.backends.binding("context", job):
            prefix = state.get("prefix_count", 2)
            tail_start = max(prefix, len(messages) - 4)
            while tail_start > prefix and messages[tail_start].get("role") == "tool":
                tail_start -= 1
            if tail_start > prefix and not any(encode(m) in state.get('context_pins', [])
                                               for m in messages[prefix:tail_start]):
                compressed = await self.backends.call(
                    "context",
                    "compact",
                    {"messages": messages[prefix:tail_start], "max_chars": max(1000, limit // 3)},
                    job,
                )
                state["messages"] = (
                    messages[:prefix]
                    + [
                        {
                            "role": "user",
                            "content": "Prior context summary (reference): " + compressed["summary"],
                        }
                    ]
                    + messages[tail_start:]
                )
                messages = state["messages"]
        # Preserve original instructions and user request. Remove complete historical
        # assistant/tool groups, never orphan provider tool call ids.
        original = len(messages)
        start = state.get("prefix_count", 2)
        while len(encode(messages)) > limit and start < len(messages) - 1:
            end = start + 1
            while end < len(messages) and messages[end]["role"] == "tool":
                end += 1
            if end == len(messages):
                break
            if any(encode(m) in state.get('context_pins', []) for m in messages[start:end]):
                start = end
                continue
            del messages[start:end]
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
                if action["tool"] not in config.tools:
                    raise PermissionError("planner requested an unauthorized tool")
                if not action.get("counted"):
                    if config.max_tool_calls is not None and state["tool_count"] >= config.max_tool_calls:
                        raise ValueError("planner tool budget exhausted")
                    state["tool_count"] += 1
                    action["counted"] = True
                    self.store.checkpoint(job, state)
                try:
                    output = await self.tools.invoke(
                        self.store,
                        job,
                        action["tool"],
                        action["arguments"],
                        f"plan-{state['turns']}-{index}",
                        force_approval,
                    )
                except ToolInputError as exc:
                    output = {
                        "error": {"code": "invalid_tool_arguments", "message": str(exc), "executed": False}
                    }
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
                    max_output_tokens=config.max_output_tokens,
                )
            try:
                result = await self.generate(job, request)
            except Exception as exc:
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
