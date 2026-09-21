"""Business input contracts and frozen workflow descriptions used during routing."""

from jsonschema import Draft202012Validator


def input_contract(flow):
    explicit = flow.get("metadata", {}).get("input_schema") or flow.get("metadata", {}).get(
        "component_input_schema"
    )
    if explicit:
        Draft202012Validator.check_schema(explicit)
        return explicit
    names = set(flow.get("inputs", {}))
    required = set()

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("$input."):
                required.add(ref.split(".")[1])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    # Nested workflows have their own inputs, so inspect only this level's inputs.
    for step in flow["steps"]:
        visit(step.get("input", {}))
    names |= required
    return {
        "type": "object",
        "properties": {n: {} for n in sorted(names)},
        "required": sorted(required),
        "additionalProperties": False,
    }


def workflow_inputs(flow, inputs):
    """Ignore empty runtime placeholders injected by older builders outside an explicit contract."""
    values = {**flow.get("inputs", {}), **inputs}
    schema = input_contract(flow)
    if schema.get("additionalProperties") is False:
        for key, empty in (("message", ""), ("attachment_ids", []), ("attachments", [])):
            if key not in schema.get("properties", {}) and key not in inputs and values.get(key) == empty:
                values.pop(key, None)
    return values


def routing_steps(hub, flow):
    """Expose the actual frozen child operations, including their data wiring."""

    def describe(workflow):
        result = []
        for step in workflow["steps"]:
            item = {k: step[k] for k in ("id", "kind", "target", "depends_on", "input")}
            item["title"] = workflow.get("metadata", {}).get("step_labels", {}).get(step["id"], step["id"])
            if step["kind"] == "tool":
                spec = hub.tools.spec(step["target"], step.get("tool_revision"))
                item["description"] = spec.description[:1500]
            if step.get("body"):
                item["children"] = describe(step["body"])
            result.append(item)
        return result

    return describe(hub.prepare(flow).model_dump())
