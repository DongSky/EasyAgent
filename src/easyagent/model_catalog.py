"""Model-name catalog and native API protocol adapters, independent of gateway branding."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

import httpx
from pydantic import Field

from .contracts import Contract
from .http_tools import HTTPTool, Polling
from .models import HTTPProvider
from .node_library import PublishComponent


class CatalogConnection(Contract):
    base_url: str | None = None
    api_key_env: str = 'OPENAI_API_KEY'


class SelectModel(Contract):
    model: str
    alias: str | None = None
    dialect: str | None = None


class SelectOperation(Contract):
    operation_id: str
    model: str | None = None
    defaults: dict = Field(default_factory=dict)
    polling: Polling | None = None


def gateway_root(base):
    base = base.rstrip('/')
    root = base[:-3] if base.endswith('/v1') else base
    HTTPTool(name='catalog.validate', description='Validate model service', url=root)
    return root


def operation_title(op):
    return {
        '/v1/chat/completions': 'Chat Completions', '/v1/responses': 'Responses',
        '/v1/messages': 'Messages', '/v1beta/models/{model}:generateContent': 'Gemini generateContent',
        '/v1/images/generations': '图像生成', '/v1/images/edits': '图像编辑',
        '/v1/audio/speech': '语音合成', '/v1/audio/transcriptions': '语音转写',
    }.get(op['path'], op['method'] + ' ' + op['path'])


class ModelCatalog:
    def __init__(self, hub):
        self.hub = hub
        self.snapshot = json.loads(Path(__file__).with_name('data').joinpath('model_protocols.json').read_text())
        self.operations = {op['id']: op for op in self.snapshot['operations']}
        self.connection = None
        self.models = self.snapshot['models']
        self.endpoints = self.snapshot['endpoint_types']
        rows = hub.store.memory_search('model-catalog', limit=1)
        if rows:
            saved = rows[0]['value']
            self.connection, self.models, self.endpoints = saved['connection'], saved['models'], saved['endpoint_types']

    async def discover(self, options=None):
        options = CatalogConnection.model_validate(options or {})
        base = options.base_url or os.environ.get('OPENAI_BASE_URL')
        if not base:
            raise ValueError('请配置模型服务地址或 OPENAI_BASE_URL')
        root = gateway_root(base)
        key = os.environ.get(options.api_key_env)
        if not key:
            raise ValueError('请设置服务端凭证环境变量：' + options.api_key_env)
        async def fetch(path, headers):
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                async with client.stream('GET', root+path, headers=headers) as response:
                    if response.status_code != 200:
                        raise ValueError('model catalog HTTP ' + str(response.status_code))
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 5_000_000:
                            raise ValueError('model catalog response exceeds byte budget')
            try:
                return json.loads(data)
            except ValueError:
                raise ValueError('model catalog did not return JSON') from None
        data = await fetch('/v1/models', {'Authorization': 'Bearer '+key})
        if not isinstance(data.get('data'), list):
            raise ValueError('missing model catalog data')
        models = []
        for row in data['data']:
            if not isinstance(row.get('id'), str) or not row['id']:
                raise ValueError('invalid model identifier')
            models.append({k: row[k] for k in ('id', 'model_type', 'supported_endpoint_types') if k in row})
        endpoints = self.snapshot['endpoint_types']
        try:
            metadata = await fetch('/api/pricing', {})
            if isinstance(metadata.get('supported_endpoint'), dict):
                endpoints = metadata['supported_endpoint']
        except (ValueError, httpx.HTTPError):
            # Discovery is still useful on gateways exposing only /v1/models.
            pass
        self.models, self.endpoints = models, endpoints
        self.connection = {'base_url': root, 'api_key_env': options.api_key_env}
        self.hub.store.memory_put('model-catalog', 'active', {'connection': self.connection,
            'models': models, 'endpoint_types': endpoints}, 'model-service')
        return self.catalog()

    def route(self, label):
        entry = self.endpoints.get(label)
        if not isinstance(entry, dict):
            return None
        path, method = '/' + str(entry.get('path', '')).lstrip('/'), str(entry.get('method', 'POST')).upper()
        if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'):
            return None
        for op in list(self.operations.values()):
            if op['path'] == path and op['method'] == method:
                return op
        pattern = '^' + re.sub(r'\\\{[^}]+\\\}', '[^/]+', re.escape(path)) + '$'
        for op in list(self.operations.values()):
            same_shape = re.sub(r'\{[^}]+\}', '{}', op['path']) == re.sub(r'\{[^}]+\}', '{}', path)
            if op['method'] == method and (same_shape or re.fullmatch(pattern, op['path'])):
                adapted = copy.deepcopy(op)
                adapted['id'] += '.binding_' + hashlib.sha256(path.encode()).hexdigest()[:8]
                old_names, new_names = re.findall(r'\{([^}]+)\}', op['path']), re.findall(r'\{([^}]+)\}', path)
                renames = dict(zip(old_names, new_names))
                adapted['path'] = path
                for p in adapted['parameters']:
                    if p.get('in') == 'path':
                        p['name'] = renames.get(p['name'], p['name'])
                self.operations[adapted['id']] = adapted
                return adapted
        # A published route can be used without inventing its undocumented parameter schema.
        identifier = 'route.' + hashlib.sha256((method+path).encode()).hexdigest()[:16]
        operation = {'id': identifier, 'family': label, 'path': path, 'method': method,
            'title': label, 'request_encoding': 'json', 'request_schema': {}, 'parameters': [],
            'schema_status': 'route_only', 'source': ''}
        self.operations[identifier] = operation
        return operation

    def model(self, name):
        result = next((m for m in self.models if m['id'] == name), None)
        if result is None:
            raise KeyError(name)
        return result

    def catalog(self):
        rows = []
        for model in self.models:
            routes = []
            for label in model.get('supported_endpoint_types', []):
                if op := self.route(label):
                    routes.append({'protocol': label, 'operation_id': op['id'], 'method': op['method'], 'path': op['path'],
                                   'schema_status': op.get('schema_status', 'documented')})
            normalized = self.normalized(model)
            rows.append({**model, 'operations': routes, 'normalized_adapter': normalized,
                         'status': 'available' if routes else 'needs_protocol', 'live_verified': False})
        return {'models': rows, 'model_count': len(rows), 'operation_count': len(self.operations),
                'connected': self.connection is not None, 'snapshot_date': self.snapshot['retrieved'],
                'missing_protocol_count': sum(not m['operations'] for m in rows),
                'scope': 'API transport and protocol compatibility; model availability and output quality require live verification'}

    @staticmethod
    def normalized(model):
        endpoints = model.get('supported_endpoint_types', [])
        # Image/audio models may expose chat wire format but must not be offered as a text Agent.
        if model.get('model_type') != '对话':
            return None
        for label, dialect in [('openai-response', 'responses'), ('openai', 'chat'), ('anthropic', 'anthropic')]:
            if label in endpoints:
                return dialect
        return None

    def connect_model(self, request):
        request = SelectModel.model_validate(request)
        model = self.model(request.model)
        dialect = request.dialect or self.normalized(model)
        if request.dialect and {'chat':'openai','responses':'openai-response','anthropic':'anthropic'}.get(dialect) not in model.get('supported_endpoint_types', []):
            raise ValueError('the model catalog does not declare the selected dialect')
        if not dialect:
            raise ValueError('该模型请使用对应原生接口节点；不套用文字对话协议')
        if not self.connection:
            raise ValueError('请先连接并同步模型目录')
        alias = request.alias or request.model
        if alias in self.hub.models.bindings:
            raise ValueError('model alias is already connected')
        key = os.environ.get(self.connection['api_key_env'])
        if not key:
            raise ValueError('missing model credential environment variable')
        self.hub.models.register(alias, HTTPProvider(self.connection['base_url']+'/v1', key, dialect=dialect, timeout=120),
                                 request.model, ['chat', 'decision'])
        return {'alias': alias, 'model': request.model, 'dialect': dialect}

    def native_definition(self, request):
        request = SelectOperation.model_validate(request)
        if not self.connection:
            raise ValueError('请先连接并同步模型目录')
        # Materialize routes declared by the current catalog, including route-only entries.
        self.catalog()
        op = self.operations[request.operation_id]
        if '*' in op['path']:
            raise ValueError('documentation wildcard: choose a concrete operation')
        defaults = copy.deepcopy(request.defaults)
        request_schema = self.schema_for_model(op['request_schema'], request.model)
        props, required, locations = {}, [], {}
        for param in op['parameters']:
            name, location = param.get('name'), param.get('in')
            if not name or location not in ('path', 'query', 'header', 'cookie'):
                continue
            if location in ('query', 'header') and name.lower() in (
                    'key', 'api_key', 'api-key', 'apikey', 'authorization', 'x-api-key', 'x-goog-api-key'):
                # Gateway authentication is bound by the connection, never a model-visible input.
                continue
            props[name] = param.get('schema', {'type': 'string'})
            if 'default' in props[name]:
                defaults.setdefault(name, props[name]['default'])
            if param.get('required') or location == 'path':
                required.append(name)
            if location != 'path':
                locations[name] = location
        for name in re.findall(r'\{([^}]+)\}', op['path']):
            props.setdefault(name, {'type': 'string'})
            if name not in required:
                required.append(name)
        body = op['method'] not in ('GET', 'HEAD')
        if body:
            # Preserve native fields, including new vendor options. Provider schemas are available
            # for authoring, but not treated as authoritative validators when metadata is incomplete.
            props['body'] = self.authoring_schema(request_schema) | {'title': '原生请求参数'}
            required.append('body')
            defaults.setdefault('body', {})
        if request.model:
            model = self.model(request.model)
            permitted = {self.route(label)['id'] for label in model.get('supported_endpoint_types', []) if self.route(label)}
            if request.operation_id not in permitted:
                raise ValueError('该模型的目录未声明此接口；请显式创建不绑定模型的原生接口节点')
            if 'model' in props:
                defaults['model'] = request.model
            elif body and (op['path'].startswith('/v1/') or self.schema_has_model(op['request_schema'])):
                defaults['body'].setdefault('model', request.model)
        fingerprint = hashlib.sha256(json.dumps([op['id'], request.model, self.connection, request.polling.model_dump() if request.polling else None], sort_keys=True).encode()).hexdigest()[:16]
        name = 'protocol.' + re.sub('[^A-Za-z0-9_-]', '_', op['family'])[:32] + '.' + fingerprint
        schema = self.authoring_schema(request_schema)
        files = [k for k,v in schema.get('properties', {}).items() if isinstance(v,dict) and
                 (v.get('format') == 'binary' or v.get('items', {}).get('format') == 'binary')]
        # Exact endpoint, fixed credentials, JSON/multipart, binary/base64/SSE receipts share one runtime.
        definition = HTTPTool(name=name, description=(operation_title(op) + '. Native protocol request; inputs follow the published API definition. '
            'POST may submit a billable job. Upload fields take artifact IDs. Media responses use result/artifacts envelopes.'),
            method=op['method'], url=self.connection['base_url']+op['path'],
            input_schema={'type':'object', 'properties':props, 'required':required, 'additionalProperties':False},
            parameter_locations=locations, body_parameter='body' if body else None,
            request_encoding=op['request_encoding'], file_parameters=files,
            artifact_url_parameters=[f'content.*.{kind}_url.url' for kind in ('image','video','audio')]
                if op['path'] == '/api/v3/contents/generations/tasks' and body else [],
            response_mode='json' if request.polling else 'media', polling=request.polling,
            max_response_bytes=1_000_000 if request.polling else 10_000_000, timeout_seconds=120,
            api_key_env=self.connection['api_key_env'], effect='read' if op['method']=='GET' else 'write', idempotent=False)
        return definition, defaults, op

    @staticmethod
    def schema_for_model(schema, model):
        """Select a documented model variant before discovering multipart upload fields."""
        for field in ('oneOf', 'anyOf'):
            for variant in schema.get(field, []):
                selector = variant.get('properties', {}).get('model', {})
                if model and (selector.get('const') == model or model in selector.get('enum', [])):
                    return variant
        return schema

    @staticmethod
    def authoring_schema(schema):
        schema = copy.deepcopy(schema)
        variants = schema.get('oneOf', schema.get('anyOf', []))
        if variants:
            properties = {}
            for variant in variants:
                properties.update(ModelCatalog.authoring_schema(variant).get('properties', {}))
            required = set.intersection(*(set(v.get('required', [])) for v in variants)) if variants else set()
            schema = {'type':'object', 'properties':properties, 'required':sorted(required)}
        schema.setdefault('type', 'object')
        if schema.get('type') != 'object':
            return {'type':'object'}
        # Provider catalogs grow independently of per-model enum examples in the docs.
        if 'model' in schema.get('properties', {}):
            schema['properties']['model'] = {'type':'string'}
        schema['additionalProperties'] = True
        return schema

    @staticmethod
    def schema_has_model(schema):
        return 'model' in schema.get('properties', {}) or any(ModelCatalog.schema_has_model(v)
            for field in ('oneOf', 'anyOf', 'allOf') for v in schema.get(field, []))

    def install_operation(self, request):
        request = SelectOperation.model_validate(request)
        definition, defaults, op = self.native_definition(request)
        try:
            manifest = self.hub.library.get(definition.name)
        except KeyError:
            manifest = None
        from .http_tools import export_definition
        definition_changed = manifest is None or self.hub.development.get('api', definition.name, manifest.source.revision)['definition'] != export_definition(definition)
        if definition_changed:
            prior = self.hub.development.get('api', definition.name)['revision'] if manifest else 0
            saved = self.hub.development.save_api(definition, prior)
        else:
            saved = {'revision': manifest.source.revision}
        if definition_changed or manifest.defaults != defaults:
            manifest = self.hub.library.publish(PublishComponent(id=definition.name, kind='api', source_id=definition.name,
                source_revision=saved['revision'], title=(request.model + ' · ' if request.model else '') + operation_title(op),
                description=definition.description, defaults=defaults, expected_revision=manifest.revision if manifest else 0), docs=[op['source']] if op.get('source') else [],
                validation='protocol_integration')
        step = self.hub.library.instantiate(manifest.id)['step']
        step['input'], step['timeout_seconds'] = defaults, 130
        return {'manifest': manifest.model_dump(), 'defaults': defaults, 'operation': op, 'step': step}


def install_model_catalog(app, hub):
    @app.get('/v1/studio/model-catalog')
    async def catalog():
        return hub.model_catalog.catalog()

    @app.post('/v1/studio/model-catalog/discover')
    async def discover(body: CatalogConnection):
        return await hub.model_catalog.discover(body)

    @app.get('/v1/studio/model-catalog/operations')
    async def operations():
        return list(hub.model_catalog.operations.values())

    @app.post('/v1/studio/model-catalog/connect')
    async def connect(body: SelectModel):
        return hub.model_catalog.connect_model(body)

    @app.post('/v1/studio/model-catalog/nodes', status_code=201)
    async def node(body: SelectOperation):
        return hub.model_catalog.install_operation(body)
