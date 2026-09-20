"""Public, language-neutral extension contracts. All payloads are strict JSON."""

from __future__ import annotations

from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator, model_serializer

from .contracts import Contract, ToolSpec
from .backends import BackendContribution, OPERATIONS

EVENTS = (
    "workflow.before_step",
    "workflow.after_step",
    "agent.start",
    "agent.end",
    "turn.start",
    "turn.end",
    "context.transform",
    "model.before_request",
    "model.after_response",
    "model.delta",
    "tool.before_call",
    "tool.after_result",
    "tool.error",
    "session.start",
    "session.info_changed",
    "model.select",
    "session.input",
    "session.before_fork",
    "session.fork",
    "session.before_compact",
    "session.compact",
    "session.shutdown",
    "resources.discover",
    "message.start",
    "message.update",
    "message.end",
)


class ExtensionHook(Contract):
    event: str
    handler: str
    priority: int = Field(default=0, ge=-100, le=100)
    required: bool = True

    @model_validator(mode="after")
    def known(self):
        if self.event not in EVENTS and not self.event.startswith("custom."):
            raise ValueError("unsupported extension event: " + self.event)
        return self


class ExtensionAction(Contract):
    spec: ToolSpec
    handler: str
    title: str = ""
    shortcut: str | None = None


class ExtensionProvider(Contract):
    stream_handler: str | None = None
    cancel_handler: str | None = None
    alias: str
    model: str
    capabilities: list[str] = Field(min_length=1)
    handler: str

    @model_serializer(mode="wrap")
    def preserve_v1_fields(self, handler):
        result = handler(self)
        for name in ("stream_handler", "cancel_handler"):
            if name not in self.model_fields_set and getattr(self, name) is None:
                result.pop(name, None)
        return result


class ExtensionView(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str
    placement: Literal["panel", "sidebar", "editor", "message", "status"] = "panel"
    command: str | None = None
    entrypoint: str | None = None
    description: str = ""


class ExtensionManifest(Contract):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = ""
    runtime: Literal["javascript", "wasm", "python", "node", "rust"] = "javascript"
    entrypoint: str = "extension.js"
    transport: Literal["oneshot", "ndjson"] = "oneshot"
    migrations: dict[int, str] = Field(default_factory=dict)
    dependencies: dict[str, int] = Field(default_factory=dict)
    required_services: list[str] = Field(default_factory=list)
    service_versions: dict[str, int] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    env_allow: list[str] = Field(default_factory=list)
    platforms: list[Literal["macos", "windows", "linux", "android", "ios"]] = Field(
        default_factory=lambda: ["macos", "windows", "linux"]
    )
    timeout_seconds: float = Field(default=10, gt=0, le=120)
    memory_mb: int = Field(default=32, ge=8, le=256)
    tools: list[ExtensionAction] = Field(default_factory=list)
    services: list[ExtensionAction] = Field(default_factory=list)
    commands: list[ExtensionAction] = Field(default_factory=list)
    providers: list[ExtensionProvider] = Field(default_factory=list)
    backends: list[BackendContribution] = Field(default_factory=list)
    hooks: list[ExtensionHook] = Field(default_factory=list)
    views: list[ExtensionView] = Field(default_factory=list)
    skills: dict[str, str] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)
    flags_schema: dict = Field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    flags: dict = Field(default_factory=dict)
    settings_schema: dict = Field(default_factory=lambda: {"type": "object", "additionalProperties": False})
    settings: dict = Field(default_factory=dict)
    state_schema: dict = Field(default_factory=lambda: {"type": "object"})
    initial_state: dict = Field(default_factory=dict)
    lock: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def preserve_v1_fields(self, handler):
        # Optional additions must not change the signed bytes of existing v1 packages.
        result = handler(self)
        for name in ("backends", "service_versions"):
            if name not in self.model_fields_set and not getattr(self, name):
                result.pop(name, None)
        return result

    @model_validator(mode="after")
    def valid(self):
        from jsonschema import validate

        for schema in (self.settings_schema, self.state_schema, self.flags_schema):
            Draft202012Validator.check_schema(schema)
        validate(self.flags, self.flags_schema)
        validate(self.settings, self.settings_schema)
        validate(self.initial_state, self.state_schema)
        if len({b.kind for b in self.backends}) != len(self.backends):
            raise ValueError("duplicate backend kind")
        for backend in self.backends:
            if set(backend.operations) - set(OPERATIONS[backend.kind]):
                raise ValueError("unsupported backend operations")
        names = [a.spec.name for a in self.tools + self.services + self.commands]
        names += [p.alias for p in self.providers]
        if len(names) != len(set(names)) or any(not n.startswith(self.id + ".") for n in names):
            raise ValueError("contributions must be unique and use the extension ID namespace")
        if self.id in self.dependencies:
            raise ValueError("an extension cannot depend on itself")
        if self.runtime not in ("javascript", "wasm") and "trusted_process" not in self.permissions:
            raise ValueError("Python/Node/Rust require explicit trusted_process permission")
        if self.runtime in ("javascript", "wasm") and self.env_allow:
            raise ValueError("isolated JavaScript has no environment access")
        if len({v.id for v in self.views}) != len(self.views):
            raise ValueError("duplicate view ID")
        for view in self.views:
            if view.command and view.command not in [a.spec.name for a in self.commands]:
                raise ValueError("view command must belong to this extension")
        return self


class ExtensionPackage(Contract):
    format: Literal["easyagent.extension.v1"] = "easyagent.extension.v1"
    manifest: ExtensionManifest
    files: dict[str, str]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    publisher: str | None = None
    signature: str | None = None


class ExtensionInstall(Contract):
    package: ExtensionPackage
    grants: list[str] = Field(default_factory=list)
    trust_digest: str | None = None
    activate: bool = True


class ExtensionCall(Contract):
    conversation_id: str | None = None
    input: dict = Field(default_factory=dict)


class ExtensionStateWrite(Contract):
    revision: int = Field(ge=0)
    value: dict


class ExtensionActivation(Contract):
    revision: int = Field(ge=1)


class ExtensionSettings(Contract):
    value: dict


class PublisherTrust(Contract):
    public_key: str


class ExtensionRequest(Contract):
    protocol_version: Literal[1] = 1
    id: str
    method: str
    params: dict
    context: dict


class ExtensionServiceCall(Contract):
    name: str
    input: dict


class ExtensionError(Contract):
    code: str = Field(min_length=1, max_length=80)
    message: str = Field(max_length=1000)
    retryable: bool = False


class ExtensionResponse(Contract):
    result: object = None
    state: dict | None = None
    calls: list[ExtensionServiceCall] | None = Field(default=None, max_length=16)
    continuation: str | None = Field(default=None, alias="continue")
    error: ExtensionError | None = None
