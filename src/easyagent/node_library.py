"""Versioned, inspectable node library backed by ordinary HTTP/workflow definitions."""
from __future__ import annotations

from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field

from .components import (ComponentManifest, ComponentReference, RuntimeProfile, RuntimeRequirements,
                         digest, local_platform, matches_digest, resolve_runtime)
from .contracts import Contract, Step, Workflow
from .http_tools import HTTPTool
from .node_recipes import obj, recipes
from .nodes import NodeDefinition
from .store import Conflict


class PublishComponent(Contract):
    id: str
    kind: Literal['api', 'node', 'workflow'] = 'workflow'
    source_id: str
    source_revision: int = Field(ge=0)
    title: str
    description: str
    input_schema: dict = Field(default_factory=lambda: {'type': 'object'})
    defaults: dict = Field(default_factory=dict)
    expected_revision: int = Field(default=0, ge=0)


class InstallRecipe(Contract):
    api_key: str = ''
    api_key_env: str | None = None
    endpoint: str | None = None


class SaveNode(Contract):
    id: str
    definition: NodeDefinition
    expected_revision: int = Field(default=0, ge=0)


class InstantiateComponent(Contract):
    revision: int | None = Field(default=None, ge=1)
    step_id: str = 'component'
    input: dict | None = None


class ResolveComponent(Contract):
    revision: int | None = None
    profiles: list[RuntimeProfile]
    allow_remote: bool = False


class NodeLibrary:
    def __init__(self, hub):
        self.hub = hub
        self.recipes = {r['id']: r for r in recipes()}
        # Append canonical metadata versions for local pre-release manifests. Executable
        # API/workflow versions and existing consumers stay untouched.
        latest = {row['id']: row for row in hub.development.list_versions('component')}
        for row in latest.values():
            manifest = ComponentManifest.model_validate(row['definition'])
            refs = []
            for reference in [manifest.source, *manifest.dependencies]:
                current, body = self.reference(reference.kind, reference.id, reference.revision)
                if not matches_digest(body, reference.digest):
                    raise Conflict('component dependency digest mismatch: ' + reference.id)
                refs.append(current)
            if refs != [manifest.source, *manifest.dependencies]:
                updated = manifest.model_copy(update={'revision': manifest.revision + 1, 'source': refs[0], 'dependencies': refs[1:]})
                hub.development.put('component', manifest.id, updated.model_dump(), manifest.revision)
        if 'library.json.receipt' in latest:
            self.install_local()

    def reference(self, kind, identifier, revision):
        if kind not in ('api', 'node', 'workflow'):
            raise ValueError('library source must be a versioned API, node or workflow')
        row = self.hub.development.get(kind, identifier, revision)
        body = row['workflow' if kind == 'workflow' else 'definition']
        return ComponentReference(kind=kind, id=identifier, revision=row['revision'], digest=digest(body)), body

    def inspect(self, kind, identifier, revision, definitions=None):
        dependencies, capabilities, origins, credentials, effects = {}, set(), set(), set(), set()
        def visit(k, i, r, depth=0):
            if depth > 8:
                raise ValueError('component nesting exceeds 8 levels')
            if definitions is not None and (k, i, r) in definitions:
                body = definitions[k, i, r]
                ref = ComponentReference(kind=k, id=i, revision=r, digest=digest(body))
            else:
                ref, body = self.reference(k, i, r)
            dependencies[(k, i, ref.revision)] = ref
            if k == 'api':
                definition = HTTPTool.model_validate(body)
                capabilities.add('http.v1')
                origin = urlsplit(definition.url)
                origins.add(f'{origin.scheme}://{origin.netloc}')
                if definition.api_key_env:
                    credentials.add(definition.api_key_env)
                if definition.response_mode in ('artifact', 'media'):
                    capabilities.add('artifacts.v1')
                if definition.polling:
                    capabilities.add('http.poll.v1')
                effects.add(definition.effect or ('read' if definition.method in ('GET', 'HEAD', 'OPTIONS') else 'write'))
            elif k == 'node':
                definition = NodeDefinition.model_validate(body)
                walk({'steps': [definition.step.model_dump()]}, depth)
            else:
                capabilities.add('workflow.v1')
                walk(Workflow.model_validate(body).model_dump(), depth)
            return body

        def walk(workflow, depth):
            for step in workflow['steps']:
                if step['kind'] == 'tool':
                    name, revision = step['target'], step.get('tool_revision')
                    if revision:
                        visit('api', name, revision, depth + 1)
                    else:
                        # Process/MCP/custom tools need their registered host, not just HTTP support.
                        capabilities.add('tool:' + name)
                        effects.add(self.hub.tools.spec(name).effect)
                elif step['kind'] in ('model', 'agent'):
                    raise ValueError('publish deterministic API/composite components first; model/agent bindings are not immutable packages yet')
                elif step['kind'] == 'artifact':
                    capabilities.add('artifacts.v1')
                    effects.add('local')
                if step.get('compensate'):
                    raise ValueError('compensation packaging requires explicit versioned dependencies')
                if step.get('workflow_ref'):
                    child = step['workflow_ref']
                    visit('workflow', child['id'], child['revision'], depth + 1)
                elif step.get('body'):
                    walk(step['body'], depth + 1)

        body = visit(kind, identifier, revision)
        source = dependencies.pop((kind, identifier, revision))
        requirements = RuntimeRequirements(capabilities=sorted(capabilities), network_origins=sorted(origins),
                                           credential_refs=sorted(credentials))
        effect = 'write' if 'write' in effects else 'local' if 'local' in effects else 'read'
        return source, body, list(dependencies.values()), requirements, effect

    def publish(self, request, *, docs=None, validation='unverified'):
        request = PublishComponent.model_validate(request)
        source, body, dependencies, requirements, effect = self.inspect(request.kind, request.source_id, request.source_revision)
        manifest = ComponentManifest(id=request.id, revision=request.expected_revision + 1, title=request.title,
            description=request.description, source=source, dependencies=dependencies, requirements=requirements,
            input_schema=body['input_schema'] if request.kind in ('api', 'node') else request.input_schema,
            output_schema=body.get('output_schema', {'type': 'object'}), defaults=request.defaults or body.get('defaults', {}),
            effect=effect, docs=docs or [], validation=validation)
        self.hub.development.put('component', request.id, manifest.model_dump(), request.expected_revision)
        return manifest

    def get(self, identifier, revision=None):
        return ComponentManifest.model_validate(self.hub.development.get('component', identifier, revision)['definition'])

    def save_node(self, request):
        request = SaveNode.model_validate(request)
        definition = request.definition.model_copy(deep=True)
        if definition.step.kind == 'tool':
            step = definition.step
            self.hub.tools.spec(step.target, step.tool_revision)
            if step.tool_revision is None:
                step.tool_revision = self.hub.tools.revision(step.target)
        return self.hub.development.put('node', request.id, definition.model_dump(), request.expected_revision)

    def profile(self, manifest):
        credential_checks = {}
        for ref in [manifest.source, *manifest.dependencies]:
            if ref.kind == 'api':
                _, body = self.reference(ref.kind, ref.id, ref.revision)
                if key := body.get('api_key_env'):
                    credential_checks.setdefault(key, []).append(bool(self.hub.development.credential(key, ref.id, ref.revision)))
        available = [ref for ref, checks in credential_checks.items() if all(checks)]
        return RuntimeProfile(id='this-hub', platform=local_platform(), location='local',
            capabilities=['workflow.v1', 'http.v1', 'http.poll.v1', 'artifacts.v1'] + ['tool:'+n for n in self.hub.tools.entries],
            # These origins were explicitly installed through operator management endpoints.
            network_origins=manifest.requirements.network_origins, credential_refs=available)

    def check(self, manifest):
        for ref in [manifest.source, *manifest.dependencies]:
            _, body = self.reference(ref.kind, ref.id, ref.revision)
            if not matches_digest(body, ref.digest):
                raise Conflict('component dependency digest mismatch: ' + ref.id)
        return resolve_runtime(manifest.requirements, [self.profile(manifest)])

    def catalog(self):
        installed = {}
        hidden = self.hub.development.archived_ids('component')
        for row in self.hub.development.list_versions('component', include_archived=False):
            manifest = ComponentManifest.model_validate(row['definition'])
            resolution = self.check(manifest)
            installed[manifest.id] = {'id': manifest.id, 'title': manifest.title, 'description': manifest.description,
                'component_type': manifest.component_type,
                'category': self.recipes.get(manifest.id, {}).get('category', '可复用子工作流' if manifest.component_type == 'subworkflow' else '节点'),
                'installed': True, 'available': bool(resolution['selected']), 'manifest': manifest.model_dump(),
                'credential_ref': self.recipes.get(manifest.id, {}).get('definition', {}).get('api_key_env'),
                'resolution': resolution, 'docs': manifest.docs,
                'imported': bool(self.hub.store.memory_search('component-imports', manifest.id, limit=1))}
        for identifier, recipe in self.recipes.items():
            if identifier not in installed and identifier not in hidden:
                definition = recipe['definition']
                installed[identifier] = {'id': identifier, 'title': recipe['title'], 'category': recipe['category'],
                    'component_type': 'node',
                    'description': definition['description'], 'installed': False, 'available': False,
                    'credential_ref': definition['api_key_env'], 'docs': recipe['docs'],
                    'definition': definition, 'defaults': recipe['defaults']}
        if 'library.json.receipt' not in installed and 'library.json.receipt' not in hidden:
            installed['library.json.receipt'] = {'id': 'library.json.receipt', 'title': '保存 JSON 回执',
                'component_type': 'node',
                'description': '把任意结构化结果保存为带 SHA-256 的可下载文件。', 'category': '结果与回执',
                'installed': False, 'available': True, 'builtin': True, 'docs': []}
        return list(installed.values())

    def install(self, identifier, options=None):
        recipe = self.recipes[identifier]
        options = InstallRecipe.model_validate(options or {})
        try:
            existing = self.get(identifier)
        except KeyError:
            existing = None
        if existing:
            if options.api_key:
                if any(c in options.api_key for c in '\r\n'):
                    raise ValueError('credentials must not contain line breaks')
                ref = existing.source
                _, body = self.reference(ref.kind, ref.id, ref.revision)
                self.hub.development.session_keys[body['api_key_env']] = options.api_key
            return existing
        definition = HTTPTool.model_validate(recipe['definition'])
        if options.endpoint:
            definition.url = options.endpoint
        if options.api_key:
            definition.api_key, definition.api_key_env = options.api_key, None
        elif options.api_key_env:
            definition.api_key_env = options.api_key_env
        definition = HTTPTool.model_validate(definition.model_dump())
        try:
            saved = self.hub.development.get('api', identifier)
            # Do not adopt a pre-existing definition under a trusted recipe's title.
            from .http_tools import export_definition
            if saved['definition'] != export_definition(HTTPTool.model_validate(definition.model_dump())):
                raise Conflict('recipe API id is already used by a different definition')
        except KeyError:
            saved = self.hub.development.save_api(definition)
        manifest = self.publish(PublishComponent(id=identifier, kind='api', source_id=identifier,
            source_revision=saved['revision'], title=recipe['title'], description=definition.description,
            defaults=recipe['defaults']), docs=recipe['docs'], validation='protocol_integration')
        if identifier == 'library.typesafe.evaluate':
            self.install_classification()
        if identifier in ('library.runway.image', 'library.runway.video'):
            wait_options = options.model_copy()
            if options.endpoint:
                parts = urlsplit(options.endpoint)
                wait_options.endpoint = f'{parts.scheme}://{parts.netloc}/v1/tasks/{{id}}'
            self.install('library.runway.wait', wait_options.model_dump())
            self.install_media_flow(identifier)
        return manifest

    def saved_workflow(self, identifier, title, schema, inputs, steps, description, docs=None):
        try:
            return self.get(identifier)
        except KeyError:
            pass
        body = {'name': title, 'inputs': inputs, 'steps': steps, 'metadata': {'component_input_schema': schema}}
        try:
            self.hub.development.get('workflow', identifier)
            raise Conflict('component workflow id already exists without a manifest')
        except KeyError:
            pass
        saved = self.hub.development.save_workflow(identifier, body)
        return self.publish(PublishComponent(id=identifier, kind='workflow', source_id=identifier,
            source_revision=saved['revision'], title=title, description=description, input_schema=schema,
            defaults=inputs), docs=docs, validation='protocol_integration')

    def install_local(self):
        identifier = 'library.json.receipt'
        try:
            existing = self.get(identifier)
        except KeyError:
            existing = None
        if existing and existing.source.kind != 'workflow':
            return existing
        definition = NodeDefinition(input_schema=obj({
            'value': {}, 'filename': {'type': 'string', 'minLength': 1, 'title': '文件名'}}),
            defaults={'value': {}, 'filename': 'receipt.json'}, step=Step(id='receipt', kind='artifact', input={
                'name': {'$ref': '$input.filename'}, 'content': {'$ref': '$input.value'}, 'media_type': 'application/json'}))
        if existing:
            # Only migrate our exact old built-in. Preserve customized definitions,
            # all historical manifests, workflow versions and pinned consumers.
            legacy = Workflow(name='保存 JSON 回执', inputs=definition.defaults, steps=[definition.step],
                              metadata={'component_input_schema': definition.input_schema})
            if (existing.source.id != identifier or existing.defaults != definition.defaults
                    or existing.input_schema != definition.input_schema
                    or self.reference('workflow', existing.source.id, existing.source.revision)[1] != legacy.model_dump()):
                return existing
        try:
            saved = self.hub.development.get('node', identifier)
            if saved['definition'] != definition.model_dump():
                raise Conflict('builtin node id is already used by a different definition')
        except KeyError:
            saved = self.save_node(SaveNode(id=identifier, definition=definition))
        return self.publish(PublishComponent(id=identifier, kind='node', source_id=identifier,
            source_revision=saved['revision'], title='保存 JSON 回执',
            expected_revision=existing.revision if existing else 0,
            description='把任意结构化结果保存为带 SHA-256 的文件。'), validation='protocol_integration')

    def install_classification(self):
        self.install_local()
        schema = obj({'state': {'type': ['string', 'object', 'array'], 'title': '待分类内容'},
            'instructions': {'type': 'string', 'minLength': 1, 'title': '分类标准'},
            'criteria': {'type': 'object', 'minProperties': 2, 'additionalProperties': {'type': ['string', 'null']}, 'title': '候选类别与说明'},
            'filename': {'type': 'string', 'minLength': 1, 'title': '回执文件名'}})
        self.saved_workflow('library.decision.classify_receipt', '通用分类与回执', schema,
            {'state': '', 'instructions': '按给定标准分类；信息不足时选择 other。',
             'criteria': {'match': '符合分类标准', 'other': '不符合或信息不足'}, 'filename': 'classification.json'}, [
            {'id': 'evaluate', 'target': 'library.typesafe.evaluate', 'tool_revision': 1, 'max_attempts': 1,
             'timeout_seconds': 90, 'input': {'state': {'$ref': '$input.state'}, 'model': 'jev-latest',
                 'questions': {'result': {'type': 'choice', 'instructions': {'$ref': '$input.instructions'},
                                          'criteria': {'$ref': '$input.criteria'}}}}},
            {'id': 'decision', 'kind': 'transform', 'depends_on': ['evaluate'], 'input': {
                'choice': {'$ref': 'evaluate.answers.result.choice'}, 'confidence': {'$ref': 'evaluate.answers.result.confidence'},
                'probabilities': {'$ref': 'evaluate.answers.result.probabilities'}}},
            {**self.instantiate('library.json.receipt', {'step_id': 'receipt', 'input': {
                'value': {'$ref': 'evaluate'}, 'filename': {'$ref': '$input.filename'}}})['step'],
             'depends_on': ['evaluate']}],
            '指定内容、分类标准和候选类别，返回类别、置信度、概率分布并保存原始回执；不限于生活事务。',
            ['https://docs.typesafe.ai/api'])

    def install_media_flow(self, source_id):
        manifest = self.get(source_id)
        defaults = manifest.defaults
        self.saved_workflow(source_id + '_flow', manifest.title + '完整流程', manifest.input_schema, defaults, [
            {'id': 'submit', 'target': source_id, 'tool_revision': manifest.source.revision,
             'max_attempts': 1, 'input': {key: {'$ref': '$input.' + key} for key in defaults}},
            {'id': 'result', 'target': 'library.runway.wait', 'tool_revision': self.get('library.runway.wait').source.revision,
             'depends_on': ['submit'], 'input': {'id': {'$ref': 'submit.id'}}}],
            '提交生成任务后持久轮询；返回临时结果链接。重启不会重复提交已取得回执的任务。停止本地流程不等于取消服务商任务。',
            manifest.docs)

    def instantiate(self, identifier, request=None):
        request = InstantiateComponent.model_validate(request or {})
        if identifier == 'library.json.receipt':
            self.install_local()
        manifest = self.get(identifier, request.revision)
        result = self.check(manifest)
        if not result['selected']:
            raise ValueError('组件尚未就绪：' + '; '.join(result['candidates'][0]['reasons']))
        arguments = {**manifest.defaults, **(request.input or {})}
        # Explicitly supplied inputs must satisfy the component contract before submission.
        if request.input is not None:
            from .authoring import check_literals
            check_literals(arguments, manifest.input_schema)
        ref = manifest.source
        if ref.kind == 'api':
            step = Step(id=request.step_id, target=ref.id, tool_revision=ref.revision, input=arguments,
                        max_attempts=1 if manifest.effect == 'write' else 3,
                        timeout_seconds=130 if ref.id == 'library.elevenlabs.speech' else 90)
        elif ref.kind == 'node':
            definition = NodeDefinition.model_validate(self.reference(ref.kind, ref.id, ref.revision)[1])
            step = definition.instantiate(request.step_id, arguments)
        else:
            step = Step(id=request.step_id, kind='subworkflow', workflow_ref={'id': ref.id, 'revision': ref.revision},
                        body=self.reference(ref.kind, ref.id, ref.revision)[1], input=arguments)
        return {'step': step.model_dump(), 'component': manifest.model_dump(),
                'component_type': manifest.component_type, 'resolution': result}


def install_node_library(app, hub):
    @app.get('/v1/library')
    async def catalog():
        return hub.library.catalog()

    @app.get('/v1/library/schema')
    async def schema():
        from .components import CodePackage
        return {'component': ComponentManifest.model_json_schema(), 'runtime': RuntimeProfile.model_json_schema(),
                'node': NodeDefinition.model_json_schema(), 'code_package': CodePackage.model_json_schema()}

    @app.get('/v1/library/sources')
    async def sources():
        return [{**row, 'kind': kind, 'component_type': 'subworkflow' if kind == 'workflow' else 'node'}
                for kind in ('api', 'node', 'workflow')
                for row in hub.development.list_versions(kind, include_archived=False)]

    @app.post('/v1/library/nodes', status_code=201)
    async def save_node(body: SaveNode):
        return hub.library.save_node(body)

    @app.post('/v1/library/publish', status_code=201)
    async def publish(body: PublishComponent):
        return hub.library.publish(body)

    @app.get('/v1/library/{identifier}')
    async def component(identifier: str, revision: int | None = None):
        return hub.library.get(identifier, revision)

    @app.post('/v1/library/{identifier}/install', status_code=201)
    async def install(identifier: str, body: InstallRecipe):
        return hub.library.install(identifier, body)

    @app.post('/v1/library/{identifier}/instantiate')
    async def instantiate(identifier: str, body: InstantiateComponent):
        return hub.library.instantiate(identifier, body)

    @app.post('/v1/library/{identifier}/resolve')
    async def resolve(identifier: str, body: ResolveComponent):
        manifest = hub.library.get(identifier, body.revision)
        return resolve_runtime(manifest.requirements, body.profiles, allow_remote=body.allow_remote)
