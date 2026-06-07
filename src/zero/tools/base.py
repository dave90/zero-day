"""Tool ABC, ToolResult, risk levels."""
from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class RiskLevel(str, enum.Enum):
    SAFE = "safe"            # read-only, no side effects beyond local disk
    ACTIVE = "active"        # touches a target (probes, fuzz, scans)
    EXPLOIT = "exploit"      # attempts to prove an exploit (sqli, rce, idor write)
    DESTRUCTIVE = "destructive"  # writes/deletes data on the target


@dataclass
class ToolResult:
    ok: bool
    output: str
    truncated: bool = False
    meta: dict[str, Any] | None = None


class Tool(ABC):
    """Base class for all tools.

    Subclasses define `name`, `description`, `risk_level`, an `Args` pydantic
    model, and implement `run(args)`.
    """

    name: str = ""
    description: str = ""
    risk_level: RiskLevel = RiskLevel.SAFE
    # True for tools that spawn a real process / execute code or binaries on
    # the HOST (python_exec, the BinaryTool family). The ApprovalGate refuses
    # to auto-approve these unless they're sandboxed (run in the container, via
    # RemoteTool) or the user explicitly passed --allow-host-exec. This keeps
    # arbitrary host code from running unattended under mode-c / --yolo.
    runs_on_host: bool = False
    # False for tools with no external side effect and no host code execution —
    # purely local reads (e.g. reading a Burp XML export). These bypass the
    # ApprovalGate entirely: prompting for them is
    # pure friction. Network/target-touching and code-executing tools keep this
    # True and are gated normally.
    requires_approval: bool = True
    Args: type[BaseModel]

    @abstractmethod
    async def run(self, args: BaseModel) -> ToolResult: ...

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema for the tool's args, used in the LLM tool catalog."""
        return self.Args.model_json_schema()
