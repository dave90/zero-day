"""Live orchestrator thinking indicator in the inline toolbar."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prompt_toolkit.formatted_text import to_plain_text

from zero.ui import theme
from zero.ui.app import ZeroUI
from zero.ui.events import SessionEnd, Status, TurnStart


class _StubOrch:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path, "context_window": 0})()
        self.subagents = None

    def request_cancel_turn(self) -> bool:
        return False


async def _wait_until(predicate) -> None:
    for _ in range(20):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


async def test_orchestrator_thinking_animates_until_matching_status(
    tmp_path: Path, monkeypatch
):
    ui = ZeroUI(_StubOrch(tmp_path))
    captured: list[str] = []
    ui._print = lambda lines: captured.extend(list(lines))  # type: ignore[method-assign]
    monkeypatch.setattr(theme, "thinking_dots", lambda: "..")

    pump = asyncio.create_task(ui.pump_events())
    ui.orch.ui_queue.put_nowait(TurnStart(agent="orchestrator", turn=3))
    await _wait_until(lambda: ui._thinking_turn == 3)

    assert "zero · turn 3 · thinking.." in to_plain_text(ui._toolbar())
    # The former static TurnStart line is not left behind in scrollback.
    assert not any("thinking" in line for line in captured)

    # A counter-only status from the preceding turn must not end the animation.
    ui.orch.ui_queue.put_nowait(Status(turn=2, mode="c"))
    await asyncio.sleep(0)
    assert ui._thinking_turn == 3

    ui.orch.ui_queue.put_nowait(Status(turn=3, mode="c"))
    await _wait_until(lambda: ui._thinking_turn is None)
    assert "thinking" not in to_plain_text(ui._toolbar())

    ui.orch.ui_queue.put_nowait(SessionEnd())
    await asyncio.wait_for(pump, timeout=5)


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        ("analyzer", "ralph · analyzer"),
        ("analyzer_validator", "ralph · validator"),
    ],
)
async def test_ralph_thinking_roles_animate(
    tmp_path: Path, monkeypatch, agent: str, label: str
):
    ui = ZeroUI(_StubOrch(tmp_path))
    ui._print = lambda lines: None  # type: ignore[method-assign]
    monkeypatch.setattr(theme, "thinking_dots", lambda: "...")

    pump = asyncio.create_task(ui.pump_events())
    ui.orch.ui_queue.put_nowait(TurnStart(agent=agent, turn=2))
    await _wait_until(lambda: ui._thinking_agent == agent and ui._thinking_turn == 2)

    assert f"{label} · turn 2 · thinking..." in to_plain_text(ui._toolbar())

    ui.orch.ui_queue.put_nowait(Status(turn=2, mode="ralph"))
    await _wait_until(lambda: ui._thinking_turn is None)
    assert "thinking" not in to_plain_text(ui._toolbar())

    ui.orch.ui_queue.put_nowait(SessionEnd())
    await asyncio.wait_for(pump, timeout=5)
