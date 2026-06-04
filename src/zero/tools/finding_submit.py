"""finding_submit — orchestrator-intercepted.

The Pentest agent calls this when it believes it has enough evidence. The
orchestrator notices the tool name in `_do_action`, validates the Finding,
runs dedup, and either drops the call (with an observation) or routes it
through the Validator agent.

`run()` is never invoked in normal flow; if it ever is (e.g. orchestrator
bypass), it returns an instructive error.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from zero.findings.schema import Finding
from zero.tools.base import RiskLevel, Tool, ToolResult


class FindingSubmitArgs(BaseModel):
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

    def to_finding(self) -> Finding:
        return Finding(**self.model_dump())


class FindingSubmitTool(Tool):
    name = "finding_submit"
    description = (
        "Submit a candidate Finding. The orchestrator runs dedup, then routes"
        " the finding to the Validator agent. You receive back either"
        " a duplicate notice, a needs-more-evidence prompt, or the Validator's"
        " verdict."
    )
    risk_level = RiskLevel.SAFE  # the submission itself is safe; downstream may not be
    Args = FindingSubmitArgs

    async def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(
            ok=False,
            output="finding_submit must be handled by the orchestrator, not run directly.",
        )


def is_finding_submit(tool_name: str) -> bool:
    return tool_name == FindingSubmitTool.name


def args_to_finding(raw_args: dict[str, Any]) -> Finding:
    parsed = FindingSubmitArgs(**raw_args)
    return parsed.to_finding()
