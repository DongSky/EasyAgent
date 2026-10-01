"""Contracts for EasyAgent rewrite (SPEC §1): the ten pydantic models.

Field names match SPEC exactly. Enums use Literals so invalid values fail fast.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RunStatus = Literal[
    "pending", "running", "paused", "awaiting_confirm", "cancelled", "done", "failed"
]
RunEventType = Literal[
    "plan",
    "thought",
    "tool_call",
    "tool_result",
    "artifact",
    "checkpoint",
    "status",
    "steer",
    "done",
    "error",
    "tools_trimmed",
    "prune_candidates",
]
Trust = Literal["trusted", "untrusted"]
DecisionKind = Literal["noul", "choice", "score"]
SteerAction = Literal["pause", "resume", "cancel", "redirect"]


class Budget(BaseModel):
    max_steps: int = 50
    max_cost_usd: float | None = None
    max_wall_clock_s: int = 1800


class Mission(BaseModel):
    id: str
    goal: str
    budget: Budget
    created_at: str


class RunState(BaseModel):
    run_id: str
    mission_id: str
    status: RunStatus
    step: int
    plan: list[str]
    started_at: str
    updated_at: str


class RunEvent(BaseModel):
    run_id: str
    seq: int
    type: RunEventType
    payload: dict = Field(default_factory=dict)
    ts: str


class Checkpoint(BaseModel):
    run_id: str
    step: int
    state_json: str
    created_at: str


class ToolManifest(BaseModel):
    name: str
    version: str
    description: str
    trust: Trust


class DecisionQuestion(BaseModel):
    kind: DecisionKind
    question: str
    options: list[str] | None = None
    state: dict = Field(default_factory=dict)


class Decision(BaseModel):
    answer: str | bool | float
    probability: float
    provider: str


class SteerCommand(BaseModel):
    action: SteerAction
    message: str | None = None


class PluginFixture(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    expect: dict = Field(default_factory=dict)
