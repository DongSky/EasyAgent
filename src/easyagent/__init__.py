"""EasyAgent: one durable runtime, multiple language interfaces."""

__all__ = ["Agent", "Artifact", "Call", "Model", "Module", "Node", "Sequential", "Subflow", "node", "tool", "Runtime", "Result",
           "ModelRequest", "ModelResult", "Step", "ToolSpec", "Workflow", "__version__"]
__version__ = "0.1.0"


def __getattr__(name):
    # Keep disposable JS/WASM worker startup independent of SDK/schema imports.
    if name in ("ModelRequest", "ModelResult", "Step", "ToolSpec", "Workflow"):
        from . import contracts
        return getattr(contracts, name)
    if name in ("Agent", "Artifact", "Call", "Model", "Module", "Node", "Sequential", "Subflow", "node", "tool"):
        from . import modules
        return getattr(modules, name)
    if name in ("Runtime", "Result"):
        from .local import Runtime, Result
        return {"Runtime": Runtime, "Result": Result}[name]
    raise AttributeError(name)
