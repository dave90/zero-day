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
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import clear

from zero.orchestrator.approval import Decision
from zero.ui import theme
from zero.ui.commands import build_default_registry
from zero.ui.events import (
    ActivityLine,
    AgentTextDelta,
    ApprovalPrompt,
    AskUser,
    RalphIteration,
    ReasoningSummary,
    SessionEnd,
    SessionSummary,
    Status,
    SubagentFinished,
    SubagentProgress,
    SubagentStarted,
    TodoUpdate,
    ToolObservation,
    ToolProposal,
    ToolRunning,
    TurnCancelled,
    TurnStart,
)
from zero.ui.render import fmt_context, ralph_status_label, render_event, short_reason

if TYPE_CHECKING:
    from zero.orchestrator.orchestrator import Orchestrator


_DOUBLE_CTRLC_WINDOW_SEC = 2.0
_THINKING_LABELS = {
    "orchestrator": "zero",
    "analyzer": "ralph · analyzer",
    "analyzer_validator": "ralph · validator",
}


class ZeroUI:
    def __init__(self, orch: "Orchestrator") -> None:
        self.orch = orch
        self._status = Status()
        # Ralph-mode footer indicator: latest iteration/total/phase from the
        # RalphIteration events. Shown in the status bar only while mode == "ralph".
        self._ralph_iter = 0
        self._ralph_total = 0
        self._ralph_phase = ""
        self._pending_approval: ApprovalPrompt | None = None
        self._pending_ask: AskUser | None = None
        # Concurrent subagents can raise several approvals at once. We show one
        # at a time (queue the rest) and BUFFER chat output while any prompt is
        # up, so the prompt stays put instead of scrolling away.
        self._interaction_queue: list = []
        self._output_buffer: list[str] = []
        # call_id → {tool, label, start (monotonic), timeout}. A live "running"
        # indicator in the toolbar (ticks via the 0.5s refresh) so a long tool call
        # doesn't look like a hang. Populated on ToolRunning, cleared on ToolObservation.
        self._running_tools: dict[str, dict] = {}
        # The orchestrator and Ralph planning roles emit TurnStart immediately before
        # each blocking LLM call. Keep that wait in the live toolbar so `thinking.`,
        # `thinking..`, `thinking...` can animate instead of leaving a static line.
        self._thinking_agent: str | None = None
        self._thinking_turn: int | None = None
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
            swarm_snapshot=self._swarm_lines,
            respond_now=self._respond_now,
        )
        # PromptSession construction does NOT touch the TTY (that happens in
        # prompt_async), so this is safe to build in tests too.
        self._session: PromptSession = PromptSession(
            message=self._prompt_message,
            bottom_toolbar=self._toolbar,
            key_bindings=self._build_key_bindings(),
            style=theme.THEME,            # resolve `class:` names everywhere
            multiline=False,
            refresh_interval=0.5,  # keep the toolbar (token/turn counts) live
        )

    # --------------------------------------------------------- prompt chrome

    def _prompt_message(self) -> FormattedText:
        # A violet bar above the input line (Claude-style). The matching bar
        # below lives in the toolbar (_toolbar), so the input sits between them.
        top = [theme.rule(self._term_width()), ("", "\n")]
        if self._pending_approval is not None:
            return FormattedText([*top, ("class:prompt.approve", "approve? [y/n/a] › ")])
        if self._pending_ask is not None:
            return FormattedText([*top, ("class:prompt.ask", "answer › ")])
        return FormattedText([*top, ("class:prompt", "› ")])

    def _running_snapshot(self) -> list:
        subs = getattr(self.orch, "subagents", None)
        if subs is not None:
            return subs.snapshot()
        runtime = getattr(self.orch, "_swarm_runtime", None)
        if runtime is None:
            return []
        return [item for worker in runtime.workers for item in worker.subagents.snapshot()]

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

    def _toolbar(self) -> FormattedText:
        """Live bottom area: each running subagent with its goal + elapsed
        time (Claude-Code style), above the status line. Subagent *logs* never
        hit the chat — only this live view + the status counters. A Braille
        spinner animates per refresh (0.5 s) so the bar feels alive.

        ↑/↓ select a row; Enter expands the selected subagent's full
        description (word-wrapped to the terminal width)."""
        frags: list = []

        def row(*parts) -> None:
            frags.extend(parts)
            frags.append(("", "\n"))

        # The bar below the input line (its twin above is in _prompt_message).
        row(theme.rule(self._term_width()))

        if self._thinking_agent is not None and self._thinking_turn is not None:
            row(
                ("class:accent", f" {_THINKING_LABELS[self._thinking_agent]}"),
                ("class:dim", f" · turn {self._thinking_turn} · thinking"),
                ("class:accent", theme.thinking_dots()),
            )

        snap = self._running_snapshot()
        if snap:
            if self._sel_idx >= len(snap):
                self._sel_idx = len(snap) - 1
            width = self._term_width()
            configured_window = getattr(getattr(self.orch, "cfg", None), "context_window", 0)
            window_is_explicit = getattr(self.orch, "_context_window_explicit", False)
            spin = theme.spinner_frame()
            for i, s in enumerate(snap[:8]):
                selected = i == self._sel_idx
                marker = "›" if selected else " "
                # Long gruyere-style targets would eat the row — keep the tail.
                tgt = self._short(s["target"], 28)
                idea = self._short(s["idea"], 44)
                window = (
                    configured_window
                    if window_is_explicit
                    else s.get("context_window", 0) or configured_window
                )
                ctx = fmt_context(s.get("context_tokens", 0), window)
                ctx_seg = f" · {ctx}" if ctx else ""
                marker_cls = "class:toolbar.sel" if selected else "class:dim"
                row(
                    (marker_cls, f"{marker}{spin} "),
                    ("class:accent", f"{s['id']} "),
                    ("class:dim", f"{s['kind']} {tgt} "),
                    ("", f"— {idea}"),
                    ("class:dim", f"  ({s['elapsed']:.0f}s{ctx_seg})"),
                )
                if selected and self._sel_expanded:
                    for ln in self._wrap_field("goal", s["idea"], width):
                        row(("class:dim", ln))
                    for ln in self._wrap_field("target", s["target"], width):
                        row(("class:dim", ln))
                    row(("class:dim", f"      turn {s['turn']} · {s['status']} · {s['elapsed']:.0f}s{ctx_seg}"))
            if len(snap) > 8:
                row(("class:dim", f"  … +{len(snap) - 8} more running"))
            row(("class:dim", " [↑/↓ select · Enter expand/collapse]"))
        else:
            self._sel_idx = 0
            self._sel_expanded = False

        # Top-level (orchestrator / mode-a) tools currently executing. Labelled
        # (subagent) tool runs are already implied by the animated subagent rows
        # above, so we only surface the unlabelled ones here — that's the case that
        # otherwise looks frozen (e.g. the orchestrator's own 300s ffuf during recon).
        running = [r for r in self._running_tools.values() if not r.get("label")]
        if running:
            spin = theme.spinner_frame()
            now = time.monotonic()
            for r in running[:4]:
                el = now - r["start"]
                el_txt = f"{el:.0f}s" + (f"/{r['timeout']}s" if r.get("timeout") else "")
                row(
                    ("class:accent", f" {spin} "),
                    ("class:dim", "running "),
                    ("", r["tool"]),
                    ("class:dim", f"  ({el_txt})"),
                )
            if len(running) > 4:
                row(("class:dim", f"  … +{len(running) - 4} more tools running"))

        frags.extend(self._status_fragments())
        return FormattedText(frags)

    def _status_fragments(self) -> list:
        """Styled twin of render.status_line() — same fields, with color + a
        ctx usage bar. (render.status_line stays plain; tests pin its text.)"""
        st = self._status
        appr_cls = "class:warn" if st.pending_approvals else "class:dim"
        # In Ralph mode, replace the plain "mode ralph" with a live iteration
        # indicator (planning / iter N/total / done); other modes are unchanged.
        if st.mode == "ralph":
            mode_frags = [
                ("class:dim", " · "),
                ("class:accent", ralph_status_label(
                    self._ralph_iter, self._ralph_total, self._ralph_phase)),
                ("class:dim", " · "),
            ]
        elif st.mode == "swarm":
            runtime = getattr(self.orch, "_swarm_runtime", None)
            snap = runtime.ui_snapshot() if runtime is not None else None
            if snap is None:
                mode_frags = [("class:dim", " · swarm · ")]
            else:
                phase = str(snap["phase"])
                detail = (
                    f"bootstrap {snap['bootstrapped']}/{snap['size']}"
                    if phase == "bootstrap" else
                    f"{snap['contributions']}/{snap['size']} contributed"
                )
                mode_frags = [
                    ("class:dim", " · "),
                    ("class:accent.b", f"SWARM {snap['size']}"),
                    ("class:dim", f" · {phase} · {detail}"),
                    ("class:dim", f" · active {snap['active']}"),
                    ("class:dim", f" · work {snap['work_done']}/{snap['work_total']}"),
                    ("class:dim", f" · msg {snap['messages']} · "),
                ]
        else:
            mode_frags = [("class:dim", f" · mode {st.mode} · ")]
        out: list = [
            ("class:accent", f" turn {st.turn}"),
            *mode_frags,
            (appr_cls, f"approvals waiting: {st.pending_approvals}"),
            ("class:dim", " · findings: "),
            ("class:ok", f"{st.validations_confirmed}✓"),
            ("class:dim", " / "),
            ("class:fail", f"{st.validations_rejected}✗"),
        ]
        bar = theme.ctx_bar(st.context_tokens, st.context_window)
        if bar:
            out.append(("class:dim", " · "))
            out.extend(bar)
        out.append(("", " "))
        return out

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
            runtime = getattr(self.orch, "_swarm_runtime", None)
            if runtime is not None:
                lines = runtime.inspection_lines("agents")
                for worker in runtime.workers:
                    snapshot = worker.subagents.format_snapshot()
                    if "(none)" not in snapshot:
                        lines.extend(snapshot.splitlines())
                return lines
            return ["(no subagents — /running is for orchestrated mode)"]
        return subs.format_snapshot().splitlines()

    def _swarm_lines(self, view: str) -> list[str]:
        runtime = getattr(self.orch, "_swarm_runtime", None)
        return runtime.inspection_lines(view) if runtime is not None else ["(no live swarm)"]

    def _respond_now(self) -> bool:
        runtime = getattr(self.orch, "_swarm_runtime", None)
        return runtime.respond_now() if runtime is not None else False

    def _request_exit(self) -> None:
        self._exit = True

    # ------------------------------------------------------------- printing

    def _print(self, lines: Iterable[str]) -> None:
        for ln in lines:
            # Already-styled chrome (welcome banner / approval prompt) passes
            # straight through; plain strings get colorized by their marker.
            ft = ln if isinstance(ln, FormattedText) else theme.stylize(ln)
            print_formatted_text(ft, style=theme.THEME)

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
                self._thinking_agent = None
                self._thinking_turn = None
                self._flush_stream(stream_buf)
                self._flush_buffer()
                self._print(list(render_event(ev)))
                self._exit = True
                app = self._session.app
                if app.is_running:
                    app.exit()
                return

            # An inline scrollback line cannot animate in place, while the toolbar
            # is redrawn every 0.5 s. Keep supported top-level/Ralph roles there until
            # the matching turn status (or an error/cancel response) arrives.
            if isinstance(ev, TurnStart) and ev.agent in _THINKING_LABELS:
                self._thinking_agent = ev.agent
                self._thinking_turn = ev.turn
                self._invalidate()
                continue

            if self._thinking_agent is not None and self._thinking_turn is not None:
                turn_resolved = (
                    isinstance(ev, Status)
                    and ev.turn >= self._thinking_turn
                ) or (
                    isinstance(ev, ReasoningSummary)
                    and ev.agent == self._thinking_agent
                    and ev.turn >= self._thinking_turn
                )
                turn_interrupted = isinstance(ev, (AgentTextDelta, TurnCancelled))
                if turn_resolved or turn_interrupted:
                    self._thinking_agent = None
                    self._thinking_turn = None
                    self._invalidate()

            # Coalesce streamed agent text (the orchestrator's thinking).
            if ev.kind == "agent_text_delta":
                stream_buf.append(ev.text)
                continue
            if ev.kind == "agent_text_end":
                self._flush_stream(stream_buf)
                continue

            self._flush_stream(stream_buf)

            # Maintain the live "running tool" set (a toolbar indicator), independent
            # of whether the event reaches the chat. ToolRunning never prints; the
            # paired ToolObservation clears it and then falls through to normal
            # handling (labelled→hidden, top-level→chat).
            if isinstance(ev, ToolRunning):
                self._running_tools[ev.call_id] = {
                    "tool": ev.tool, "label": ev.label,
                    "start": time.monotonic(), "timeout": ev.timeout_seconds,
                }
                self._invalidate()
                continue
            if isinstance(ev, ToolObservation):
                self._running_tools.pop(ev.call_id, None)
                self._invalidate()
            # A cancelled turn abandons any in-flight tool (its observation may never
            # come), so drop the running indicators rather than let them hang.
            if ev.kind == "turn_cancelled":
                self._running_tools.clear()
                self._invalidate()

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
            # Subagent reasoning is labelled and stays out of the chat too.
            if isinstance(ev, ReasoningSummary) and ev.label:
                continue
            # Subagent plans are labelled and stay out of the chat too.
            if isinstance(ev, TodoUpdate) and ev.label:
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
            elif isinstance(ev, RalphIteration):
                # Track the current iteration for the footer indicator, and still
                # print the boundary banner to the chat.
                if ev.phase == "iteration":
                    self._ralph_iter, self._ralph_total = ev.iteration, ev.total
                elif ev.phase == "complete" and ev.total:
                    self._ralph_total = ev.total
                self._ralph_phase = ev.phase
                self._emit_chat(list(render_event(ev)))
                self._invalidate()
            else:
                self._emit_chat(list(render_event(ev)))

    def _flush_stream(self, buf: list[str]) -> None:
        if not buf:
            return
        text = "".join(buf).rstrip()
        buf.clear()
        if not text:
            return
        # The agent answers in Markdown. Render it to styled text so the user
        # doesn't read raw `##`, `**`, backticks. Plain agent lines (startup
        # config, "ready —", error tags) have no markdown and keep the existing
        # marker-based styling via stylize().
        if theme.has_markdown(text):
            self._emit_chat([theme.render_markdown(text, self._term_width())])
        else:
            self._emit_chat([text])

    def _flush_buffer(self) -> None:
        if self._output_buffer:
            self._print(self._output_buffer)
            self._output_buffer = []

    # --------------------------------------------------------------- run

    async def run(self) -> None:
        with patch_stdout():
            cfg = getattr(self.orch, "cfg", None)
            if cfg is not None:
                print_formatted_text(
                    theme.welcome_banner(cfg, width=self._term_width()),
                    style=theme.THEME,
                )
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


def _approval_lines(ev: ApprovalPrompt) -> list[FormattedText]:
    """Styled approval prompt — risk badge colored by level, hint legible."""
    risk_cls = theme.risk_class(ev.risk_level)
    who = f"  {ev.label}" if ev.label else ""
    lines: list[FormattedText] = [
        FormattedText([("", "")]),
        FormattedText(
            [
                ("class:warn", f"  → approve tool call?{who}  "),
                ("class:accent", f"tool: {ev.tool}"),
                ("", "  risk: "),
                (f"class:{risk_cls}", ev.risk_level),
            ]
        ),
    ]
    why = short_reason(ev.reason)
    if why:
        lines.append(FormattedText([("class:dim", f"    why: {why}")]))
    lines.append(FormattedText([("class:dim", f"    args: {ev.args}")]))
    lines.append(
        FormattedText(
            [("class:dim", "    [y] yes   [n] no   [a] always-this-tool   (or type + Enter; Esc denies)")]
        )
    )
    return lines


# Public entry point ----------------------------------------------------------


async def run_ui(orch: "Orchestrator") -> None:
    """Build and run the UI. Returns when the user exits."""
    ui = ZeroUI(orch)
    await ui.run()
