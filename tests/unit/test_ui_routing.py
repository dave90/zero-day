"""What reaches the chat in orchestrated mode (M8 UX).

Shown: orchestrator thinking + its own recon tool calls + finding cards.
Hidden: subagent (labelled) tool traffic, subagent lifecycle events, and the
big end-of-run summary card (replaced by a one-line report pointer).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from zero.ui.app import ZeroUI
from zero.ui.events import (
    AgentTextDelta,
    AgentTextEnd,
    Finding,
    ReasoningSummary,
    SessionEnd,
    SessionSummary,
    SubagentStarted,
    TodoUpdate,
    ToolObservation,
    ToolProposal,
)


class _StubOrch:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path})()
        self.subagents = None

    def request_cancel_turn(self) -> bool:
        return False


async def test_chat_routing_shows_orchestrator_hides_subagents(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path))
    captured: list[str] = []
    ui._print = lambda lines: captured.extend(list(lines))  # type: ignore

    for ev in [
        AgentTextDelta(text="I'll test SQLi on /api/orders", agent="orchestrator"),
        AgentTextEnd(),
        ReasoningSummary(text="Mapping auth + endpoints", agent="orchestrator"),
        ReasoningSummary(text="probing sqli", label="pentester[#3 /api/orders]"),
        TodoUpdate(items=[{"step": "map endpoints", "status": "in_progress"}]),
        TodoUpdate(items=[{"step": "hidden subagent step"}], label="pentester[#3 /api/orders]"),
        ToolProposal(tool="sqlmap", risk_level="exploit", label="pentester[#3 /api/orders]"),
        ToolObservation(tool="sqlmap", ok=True, output="dumped", label="pentester[#3 /api/orders]"),
        SubagentStarted(subagent_id="#3", subkind="pentester", target="/api/orders", idea="SQLi"),
        ToolProposal(tool="web_fetch", risk_level="active", label=""),  # orchestrator recon
        Finding(verdict="confirmed", title="SQLi in orders", finding_id="abc"),
        SessionSummary(confirmed=[{"id": "abc", "title": "SQLi in orders"}], report_path=str(tmp_path / "report.md")),
        SessionEnd(),
    ]:
        ui.orch.ui_queue.put_nowait(ev)

    await asyncio.wait_for(ui.pump_events(), timeout=5)
    blob = "\n".join(captured)

    # orchestrator thinking + its own recon are shown
    assert "I'll test SQLi on /api/orders" in blob
    assert "Mapping auth + endpoints" in blob   # orchestrator reasoning summary shown
    assert "probing sqli" not in blob           # subagent (labelled) reasoning hidden
    assert "map endpoints" in blob              # orchestrator todo shown
    assert "hidden subagent step" not in blob   # subagent (labelled) todo hidden
    assert "web_fetch" in blob
    # finding card is shown (it's a result, not a log)
    assert "SQLi in orders" in blob
    # subagent logs are NOT shown
    assert "sqlmap" not in blob
    assert "dumped" not in blob
    # subagent lifecycle is not dumped as a chat line
    assert "started — /api/orders" not in blob
    # the report IS shown in the chat at finalize, and the session stays open
    assert "ENGAGEMENT REPORT" in blob
    assert "engagement still open" in blob
    assert "report.md" in blob


async def test_concurrent_approvals_are_queued_one_at_a_time(tmp_path: Path):
    from zero.orchestrator.approval import Decision
    from zero.ui.events import ApprovalPrompt

    approvals: list = []
    ui = ZeroUI(_StubOrch(tmp_path))
    ui.orch.submit_approval = lambda call_id, decision: approvals.append((call_id, decision))  # type: ignore

    # Two subagent approvals arrive back-to-back.
    ui._enqueue_interaction(ApprovalPrompt(tool="sqlmap", call_id="c1", label="pentester[#1 /a]"))
    ui._enqueue_interaction(ApprovalPrompt(tool="nuclei", call_id="c2", label="pentester[#2 /b]"))

    # Only the first is shown; the second is queued (not lost).
    assert ui._pending_approval is not None and ui._pending_approval.call_id == "c1"
    assert len(ui._interaction_queue) == 1

    # Answer the first → the second becomes current.
    ui._submit_approval(Decision.YES)
    assert approvals == [("c1", Decision.YES)]
    assert ui._pending_approval is not None and ui._pending_approval.call_id == "c2"
    assert ui._interaction_queue == []

    ui._submit_approval(Decision.NO)
    assert approvals == [("c1", Decision.YES), ("c2", Decision.NO)]
    assert ui._pending_approval is None
