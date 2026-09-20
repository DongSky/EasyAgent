"""EasyAgent: one durable runtime, multiple language interfaces."""

from .contracts import ModelRequest, ModelResult, Step, ToolSpec, Workflow
from .modules import Agent, Artifact, Call, Model, Module, Node, Sequential, Subflow, node, tool

__all__ = ["Agent", "Artifact", "Call", "Model", "Module", "Node", "Sequential", "Subflow", "node", "tool", "Runtime", "Result",
           "ModelRequest", "ModelResult", "Step", "ToolSpec", "Workflow", "__version__"]
__version__ = "0.1.0"


def __getattr__(name):
    if name in ("Runtime", "Result"):
        from .local import Runtime, Result
        return {"Runtime": Runtime, "Result": Result}[name]
    raise AttributeError(name)
