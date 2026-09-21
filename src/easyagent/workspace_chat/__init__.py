"""Public conversation dispatch API; follow controller.WorkspaceChat for the lifecycle."""

from .controller import WorkspaceChat
from .inputs import input_contract, workflow_inputs, routing_steps
from .state import Phase, DispatchDecision, RUN_TERMINAL, WORKING_LABEL

__all__ = [
    "WorkspaceChat",
    "input_contract",
    "workflow_inputs",
    "routing_steps",
    "Phase",
    "DispatchDecision",
    "RUN_TERMINAL",
    "WORKING_LABEL",
]
