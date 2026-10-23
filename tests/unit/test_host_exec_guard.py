"""Host-execution hardening: code/binary tools don't run unattended on the host.

External review found that with a safe+active auto-approve policy and no
container, python_exec (ACTIVE) ran arbitrary code on the host with no prompt.
The fix tags host-executing tools and the gate refuses to auto-approve them
unless sandboxed (RemoteTool) or --allow-host-exec is set.
"""
from __future__ import annotations

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
    from zero.tools.burp_traffic import BurpTrafficTool

    assert BurpTrafficTool().requires_approval is False
    assert WebFetchTool().requires_approval is True  # network tool still gated


async def test_burp_traffic_bypasses_the_gate(tmp_path: Path, monkeypatch):
    """burp_traffic is a local/no-side-effect read — it must NEVER prompt, even
    with no auto-approve policy and a denying prompter. A network tool
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

    r_burp = await orch._run_one_tool("burp_traffic", {"action": "list_hosts"})
    assert prompted == []          # the local tool did not prompt
    # It actually ran (bypassed the deny-everything prompter) rather than being
    # blocked — the output is the tool's own result, not a denial.
    assert r_burp.output != "user denied the tool call"

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


async def test_session_always_never_bypasses_host_or_destructive_boundaries():
    from zero.orchestrator.approval import ApprovalGate, ApprovalRequest, Decision
    from zero.tools.base import RiskLevel

    decisions = iter([Decision.ALWAYS, Decision.NO, Decision.NO])

    async def prompt(_req):
        return next(decisions)

    gate = ApprovalGate(prompt)
    safe = ApprovalRequest("same", {}, RiskLevel.SAFE, "1")
    assert await gate.check(safe)
    assert not await gate.check(ApprovalRequest(
        "same", {}, RiskLevel.SAFE, "2", host_exec=True
    ))
    assert not await gate.check(ApprovalRequest(
        "same", {}, RiskLevel.DESTRUCTIVE, "3"
    ))
