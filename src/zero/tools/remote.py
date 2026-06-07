"""RemoteTool: proxy a tool call through the container's RPC server.

Keeps the host-side Tool ABC so the orchestrator + registry are unchanged.
The wrapped tool carries the same `Args` schema as the underlying tool, so
JSON-Schema generation in the LLM tool catalog stays correct.
"""
from __future__ import annotations

from typing import Type

from pydantic import BaseModel

from zero.container.rpc import ContainerRPC
from zero.tools.base import RiskLevel, Tool, ToolResult


class RemoteTool(Tool):
    """Proxy for a tool that lives inside the sandbox.

    Construct with the same metadata as the local tool it replaces; calls
    are forwarded over RPC to the in-container implementation of the same
    name.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        risk_level: RiskLevel,
        args_model: Type[BaseModel],
        rpc: ContainerRPC,
        requires_approval: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.risk_level = risk_level
        self.Args = args_model
        self._rpc = rpc
        # Preserve the local tool's approval policy. A remoted tool runs in the
        # sandbox (runs_on_host stays False), but a no-approval local tool
        # (burp_traffic) must STILL bypass the gate once remoted.
        self.requires_approval = requires_approval

    async def run(self, args: BaseModel) -> ToolResult:
        return await self._rpc.call(self.name, args.model_dump(mode="json"))


def wrap_with_remote(local: Tool, rpc: ContainerRPC) -> RemoteTool:
    """Return a RemoteTool with the same name/schema/risk/approval as `local`."""
    return RemoteTool(
        name=local.name,
        description=local.description,
        risk_level=local.risk_level,
        args_model=local.Args,
        rpc=rpc,
        requires_approval=local.requires_approval,
    )
