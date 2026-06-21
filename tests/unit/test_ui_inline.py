"""Inline UI (PromptSession) regression tests.

The UI renders inline like Claude Code (no full-screen alt-screen, no overlay
windows). These build a real ZeroUI with a stub orchestrator and exercise the
approval / ask_user state machine + slash dispatch without a TTY — so a
missing import or a broken handler surfaces in `pytest`, not in a real run.

ZeroUI.__init__ builds a PromptSession but does NOT touch the terminal (that
only happens in .run()/.prompt_async()), so construction is test-safe.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.formatted_text import fragment_list_to_text

from zero.orchestrator.approval import Decision
from zero.ui.app import ZeroUI
from zero.ui.events import ApprovalPrompt, AskUser


def _msg(ui) -> str:
    """The prompt message is styled FormattedText now — extract its plain text."""
    return fragment_list_to_text(ui._prompt_message())


class _StubOrch:
    def __init__(self, tmp_path: Path) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path})()
        self._approvals: list = []
        self._asks: list = []
        self._user_texts: list = []

    def submit_approval(self, call_id, decision) -> None:
        self._approvals.append((call_id, decision))

    def submit_ask_user_answer(self, ask_id, answer) -> None:
        self._asks.append((ask_id, answer))

    def submit_user_text(self, text) -> None:
        self._user_texts.append(text)

    def request_cancel_turn(self) -> bool:
        return False


def _make_ui(tmp_path: Path) -> tuple[ZeroUI, _StubOrch]:
    stub = _StubOrch(tmp_path)
    return ZeroUI(stub), stub


def test_construction_does_not_crash(tmp_path: Path):
    """Guards against import/init regressions (e.g. a removed prompt_toolkit
    symbol still referenced)."""
    ui, _ = _make_ui(tmp_path)
    assert ui._pending_approval is None
    assert ui._pending_ask is None
    # The prompt message reflects pending state. (It now starts with a violet
    # rule + newline above the input — the Claude-style bar — then the "› ".)
    assert _msg(ui).endswith("› ")


def test_approval_single_key_yes(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._pending_approval = ApprovalPrompt(tool="sqlmap", risk_level="exploit", call_id="c-1")
    # The single-key binding routes through _submit_approval.
    ui._submit_approval(Decision.YES)
    assert stub._approvals == [("c-1", Decision.YES)]
    assert ui._pending_approval is None


def test_approval_via_typed_line(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._pending_approval = ApprovalPrompt(tool="t", risk_level="safe", call_id="c-2")
    ui._dispatch_line("a")  # Enter-fallback path
    assert stub._approvals == [("c-2", Decision.ALWAYS)]
    assert ui._pending_approval is None
    # The pending approval consumed the line — it was NOT sent as user text.
    assert stub._user_texts == []


def test_empty_or_unknown_line_denies_approval(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._pending_approval = ApprovalPrompt(tool="t", risk_level="safe", call_id="c-3")
    ui._dispatch_line("")  # deny, never deadlock the agent
    assert stub._approvals == [("c-3", Decision.NO)]


def test_prompt_message_changes_with_pending_state(tmp_path: Path):
    ui, _ = _make_ui(tmp_path)
    ui._pending_approval = ApprovalPrompt(tool="t", risk_level="safe", call_id="c")
    assert "approve?" in _msg(ui)
    ui._pending_approval = None
    ui._pending_ask = AskUser(question="q", options=[], ask_id="a")
    assert "answer" in _msg(ui)


def test_ask_digit_selects_option(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._pending_ask = AskUser(
        question="which scope?", options=["auth", "orders"], ask_id="a-1"
    )
    ui._answer_ask("2")
    assert stub._asks == [("a-1", "orders")]
    assert ui._pending_ask is None


def test_ask_free_form_passes_text_through(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._pending_ask = AskUser(question="q", options=["a", "b"], ask_id="a-2")
    ui._dispatch_line("something custom")
    assert stub._asks == [("a-2", "something custom")]
    assert ui._pending_ask is None


def test_slash_command_not_sent_to_agent(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._dispatch_line("/help")
    assert stub._user_texts == []  # consumed as a command


def test_plain_text_goes_to_agent(tmp_path: Path):
    ui, stub = _make_ui(tmp_path)
    ui._dispatch_line("test the login form")
    assert stub._user_texts == ["test the login form"]
