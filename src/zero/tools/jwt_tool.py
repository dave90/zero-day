"""jwt_tool wrapper — JWT scan/manipulate."""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel
from zero.tools.binary import BinaryTool


class JwtToolArgs(BaseModel):
    token: str
    mode: str = Field(default="-S", description="jwt_tool mode flag, e.g. '-S' scan, '-T' tamper")
    extra_flags: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=60, ge=5, le=600)


class JwtTool(BinaryTool):
    name = "jwt_tool"
    description = "jwt_tool — analyze/tamper JWTs. ACTIVE."
    risk_level = RiskLevel.ACTIVE
    Args = JwtToolArgs
    binary = "jwt_tool"

    def args_to_argv(self, args):
        a = [args.token, args.mode]
        a += args.extra_flags
        return a
