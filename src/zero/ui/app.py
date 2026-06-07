"""Inline interactive shell — Claude-Code style.

Unlike a full-screen TUI, this renders INLINE in the terminal's normal
scrollback: the transcript is printed to stdout (so the native mouse wheel
scrolls it and native click-drag selection / copy works), and only a small
input prompt + a status toolbar are redrawn at the bottom. Built on
prompt_toolkit's `PromptSession` + `patch_stdout()` — no alternate screen.

Approval and ask_user prompts appear inline in the same stream (no overlay
windows): press y / n / a (or 1-9 for an option) for a one-key answer, or
type the answer and press Enter. Single Ctrl+C cancels the current agent
turn; a second within 2 s exits. Enter submits.

Slash commands are handled here without going through the LLM.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear

from zero.orchestrator.approval import Decision
from zero.ui.commands import build_default_registry
from zero.ui.events import (
    ActivityLine,
    ApprovalPrompt,
    AskUser,
    SessionEnd,
    SessionSummary,
    Status,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    ToolObservation,
    ToolProposal,
)
from zero.ui.render import render_event, short_reason, status_line

if TYPE_CHECKING:
    from zero.orchestrator.orchestrator import Orchestrator


_DOUBLE_CTRLC_WINDOW_SEC = 2.0


class ZeroUI:
    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch
        self._status = Status()
        self._pending_approval: ApprovalPrompt | None = None
        self._pending_ask: AskUser | None = None
        # Concurrent subagents can raise several approvals at once. We show one
        # at a time (queue the rest) and BUFFER chat output while any prompt is
        # up, so the prompt stays put instead of scrolling away.
        self._interaction_queue: list = []
        self._output_buffer: list[str] = []
        # Live subagent list navigation (↑/↓ select, Enter expand/collapse).
        self._sel_idx = 0
        self._sel_expanded = False
        self._last_ctrlc: float = 0.0
        self._exit = False

        self._commands = build_default_registry(
            findings_dir=Path.cwd() / ".pentest" / "findings",
            clear_scrollback=self._clear_scrollback,
            request_exit=self._request_exit,
            running_snapshot=self._running_lines,
        )
        # PromptSession construction does NOT touch the TTY (that happens in
        # prompt_async), so this is safe to build in tests too.
        self._session: PromptSession = PromptSession(
            message=self._prompt_message,
            bottom_toolbar=self._toolbar,
            key_bindings=self._build_key_bindings(),
            multiline=False,
            refresh_interval=0.5,  # keep the toolbar (token/turn counts) live
        )

    # --------------------------------------------------------- prompt chrome

    def _prompt_message(self) -> str:
        if self._pending_approval is not None:
            return "approve? [y/n/a] › "
        if self._pending_ask is not None:
            return "answer › "
        return "› "

    def _running_snapshot(self) -> list:
        subs = getattr(self.orch, "subagents", None)
        return subs.snapshot() if subs is not None else []

    def _term_width(self) -> int:
        try:
            cols = self._session.app.output.get_size().columns
            if cols and cols > 0:
                return cols
        except Exception:  # noqa: BLE001
            pass
        import shutil

        return shutil.get_terminal_size((100, 24)).columns

    @staticmethod
    def _short(text: str, n: int) -> str:
        return text if len(text) <= n else "…" + text[-(n - 1):]

    def _wrap_field(self, label: str, text: str, width: int) -> list[str]:
        import textwrap

        indent = "      "  # continuation indent under "    ↳ "
        head = f"    ↳ {label}: "
        avail = max(20, width - len(head))
        wrapped = textwrap.wrap(text, avail) or [""]
        out = [head + wrapped[0]]
        out.extend(indent + w for w in wrapped[1:])
        return out

    def _toolbar(self) -> str:
        """Live bottom area: each running subagent with its goal + elapsed
        time (Claude-Code style), above the status line. Subagent *logs* never
        hit the chat — only this live view + the status counters.

        ↑/↓ select a row; Enter expands the selected subagent's full
        description (word-wrapped to the terminal width)."""
        lines: list[str] = []
        snap = self._running_snapshot()
        if snap:
            if self._sel_idx >= len(snap):
                self._sel_idx = len(snap) - 1
            width = self._term_width()
            for i, s in enumerate(snap[:8]):
                marker = "›" if i == self._sel_idx else " "
                # Long gruyere-style targets would eat the row — keep the tail.
                tgt = self._short(s["target"], 28)
                idea = self._short(s["idea"], 44)
                lines.append(
                    f"{marker}⏳ {s['id']} {s['kind']} {tgt} — {idea}  ({s['elapsed']:.0f}s)"
                )
                if i == self._sel_idx and self._sel_expanded:
                    lines.extend(self._wrap_field("goal", s["idea"], width))
                    lines.extend(self._wrap_field("target", s["target"], width))
                    lines.append(
                        f"      turn {s['turn']} · {s['status']} · {s['elapsed']:.0f}s"
                    )
            if len(snap) > 8:
                lines.append(f"  … +{len(snap) - 8} more running")
            lines.append(" [↑/↓ select · Enter expand/collapse]")
        else:
            self._sel_idx = 0
            self._sel_expanded = False
        lines.append(status_line(self._status))
        return "\n".join(lines)

    # ------------------------------------------------------------ key binds

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        def _buffer_empty() -> bool:
            return len(get_app().current_buffer.text) == 0

        # Single-key answers, active only while a prompt is pending AND the
        # user hasn't started typing a longer answer (so a literal 'y' inside
        # free-form text isn't hijacked — they can always type + Enter).
        approval_active = Condition(
            lambda: self._pending_approval is not None and _buffer_empty()
        )
        ask_active = Condition(
            lambda: self._pending_ask is not None and _buffer_empty()
        )

        for key, decision in (("y", Decision.YES), ("n", Decision.NO), ("a", Decision.ALWAYS)):
            kb.add(key, filter=approval_active)(self._approval_key(decision))

        for digit in range(1, 10):
            kb.add(str(digit), filter=ask_active)(self._ask_digit_key(digit))

        # Live subagent list navigation — active only when the input is empty,
        # no prompt is pending, and subagents are running. Then ↑/↓ select a
        # running subagent and Enter expands/collapses its description.
        nav_active = Condition(
            lambda: (
                _buffer_empty()
                and not self._interaction_active()
                and len(self._running_snapshot()) > 0
            )
        )

        @kb.add("up", filter=nav_active)
        def _sel_up(event):
            n = len(self._running_snapshot())
            if n:
                self._sel_idx = (self._sel_idx - 1) % n
            event.app.invalidate()

        @kb.add("down", filter=nav_active)
        def _sel_down(event):
            n = len(self._running_snapshot())
            if n:
                self._sel_idx = (self._sel_idx + 1) % n
            event.app.invalidate()

        @kb.add("enter", filter=nav_active)
        def _sel_toggle(event):
            # Toggle the expanded description of the selected subagent instead
            # of submitting an empty line.
            self._sel_expanded = not self._sel_expanded
            event.app.invalidate()

        @kb.add("c-c")
        def _ctrl_c(event):
            now = time.monotonic()
            if now - self._last_ctrlc < _DOUBLE_CTRLC_WINDOW_SEC:
                self._exit = True
                event.app.exit()
                return
            self._last_ctrlc = now
            cancelled = self.orch.request_cancel_turn()
            note = (
                "  (Ctrl+C — turn cancelled; press again to exit)"
                if cancelled
                else "  (Ctrl+C — nothing to cancel; press again to exit)"
            )
            print_formatted_text(note)
            event.current_buffer.reset()

        @kb.add("escape", eager=True)
        def _esc(event):
            # Esc denies a pending approval (so the agent never deadlocks).
            if self._pending_approval is not None:
                self._submit_approval(Decision.NO)
                event.app.invalidate()

        return kb

    def _approval_key(self, decision: Decision):
        def _handler(event):
            if self._pending_approval is None:
                return
            self._submit_approval(decision)
            event.current_buffer.reset()
            event.app.invalidate()

        return _handler

    def _ask_digit_key(self, digit: int):
        def _handler(event):
            ask = self._pending_ask
            if ask is None or digit > len(ask.options):
                return
            self._answer_ask(str(digit))
            event.current_buffer.reset()
            event.app.invalidate()

        return _handler

    # ------------------------------------------------------------ dispatch

    def _dispatch_line(self, text: str | None) -> None:
        text = text or ""

        # An open ask_user / approval claims the next submitted line.
        if self._pending_ask is not None:
            self._answer_ask(text)
            return
        if self._pending_approval is not None:
            self._submit_approval(self._decision_from_text(text))
            return

        stripped = text.strip()
        if not stripped:
            return
        if stripped.startswith("/"):
            was_cmd, out = self._commands.dispatch(stripped)
            if was_cmd:
                if isinstance(out, str):
                    self._print([out])
                elif isinstance(out, list):
                    self._print(out)
                return

        # No echo: prompt_toolkit already leaves the typed "› <text>" line in
        # the scrollback, so echoing it again would double the message.
        self.orch.submit_user_text(text)

    @staticmethod
    def _decision_from_text(text: str) -> Decision:
        c = text.strip()[:1].lower()
        if c == "y":
            return Decision.YES
        if c == "a":
            return Decision.ALWAYS
        return Decision.NO  # 'n', empty, or anything else → deny (no deadlock)

    def _submit_approval(self, decision: Decision) -> None:
        req = self._pending_approval
        if req is None:
            return
        self.orch.submit_approval(req.call_id, decision)
        who = f" {req.label}" if req.label else ""
        self._print([f"  [approval/{decision.value}]{who} {req.tool}"])
        self._pending_approval = None
        self._after_interaction()

    def _answer_ask(self, text: str) -> None:
        ask = self._pending_ask
        if ask is None:
            return
        answer = text
        stripped = text.strip()
        if stripped.isdigit() and 1 <= int(stripped) <= len(ask.options):
            answer = ask.options[int(stripped) - 1]
        self.orch.submit_ask_user_answer(ask.ask_id, answer)
        self._print([f"  [answer] {answer}"])
        self._pending_ask = None
        self._after_interaction()

    # -------------------------------------------------- interaction queue/buffer

    def _interaction_active(self) -> bool:
        return self._pending_approval is not None or self._pending_ask is not None

    def _enqueue_interaction(self, ev) -> None:
        if self._interaction_active():
            self._interaction_queue.append(ev)
        else:
            self._show_interaction(ev)

    def _show_interaction(self, ev) -> None:
        if isinstance(ev, ApprovalPrompt):
            self._pending_approval = ev
            self._print(_approval_lines(ev))
        else:  # AskUser
            self._pending_ask = ev
            self._print(list(render_event(ev)))
            self._print(["  (type your answer, a digit for an option, then Enter)"])
        self._invalidate()

    def _after_interaction(self) -> None:
        """A prompt was just answered: show the next queued one, or flush the
        chat output that was held back while a prompt was on screen."""
        if self._interaction_queue:
            self._show_interaction(self._interaction_queue.pop(0))
            return
        if self._output_buffer:
            self._print(self._output_buffer)
            self._output_buffer = []
        self._invalidate()

    def _emit_chat(self, lines: list[str]) -> None:
        """Print chat lines — but hold them in a buffer while a prompt is up so
        the prompt doesn't get pushed around by incoming output."""
        if not lines:
            return
        if self._interaction_active():
            self._output_buffer.extend(lines)
        else:
            self._print(lines)

    # ------------------------------------------------------------ command cbs

    def _clear_scrollback(self) -> None:
        clear()

    def _running_lines(self) -> list[str]:
        """Snapshot of running subagents for the /running command (mode c)."""
        subs = getattr(self.orch, "subagents", None)
        if subs is None:
            return ["(no subagents — /running is for orchestrated mode)"]
        return subs.format_snapshot().splitlines()

    def _request_exit(self) -> None:
        self._exit = True

    # ------------------------------------------------------------- printing

    def _print(self, lines: Iterable[str]) -> None:
        for ln in lines:
            print_formatted_text(ln)

    def _invalidate(self) -> None:
        app = self._session.app
        if app.is_running:
            app.invalidate()

    # ------------------------------------------------------------- ui pump

    async def pump_events(self) -> None:
        """Consume UIEvents and decide what reaches the chat.

        Shown in chat: the orchestrator's thinking (streamed text), its own
        recon tool calls, and finding cards. Hidden: subagent tool traffic
        (labelled) and lifecycle events — those drive the live bottom view via
        the status snapshot. The end-of-run summary card is suppressed too
        (only a one-line pointer to the report is printed)."""
        stream_buf: list[str] = []
        while True:
            ev = await self.orch.ui_queue.get()
            if isinstance(ev, SessionEnd):
                self._flush_stream(stream_buf)
                self._flush_buffer()
                self._print(list(render_event(ev)))
                self._exit = True
                app = self._session.app
                if app.is_running:
                    app.exit()
                return

            # Coalesce streamed agent text (the orchestrator's thinking).
            if ev.kind == "agent_text_delta":
                stream_buf.append(ev.text)
                continue
            if ev.kind == "agent_text_end":
                self._flush_stream(stream_buf)
                continue

            self._flush_stream(stream_buf)

            # Subagent start + per-turn progress stay out of the chat (the live
            # bottom toolbar shows what's running). A subagent FINISHING does
            # reach the chat as a concise "✓ …" line so the operator sees results
            # land; the dispatch ActivityLine already served as the "started" cue.
            if isinstance(ev, (SubagentStarted, SubagentProgress)):
                self._invalidate()
                continue
            if isinstance(ev, SubagentFinished):
                self._invalidate()
                self._emit_chat(list(render_event(ev)))
                continue
            if isinstance(ev, (ToolProposal, ToolObservation)) and ev.label:
                continue
            # Subagent recon activity is labelled and stays out of the chat.
            if isinstance(ev, ActivityLine) and ev.label:
                continue

            if isinstance(ev, Status):
                self._status = ev
                self._invalidate()
            elif isinstance(ev, (ApprovalPrompt, AskUser)):
                self._enqueue_interaction(ev)
            elif isinstance(ev, SessionSummary):
                # The report, shown in the chat at `finalize`. The session does
                # NOT end here — the orchestrator goes idle for the next prompt.
                self._emit_chat(list(render_event(ev)))
            else:
                self._emit_chat(list(render_event(ev)))

    def _flush_stream(self, buf: list[str]) -> None:
        if buf:
            self._emit_chat(["".join(buf).rstrip()])
            buf.clear()

    def _flush_buffer(self) -> None:
        if self._output_buffer:
            self._print(self._output_buffer)
            self._output_buffer = []

    # --------------------------------------------------------------- run

    async def run(self) -> None:
        with patch_stdout():
            pump = asyncio.create_task(self.pump_events(), name="ui_pump")
            try:
                while not self._exit:
                    try:
                        text = await self._session.prompt_async()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if self._exit:
                        break
                    self._dispatch_line(text)
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump


def _approval_lines(ev: ApprovalPrompt) -> list[str]:
    who = f"  {ev.label}" if ev.label else ""
    lines = [
        "",
        f"  → approve tool call?{who}  tool: {ev.tool}  risk: {ev.risk_level}",
    ]
    why = short_reason(ev.reason)
    if why:
        lines.append(f"    why: {why}")
    lines.append(f"    args: {ev.args}")
    lines.append("    [y] yes   [n] no   [a] always-this-tool   (or type + Enter; Esc denies)")
    return lines


# Public entry point ----------------------------------------------------------


async def run_ui(orch: "Orchestrator") -> None:
    """Build and run the UI. Returns when the user exits."""
    ui = ZeroUI(orch)
    await ui.run()
