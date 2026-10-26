"""Host-intercepted collaboration tools exposed only to top-level swarm peers."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult


class _SwarmTool(Tool):
    risk_level = RiskLevel.SAFE
    requires_approval = False

    async def run(self, args: BaseModel) -> ToolResult:  # pragma: no cover - intercepted
        return ToolResult(ok=False, output="swarm tools are handled by the swarm runtime")


class EmptyArgs(BaseModel):
    pass


class SwarmStatusTool(_SwarmTool):
    name = "swarm_status"
    description = "Inspect top-level peers, their lanes and states, and shared work."
    Args = EmptyArgs


class SwarmSendArgs(BaseModel):
    body: str = Field(max_length=20_000)
    kind: Literal["intel", "request", "handoff", "coordination", "alert"] = "intel"
    to: str = Field(default="broadcast", description="worker id, 'broadcast', or 'group'")
    group_id: str | None = None
    work_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SwarmSendTool(_SwarmTool):
    name = "swarm_send"
    description = "Send durable intel directly, to a group, or to every other top-level peer."
    Args = SwarmSendArgs


class SwarmInboxArgs(BaseModel):
    acknowledge_ids: list[str] = Field(default_factory=list)


class SwarmInboxTool(_SwarmTool):
    name = "swarm_inbox"
    description = "Inspect retained peer messages and optionally acknowledge message ids."
    Args = SwarmInboxArgs


class AwaitSwarmEventArgs(BaseModel):
    timeout: float = Field(default=30, ge=0, le=300)


class AwaitSwarmEventTool(_SwarmTool):
    name = "await_swarm_event"
    description = "Wait briefly for peer messages or coordination-state changes."
    Args = AwaitSwarmEventArgs


class SwarmCreateGroupArgs(BaseModel):
    name: str
    topic: str = ""
    member_ids: list[str]


class SwarmCreateGroupTool(_SwarmTool):
    name = "swarm_create_group"
    description = "Create a flat temporary group from existing top-level workers."
    Args = SwarmCreateGroupArgs


class SwarmUpdateGroupArgs(BaseModel):
    group_id: str
    member_ids: list[str] | None = None
    close: bool = False


class SwarmUpdateGroupTool(_SwarmTool):
    name = "swarm_update_group"
    description = "Replace a temporary group's membership or close it; this never creates actors."
    Args = SwarmUpdateGroupArgs


class SwarmCreateWorkArgs(BaseModel):
    kind: str = "hypothesis"
    title: str
    target: str
    hypothesis: str
    priority: int = 0
    dependency_ids: list[str] = Field(default_factory=list)
    group_id: str | None = None
    claim: bool = Field(
        default=False,
        description="Atomically claim the work after creating/reusing it.",
    )


class SwarmCreateWorkTool(_SwarmTool):
    name = "swarm_create_work"
    description = "Publish a deduplicated vulnerability-search task, optionally with dependencies."
    Args = SwarmCreateWorkArgs


class SwarmListWorkArgs(BaseModel):
    state: str | None = None
    mine: bool = False


class SwarmListWorkTool(_SwarmTool):
    name = "swarm_list_work"
    description = "List ready, blocked, owned, or completed shared work."
    Args = SwarmListWorkArgs


class SwarmClaimWorkArgs(BaseModel):
    work_id: str
    lease_seconds: float = Field(default=300, gt=0, le=3600)


class SwarmClaimWorkTool(_SwarmTool):
    name = "swarm_claim_work"
    description = "Atomically claim ready work and receive its unforgeable lease token."
    Args = SwarmClaimWorkArgs


class SwarmUpdateWorkArgs(BaseModel):
    work_id: str
    lease_token: str
    action: Literal["start", "renew", "release", "block", "complete", "fail", "cancel"]
    outcome: str = ""


class SwarmUpdateWorkTool(_SwarmTool):
    name = "swarm_update_work"
    description = "Renew or transition work owned by this peer; the current lease token is required."
    Args = SwarmUpdateWorkArgs


class SwarmPublishEvidenceArgs(BaseModel):
    kind: str
    target: str
    summary: str = Field(max_length=2_000)
    content: str = Field(max_length=100_000)
    visibility: Literal["private", "group", "swarm", "validator"] = "swarm"
    work_id: str | None = None
    group_id: str | None = None


class SwarmPublishEvidenceTool(_SwarmTool):
    name = "swarm_publish_evidence"
    description = "Publish immutable content-addressed evidence for reuse by peers and validators."
    Args = SwarmPublishEvidenceArgs


class SwarmQueryEvidenceArgs(BaseModel):
    evidence_id: str | None = None
    work_id: str | None = None


class SwarmQueryEvidenceTool(_SwarmTool):
    name = "swarm_query_evidence"
    description = "Retrieve evidence visible to this peer by id or work item."
    Args = SwarmQueryEvidenceArgs


class SwarmFinishBootstrapArgs(BaseModel):
    summary: str = Field(
        default="",
        max_length=2_000,
        description="Concise agreed ownership boundaries for this worker.",
    )


class SwarmFinishBootstrapTool(_SwarmTool):
    name = "swarm_finish_bootstrap"
    description = (
        "Complete initial ownership coordination before target tools or child dispatches."
    )
    Args = SwarmFinishBootstrapArgs


class SwarmFinishCycleArgs(BaseModel):
    contribution: str = Field(max_length=50_000)
    unavailable: bool = False


class SwarmFinishCycleTool(_SwarmTool):
    name = "swarm_finish_cycle"
    description = "Submit this peer's response contribution and finish its current response cycle."
    Args = SwarmFinishCycleArgs


_TOOLS = (
    SwarmStatusTool, SwarmSendTool, SwarmInboxTool, AwaitSwarmEventTool,
    SwarmCreateGroupTool, SwarmUpdateGroupTool, SwarmCreateWorkTool, SwarmListWorkTool,
    SwarmClaimWorkTool, SwarmUpdateWorkTool, SwarmPublishEvidenceTool,
    SwarmQueryEvidenceTool, SwarmFinishBootstrapTool, SwarmFinishCycleTool,
)


def swarm_tools() -> list[Tool]:
    return [cls() for cls in _TOOLS]


SWARM_TOOL_NAMES = frozenset(cls.name for cls in _TOOLS)


def is_swarm_tool(name: str) -> bool:
    return name in SWARM_TOOL_NAMES


def swarm_catalog() -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "risk_level": t.risk_level.value,
         "schema": t.json_schema()}
        for t in swarm_tools()
    ]
