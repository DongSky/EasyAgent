"""Binding model-authored HTTP tools to a namespace, without ever handing over credentials.

Both the compile pipeline and the autonomous operator register new API nodes through this one
function, so the same origin/namespace rules apply no matter who wrote the definition.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

from .http_tools import HTTPTool, export_definition
from .models import HTTPProvider


async def bind_api_definition(hub, definition, service, namespace):
    """Register a model-authored HTTP tool: namespaced, credential-free, same-origin credential binding."""
    from .capability_research import public_url
    definition = HTTPTool.model_validate(definition).model_copy(deep=True)
    if not definition.name.startswith(namespace + '.'):
        raise PermissionError('new API must use the namespace ' + namespace + '.<name>')
    if definition.api_key or definition.api_key_env:
        raise PermissionError('generated definitions cannot supply credentials; bind a connected service alias instead')
    if service:
        binding = hub.models.bindings.get(service)
        if not binding or not isinstance(binding.provider, HTTPProvider):
            raise ValueError('service is not connected: ' + service)
        target, source = urlsplit(definition.url), urlsplit(binding.provider.base_url)
        if (target.scheme, target.netloc) != (source.scheme, source.netloc):
            raise PermissionError('connected credentials cannot be sent to a different service origin')
        if binding.provider.api_key:
            credential = 'adapter.' + hashlib.sha256(service.encode()).hexdigest()[:16]
            hub.connections.put_secret(credential, binding.provider.api_key)
            definition.api_key_env = credential
    else:
        await public_url(definition.url)
    # Conservatively retain approval for every non-read request, independent of generated claims.
    definition.effect = 'read' if definition.method in ('GET', 'HEAD', 'OPTIONS') else 'write'
    definition.idempotent = definition.effect == 'read'
    body = export_definition(definition)
    try:
        prior = hub.development.get('api', definition.name)
    except KeyError:
        prior = None
    if not prior or prior['definition'] != body:
        hub.development.put('api', definition.name, body, prior['revision'] if prior else 0)
    hub.development.refresh_api(definition.name)
    return hub.development.get('api', definition.name)


def publish_api_node(hub, name, revision, description):
    """Make a registered API node discoverable in the node library (best effort, never fatal)."""
    from .node_library import PublishComponent
    try:
        previous = hub.library.get(name).revision
    except KeyError:
        previous = 0
    hub.library.publish(PublishComponent(id=name, kind='api', source_id=name, source_revision=revision,
                                         expected_revision=previous, title=description[:120] or name,
                                         description=description or name), validation='protocol_integration')
