"""ffuf wrapper (fuzz endpoints / params)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class FfufArgs(BaseModel):
    url: str = Field(description="target with FUZZ placeholder, e.g. http://host/FUZZ")
    wordlist: str = Field(description="path to wordlist (in-container)")
    method: str = "GET"
    data: str | None = None
    headers: list[str] = Field(default_factory=list, description="extra headers, 'k: v'")
    extensions: str | None = Field(default=None, description="e.g. '.php,.bak'")
    filter_status: str | None = Field(default=None, description="-fc, e.g. '404,403'")
    match_status: str | None = Field(default=None, description="-mc, e.g. '200,301'")
    threads: int = Field(default=20, ge=1, le=200)
    rate_limit: int | None = Field(default=None, description="reqs/sec cap")
    extra_flags: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=10, le=900)


class FfufTool(BinaryTool):
    name = "ffuf"
    description = "Fuzz URL paths / parameters with a wordlist. ACTIVE."
    risk_level = RiskLevel.ACTIVE
    Args = FfufArgs
    binary = "ffuf"

    def args_to_argv(self, args):
        a = ["-u", args.url, "-w", args.wordlist, "-X", args.method, "-t", str(args.threads)]
        if args.data:
            a += ["-d", args.data]
        for h in args.headers:
            a += ["-H", h]
        if args.extensions:
            a += ["-e", args.extensions]
        if args.filter_status:
            a += ["-fc", args.filter_status]
        if args.match_status:
            a += ["-mc", args.match_status]
        if args.rate_limit:
            a += ["-rate", str(args.rate_limit)]
        a += args.extra_flags
        # Non-interactive: don't ask before overwriting output, no colors.
        if "-noninteractive" not in a:
            a.append("-noninteractive")
        return a
