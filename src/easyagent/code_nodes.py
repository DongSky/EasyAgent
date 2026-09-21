"""Publishing agent-written pure tools into the reusable node library.

A published code tool is one node with one input/output contract; it never masquerades as a
sub-workflow. Registration is idempotent per definition, so re-publishing the same revision
does not create duplicate library entries.
"""
from __future__ import annotations

from .node_library import PublishComponent


def node_definition(tool):
    return {'input_schema': tool.spec.input_schema, 'output_schema': tool.spec.output_schema,
            'step': {'id': 'execute', 'target': tool.spec.name, 'input': {'$ref': '$input'}}}


def save_code_nodes(hub, manifest):
    """Register every tool of a published pure-code package as a reusable node."""
    saved = []
    for tool in manifest.tools:
        name = tool.spec.name
        definition = node_definition(tool)
        try:
            previous = hub.development.get('node', name)
            revision = previous['revision']
        except KeyError:
            previous, revision = None, 0
        if previous and previous['definition'] == definition:
            row = previous
        else:
            row = hub.library.save_node({'id': name, 'definition': definition, 'expected_revision': revision})
        try:
            current = hub.library.get(name).revision
        except KeyError:
            current = 0
        hub.library.publish(PublishComponent(id=name, kind='node', source_id=name, source_revision=row['revision'],
                                             expected_revision=current,
                                             title=tool.title or tool.spec.description or name,
                                             description=tool.spec.description or name),
                            validation='protocol_integration')
        saved.append(name)
    return saved
