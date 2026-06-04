"""http_replay — predictable HTTP requests via Python httpx.

The orchestrator's primary 'touch the target' tool. Carries the same risk
class as curl_raw / sqlmap. Returns a structured-looking summary plus a
truncated body.
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult


DEFAULT_BODY_LIMIT = 50_000


class HttpReplayArgs(BaseModel):
    method: str = Field(default="GET", description="HTTP method")
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    json_body: dict | list | None = Field(
        default=None, description="if set, sent as JSON (overrides body)"
    )
    follow_redirects: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    body_limit: int = Field(default=DEFAULT_BODY_LIMIT, ge=1024, le=500_000)


class HttpReplayTool(Tool):
    name = "http_replay"
    description = (
        "Send an arbitrary HTTP request and return status + headers + body."
        " ACTIVE / EXPLOIT — touches the target. Use for IDOR/auth tests, etc."
    )
    risk_level = RiskLevel.EXPLOIT  # write-method replays can prove exploitation
    Args = HttpReplayArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, HttpReplayArgs)
        kwargs: dict = {"headers": args.headers}
        if args.json_body is not None:
            kwargs["json"] = args.json_body
        elif args.body is not None:
            kwargs["content"] = args.body

        try:
            async with httpx.AsyncClient(
                timeout=args.timeout_seconds, follow_redirects=args.follow_redirects
            ) as client:
                resp = await client.request(args.method.upper(), args.url, **kwargs)
        except httpx.HTTPError as e:
            return ToolResult(ok=False, output=f"http error: {e}")

        body_bytes = resp.content
        truncated = len(body_bytes) > args.body_limit
        body_view = body_bytes[: args.body_limit].decode("utf-8", "replace")

        lines = [
            f"{args.method.upper()} {resp.url} → {resp.status_code} {resp.reason_phrase}",
            "--- response headers ---",
            *[f"{k}: {v}" for k, v in resp.headers.items()],
            "--- response body ---",
            body_view,
        ]
        if truncated:
            lines.append(f"… (truncated; {len(body_bytes) - args.body_limit} more bytes)")
        return ToolResult(
            ok=True,
            output="\n".join(lines),
            truncated=truncated,
            meta={"status": resp.status_code, "bytes": len(body_bytes)},
        )
