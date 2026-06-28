"""The live "running tool" toolbar indicator.

A long tool call (e.g. a 300s ffuf in a batch) used to make the session look hung —
nothing renders between the approval and the observation. ToolRunning now feeds a
live toolbar indicator that ticks via the 0.5s refresh and clears on the paired
ToolObservation (or a cancelled turn)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.formatted_text import to_plain_text

from zero.ui.app import ZeroUI
from zero.ui.events import (
    SessionEnd,
    ToolObservation,
    ToolProposal,
    ToolRunning,
    TurnCancelled,
)


class _StubOrch:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path, "context_window": 0})()
        self.subagents = None

    def request_cancel_turn(self) -> bool:
        return False


def _toolbar_text(ui: ZeroUI) -> str:
    return to_plain_text(ui._toolbar())


async def _pump(ui: ZeroUI, events) -> list[str]:
    captured: list[str] = []
    ui._print = lambda lines: captured.extend(list(lines))  # type: ignore
    for ev in events:
        ui.orch.ui_queue.put_nowait(ev)
    ui.orch.ui_queue.put_nowait(SessionEnd())
    await asyncio.wait_for(ui.pump_events(), timeout=5)
    return captured


async def test_running_tool_shows_in_toolbar_and_not_in_chat(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path))
    captured = await _pump(ui, [
        ToolProposal(tool="ffuf", risk_level="active", call_id="c1"),
        ToolRunning(tool="ffuf", call_id="c1", timeout_seconds=300),
    ])
    # Still running (no observation) → toolbar surfaces it with the timeout cap.
    assert "c1" in ui._running_tools
    bar = _toolbar_text(ui)
    assert "running" in bar and "ffuf" in bar and "/300s" in bar
    # ToolRunning must never reach the chat scrollback.
    assert "running ffuf" not in "\n".join(captured)


async def test_observation_clears_running_indicator(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path))
    await _pump(ui, [
        ToolRunning(tool="ffuf", call_id="c1", timeout_seconds=300),
        ToolObservation(tool="ffuf", call_id="c1", ok=True, output="done"),
    ])
    assert ui._running_tools == {}
    assert "running" not in _toolbar_text(ui)


async def test_cancelled_turn_clears_running_indicators(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path))
    await _pump(ui, [
        ToolRunning(tool="ffuf", call_id="c1", timeout_seconds=300),
        TurnCancelled(),
    ])
    assert ui._running_tools == {}


async def test_labelled_subagent_tool_not_shown_as_top_level_running(tmp_path: Path):
    """Subagent (labelled) tool runs are implied by the animated subagent rows, so
    they are NOT duplicated as top-level running lines."""
    ui = ZeroUI(_StubOrch(tmp_path))
    await _pump(ui, [
        ToolRunning(tool="sqlmap", call_id="c9", label="pentester[#3 /x]", timeout_seconds=120),
    ])
    # Tracked (so the observation can clear it) but absent from the top-level section.
    assert "c9" in ui._running_tools
    assert "running" not in _toolbar_text(ui)
