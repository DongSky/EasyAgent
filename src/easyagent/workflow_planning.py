"""Shared construction rules and deterministic catalog summaries (no model calls)."""

CONSTRUCTION_GUIDE = """Workflow construction and orchestration:
- Build one end-to-end workflow for one reusable business task. Combine its search, processing and delivery
  stages in that workflow rather than creating a separate workflow for every minor operation.
- Combine adjacent pure computations (parse, filter, aggregate, format) into one tested code node when they
  use the same inputs and share a retry boundary. Do not use models or sub-agents for deterministic glue.
- Model and agent inputs accept context: an object/array with ordinary $ref data wiring. It is rendered as
  JSON user content by the runtime. Use context instead of adding core.to_text steps solely for serialization.
  Keep instructions/prompt separate from untrusted context. Multiple outputs can be combined in one context object.
- Separate independent branches; add depends_on only for data dependencies or required effect ordering.
  A join waits for all its inputs. Do not serialize independent branches just to mirror a numbered prose plan.
- Reuse a fixed-version subworkflow for a meaningful multi-step capability used more than once; pass business
  inputs explicitly and expose outputs through metadata.outputs. Do not wrap a single node merely to add a layer.
  Subworkflow/foreach results contain arrays: reference sub.outputs.0.field for one child, or consume the
  foreach outputs array. results.0.step_id exposes a child's raw step result when no named output is declared.
- Keep external writes, approvals, user inputs and remote waiting as distinct durable boundaries. Never merge
  them into an opaque script, remove their receipts, or change an already saved workflow to reduce node count.
- Use an agent node for adaptive judgment or tool iteration within its granted tools. Delegate only substantial,
  independent work; avoid duplicate parent/child work. Workflow branches and delegation inside a node are distinct.
- Research is adaptive: discover the subject's identity before adding rendering/style keywords. If a search returns
  irrelevant results, refine the query using observed evidence. Do not pass an empty URL to a downstream downloader.
  Use an agent with granted research tools, or explicit alternate searches and validation when compiling a static graph.
"""


def summarize_steps(steps):
    """Discovery only. Exact input wiring must still be inspected before execution."""
    operations = {}
    count = 0

    def visit(items):
        nonlocal count
        for step in items:
            count += 1
            key = (step['kind'], step.get('target', ''))
            if key not in operations:
                operations[key] = {'kind': key[0], 'target': key[1], 'count': 0,
                                   'description': step.get('description', '')[:180]}
            operations[key]['count'] += 1
            if step.get('children'):
                visit(step['children'])
    visit(steps)
    return {'step_count': count, 'operations': list(operations.values())}
