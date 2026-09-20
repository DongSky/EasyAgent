"""Dependency-complete, unsigned component packages. Import never executes a component."""
from __future__ import annotations

import json
import re
import time
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field

from .components import ComponentManifest, digest, matches_digest
from .contracts import Contract, Workflow
from .http_tools import HTTPTool
from .nodes import NodeDefinition
from .store import Conflict, encode


class PackageDefinition(Contract):
    kind: Literal['api', 'node', 'workflow', 'component']
    id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$')
    revision: int = Field(ge=0)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    body: dict


class ComponentPackage(Contract):
    format: Literal['easyagent.component.v1'] = 'easyagent.component.v1'
    root: str
    root_revision: int = Field(ge=1)
    runtime_contract: Literal['easyagent.workflow.v1'] = 'easyagent.workflow.v1'
    definitions: list[PackageDefinition] = Field(min_length=1, max_length=500)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class ImportComponentPackage(Contract):
    package: ComponentPackage
    # Local environment variable aliases, never values; kept outside immutable definitions.
    credential_bindings: dict[str, str] = Field(default_factory=dict)


def export_package(hub, identifier, revision=None):
    root = hub.library.get(identifier, revision)
    hub.library.check(root)
    if any(hub.library.reference(r.kind, r.id, r.revision)[0].digest != r.digest for r in [root.source, *root.dependencies]):
        raise ValueError('legacy component metadata: export the latest component revision for cross-language sharing')
    refs = {(r.kind, r.id, r.revision) for r in [root.source, *root.dependencies]}
    definitions = []
    for kind, name, number in sorted(refs):
        ref, body = hub.library.reference(kind, name, number)
        definitions.append(PackageDefinition(kind=kind, id=name, revision=number, digest=ref.digest, body=body))
    # Include library identities for nested components as well as their executable definitions.
    latest = {r['id']: r for r in hub.development.list_versions('component')}
    latest[root.id] = {'definition': root.model_dump()}
    for row in latest.values():
        manifest = ComponentManifest.model_validate(row['definition'])
        source = manifest.source
        if manifest.id == root.id and manifest.revision != root.revision:
            continue
        if (source.kind, source.id, source.revision) in refs and all((r.kind, r.id, r.revision) in refs for r in manifest.dependencies):
            definitions.append(PackageDefinition(kind='component', id=manifest.id, revision=manifest.revision,
                                                  body=manifest.model_dump(), digest=digest(manifest.model_dump())))
    body = {'format':'easyagent.component.v1', 'root': root.id, 'root_revision': root.revision,
            'runtime_contract':'easyagent.workflow.v1', 'definitions':[r.model_dump() for r in definitions]}
    package = ComponentPackage(**body, digest=digest(body))
    validate_package(hub, ImportComponentPackage(package=package))
    return package


def validate_package(hub, request):
    request = ImportComponentPackage.model_validate(request)
    package = request.package
    if not matches_digest(package.model_dump(exclude={'digest'}), package.digest):
        raise ValueError('component package digest mismatch')
    indexed, manifests = {}, []
    for row in package.definitions:
        key = (row.kind, row.id, row.revision)
        if key in indexed or not matches_digest(row.body, row.digest):
            raise ValueError('duplicate definition or definition digest mismatch')
        indexed[key] = row.body
        if row.kind == 'api':
            api = HTTPTool.model_validate(row.body)
            if api.name != row.id or api.api_key:
                raise ValueError('API identity mismatch or inline credential in package')
            if any(re.search(r'authorization|api[-_]?key|secret|token|cookie', name, re.I) for name in api.headers):
                raise ValueError('move sensitive static headers to credential references before sharing')
            if any(re.search(r'authorization|api[-_]?key|secret|token|password', name, re.I)
                   for name, _ in parse_qsl(urlsplit(api.url).query)):
                raise ValueError('move sensitive URL query fields to credential references before sharing')
        elif row.kind == 'workflow':
            Workflow.model_validate(row.body)
        elif row.kind == 'node':
            NodeDefinition.model_validate(row.body)
        else:
            manifest = ComponentManifest.model_validate(row.body)
            if manifest.id != row.id or manifest.revision != row.revision:
                raise ValueError('manifest identity mismatch')
            manifests.append(manifest)
    if ('component', package.root, package.root_revision) not in indexed:
        raise ValueError('package root component is missing')
    requirements = set()
    for manifest in manifests:
        for ref in [manifest.source, *manifest.dependencies]:
            if (ref.kind, ref.id, ref.revision) not in indexed or not matches_digest(indexed[ref.kind, ref.id, ref.revision], ref.digest):
                raise ValueError('package is missing a pinned dependency: ' + ref.id)
        ref = manifest.source
        _, _, dependencies, actual, effect = hub.library.inspect(ref.kind, ref.id, ref.revision, indexed)
        if {(r.kind, r.id, r.revision) for r in dependencies} != {(r.kind, r.id, r.revision) for r in manifest.dependencies}:
            raise ValueError('manifest does not describe its full dependency graph')
        if effect != manifest.effect or actual != manifest.requirements:
            raise ValueError('manifest permissions do not match its executable dependencies')
        requirements.update(actual.credential_refs)
    # Bundles cannot smuggle unused APIs or workflows outside the declared dependency closure.
    referenced = {('component', m.id, m.revision) for m in manifests}
    referenced.update((r.kind, r.id, r.revision) for m in manifests for r in [m.source, *m.dependencies])
    if set(indexed) != referenced:
        raise ValueError('package contains undeclared definitions')
    if set(request.credential_bindings) - requirements:
        raise ValueError('credential binding does not belong to this package')
    for value in request.credential_bindings.values():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
            raise ValueError('credential bindings must name environment variables, not secret values')
    for (kind, name, revision), body in indexed.items():
        try:
            existing = hub.development.get(kind, name, revision)
        except KeyError:
            if kind == 'workflow':
                try:
                    legacy = hub.development.get(kind, name)
                    if legacy['revision'] == 0:
                        raise Conflict('a legacy workflow already uses this id: ' + name)
                except KeyError:
                    pass
            continue
        if kind == 'api' and (ref := body.get('api_key_env')) in request.credential_bindings:
            with hub.store.connect() as db:
                binding = db.execute("SELECT value FROM memory WHERE namespace='credential-bindings' AND key=?", (f'{name}@{revision}:{ref}',)).fetchone()
            current_binding = json.loads(binding[0]) if binding else ref
            if current_binding != request.credential_bindings[ref]:
                raise Conflict('import cannot rebind an already installed API version')
        if digest(existing['workflow' if kind == 'workflow' else 'definition']) != digest(body):
            raise Conflict('installed version has different content: '+name+'@'+str(revision))
    return {'components': [m.id for m in manifests], 'definition_count':len(indexed),
            'credential_refs': sorted(requirements), 'publisher_authenticated':False, 'execution_started':False}, indexed


def import_package(hub, request):
    request = ImportComponentPackage.model_validate(request)
    preview, indexed = validate_package(hub, request)
    with hub.store.transaction() as db:
        for (kind, name, revision), body in indexed.items():
            existing = db.execute('SELECT body FROM definition_versions WHERE kind=? AND id=? AND revision=?', (kind,name,revision)).fetchone()
            if existing:
                if digest(json.loads(existing[0])) != digest(body):
                    raise Conflict('installed version changed during import')
                continue
            db.execute('INSERT INTO definition_versions VALUES(?,?,?,?,NULL,NULL,?)', (kind,name,revision,encode(body),time.time()))
        for kind, name, _ in indexed:
            if kind != 'workflow':
                continue
            latest = db.execute("SELECT body FROM definition_versions WHERE kind='workflow' AND id=? ORDER BY revision DESC LIMIT 1", (name,)).fetchone()[0]
            db.execute("INSERT OR REPLACE INTO memory VALUES('studio-workflows',?,?,?,?)", (name,latest,'component-package',time.time()))
        for (kind, name, revision), body in indexed.items():
            ref = body.get('api_key_env')
            if kind == 'api' and ref in request.credential_bindings:
                key = f'{name}@{revision}:{ref}'
                db.execute("INSERT OR REPLACE INTO memory VALUES('credential-bindings',?,?,?,?)", (key,encode(request.credential_bindings[ref]),'local-import-binding',time.time()))
        for name in preview['components']:
            db.execute("INSERT OR REPLACE INTO memory VALUES('component-imports',?,?,?,?)", (name,encode({'digest':request.package.digest,'unsigned':True}),'component-package',time.time()))
    for kind, name, revision in indexed:
        if kind == 'api':
            hub.development.refresh_api(name, revision)
    return {**preview, 'root':request.package.root, 'revision':request.package.root_revision, 'imported':True}


def install_component_packages(app, hub):
    from fastapi import Response
    @app.get('/v1/library/{identifier}/package')
    async def export(identifier: str, revision: int | None = None):
        package = export_package(hub, identifier, revision)
        return Response(package.model_dump_json(indent=2), media_type='application/json',
                        headers={'Content-Disposition':f'attachment; filename="{identifier}.eah-component.json"'})

    @app.post('/v1/library/packages/preview')
    async def preview(body: ImportComponentPackage):
        return validate_package(hub, body)[0]

    @app.post('/v1/library/packages/import', status_code=201)
    async def imported(body: ImportComponentPackage):
        return import_package(hub, body)
