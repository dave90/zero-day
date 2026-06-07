"""A tool that raises must become a clean failed observation, never an unhandled
exception that crashes the caller.

Regression (Blueprint_Heist): a NUL byte in a payload reached a subprocess/path
call and raised `ValueError: embedded null byte` inside the tool's run(); that
propagated out of `_invoke_tool` and silently killed the whole subagent
(status=ERROR) instead of letting the agent see the error and adapt.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.tools.base import RiskLevel, Tool, ToolResult


class _Empty(BaseModel):
    pass


class _NulRaisingTool(Tool):
    name = "boom"
    description = "simulates a NUL-in-subprocess ValueError"
    risk_level = RiskLevel.ACTIVE
    Args = _Empty

    async def run(self, args) -> ToolResult:  # noqa: D401
        raise ValueError("embedded null byte")


class _NulOutputTool(Tool):
    name = "nul_out"
    description = "returns output containing raw NUL bytes (e.g. a binary body)"
    risk_level = RiskLevel.ACTIVE
    Args = _Empty

    async def run(self, args) -> ToolResult:
        return ToolResult(ok=True, output="HTTP/1.1 200\r\n\r\n\x00\x00font-bytes\x00here")


async def test_tool_exception_becomes_failed_observation(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(
        OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c", use_container=False)
    )
    orch.registry.register(_NulRaisingTool())

    result = await orch._invoke_tool(orch.registry.get("boom"), {})

    assert result.ok is False
    assert "embedded null byte" in result.output
    # The raised type is surfaced for debuggability.
    assert "ValueError" in result.output


async def test_tool_output_nul_is_neutralized(tmp_path: Path, monkeypatch):
    """Regression (Blueprint_Heist): a tool that returns output WITH NUL bytes
    (binary HTTP body / font / null-byte reflection) must have them neutralized
    before the output enters the transcript — otherwise they flow into the next
    turn's `claude -p` argv and raise 'embedded null byte' at spawn, killing the
    subagent. The bytes are replaced with a visible, count-preserving marker (not
    silently dropped — the presence of nulls can itself be the finding)."""
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(
        OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c", use_container=False)
    )
    orch.registry.register(_NulOutputTool())

    result = await orch._invoke_tool(orch.registry.get("nul_out"), {})

    assert result.ok is True
    assert "\x00" not in result.output          # no raw NUL (would crash the next spawn)
    assert "[NUL*2]" in result.output           # the \x00\x00 run is marked with its count
    assert "font-bytes" in result.output         # surrounding content preserved
