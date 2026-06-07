"""The approval prompt surfaces a short 'why' (the agent's thought) so the user
understands the purpose of a gated tool call before approving."""
from __future__ import annotations

from pathlib import Path

from zero.orchestrator.approval import Decision
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.app import _approval_lines
from zero.ui.events import ApprovalPrompt
from zero.ui.render import short_reason


async def test_reason_reaches_the_gate(tmp_path: Path, monkeypatch):
    """_run_one_tool forwards `reason` (the envelope thought) into the
    ApprovalRequest the gate's prompter receives."""
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c"))

    captured: dict = {}

    async def fake_prompter(req):
        captured["reason"] = req.reason
        captured["tool"] = req.tool
        return Decision.NO

    orch.gate._prompter = fake_prompter
    await orch._run_one_tool(
        "web_fetch", {"url": "https://example.com/"},
        reason="probing the login form for SQL injection",
    )
    assert captured["tool"] == "web_fetch"
    assert captured["reason"] == "probing the login form for SQL injection"


def test_short_reason_condenses_to_one_clause():
    long = "Confirm reflected XSS in the uid param.  Then I will weaponize it against Carlos."
    assert short_reason(long) == "Confirm reflected XSS in the uid param"
    assert short_reason("") == ""
    assert short_reason("x" * 500).endswith("…")


def test_approval_lines_show_why():
    lines = _approval_lines(
        ApprovalPrompt(
            tool="http_replay", risk_level="exploit",
            reason="Confirm reflected XSS in uid. Then weaponize.",
        )
    )
    joined = "\n".join(lines)
    assert "why: Confirm reflected XSS in uid" in joined
    assert "weaponize" not in joined  # only the first clause is shown


def test_approval_lines_omit_why_when_no_reason():
    lines = _approval_lines(ApprovalPrompt(tool="web_fetch", risk_level="safe"))
    assert not any("why:" in ln for ln in lines)
