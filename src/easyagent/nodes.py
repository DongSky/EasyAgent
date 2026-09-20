"""Reusable single-step definitions; workflows keep their own graph contract."""
from copy import deepcopy

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from .contracts import Contract, Step, Workflow


class NodeDefinition(Contract):
    """A public input contract and exactly one executable Step template."""

    step: Step
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict = Field(default_factory=lambda: {"type": "object"})
    defaults: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def boundary(self):
        Draft202012Validator.check_schema(self.input_schema)
        Draft202012Validator.check_schema(self.output_schema)
        if self.step.kind in ("subworkflow", "foreach", "goal") or self.step.body is not None or self.step.workflow_ref:
            raise ValueError("a node cannot contain a child workflow; save it as a workflow instead")
        if self.step.depends_on or self.step.when is not None or self.step.compensate is not None:
            raise ValueError("dependencies, conditions and compensation belong to the consuming workflow")
        Workflow(name="Node validation", steps=[self.step])
        return self

    def instantiate(self, identifier, arguments):
        # Substitute only template references. References supplied by the consumer
        # remain symbolic and are resolved by its own workflow at execution time.
        def substitute(value):
            if isinstance(value, dict):
                if set(value) == {"$ref"}:
                    parts = value["$ref"].split(".")
                    if parts[0] != "$input":
                        raise ValueError("node templates may only reference their public inputs")
                    result = arguments
                    for index, part in enumerate(parts[1:]):
                        if isinstance(result, dict) and set(result) == {"$ref"}:
                            return {"$ref": result["$ref"] + "." + ".".join(parts[index + 1:])}
                        try:
                            result = result[int(part)] if isinstance(result, list) else result[part]
                        except (KeyError, TypeError, IndexError, ValueError):
                            raise ValueError("missing node input: " + value["$ref"]) from None
                    return deepcopy(result)
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            return value

        body = self.step.model_dump()
        body["id"] = identifier
        literal_fields = {"response_schema", "development"} if self.step.kind in ("model", "agent") else {"schema"} if self.step.kind == "input" else set()
        if set(self.step.input) == {"$ref"}:
            body["input"] = substitute(self.step.input)
        else:
            body["input"] = {k: deepcopy(v) if k in literal_fields else substitute(v) for k, v in self.step.input.items()}
        return Step.model_validate(body)
