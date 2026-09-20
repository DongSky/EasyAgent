"""Discover connected capabilities and verify generated nodes before compiling a task."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from pydantic import Field
from jsonschema import ValidationError, SchemaError, validate

from .code_development import CodeScenario
from .contracts import Contract, ModelRequest, ToolSpec
from .http_tools import HTTPTool, export_definition
from .models import HTTPProvider
from .store import encode


class IndependentChecks(Contract):
    scenarios: list[CodeScenario] = Field(min_length=2, max_length=12)


class DiscoveredAPI(Contract):
    source_url: str
    service: str | None = None
    definition: HTTPTool


def prepare_connected_media(hub):
    """Bind documented media interfaces locally, without a billable probe or credential disclosure."""
    catalog = hub.model_catalog
    if catalog.connection:
        for op in catalog.operations.copy().values():
            if op['path'] in ('/v1/images/edits', '/v1/images/generations', '/v1/audio/speech',
                              '/v1/audio/transcriptions') and op.get('schema_status') != 'route_only':
                # The connected catalog is authoritative; the bundled snapshot alone is not a connection.
                models = [m['id'] for m in catalog.models if any(
                    route and route['id'] == op['id'] for route in
                    (catalog.route(label) for label in m.get('supported_endpoint_types', [])))]
                if not models:
                    continue
                try:
                    installed = catalog.install_operation({'operation_id': op['id']})
                except ValueError:
                    # An unavailable catalog credential must not break unrelated text tasks.
                    continue
                name = installed['step']['target']
                spec, handler = hub.tools.entries[name]
                spec = spec.model_copy(update={'description': spec.description + ' Available models: ' + ', '.join(models)[:3000]})
                hub.tools.entries[name] = (spec, handler)
    for alias, binding in list(hub.models.bindings.items()):
        provider = binding.provider
        if 'image' not in binding.capabilities or not isinstance(provider, HTTPProvider) or provider.dialect == 'anthropic':
            continue
        for mode in ('generations', 'edits'):
            op = next(o for o in catalog.operations.values() if o['path'] == '/v1/images/' + mode)
            variants = op['request_schema'].get('oneOf', [])
            # Edits require a documented model contract; don't infer it from an arbitrary model name.
            if mode == 'edits' and not any(
                v.get('properties', {}).get('model', {}).get('const') == binding.model
                or binding.model in v.get('properties', {}).get('model', {}).get('enum', []) for v in variants
            ):
                continue
            name = 'media.' + hashlib.sha256(alias.encode()).hexdigest()[:12] + '.' + mode
            schema = catalog.authoring_schema(catalog.schema_for_model(op['request_schema'], binding.model))
            schema.setdefault('properties', {})['model'] = {'type': 'string', 'const': binding.model}
            schema['properties']['model']['default'] = binding.model
            schema['required'] = list(dict.fromkeys([*schema.get('required', []), 'model', 'prompt',
                                                    *(['image'] if mode == 'edits' else [])]))
            definition = HTTPTool(name=name, description=(
                f'{binding.model}: ' + ('Edit the uploaded original image. image is an artifact ID, not metadata. '
                                       if mode == 'edits' else 'Generate an image from a prompt. ')
                + 'Use body.model=' + binding.model + '. Returns {result,artifacts:[{id,name,media_type}]}. '
                'Original artifacts are preserved; POST requires the normal execution approval.'),
                method='POST', url=provider.base_url + '/images/' + mode,
                input_schema={'type': 'object', 'properties': {'body': schema}, 'required': ['body'],
                              'additionalProperties': False},
                body_parameter='body', request_encoding='multipart' if mode == 'edits' else 'json',
                file_parameters=['image', 'mask'] if mode == 'edits' else [], response_mode='media',
                max_response_bytes=10_000_000, timeout_seconds=120, effect='write', idempotent=False)
            if provider.api_key:
                credential = 'media.' + hashlib.sha256(alias.encode()).hexdigest()[:12]
                hub.connections.put_secret(credential, provider.api_key)
                definition.api_key_env = credential
            public = export_definition(definition)
            try:
                old = hub.development.get('api', name)
            except KeyError:
                old = None
            if not old or old['definition'] != public or name in hub.development.archived_ids('api'):
                hub.development.put('api', name, public, old['revision'] if old else 0)
            hub.development.set_archived('api', name, False)
            hub.development.refresh_api(name)


class BuildCapabilities:
    def __init__(self, hub):
        self.hub = hub
        hub.tools.register(ToolSpec(name='development.verify_build',
            description='Internal task compiler: validate, test and repair missing pure-code nodes.',
            effect='local', idempotent=True), self.verify)
        hub.tools.internal_names.add('development.verify_build')

    def checkpoint(self, ctx, state, **updates):
        state.update(updates)
        self.hub.store.checkpoint(ctx.job, state)

    async def ask(self, ctx, model, schema, instruction, content, tokens=8192, check=None):
        messages = [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': encode(content)}]
        for attempt in range(2):
            try:
                result = await self.hub.generate(ctx.job, ModelRequest(model=model, capability='decision',
                    max_output_tokens=tokens, response_schema=schema, messages=messages))
                if check:
                    check(result.data)
                return result.data
            except ValidationError as exc:
                if attempt:
                    raise
                messages.append({'role': 'user', 'content':
                    'Your previous response failed the required JSON schema. Return a complete corrected object, '
                    'without additional fields. Constraint: ' + str(exc.validator) + '=' + str(exc.validator_value)
                    + '; path=' + '.'.join(map(str, exc.absolute_path)) + '; detail=' + exc.message[:600]})

    async def verify(self, args, ctx):
        from .assistant_builder import BuildDraft, code_namespace, stored, validate_compiled
        if not ctx.job.get('spec') or self.hub.store.run(ctx.run_id)['spec']['metadata'].get('assistant_builder') != args['assistant_id']:
            raise PermissionError('verification belongs to a task build')
        state = ctx.job['state'] or {'draft': args['draft'], 'attempt': 0, 'development': []}
        draft = BuildDraft.model_validate(state['draft'])
        namespace = code_namespace(args['assistant_id'], args['assistant'])
        build = stored(self.hub, 'studio-assistant-builds', args['assistant_id'])
        first_revision = build.get('code_revision', 1)
        if draft.research_queries and not state.get('researched'):
            from .capability_research import research
            evidence = await research(self.hub, ctx, draft.research_queries)
            self.checkpoint(ctx, state, researched=True, evidence=evidence)
        if state.get('researched') and not state.get('research_compiled'):
            run = self.hub.store.run(ctx.run_id)
            original = run['spec']['steps'][0]['input']['messages']
            content = json.loads(original[-1]['content'])
            content['research_evidence'] = state['evidence']
            proposal = await self.ask(ctx, args['model'], BuildDraft.model_json_schema(),
                original[0]['content'] + '\nResearch is complete for this attempt. Use the actual evidence; do not request another search. '
                'If access is missing, name the service and the specific login/key required, not tool schemas.', content)
            self.checkpoint(ctx, state, draft=proposal, research_compiled=True)
            draft = BuildDraft.model_validate(proposal)
        created = list(state.get('api_tools', []))
        if draft.api_candidates and not created:
            try:
                for api in draft.api_candidates:
                    await self.install_api(api, namespace, state.get('evidence', []))
                    created.append(api.definition.name)
                self.checkpoint(ctx, state, api_tools=created)
            except (ValueError, KeyError, PermissionError, ValidationError, SchemaError) as exc:
                return {'draft': draft.model_dump(), 'errors': ['接口适配未通过检查：' + str(exc)[:1000]]}
        if not draft.code_candidate:
            return {'draft': draft.model_dump(), 'tools': created,
                    'development': state['development'], 'research': state.get('evidence', [])}
        while state['attempt'] < 3:
            draft = BuildDraft.model_validate(state['draft'])
            code = draft.code_candidate
            report = None
            try:
                if not code or code.manifest.id != namespace or code.manifest.revision != first_revision + state['attempt']:
                    raise ValueError('code must use the supplied namespace and attempt revision')
                if draft.workflow is None or draft.questions:
                    raise ValueError('generated code needs a complete workflow for the original task')
                names = [t.spec.name for t in code.manifest.tools]
                if not state.get('checks'):
                    # The independent test author receives the requirement and interfaces, never implementation or expected values.
                    def valid_checks(data):
                        scenarios = IndependentChecks.model_validate(data).scenarios
                        if (set(s.tool for s in scenarios) != set(names)
                                or any(sum(s.tool == n for s in scenarios) < 2 for n in names)):
                            raise ValidationError('Provide at least two tests for each tool and no unknown tools')
                        specs = {t.spec.name: t.spec for t in code.manifest.tools}
                        for scenario in scenarios:
                            validate(scenario.input, specs[scenario.tool].input_schema)
                            validate(scenario.expected, specs[scenario.tool].output_schema)
                    checks = await self.ask(ctx, args['model'], IndependentChecks.model_json_schema(),
                        'Write independent executable input/expected tests from the requirement and tool schemas. '
                        'Cover every tool, normal and boundary inputs. Expected results must follow the requirement. '
                        'Use 2 to 4 VALID input cases per tool, at most 12 total. Each case has ONLY tool, input, expected. '
                        'expected is the exact output JSON, not a description, assertion or expected error. '
                        'Do not weaken tests to fit an implementation. Pure deterministic processing only.',
                        {'requirement': args['assistant']['purpose'], 'tools': [t.spec.model_dump() for t in code.manifest.tools]},
                        4096, check=valid_checks)
                    checks = IndependentChecks.model_validate(checks)
                    self.checkpoint(ctx, state, checks=checks.model_dump(), contracts=[t.spec.model_dump() for t in code.manifest.tools])
                def interfaces(tools):
                    return [{k: t[k] for k in ('name', 'input_schema', 'output_schema')} for t in tools]
                if interfaces([t.spec.model_dump() for t in code.manifest.tools]) != interfaces(state['contracts']):
                    raise ValueError('repair must preserve the independently tested tool contracts')
                candidate = self.hub.code.propose(code.model_copy(update={
                    'scenarios': [*code.scenarios, *IndependentChecks.model_validate(state['checks']).scenarios]}))
                report = await self.hub.code.test(candidate['id'])
                record = {'candidate_id': candidate['id'], 'attempt': state['attempt'] + 1, **report}
                self.checkpoint(ctx, state, development=[*state['development'], record])
                if not report['passed']:
                    raise ValueError('generated node failed executable acceptance tests')
                build = stored(self.hub, 'studio-assistant-builds', args['assistant_id'])
                # Validate the complete graph before publication. This synchronous overlay cannot execute code.
                prior = {name: self.hub.tools.entries.get(name) for name in names}
                keys = [(name, code.manifest.revision) for name in names]
                prior_versions = {key: self.hub.tools.versions.get(key) for key in keys}
                try:
                    for tool in code.manifest.tools:
                        self.hub.tools.entries[tool.spec.name] = (tool.spec, None)
                        self.hub.tools.versions[tool.spec.name, code.manifest.revision] = (tool.spec, None)
                    validate_compiled(self.hub, draft.workflow, {**build, 'tools': [*build['tools'], *created, *names]})
                finally:
                    for name, entry in prior.items():
                        if entry is None:
                            self.hub.tools.entries.pop(name, None)
                        else:
                            self.hub.tools.entries[name] = entry
                    for key, entry in prior_versions.items():
                        if entry is None:
                            self.hub.tools.versions.pop(key, None)
                        else:
                            self.hub.tools.versions[key] = entry
                # Only restricted JS/WASM tools with zero host permissions reach publication.
                await self.hub.code.publish(candidate['id'])
                self.save_skill(args, namespace, code, state['development'])
                self.save_nodes(code)
                return {'draft': draft.model_dump(), 'tools': [*created, *names], 'development': state['development']}
            except (ValueError, KeyError, PermissionError, ValidationError, SchemaError) as exc:
                self.checkpoint(ctx, state, last_error=type(exc).__name__ + ': ' + str(exc)[:1500])
                if state['attempt'] >= 2:
                    return {'draft': draft.model_dump(), 'tools': [], 'development': state['development'],
                            'errors': ['自动开发未通过验证：' + str(exc)[:1000]]}
                revision = first_revision + state['attempt'] + 1
                repaired = await self.ask(ctx, args['model'], BuildDraft.model_json_schema(),
                    'Repair the generated code and workflow using actual test failures. Preserve tool contracts and expected behavior. '
                    'Never change independent tests, request permissions, fabricate services or replace computation with hardcoded examples. '
                    'Pure JavaScript tools must use spec.effect=read, idempotent=true and zero host permissions. '
                    'Return the complete draft; use the given namespace and revision. Keep the original requirement.',
                    {'requirement': args['assistant']['purpose'], 'draft': draft.model_dump(), 'error': str(exc)[:1500],
                     'report': report, 'tested_contracts': state.get('contracts', []),
                     'code_namespace': namespace, 'revision': revision})
                self.checkpoint(ctx, state, draft=repaired, attempt=state['attempt'] + 1)
        raise RuntimeError('node development budget exhausted')

    def save_nodes(self, code):
        from .node_library import PublishComponent
        for tool in code.manifest.tools:
            name = tool.spec.name
            definition = {'input_schema': tool.spec.input_schema, 'output_schema': tool.spec.output_schema,
                          'step': {'id': 'execute', 'target': name,
                                   'input': {'$ref': '$input'}}}
            try:
                previous = self.hub.development.get('node', name)
                revision = previous['revision']
            except KeyError:
                revision = 0
            saved = self.hub.library.save_node({'id': name, 'definition': definition, 'expected_revision': revision})
            try:
                manifest = self.hub.library.get(name)
                previous_revision = manifest.revision
            except KeyError:
                previous_revision = 0
            self.hub.library.publish(PublishComponent(id=name, kind='node', source_id=name,
                source_revision=saved['revision'], expected_revision=previous_revision,
                title=tool.title or tool.spec.description or name, description=tool.spec.description or name),
                validation='protocol_integration')

    async def install_api(self, api, namespace, evidence):
        from .capability_research import public_url
        if api.source_url not in {row.get('url') for row in evidence if row.get('text')}:
            raise ValueError('API definition must cite documentation actually read during this build')
        definition = api.definition.model_copy(deep=True)
        if not definition.name.startswith(namespace + '.'):
            raise PermissionError('new API must use the build namespace')
        if definition.api_key or definition.api_key_env or definition.headers:
            raise PermissionError('generated definitions cannot supply credentials or headers')
        if api.service:
            binding = self.hub.models.bindings.get(api.service)
            if not binding or not isinstance(binding.provider, HTTPProvider):
                raise ValueError('service is not connected')
            target, source = urlsplit(definition.url), urlsplit(binding.provider.base_url)
            if (target.scheme, target.netloc) != (source.scheme, source.netloc):
                raise PermissionError('connected credentials cannot be sent to a different service')
            if binding.provider.api_key:
                credential = 'adapter.' + hashlib.sha256(api.service.encode()).hexdigest()[:16]
                self.hub.connections.put_secret(credential, binding.provider.api_key)
                definition.api_key_env = credential
        else:
            await public_url(definition.url)
        # Conservatively retain approval for every non-read request, independent of generated claims.
        definition.effect = 'read' if definition.method in ('GET', 'HEAD', 'OPTIONS') else 'write'
        definition.idempotent = definition.effect == 'read'
        body = export_definition(definition)
        try:
            prior = self.hub.development.get('api', definition.name)
        except KeyError:
            prior = None
        if not prior or prior['definition'] != body:
            self.hub.development.put('api', definition.name, body, prior['revision'] if prior else 0)
        self.hub.development.refresh_api(definition.name)

    def save_skill(self, args, namespace, code, reports):
        """Persist procedures only with actual executable evidence, separate from the node implementation."""
        name = namespace.replace('_', '-')
        description = 'Verified generated nodes: ' + ', '.join(t.spec.name for t in code.manifest.tools)
        text = ('---\nname: ' + name + '\ndescription: ' + json.dumps(description) + '\n---\n\n'
                '# Verified data processing nodes\n\n'
                'Use these explicit workflow tools for matching input contracts. They run pure code with no host access.\n\n'
                + '\n'.join('- `' + t.spec.name + '`: ' + t.spec.description for t in code.manifest.tools)
                + '\n\nRead references/contracts.json for input and output schemas and references/checks.json for executed tests. '
                'Passing these cases does not prove correctness for every possible input.\n')
        package = {'files': {'SKILL.md': text,
                            'references/contracts.json': encode([t.spec.model_dump() for t in code.manifest.tools]),
                            'references/checks.json': encode(reports),
                            **{'scripts/' + path: content for path, content in code.files.items()}},
                   'source': {'extension': code.manifest.id, 'revision': str(code.manifest.revision)}}
        try:
            prior = self.hub.skill_packages.get(name)
            if prior['package']['files'] == package['files']:
                return
            revision = prior['revision']
        except KeyError:
            revision = 0
        self.hub.skill_packages.install({'package': package, 'expected_revision': revision})
