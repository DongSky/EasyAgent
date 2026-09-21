"""Conversation phases and model routing decisions shared with the browser."""

from typing import Literal
from pydantic import Field
from ..contracts import Contract


class Phase:
    """Every state a conversation turn can be in, with the tables that classify it.

    One vocabulary for the controller, the API and the browser: `enrich` ships the tables to the
    client so a new phase cannot be half-added (labelled in one place, classified in another).
    """

    # waiting for work
    QUEUED = "queued"
    STEERED = "steered"  # guidance appended to a run that has not consumed it yet
    # preparing or performing a run
    ROUTING = "routing"  # an existing workflow is being matched
    BUILDING = "building"  # the compiler is generating a workflow
    WORKING = "working"  # the autonomous operator is acting
    EXECUTING = "executing"  # a frozen workflow is running
    # waiting on the person or the environment
    CLARIFICATION = "clarification"
    WAITING_CONNECTIONS = "waiting_connections"
    # outcomes
    COMPLETED = "completed"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    STARTING_SUFFIX = "_starting"  # a run is submitted as <phase>_starting, then resumed as <phase>

    LABELS = {
        QUEUED: "已收到",
        ROUTING: "正在匹配合适的流程",
        BUILDING: "正在创建新流程",
        WORKING: "正在自主处理",
        EXECUTING: "正在执行",
        COMPLETED: "处理完成",
        WAITING_CONNECTIONS: "已保存 · 等待连接模型或服务",
        SUPERSEDED: "已合并到后续消息",
        STEERED: "已补充到当前任务",
        CLARIFICATION: "需要补充一点信息",
        ANSWERED: "回复",
        FAILED: "处理遇到问题",
        CANCELLED: "已停止",
    }
    # Turns that spend a step slot: the card animates and the composer offers "append guidance".
    ACTIVE = (QUEUED, ROUTING, BUILDING, WORKING, EXECUTING)
    OPERATOR = (WORKING,)
    # A turn in one of these is finished; nothing may overwrite it.
    TERMINAL = (COMPLETED, ANSWERED, SUPERSEDED, FAILED, CANCELLED)
    # Turn states a queued steer message is allowed to settle into.
    SETTLED = TERMINAL + (STEERED,)
    # A failed run is handed back to the compiler only for these phases.
    REPAIRABLE = (EXECUTING,)


# Run statuses that end a run. Distinct from Phase.TERMINAL, which describes turns.
RUN_TERMINAL = {"succeeded", "failed", "cancelled"}
WORKING_LABEL = "正在自主处理：查看下方活动记录；可以继续发消息补充要求。"


class DispatchDecision(Contract):
    action: Literal["use", "create", "clarify", "reply"]
    candidate: str | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    inputs: dict = Field(default_factory=dict)
    message: str = Field(min_length=1, max_length=4000)
    alternatives: list[str] = Field(default_factory=list, max_length=4)
    title: str = Field(default="新的助手", max_length=100)
