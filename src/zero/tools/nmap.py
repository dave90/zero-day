"""nmap wrapper."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class NmapArgs(BaseModel):
    target: str = Field(description="host, IP, CIDR, or range")
    ports: str | None = Field(default=None, description="e.g. '80,443' or '1-1024'")
    flags: list[str] = Field(default_factory=lambda: ["-Pn", "-sT"], description="passthrough flags")
    timeout_seconds: int = Field(default=300, ge=10, le=900)


class NmapTool(BinaryTool):
    name = "nmap"
    description = "Port/service scan. ACTIVE — touches the target."
    risk_level = RiskLevel.ACTIVE
    Args = NmapArgs
    binary = "nmap"

    def args_to_argv(self, args):
        a = list(args.flags)
        if args.ports:
            a += ["-p", args.ports]
        a.append(args.target)
        return a
