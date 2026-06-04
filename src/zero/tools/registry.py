"""Global tool registry."""
from __future__ import annotations

from typing import Iterator

from zero.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool.name must be set")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def replace(self, tool: Tool) -> Tool:
        """Replace an already-registered tool with the same name."""
        if tool.name not in self._tools:
            raise KeyError(f"no such tool to replace: {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def catalog(self) -> list[dict]:
        """LLM-facing description of every tool."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "risk_level": t.risk_level.value,
                "schema": t.json_schema(),
            }
            for t in self._tools.values()
        ]


def default_registry() -> ToolRegistry:
    """Registry pre-populated with the M2 + M5 tool surface."""
    from zero.tools.burp_traffic import BurpTrafficTool
    from zero.tools.curl_raw import CurlRawTool
    from zero.tools.ffuf import FfufTool
    from zero.tools.finding_submit import FindingSubmitTool
    from zero.tools.http_replay import HttpReplayTool
    from zero.tools.httpx_tool import HttpxTool
    from zero.tools.jwt_tool import JwtTool
    from zero.tools.nmap import NmapTool
    from zero.tools.notes import NotesTool
    from zero.tools.nuclei import NucleiTool
    from zero.tools.python_exec import PythonExecTool
    from zero.tools.sqlmap import SqlmapTool
    from zero.tools.web_fetch import WebFetchTool
    from zero.tools.web_search import WebSearchTool

    reg = ToolRegistry()
    # M2 host-safe tools
    reg.register(NotesTool())
    reg.register(WebSearchTool())
    reg.register(WebFetchTool())
    reg.register(PythonExecTool())
    # M5 traffic + binary tools
    reg.register(BurpTrafficTool())
    reg.register(HttpReplayTool())
    reg.register(SqlmapTool())
    reg.register(NmapTool())
    reg.register(FfufTool())
    reg.register(NucleiTool())
    reg.register(HttpxTool())
    reg.register(JwtTool())
    reg.register(CurlRawTool())
    # M6: finding submission (orchestrator-intercepted).
    reg.register(FindingSubmitTool())
    return reg
