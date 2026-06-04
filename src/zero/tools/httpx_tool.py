"""httpx-toolkit wrapper (projectdiscovery HTTP probe, distinct from Python httpx)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class HttpxToolArgs(BaseModel):
    target: str = Field(description="single host/URL")
    follow_redirects: bool = True
    status_code: bool = True
    title: bool = True
    tech_detect: bool = True
    web_server: bool = True
    extra_flags: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=60, ge=5, le=600)


class HttpxTool(BinaryTool):
    name = "httpx_tool"
    description = (
        "ProjectDiscovery httpx — fast HTTP probe (status/title/tech). ACTIVE."
        " (Distinct from the Python httpx library used internally.)"
    )
    risk_level = RiskLevel.ACTIVE
    Args = HttpxToolArgs
    binary = "httpx"  # the project ships as `httpx` on PATH (Kali aliases httpx-toolkit)

    def args_to_argv(self, args):
        a = ["-u", args.target]
        if args.follow_redirects:
            a.append("-fr")
        if args.status_code:
            a.append("-sc")
        if args.title:
            a.append("-title")
        if args.tech_detect:
            a.append("-td")
        if args.web_server:
            a.append("-server")
        a += args.extra_flags
        return a
