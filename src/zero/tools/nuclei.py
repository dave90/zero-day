"""nuclei wrapper (template-driven vuln scan)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class NucleiArgs(BaseModel):
    target: str = Field(description="single target URL or comma-separated list")
    templates: list[str] = Field(default_factory=list, description="-t paths or template IDs")
    tags: list[str] = Field(default_factory=list, description="-tags ")
    severity: str | None = Field(default=None, description="-severity, e.g. 'high,critical'")
    rate_limit: int = Field(default=100, ge=1, le=10000, description="reqs/sec")
    extra_flags: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=10, le=900)


class NucleiTool(BinaryTool):
    name = "nuclei"
    description = "Run nuclei templates against a target. ACTIVE."
    risk_level = RiskLevel.ACTIVE
    Args = NucleiArgs
    binary = "nuclei"

    def args_to_argv(self, args):
        a = ["-u", args.target, "-rl", str(args.rate_limit)]
        for t in args.templates:
            a += ["-t", t]
        if args.tags:
            a += ["-tags", ",".join(args.tags)]
        if args.severity:
            a += ["-severity", args.severity]
        a += args.extra_flags
        # Non-interactive defaults.
        if "-silent" not in a and "-jsonl" not in a:
            a.append("-silent")
        return a
