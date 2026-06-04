"""HTTP GET + readable-text extraction, with a simple in-scope block.

Refuses to fetch URLs whose host is in `blocked_hosts` (typically the user's
in-scope pentest target hosts) — those should be touched through `http_replay`
or `httpx_tool` so they pass through the approval gate as ACTIVE.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult

DEFAULT_MAX_BYTES = 200_000
DEFAULT_TIMEOUT = 15.0


class WebFetchArgs(BaseModel):
    url: str = Field(description="absolute http/https URL")
    extract_text: bool = Field(default=True, description="strip boilerplate with trafilatura")


class WebFetchTool(Tool):
    name = "web_fetch"
    description = "GET a public web page and return its readable text. Out-of-scope use only."
    risk_level = RiskLevel.SAFE
    Args = WebFetchArgs

    def __init__(
        self,
        blocked_hosts: set[str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.blocked_hosts = {h.lower() for h in (blocked_hosts or set())}
        self.max_bytes = max_bytes
        self.timeout = timeout

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, WebFetchArgs)
        parsed = urlparse(args.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ToolResult(ok=False, output="url must be absolute http(s)")
        host = parsed.hostname.lower()
        if host in self.blocked_hosts:
            return ToolResult(
                ok=False,
                output=(
                    f"refused: {host} is an in-scope target; use http_replay or httpx_tool"
                    " so the call passes through the approval gate."
                ),
            )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                resp = await client.get(args.url)
        except httpx.HTTPError as e:
            return ToolResult(ok=False, output=f"http error: {e}")

        body = resp.content[: self.max_bytes]
        truncated = len(resp.content) > self.max_bytes
        if args.extract_text:
            text = _extract_text(body) or body.decode("utf-8", errors="replace")
        else:
            text = body.decode("utf-8", errors="replace")
        header = f"GET {resp.url} -> {resp.status_code} ({len(resp.content)} bytes)\n"
        return ToolResult(ok=resp.is_success, output=header + text, truncated=truncated)


def _extract_text(body: bytes) -> str | None:
    try:
        import trafilatura  # type: ignore[import-not-found]

        return trafilatura.extract(body.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
