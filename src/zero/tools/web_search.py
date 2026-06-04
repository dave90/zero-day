"""DuckDuckGo web search via the `ddgs` library — no API key."""
from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult


class WebSearchArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web with DuckDuckGo. Returns title / url / snippet per result."
    risk_level = RiskLevel.SAFE
    Args = WebSearchArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, WebSearchArgs)
        try:
            results = await asyncio.to_thread(self._search, args.query, args.max_results)
        except Exception as e:  # noqa: BLE001
            return ToolResult(ok=False, output=f"search failed: {e}")
        if not results:
            return ToolResult(ok=True, output="(no results)")
        lines = []
        for i, r in enumerate(results, start=1):
            title = r.get("title", "")
            url = r.get("href") or r.get("url", "")
            body = r.get("body", "")
            lines.append(f"{i}. {title}\n   {url}\n   {body}")
        return ToolResult(ok=True, output="\n".join(lines))

    @staticmethod
    def _search(query: str, n: int) -> list[dict[str, Any]]:
        from ddgs import DDGS  # type: ignore[import-not-found]

        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=n))
