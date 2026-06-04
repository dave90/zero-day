"""Host-execution hardening: code/binary tools don't run unattended on the host.

External review found that with a safe+active auto-approve policy and no
container, python_exec (ACTIVE) ran arbitrary code on the host with no prompt.
The fix tags host-executing tools and the gate refuses to auto-approve them
unless sandboxed (RemoteTool) or --allow-host-exec is set.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.tools.python_exec import PythonExecTool
from zero.tools.sqlmap import SqlmapTool
from zero.tools.web_fetch import WebFetchTool


def test_tool_host_exec_flags():
    # code/binary execution → host
    assert PythonExecTool().runs_on_host is True
    assert SqlmapTool().runs_on_host is True  # BinaryTool family
    # pure-Python / network tools are not host-code-execution
    assert WebFetchTool().runs_on_host is False


def test_remote_tool_is_not_host_exec():
    from zero.tools.remote import RemoteTool

    rt = RemoteTool(
        name="python_exec", description="d",
        risk_level=PythonExecTool.risk_level, args_model=PythonExecTool.Args, rpc=object(),
    )
    assert rt.runs_on_host is False  # sandboxed in the container


async def test_mode_c_default_does_not_auto_run_python_exec_on_host(tmp_path: Path, monkeypatch):
    """With a safe+active auto-approve policy and no container, python_exec must
    NOT be auto-approved — it goes to the prompter."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c",
        auto_approve={"safe", "active"},   # explicit safe+active policy
        use_container=False,               # host path (--no-container)
    )
    orch = Orchestrator(cfg)

    prompted: list = []

    async def fake_prompter(req):
        from zero.orchestrator.approval import Decision
        prompted.append(req.tool)
        return Decision.NO

    orch.gate._prompter = fake_prompter

    # _run_one_tool builds the ApprovalRequest from the registry tool's
    # runs_on_host flag and consults the gate.
    result = await orch._run_one_tool("python_exec", {"code": "print('x')"})
    assert prompted == ["python_exec"], "python_exec must prompt, not auto-run on host"
    assert result.ok is False  # prompter denied → not executed


def test_local_tools_do_not_require_approval():
    from zero.tools.notes import NotesTool
    from zero.tools.burp_traffic import BurpTrafficTool

    assert NotesTool().requires_approval is False
    assert BurpTrafficTool().requires_approval is False
    assert WebFetchTool().requires_approval is True  # network tool still gated


async def test_notes_and_burp_traffic_bypass_the_gate(tmp_path: Path, monkeypatch):
    """notes + burp_traffic are local/no-side-effect — they must NEVER prompt,
    even with no auto-approve policy and a denying prompter. A network tool
    (web_fetch) still prompts."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c")  # no policy
    orch = Orchestrator(cfg)

    prompted: list = []

    async def fake_prompter(req):
        from zero.orchestrator.approval import Decision
        prompted.append(req.tool)
        return Decision.NO

    orch.gate._prompter = fake_prompter

    r_notes = await orch._run_one_tool("notes", {"action": "read"})
    await orch._run_one_tool("burp_traffic", {"action": "list_hosts"})
    assert prompted == []          # neither local tool prompted
    assert r_notes.ok is True      # notes ran despite the deny-everything prompter

    await orch._run_one_tool("web_fetch", {"url": "https://example.com/"})
    assert "web_fetch" in prompted  # network tool is still gated


async def test_allow_host_exec_lets_python_exec_auto_run(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c",
        auto_approve={"safe", "active"}, allow_host_exec=True, use_container=False,
    )
    orch = Orchestrator(cfg)
    prompted: list = []

    async def fake_prompter(req):
        from zero.orchestrator.approval import Decision
        prompted.append(req.tool)
        return Decision.NO

    orch.gate._prompter = fake_prompter
    result = await orch._run_one_tool("python_exec", {"code": "print('x')"})
    assert prompted == []  # auto-approved (user accepted host risk)
    assert result.ok is True
    assert "x" in result.output
