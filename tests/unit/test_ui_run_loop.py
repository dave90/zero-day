"""Integration test for the inline UI run loop.

Drives ZeroUI.run() headlessly via a prompt_toolkit pipe input + DummyOutput
(no TTY). Exercises the real loop: patch_stdout, prompt_async, line dispatch,
and `/exit`. The approval/ask state machine is covered separately by
test_ui_inline.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput


class _StubOrch:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path})()
        self.texts: list[str] = []

    def submit_user_text(self, text) -> None:
        self.texts.append(text)

    def submit_approval(self, *a) -> None:  # pragma: no cover - unused here
        pass

    def submit_ask_user_answer(self, *a) -> None:  # pragma: no cover
        pass

    def request_cancel_turn(self) -> bool:  # pragma: no cover
        return False


async def test_run_loop_delivers_user_text_then_exits(tmp_path: Path):
    from zero.ui.app import ZeroUI

    stub = _StubOrch(tmp_path)
    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()):
            ui = ZeroUI(stub)
            # A normal message, then /exit. \r is the Enter key.
            inp.send_text("hello world\r/exit\r")
            await asyncio.wait_for(ui.run(), timeout=10)

    assert stub.texts == ["hello world"]
