"""curl wrapper — full-control raw HTTP."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class CurlRawArgs(BaseModel):
    url: str
    method: str = "GET"
    headers: list[str] = Field(default_factory=list, description="'k: v' strings")
    data: str | None = None
    extra_flags: list[str] = Field(default_factory=list)
    include_headers: bool = True
    insecure: bool = False
    follow_redirects: bool = True
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class CurlRawTool(BinaryTool):
    name = "curl_raw"
    description = (
        "Raw curl. Full control over method/headers/body. ACTIVE."
        " Prefer http_replay for predictable HTTP, this for arcane cases."
    )
    risk_level = RiskLevel.ACTIVE
    Args = CurlRawArgs
    binary = "curl"

    def args_to_argv(self, args):
        a: list[str] = ["-X", args.method, "--max-time", str(args.timeout_seconds)]
        if args.include_headers:
            a.append("-i")
        if args.insecure:
            a.append("-k")
        if args.follow_redirects:
            a.append("-L")
        for h in args.headers:
            a += ["-H", h]
        if args.data is not None:
            a += ["--data-raw", args.data]
        a += args.extra_flags
        a.append(args.url)
        return a
