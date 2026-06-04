"""sqlmap wrapper. Returns stdout+stderr; the agent reads the verdict text."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class SqlmapArgs(BaseModel):
    url: str = Field(description="target URL, e.g. http://host/path?id=1")
    data: str | None = Field(default=None, description="POST body, if any")
    cookie: str | None = None
    headers: list[str] = Field(default_factory=list, description="extra headers, 'k: v' form")
    method: str | None = None
    level: int = Field(default=1, ge=1, le=5)
    risk: int = Field(default=1, ge=1, le=3)
    technique: str | None = Field(default=None, description="e.g. 'BEUSTQ'")
    dbms: str | None = None
    extra_flags: list[str] = Field(
        default_factory=list,
        description="passthrough flags, e.g. ['--dump','--batch']",
    )
    timeout_seconds: int = Field(default=300, ge=10, le=900)


class SqlmapTool(BinaryTool):
    name = "sqlmap"
    description = (
        "Run sqlmap against a URL/form. EXPLOIT-class — proves SQL injection."
        " Always pass --batch via extra_flags or sqlmap will prompt interactively."
    )
    risk_level = RiskLevel.EXPLOIT
    Args = SqlmapArgs
    binary = "sqlmap"

    def args_to_argv(self, args):
        a: list[str] = ["-u", args.url, f"--level={args.level}", f"--risk={args.risk}"]
        if args.data:
            a += ["--data", args.data]
        if args.cookie:
            a += ["--cookie", args.cookie]
        for h in args.headers:
            a += ["--header", h]
        if args.method:
            a += ["--method", args.method]
        if args.technique:
            a += [f"--technique={args.technique}"]
        if args.dbms:
            a += [f"--dbms={args.dbms}"]
        a += args.extra_flags
        # If the agent forgets --batch, force it so the tool doesn't hang.
        if "--batch" not in a:
            a.append("--batch")
        return a
