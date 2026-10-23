"""The live CLI must make swarm execution visibly distinct from mode c."""
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.formatted_text import to_plain_text

from zero.ui.app import ZeroUI
from zero.ui.events import Status


class _Runtime:
    workers = []

    @staticmethod
    def ui_snapshot():
        return {
            "phase": "bootstrap",
            "size": 10,
            "active": 8,
            "bootstrapped": 4,
            "contributions": 1,
            "messages": 23,
            "work_total": 12,
            "work_done": 3,
        }


class _Orchestrator:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path, "context_window": 0})()
        self.subagents = None
        self._swarm_runtime = _Runtime()

    def request_cancel_turn(self) -> bool:
        return False


def test_swarm_footer_shows_live_coordination_counts(tmp_path: Path):
    ui = ZeroUI(_Orchestrator(tmp_path))
    ui._status = Status(mode="swarm")

    footer = to_plain_text(ui._toolbar())

    assert "SWARM 10" in footer
    assert "bootstrap 4/10" in footer
    assert "active 8" in footer
    assert "work 3/12" in footer
    assert "msg 23" in footer
