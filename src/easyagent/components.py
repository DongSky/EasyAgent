"""Portable component contracts and explicit runtime selection (no code execution)."""
from __future__ import annotations

import hashlib
import platform
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from .contracts import Contract
from .store import encode

Platform = Literal['macos', 'windows', 'linux', 'android', 'ios']


class RuntimeRequirements(Contract):
    platforms: list[Platform] = Field(default_factory=lambda: ['macos', 'windows', 'linux', 'android', 'ios'])
    capabilities: list[str] = Field(default_factory=list)
    network_origins: list[str] = Field(default_factory=list)
    credential_refs: list[str] = Field(default_factory=list)
    # Credentials are rebound on a selected executor, never copied with definitions.
    memory_mb: int = Field(default=64, ge=1)


class RuntimeProfile(Contract):
    id: str
    platform: Platform
    capabilities: list[str]
    network_origins: list[str] = Field(default_factory=list)
    credential_refs: list[str] = Field(default_factory=list)
    memory_mb: int = Field(default=512, ge=1)
    location: Literal['local', 'remote'] = 'local'


class ComponentReference(Contract):
    kind: Literal['api', 'node', 'workflow', 'component']
    id: str
    revision: int = Field(ge=0)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class ComponentManifest(Contract):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[a-zA-Z][a-zA-Z0-9_.-]{0,100}$')
    revision: int = Field(ge=1)
    title: str
    description: str
    source: ComponentReference
    dependencies: list[ComponentReference] = Field(default_factory=list)
    input_schema: dict = Field(default_factory=lambda: {'type': 'object'})
    output_schema: dict = Field(default_factory=lambda: {'type': 'object'})
    defaults: dict = Field(default_factory=dict)
    requirements: RuntimeRequirements = Field(default_factory=RuntimeRequirements)
    effect: Literal['read', 'write', 'local'] = 'read'
    docs: list[str] = Field(default_factory=list)
    validation: Literal['protocol_integration', 'live_verified', 'unverified'] = 'unverified'

    @model_validator(mode='after')
    def schemas(self):
        Draft202012Validator.check_schema(self.input_schema)
        Draft202012Validator.check_schema(self.output_schema)
        return self

    @property
    def component_type(self):
        # Derive from the executable source; a label cannot turn a graph into a node.
        return 'subworkflow' if self.source.kind == 'workflow' else 'node'


class CodePackage(Contract):
    """Candidate metadata only. Validating this contract does not authorize execution."""
    id: str
    revision: int = Field(ge=1)
    language: Literal['python', 'javascript', 'rust', 'wasm']
    entrypoint: str
    source_digests: dict[str, str]
    dependency_lock_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    requirements: RuntimeRequirements
    integration_scenarios: list[str] = Field(min_length=1)
    state: Literal['draft', 'built', 'tested', 'published'] = 'draft'

    @model_validator(mode='after')
    def paths_and_digests(self):
        import re
        from pathlib import PurePosixPath
        if not self.source_digests or self.entrypoint not in self.source_digests:
            raise ValueError('entrypoint must be an included source file')
        for name, value in self.source_digests.items():
            p = PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or str(p) != name:
                raise ValueError('source paths must be portable package-relative paths')
            if not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('source digests must be SHA-256')
        return self


def digest(value):
    # JSON.parse/stringify (JS) serializes 90.0 as 90. Hash their common numeric value.
    # Non-integral floats round-trip to the same Python float before normalization.
    def normalize(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: normalize(v) for key, v in item.items()}
        if isinstance(item, list):
            return [normalize(v) for v in item]
        return item
    return hashlib.sha256(encode(normalize(value)).encode()).hexdigest()


def matches_digest(value, expected):
    # Read compatibility for local pre-release definitions created before numeric normalization.
    return expected in (digest(value), hashlib.sha256(encode(value).encode()).hexdigest())


def local_platform():
    return {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}[platform.system()]


def resolve_runtime(requirements, profiles, *, allow_remote=False):
    required = RuntimeRequirements.model_validate(requirements)
    candidates = []
    for value in profiles:
        profile = RuntimeProfile.model_validate(value)
        reasons = []
        if profile.platform not in required.platforms:
            reasons.append('platform is not supported by this component')
        for field in ('capabilities', 'network_origins', 'credential_refs'):
            missing = set(getattr(required, field)) - set(getattr(profile, field))
            if missing:
                reasons.append(field + ': ' + ', '.join(sorted(missing)))
        if profile.memory_mb < required.memory_mb:
            reasons.append('insufficient memory')
        if profile.location == 'remote' and not allow_remote:
            reasons.append('remote execution has not been selected')
        candidates.append({'id': profile.id, 'location': profile.location, 'compatible': not reasons, 'reasons': reasons})
    candidates.sort(key=lambda p: p['location'] != 'local')
    return {'selected': next((p['id'] for p in candidates if p['compatible']), None), 'candidates': candidates,
            'execution_started': False}
