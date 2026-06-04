"""ApprovalGate — the only hard safety boundary in Phase 1.

Every tool call passes through here. The user is prompted unless the matching
auto-approve class is set, with the following resolution order:

  destructive  -> always prompt (no flag can skip; `--yolo` does not cover it)
  exploit      -> prompt unless `--auto-approve=exploit` AND `--i-understand`
  active       -> prompt unless `--yolo` or `--auto-approve=active`
  safe         -> prompt unless `--yolo` or `--auto-approve=safe`
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Awaitable, Callable

from zero.tools.base import RiskLevel


class Decision(str, enum.Enum):
    YES = "yes"
    NO = "no"
    ALWAYS = "always"      # accept and add to session auto-approve
    EDIT = "edit"          # user wants to edit args (UI returns NO with edited args separately)


@dataclass
class ApprovalRequest:
    tool: str
    args: dict
    risk_level: RiskLevel
    call_id: str
    # Attribution for orchestrated mode, e.g. "pentester[#3 /api/orders]".
    label: str = ""
    # Short, human-readable purpose of the call (the agent's `thought`), shown
    # in the approval prompt so the user understands WHY it's being made.
    reason: str = ""
    # True when the tool executes code/binaries on the HOST (not sandboxed).
    # The gate never auto-approves these unless --allow-host-exec is set.
    host_exec: bool = False


# UI prompter: shown an ApprovalRequest, returns the user's Decision.
Prompter = Callable[[ApprovalRequest], Awaitable[Decision]]


class ApprovalGate:
    def __init__(
        self,
        prompter: Prompter,
        *,
        auto_approve: set[str] | None = None,
        yolo: bool = False,
        i_understand: bool = False,
        allow_host_exec: bool = False,
    ) -> None:
        self._prompter = prompter
        self._auto: set[RiskLevel] = set()
        for cls in auto_approve or ():
            self._auto.add(RiskLevel(cls))
        self._yolo = yolo
        self._i_understand = i_understand
        self._allow_host_exec = allow_host_exec
        self._session_always: set[str] = set()  # tool names accepted for the whole session

    def _is_auto_approved(self, req: ApprovalRequest) -> bool:
        if req.tool in self._session_always:
            return True
        # Host execution boundary: a tool that runs code/binaries on the host
        # (not sandboxed in the container) is NEVER auto-approved — not by
        # --yolo, not by mode-c's safe+active default — unless the operator
        # explicitly accepted that risk with --allow-host-exec. This is what
        # stops arbitrary host code from running unattended.
        if req.host_exec and not self._allow_host_exec:
            return False
        rl = req.risk_level
        if rl == RiskLevel.DESTRUCTIVE:
            return False
        if rl == RiskLevel.EXPLOIT:
            return RiskLevel.EXPLOIT in self._auto and self._i_understand
        if rl == RiskLevel.ACTIVE:
            return self._yolo or RiskLevel.ACTIVE in self._auto
        if rl == RiskLevel.SAFE:
            return self._yolo or RiskLevel.SAFE in self._auto
        return False

    async def check(self, req: ApprovalRequest) -> bool:
        """Return True if the call is allowed."""
        if self._is_auto_approved(req):
            return True
        decision = await self._prompter(req)
        if decision == Decision.YES:
            return True
        if decision == Decision.ALWAYS:
            self._session_always.add(req.tool)
            return True
        return False
