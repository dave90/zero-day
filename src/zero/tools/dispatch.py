"""Orchestrator-only dispatch tools (M8 hierarchical orchestration).

The senior-pentester Orchestrator agent calls these to spawn and coordinate
Pentester / Validator subagents. Like `finding_submit`, they are intercepted
by the orchestrator (`is_dispatch_tool` + `_do_orchestrator_action`) and never
actually `run()`; the `run()` bodies return an instructive error in case one is
ever invoked directly (e.g. by a non-orchestrator agent that shouldn't have
them in its catalog).
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from zero.findings.schema import Finding
from zero.tools.base import RiskLevel, Tool, ToolResult


_SENTINEL = "dispatch tools must be handled by the orchestrator, not run directly."


class _OrchestratorTool(Tool):
    risk_level = RiskLevel.SAFE  # dispatching is safe; subagents' own calls are gated

    async def run(self, args: BaseModel) -> ToolResult:  # pragma: no cover - never called
        return ToolResult(ok=False, output=_SENTINEL)


# --- dispatch_pentester ------------------------------------------------------


class DispatchPentesterArgs(BaseModel):
    attack_idea: str = Field(description="the specific hypothesis to test, e.g. 'boolean SQLi in sort param'")
    target: str = Field(description="the path/URL the subagent focuses on, e.g. '/api/orders'")
    context: str = Field(default="", description="creds/tokens/prior results to seed for chaining")
    max_turns: int | None = Field(default=None, description="optional per-subagent turn budget")


class DispatchPentesterTool(_OrchestratorTool):
    name = "dispatch_pentester"
    description = (
        "Spawn a focused Pentester subagent to exploit ONE attack idea against ONE"
        " target. Returns immediately (the subagent runs concurrently). Dispatch"
        " several independent ideas, then call await_subagents to collect results."
    )
    Args = DispatchPentesterArgs


# --- dispatch_validator ------------------------------------------------------


class DispatchValidatorArgs(BaseModel):
    title: str
    summary: str
    endpoint: str = Field(description="path + method, e.g. 'GET /api/orders/{id}'")
    method: str = "GET"
    cwe: str = "CWE-?"
    severity_hint: str = "medium"
    evidence: str
    affected_credentials: list[str] = Field(default_factory=list)
    notes: str = ""
    payload_shape: str = ""
    source_subagent: str = Field(default="", description="the pentester id that found it, e.g. '#3'")

    def to_finding(self) -> Finding:
        data = self.model_dump()
        data.pop("source_subagent", None)
        return Finding(**data)


class DispatchValidatorTool(_OrchestratorTool):
    name = "dispatch_validator"
    description = (
        "Spawn a Validator subagent to confirm-or-refute a candidate finding"
        " (typically one a pentester returned). It investigates from scratch and"
        " produces a verdict. Returns immediately; collect via await_subagents."
    )
    Args = DispatchValidatorArgs


# --- await_subagents ---------------------------------------------------------


class AwaitSubagentsArgs(BaseModel):
    which: Literal["any", "all"] = Field(
        default="any",
        description="'any' returns as soon as ≥1 subagent finishes (the default — react to results as they arrive); 'all' waits for every running subagent",
    )
    timeout: float | None = Field(default=None, description="seconds to wait; omit to wait indefinitely")


class AwaitSubagentsTool(_OrchestratorTool):
    name = "await_subagents"
    description = (
        "Block until a subagent result is available (by default, as soon as the"
        " FIRST one finishes), then return the structured results (status, summary,"
        " candidate findings, discovered creds/tokens for chaining). Call it again"
        " to collect the next completion. Use after dispatching pentesters/validators."
    )
    Args = AwaitSubagentsArgs


# --- list_subagents ----------------------------------------------------------


class ListSubagentsArgs(BaseModel):
    pass


class ListSubagentsTool(_OrchestratorTool):
    name = "list_subagents"
    description = "Non-blocking snapshot of the currently running subagents and their targets."
    Args = ListSubagentsArgs


# --- finalize ----------------------------------------------------------------


class FinalizeArgs(BaseModel):
    summary: str = Field(description="your closing assessment of the engagement")


class FinalizeTool(_OrchestratorTool):
    name = "finalize"
    description = (
        "End the engagement: drains remaining subagents, writes the final report"
        " (found & approved vs tested & rejected/failed), and stops. Call this when"
        " you've exhausted promising paths."
    )
    Args = FinalizeArgs


# --- helpers -----------------------------------------------------------------


DISPATCH_TOOL_NAMES: frozenset[str] = frozenset(
    {
        DispatchPentesterTool.name,
        DispatchValidatorTool.name,
        AwaitSubagentsTool.name,
        ListSubagentsTool.name,
        FinalizeTool.name,
    }
)


def is_dispatch_tool(tool_name: str) -> bool:
    return tool_name in DISPATCH_TOOL_NAMES


def dispatch_tools() -> list[Tool]:
    """The orchestrator-only tools, instantiated. Appended to the orchestrator's
    catalog in `_orchestrator_main` (NOT registered in `default_registry`, so
    modes a/b never see them)."""
    return [
        DispatchPentesterTool(),
        DispatchValidatorTool(),
        AwaitSubagentsTool(),
        ListSubagentsTool(),
        FinalizeTool(),
    ]


def validator_args_to_finding(raw_args: dict[str, Any]) -> Finding:
    return DispatchValidatorArgs(**raw_args).to_finding()
