"""Top-level coordinator.

Owns the AuditLog, the ApprovalGate, the tool registry, the agent loop, and
the UI queue. Runs the agent as one asyncio.Task and the UI as another;
they communicate through an asyncio.Queue of UIEvents and a separate queue
of user inputs.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zero.agents.pentest import build_pentest_loop
from zero.agents.orchestrator_agent import build_orchestrator_loop, parse_pentester_result
from zero.llm.backend import LLMBackend, Session
from zero.llm.envelope import (
    ActionEnvelope,
    ActionsEnvelope,
    AskUserEnvelope,
    FinalEnvelope,
)
from zero.orchestrator.approval import (
    ApprovalGate,
    ApprovalRequest,
    Decision,
)
from zero.orchestrator.ask_user_governor import AskUserGovernor
from zero.orchestrator.audit import AuditLog
from zero.orchestrator.thinking import ThinkingLog
from zero.orchestrator.context import project_context_for
from zero.orchestrator.rate_limit import RateLimiter
from zero.orchestrator.subagents import SUMMARY_MAX_CHARS, SubagentManager, SubagentResult
from zero.tools.base import Tool, ToolResult
from zero.tools.registry import ToolRegistry, default_registry
from zero.tools.finding_submit import args_to_finding, is_finding_submit
from zero.tools.dispatch import (
    AwaitSubagentsArgs,
    DispatchPentesterArgs,
    DispatchValidatorArgs,
    FinalizeArgs,
    ReadSubagentMemoryArgs,
    dispatch_tools,
    inspect_tools,
    is_dispatch_tool,
    is_inspect_tool,
)
from zero.tools.custom_tools import custom_mgmt_tools, is_custom_mgmt_tool
from zero.tools._customtools import delete_tool, write_tool
from zero.tools._memroot import memory_root
from zero.tools.memory import read_memory, write_memory
from zero.tools.web_fetch import WebFetchTool
from zero.findings.store import FindingsStore
from zero.findings.schema import Finding as FindingModel
from zero.findings.dedup import dedup_hash
from zero.findings.report import write_report
from zero.orchestrator.router import AgentRouter, default_validator_driver
from zero.ui.events import (
    ActivityLine,
    AgentTextDelta,
    AgentTextEnd,
    ApprovalPrompt,
    AskUser,
    Finding as FindingEvent,
    GoalBanner,
    ReasoningSummary,
    SessionEnd,
    SessionSummary,
    Status,
    SubagentProgress,
    TodoUpdate,
    ToolObservation,
    ToolProposal,
    ToolRunning,
    TurnCancelled,
    TurnStart,
    UIEvent,
)


# How many consecutive failed turns (invalid-JSON envelope, backend error) the
# agent loop tolerates — feeding the error back and retrying — before it gives up
# and waits for the operator. Lets a transient model glitch self-heal instead of
# reading as a crashed/idle session.
_MAX_CONSECUTIVE_TURN_ERRORS = 3

# Tools that mutate the actor's ONE persistent interpreter/env — within an `actions`
# batch these must run sequentially (never concurrently) to avoid interleaving the
# shared python_exec session. Everything else in a batch can run concurrently.
_SERIAL_BATCH_TOOLS = {"python_exec", "pip_install"}


def _seed_memory_message(own_memory: str) -> str:
    """Frame the orchestrator's re-seeded memory. The memory may be from a PRIOR
    process (same folder → stable "orchestrator" session id), so any subagent it calls
    dispatched/running is gone — its process died with it. Warn the model NOT to await
    those ghosts (an `await_subagents` with nothing in flight would otherwise hang); it
    must re-dispatch to continue that work."""
    return (
        "## Your memory from earlier\n\n"
        "NOTE: this may be from an earlier session. Any subagents it describes as"
        " dispatched/running are NO LONGER running (they ended with that session) — to"
        " continue that work, re-dispatch; do NOT call await_subagents for them.\n\n"
        f"{own_memory}"
    )


def _orchestrator_turn_note(turns_done: int, max_turns: int) -> str:
    """A per-turn 'turns remaining' reminder appended to the orchestrator's
    system prompt so it paces itself and stops opening new attack ideas as the
    budget runs out."""
    upcoming = turns_done + 1
    remaining = max(0, max_turns - turns_done)
    return (
        "\n\n## Turn budget (live)\n\n"
        f"You are about to take **turn {upcoming} of {max_turns}** — "
        f"**{remaining} turn(s) remain**, this one included.\n"
        "- While several turns remain: recon, dispatch pentesters, chain results, validate.\n"
        "- When only 1–2 remain: STOP searching for new vulnerabilities — do not open new "
        "attack ideas or dispatch new pentesters. Await any running subagents, validate the "
        "strongest candidates, and call `finalize`.\n"
        "- On the last turn the run stops automatically, so `finalize` before then to leave a "
        "clean report.\n"
    )


@dataclass
class OrchestratorConfig:
    cwd: Path
    goal: str | None = None
    backend: str = "codex"  # production default; "echo" is a test-only fixture
    # Persistent backend sessions: Claude keeps a `--session-id` conversation and Codex
    # keeps a thread id; both send only the new turn on resume instead of re-shipping the
    # full transcript. `--no-session-resume` reverts to the stateless path.
    session_resume: bool = True
    auto_approve: set[str] = field(default_factory=set)
    yolo: bool = False
    i_understand: bool = False
    # Allow tools that execute code/binaries on the HOST to be auto-approved.
    # Off by default: host-exec tools always prompt unless sandboxed (--container).
    allow_host_exec: bool = False
    scope_hosts: set[str] = field(default_factory=set)
    max_turns: int = 10
    # Validator gets its own, generous turn budget — it investigates a
    # finding from scratch (fetch, analyze, re-fetch, conclude) and must
    # not be capped at the pentest agent's per-call limit.
    validator_max_turns: int = 40          # Validator AgentLoop investigation steps (OUR loop)
    # claude's INTERNAL per-call --max-turns for the validator. Was 60 on the mistaken
    # belief the validator's investigation needs many claude turns — but those are OUR
    # loop turns (validator_max_turns). claude needs ~one turn to emit one envelope.
    # Keep it SMALL but not 1: under lock-down no tool is callable, yet a stray native
    # tool_use attempt at --max-turns 1 instantly trips error_max_turns; a little
    # headroom lets it fail harmlessly and recover to text (see ClaudeCodeConfig.max_turns).
    validator_claude_max_turns: int = 4    # per-call `claude -p --max-turns`
    # Per-call LLM wall-clock timeout shared by ALL agent backends — the
    # orchestrator's own turns AND the pentester/validator subagents — so they
    # are always the same value. Heavy iterative exploitation (and the
    # orchestrator's large-context turns) can exceed the bare 300s default, so we
    # use a single generous, CLI-configurable budget (`--llm-timeout`).
    llm_timeout_seconds: int = 900
    # Per-subagent turn budget for PENTESTERS. Previously pentesters inherited
    # the orchestrator's (large) max_turns, so a single attack idea could run
    # 40+ turns and burn an hour looping on the same failure. Cap them on their
    # own, smaller budget; `dispatch_pentester(max_turns=...)` can still override.
    pentester_max_turns: int = 14
    # Hard wall-clock ceiling for ONE subagent's whole run, checked between
    # turns. The 900s `--llm-timeout` is only PER backend call — a
    # subagent that keeps finding "one more thing to try" has no total cap
    # without this. Bounds a runaway subagent regardless of turn count.
    subagent_deadline_seconds: int = 1200
    # Orchestrated mode (M8): cap on concurrently-running subagents.
    max_concurrent_subagents: int = 10
    # Hard ceiling on the TOTAL subagents (pentesters + validators) dispatched over
    # the whole run — distinct from the concurrency cap above. 0 = unlimited. Once
    # reached, dispatch_pentester/dispatch_validator are refused and the orchestrator
    # is told to finalize with what it has. Cost-control knob (--max-total-subagents).
    max_total_subagents: int = 0
    # M4 sandbox knobs. Secure by default: run tools INSIDE the container, and
    # proxy ALL of them there (not just python_exec). Falls back to host (with
    # the approval guard) if Docker isn't available. Use --no-container to opt
    # into host execution.
    use_container: bool = True
    container_image: str = "zero-sandbox:latest"
    host_network: bool = False
    # Optional extra docker network the sandbox joins after start (via `docker
    # network connect`), keeping the bridge-published RPC port intact. Used by the
    # BountyBench benchmark harness to put the sandbox on BB's `shared_net` so it
    # can reach the bounty target. None ⇒ default bridge only.
    container_network: str | None = None
    burp_dir: Path | None = None
    # Root folder the read-only filesystem tools (list_dir/grep/read_file) are
    # JAILED to — they can never read outside it. Resolved against cwd; mounted at
    # /zero_files in the sandbox. Default ./zero_files under the engagement cwd.
    files_root: Path = Path("zero_files")
    container_cpus: float = 4.0
    container_memory: str = "4g"
    remote_all_tools: bool = True
    rate_limit: str = "100/min"
    replay_audit_path: Path | None = None
    execution_mode: str = "c"   # c=orchestrated (default), a=single agent, b=MCP
    # How much of the agent's free-form `thought` reaches the chat. The full
    # thought is always recorded in thinking.log; this only controls the UI.
    #   "condensed" (default) — first sentence, one clean line
    #   "full"               — stream the whole thought (legacy behavior)
    #   "off"                — show nothing (rely on the ▸ activity lines)
    thought_display: str = "condensed"
    # Model for every backend call (orchestrator + subagents + validators). The shared
    # CLI default is the newest Claude Opus 1M id; Codex deliberately ignores Claude ids
    # and uses its configured default. Override with --model; None ⇒ account default.
    model: str | None = "claude-opus-5[1m]"
    # Model context-window size used ONLY for the UI's "ctx used/window (%)" display
    # (per agent). `None` ⇒ resolve it backend-aware at startup (Codex reports its real
    # window at runtime; Claude infers 1M for a '[1m]' model else 200k) — see
    # Orchestrator._resolve_context_window. An explicit int (e.g. from --context-window)
    # is always honored.
    context_window: int | None = None
    # Per-agent LLM I/O trace → <cwd>/.pentest/llm/<actor>.log (full system prompt +
    # stdin in, full stdout/stderr out). ON by default — it's the primary way to see
    # what each agent actually received and replied. Disable with `--no-llm-log`, or
    # point it elsewhere with $ZERO_LLM_LOG_DIR (which wins if set).
    llm_log: bool = True
    # ---- Ralph mode (iterative loop wrapping the orchestrated pass) ----------
    # Opt-in; OFF by default. When True, run_interactive drives RalphDriver instead
    # of the a/b/c agent loop. The analyzer/analyzer_validator build the PRD, then
    # `ralph_iterations` bounded orchestrated passes work it (each capped at the
    # orchestrator's own `max_turns`). See zero/ralph/.
    ralph: bool = False
    ralph_iterations: int = 5
    # Resume a prior Ralph run from `.ralph/` in the engagement folder: load the
    # existing prd.json (honouring prior task statuses/notes) instead of re-planning,
    # skip the analyzer discussion + PRD checkpoint, and continue iteration numbering
    # from the last `.ralph_logs/.pentest_N`. Findings + progress.txt already persist.
    ralph_resume: bool = False
    # Pause for operator approval of the PRD after the analyzer↔validator discussion,
    # before the loop. Default on; the loop iterations themselves always run unattended.
    ralph_approve_prd: bool = True
    # Max analyzer↔analyzer_validator back-and-forth rounds before the PRD is accepted
    # as-is (with a warning) even if not approved. Reused for BOTH planning phases.
    ralph_discussion_rounds: int = 4
    # Two-phase planning: first agree a set of EPICS that partition the attack surface,
    # then decompose each into atomic tasks (every epic gets >=1 task, checked by the
    # validator). On -> structural coverage; off -> legacy single-phase task planning.
    ralph_epics: bool = True
    # The analyzer (PRD authoring) reuses the main `max_turns` budget so recon depth
    # scales with the run's budget — a big target needs many recon turns before it can
    # decompose the surface into atomic tasks. The validator review keeps its own cap.
    analyzer_validator_max_turns: int = 6


class Orchestrator:
    def __init__(self, cfg: OrchestratorConfig) -> None:
        self.cfg = cfg
        # Resolve the UI context-window denominator now (once) when left on auto, so the
        # ctx% reflects the ACTUAL backend's window — Codex (~272k) is far below Claude's
        # 1M, and a stale denominator would mislead. An explicit value is left untouched.
        if self.cfg.context_window is None:
            self.cfg.context_window = self._resolve_context_window()
        # Resolve the read-only-fs jail root to an absolute host path (relative to
        # cwd) and create it, so the mount target exists and the agent has a place
        # to drop loot. Export it so HOST-side tool instances jail to it too; in the
        # sandbox the container env (-e ZERO_FILES_ROOT=/zero_files) takes over.
        self._files_root_abs = (cfg.cwd / cfg.files_root).resolve()
        self._files_root_abs.mkdir(parents=True, exist_ok=True)
        os.environ["ZERO_FILES_ROOT"] = str(self._files_root_abs)
        # Per-agent working memory lives on the HOST under .pentest/memory and is
        # NEVER mounted into the sandbox (the memory tools are excluded from
        # remoting), so the orchestrator can read any subagent's memory. Export the
        # root so the host-side memory tool instances resolve to it.
        self._memory_root_abs = (cfg.cwd / ".pentest" / "memory").resolve()
        self._memory_root_abs.mkdir(parents=True, exist_ok=True)
        os.environ["ZERO_MEMORY_ROOT"] = str(self._memory_root_abs)
        self.audit = AuditLog(cfg.cwd / ".pentest" / "audit.jsonl")
        # Readable reasoning log for the orchestrator + every subagent.
        self.thinking = ThinkingLog(cfg.cwd / ".pentest" / "thinking.log")
        self.ui_queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        self.user_queue: asyncio.Queue[str] = asyncio.Queue()
        self._approval_replies: dict[str, asyncio.Future[Decision]] = {}
        self._ask_user_replies: dict[str, asyncio.Future[str]] = {}
        self._approvals_pending = 0
        self._current_turn_task: asyncio.Task | None = None
        # Set whenever the operator submits text. The orchestrator's blocking
        # `await_subagents` races against this so the user can interject (chat /
        # redirect) WHILE subagents are still running, instead of being frozen
        # out until the batch finishes.
        self._user_input_event = asyncio.Event()
        self._container = None
        self._rpc = None

        self.registry = self._build_registry()
        self.gate = ApprovalGate(
            prompter=self._prompt_approval,
            auto_approve=cfg.auto_approve,
            yolo=cfg.yolo,
            i_understand=cfg.i_understand,
            allow_host_exec=cfg.allow_host_exec,
        )
        self.backend = self._build_backend()
        # Pentester/validator subagents get a backend with a higher per-call
        # timeout (--subagent-timeout) so heavy iterative exploitation isn't cut
        # off mid-run; the orchestrator's own backend keeps the default timeout.
        self.subagent_backend = self._build_subagent_backend()
        self.validator_backend = self._build_validator_backend()
        self.rate_limiter = RateLimiter(default_rate=cfg.rate_limit)
        self.ask_governor = AskUserGovernor(yolo=cfg.yolo)
        self.findings_store = FindingsStore(cfg.cwd / ".pentest" / "findings")
        self.router: AgentRouter | None = None  # built after project context loads
        # Findings produced in this session, for the closing summary.
        self._session_findings: dict[str, list[dict]] = {
            "confirmed": [],
            "rejected": [],
            "pending": [],
        }
        self._total_turns = 0
        # Async validation: findings are validated in background tasks so
        # the pentest loop never blocks on the Validator.
        self._validations_pending = 0
        self._validations_confirmed = 0
        self._validations_rejected = 0
        self._inflight_hashes: set[str] = set()
        self._validation_tasks: set[asyncio.Task] = set()
        # Strict validation (mode c): count candidate findings the pentesters
        # returned vs. validators the orchestrator dispatched, so `finalize` can
        # refuse to close while exploitable candidates were never validated.
        # Only a Validator subagent may confirm a finding — the orchestrator's
        # own checks do not count.
        self._pentester_candidates = 0
        self._validators_dispatched = 0
        # Last turn/mode shown in the status line, so counter-only updates
        # (e.g. a validation finishing) keep the rest of the line stable.
        self._status_turn = 0
        self._status_mode = "a"
        # The top-level agent loop (orchestrator in mode c, pentest agent in mode a),
        # so _emit_status can report its context-window usage. Set when built.
        self._active_loop = None
        # Orchestrated mode (M8): the subagent manager, project context, an
        # optional per-subagent backend factory (tests inject scripted backends),
        # and the finalize latch.
        self.subagents: SubagentManager | None = None
        self._project_ctx: str | None = None
        self._subagent_backend_factory: Any = None
        # Set by the `finalize` tool: emit the report, then go idle and wait for
        # the next user prompt (Claude-Code style — the session does NOT close).
        self._go_idle = False
        # The most recent finalize summary — surfaced as a Ralph pass's return value.
        self._last_finalize_summary = ""
        # Set by the RPC recovery hook when a lost sandbox is re-created, so the next tool
        # observation warns the agent its in-container session state was reset.
        self._sandbox_reset_pending = False

    # ------------------------------------------------------------------ setup

    def _build_registry(self) -> ToolRegistry:
        reg = default_registry()
        # Apply scope-host blocklist to the WebFetchTool instance.
        if self.cfg.scope_hosts:
            wf = reg.get("web_fetch")
            assert isinstance(wf, WebFetchTool)
            wf.blocked_hosts = {h.lower() for h in self.cfg.scope_hosts}
        return reg

    def _make_session(self, label: str) -> Session | None:
        """A fresh persistent-claude session for one actor, or None when session-resume
        is off or the backend isn't claude (echo/replay are stateless — they must keep
        getting the full transcript). `id` is a UUID (`--session-id` requires one);
        `label` becomes the `-n` display name for debugging only."""
        if not self.cfg.session_resume or self.cfg.backend not in ("claude", "codex"):
            return None
        return Session(id=str(uuid.uuid4()), name=label)

    def _resolve_context_window(self) -> int:
        """The UI ctx denominator for the configured backend, resolved at startup.

        Codex exposes its real per-model window in its on-disk model cache (read at
        runtime) — typically ~272k, far below Claude's 1M; we fall back to 272k if the
        cache is unreadable. Claude can't report its window, so we infer it from the
        model id (1M for a '[1m]' variant, else the standard 200k)."""
        if self.cfg.backend == "codex":
            from zero.llm.codex_cli import codex_context_window

            return codex_context_window(self.cfg.model) or 272_000
        model = self.cfg.model or ""
        return 1_000_000 if "[1m]" in model else 200_000

    def _effective_model_name(self) -> str:
        """Return the model the selected backend will actually receive."""
        backend_cfg = getattr(self.backend, "cfg", None)
        model = getattr(backend_cfg, "model", None)
        if self.cfg.backend == "codex":
            from zero.llm.codex_cli import codex_effective_model

            return codex_effective_model(model)
        return model or "account-default"

    def _build_backend(self) -> LLMBackend:
        if self.cfg.backend == "echo":
            from zero.llm.echo import EchoBackend

            return EchoBackend()
        if self.cfg.backend == "claude":
            from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

            # Same per-call timeout as the subagents (`--llm-timeout`).
            return ClaudeCodeBackend(
                ClaudeCodeConfig(
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        if self.cfg.backend == "codex":
            from zero.llm.codex_cli import CodexCLIBackend, CodexCLIConfig

            return CodexCLIBackend(
                CodexCLIConfig(
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        if self.cfg.backend == "replay":
            from zero.llm.replay import ReplayBackend

            if self.cfg.replay_audit_path is None:
                raise ValueError("replay backend requires replay_audit_path")
            return ReplayBackend(self.cfg.replay_audit_path)
        raise ValueError(f"unknown backend: {self.cfg.backend!r}")

    def _build_subagent_backend(self) -> LLMBackend:
        """Backend for pentester subagents — same per-call wall-clock timeout
        (`--llm-timeout`) as the orchestrator so a long iterative exploitation
        step isn't killed mid-run. echo/replay backends are stateless and shared."""
        if self.cfg.backend == "claude":
            from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

            return ClaudeCodeBackend(
                ClaudeCodeConfig(
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        if self.cfg.backend == "codex":
            from zero.llm.codex_cli import CodexCLIBackend, CodexCLIConfig

            return CodexCLIBackend(
                CodexCLIConfig(
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        return self.backend

    def _build_validator_backend(self) -> LLMBackend:
        """A separate backend for the Validator with a much higher per-call
        turn cap (so its from-scratch investigation isn't capped at the pentest
        agent's `--max-turns`) and the shared per-call timeout (`--llm-timeout`).
        echo/replay backends are stateless and shared with the pentest loop."""
        if self.cfg.backend == "claude":
            from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

            return ClaudeCodeBackend(
                ClaudeCodeConfig(
                    max_turns=self.cfg.validator_claude_max_turns,
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        if self.cfg.backend == "codex":
            from zero.llm.codex_cli import CodexCLIBackend, CodexCLIConfig

            # Codex has no per-call internal turn cap (codex exec runs to completion),
            # so the validator backend is just a fresh Codex backend — the
            # validator_claude_max_turns headroom is a Claude-only concern.
            return CodexCLIBackend(
                CodexCLIConfig(
                    timeout_seconds=self.cfg.llm_timeout_seconds,
                    model=self.cfg.model,
                )
            )
        return self.backend

    # ------------------------------------------------------------- public api

    async def run_interactive(self) -> None:
        """Spawn agent + UI as concurrent tasks; await UI exit."""
        from zero.ui.app import run_ui

        # Per-agent LLM I/O tracing (on by default). $ZERO_LLM_LOG_DIR overrides the
        # location; it's honoured lazily by llm_trace, so only set a root here when the
        # env var is absent — and only when not explicitly disabled via --no-llm-log.
        if self.cfg.llm_log and not os.environ.get("ZERO_LLM_LOG_DIR"):
            from zero.llm.llm_trace import set_llm_log_root
            set_llm_log_root(self.cfg.cwd / ".pentest" / "llm")

        project_ctx = project_context_for(self.cfg.cwd)
        effective_model = self._effective_model_name()
        self.audit.write(
            "session_start",
            cwd=str(self.cfg.cwd),
            backend=self.cfg.backend,
            goal=self.cfg.goal,
            has_project_context=project_ctx is not None,
            use_container=self.cfg.use_container,
            execution_mode=self.cfg.execution_mode,
            model=effective_model,
            context_window=self.cfg.context_window,
        )

        # Startup banner: print the EFFECTIVE backend config the running process
        # actually loaded. A long-running interactive session started before a code
        # change keeps the old in-memory code (Python doesn't hot-reload), which is
        # the #1 cause of "subagents can't use tools" after a fix — this makes it
        # obvious whether the fix is live (model / tool lock-down / context window).
        be_cfg = getattr(self.backend, "cfg", None)
        model = effective_model
        locked = "locked-down" if getattr(be_cfg, "lock_down_tools", False) else "default"
        await self._emit(AgentTextDelta(
            text=(
                f"zero · mode {self.cfg.execution_mode} · backend {self.cfg.backend}"
                f" · model {model} · tools {locked} · ctx-window {self.cfg.context_window:,}\n"
            )
        ))
        await self._emit(AgentTextEnd())

        if self.cfg.use_container:
            try:
                await self._start_container_and_swap_tools()
            except Exception as e:  # noqa: BLE001 — Docker missing / not running / image absent
                self.audit.write("container_start_failed", error=f"{type(e).__name__}: {e}")
                for ln in (
                    "",
                    f"⚠  could not start the sandbox container ({type(e).__name__}: {e}).",
                    "   Falling back to HOST execution — code/binary tools will require",
                    "   approval on every call. Install/start Docker (and build the",
                    "   zero-sandbox image) for sandboxed execution, or pass --no-container.",
                ):
                    await self._emit(AgentTextDelta(text=ln + "\n"))
                await self._emit(AgentTextEnd())
                self._container = None
                self._rpc = None

        await self._warn_host_exec()
        await self._announce_files_root()

        # Router needs the (possibly remote-swapped) registry + project ctx.
        # The Validator runs on its own backend with a generous turn budget.
        self.router = AgentRouter(
            store=self.findings_store,
            backend=self.validator_backend,
            tools=self.registry.catalog(),
            project_context=project_ctx,
            validator_max_turns=self.cfg.validator_max_turns,
            goal=self.cfg.goal,
            session_factory=self._make_session,
        )

        if self.cfg.ralph:
            from zero.ralph.driver import RalphDriver

            agent_task = asyncio.create_task(
                RalphDriver(self, project_ctx).run(), name="ralph"
            )
        elif self.cfg.execution_mode == "b":
            agent_task = asyncio.create_task(
                self._agent_main_mode_b(project_ctx), name="agent_mode_b"
            )
        elif self.cfg.execution_mode == "c":
            agent_task = asyncio.create_task(
                self._orchestrator_main(project_ctx), name="orchestrator"
            )
        else:
            agent_task = asyncio.create_task(self._agent_main(project_ctx), name="agent")
        try:
            await run_ui(self)
        finally:
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass
            await self._close_tool_sessions()
            await self._teardown_container()
            self.audit.write("session_end")
            self.audit.close()
            self.thinking.close()

    async def _close_tool_sessions(self) -> None:
        """Tear down any persistent tool state (python_exec interpreter workers).
        In `--container` mode the workers live in the sandbox and die with it; in
        host mode they're our child processes and must be reaped here."""
        for name in self.registry.names():
            tool = self.registry.get(name)
            aclose = getattr(tool, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _warn_host_exec(self) -> None:
        """Surface the host-execution posture at startup. After any container
        swap, tools whose `runs_on_host` is still True execute on the host."""
        host_tools = sorted(
            name for name in self.registry.names()
            if getattr(self.registry.get(name), "runs_on_host", False)
        )
        if not host_tools:
            return
        self.audit.write(
            "host_exec_notice",
            tools=host_tools,
            container=self.cfg.use_container,
            allow_host_exec=self.cfg.allow_host_exec,
        )
        if self.cfg.allow_host_exec:
            lines = [
                "",
                "⚠  HOST EXECUTION AUTO-APPROVAL IS ON (--allow-host-exec).",
                f"   These tools run code/binaries on THIS machine without prompting: {', '.join(host_tools)}.",
                "   Use --container --remote-all-tools to sandbox them instead.",
            ]
        else:
            lines = [
                "",
                f"⚠  These tools execute on the host (no sandbox): {', '.join(host_tools)}.",
                "   Each call will require your approval. Run with --container --remote-all-tools",
                "   to execute them inside the sandbox instead.",
            ]
        for ln in lines:
            await self._emit(AgentTextDelta(text=ln + "\n"))
        await self._emit(AgentTextEnd())

    async def _announce_files_root(self) -> None:
        """Tell the operator whether the files-root folder has data the agent can
        read with list_dir/grep/read_file — so it's obvious at a glance whether the
        intended folder was actually loaded/mounted (vs. an empty default)."""
        root = self._files_root_abs
        try:
            entries = [p for p in root.rglob("*") if p.is_file()]
        except OSError:
            entries = []
        n = len(entries)
        mount = "/zero_files" if self.cfg.use_container else str(root)
        if n:
            sample = ", ".join(sorted(p.name for p in entries)[:5])
            more = f", +{n - 5} more" if n > 5 else ""
            line = (
                f"📂 files root: {root} → {mount}  —  LOADED: {n} file(s) available to "
                f"list_dir/grep/read_file ({sample}{more})."
            )
        else:
            line = (
                f"📂 files root: {root} → {mount}  —  EMPTY: no files loaded. Drop files "
                f"in {root} (or pass --files-root) for the agent to read."
            )
        self.audit.write("files_root_status", root=str(root), mount=mount, file_count=n)
        await self._emit(AgentTextDelta(text="\n" + line + "\n"))
        await self._emit(AgentTextEnd())

    async def _start_container_and_swap_tools(self) -> None:
        from zero.container.manager import ContainerManager, ContainerSpec
        from zero.container.rpc import ContainerRPC
        from zero.tools.remote import wrap_with_remote

        spec = ContainerSpec(
            image=self.cfg.container_image,
            cwd=self.cfg.cwd,
            burp_dir_host=self.cfg.burp_dir,
            files_root=self._files_root_abs,
            cpus=self.cfg.container_cpus,
            memory=self.cfg.container_memory,
            host_network=self.cfg.host_network,
            network=self.cfg.container_network,
        )
        self._container = ContainerManager(spec)
        await self._container.start()
        self.audit.write(
            "container_started",
            name=self._container.name,
            image=spec.image,
            rpc_url=self._container.rpc_base_url(),
        )
        self._rpc = ContainerRPC(
            self._container.rpc_base_url(), recover=self._recover_sandbox
        )
        await self._rpc.connect()

        # Always proxy python_exec (the M4 acceptance test). With
        # --remote-all-tools, proxy the whole registry so every call lands in
        # the sandbox — EXCEPT the memory tools, which must run on the host so
        # they write to .pentest/memory (the memory store is never mounted into
        # the container; the orchestrator reads it host-side).
        names_to_remote = (
            self.registry.names() if self.cfg.remote_all_tools else ["python_exec"]
        )
        names_to_remote = [
            n for n in names_to_remote
            if n not in ("memory_read", "memory_write", "todo_read", "todo_write")
        ]
        for name in names_to_remote:
            local = self.registry.get(name)
            self.registry.replace(wrap_with_remote(local, self._rpc))
        self.audit.write("tools_remoted", names=names_to_remote)

    async def _recover_sandbox(self, failed_url: str) -> str | None:
        """RPC recovery hook (see ContainerRPC): a tool call hit a transport error, so the
        sandbox is unreachable. If the container is still up it was a transient drop —
        return the same url to reconnect. If it's gone (crash / OOM / operator `docker
        rm`), re-create a fresh sandbox and return its new url. Returns None (give up) when
        there's no container manager or re-creation fails. Best-effort; never raises."""
        if self._container is None:
            return None
        try:
            if await self._container.is_running():
                return self._container.rpc_base_url()  # alive → transient; just reconnect
            self.audit.write("sandbox_recover", reason="container not running")
            await self._container.recreate()
            new_url = self._container.rpc_base_url()
            # RemoteTools share this one ContainerRPC, so updating its base_url (the caller
            # does that with our return) fixes them all — no re-remoting needed. But the new
            # container is a clean slate, so flag a one-time notice for the agent's next
            # observation (its python_exec globals / python_bg jobs are gone).
            self._sandbox_reset_pending = True
            self.audit.write("sandbox_recreated", name=self._container.name, url=new_url)
            await self._emit(ActivityLine(
                text="  ⚠ sandbox was lost — re-created a fresh one (in-container state reset)"
            ))
            return new_url
        except Exception as e:  # noqa: BLE001 — recovery must not raise into the RPC client
            self.audit.write("sandbox_recover_failed", error=f"{type(e).__name__}: {e}")
            return None

    async def _teardown_container(self) -> None:
        if self._rpc is not None:
            try:
                await self._rpc.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._rpc = None
        if self._container is not None:
            try:
                await self._container.stop()
                self.audit.write("container_stopped")
            except Exception as e:  # noqa: BLE001
                self.audit.write("container_stop_error", error=str(e))
            self._container = None

    # --------------------------------------------------------- agent driver

    async def _agent_main_mode_b(self, project_ctx: str | None) -> None:
        """Mode B: hand control to `claude` via MCP for the whole session."""
        import secrets
        from zero.llm.claude_code import ClaudeCodeBackendModeB
        from zero.llm.mcp_bridge import BridgeHandlers, BridgeServer

        await self._emit_status(turn=0, mode="b")

        async def call_tool_via_bridge(name: str, args: dict) -> dict:
            """Replicates _do_action minus the agent-loop bookkeeping."""
            call_id = uuid.uuid4().hex[:8]
            if is_finding_submit(name):
                # Mode B path for finding_submit isn't wired into the router
                # the same way — produce a deferred ack for Phase 1 and rely
                # on the M6 router from inside the main `_agent_main` loop.
                try:
                    finding = args_to_finding(args)
                except Exception as e:  # noqa: BLE001
                    return {"ok": False, "output": f"bad finding args: {e}"}
                await self._emit(
                    ToolProposal(tool=name, args=args, risk_level="safe", call_id=call_id)
                )
                self.audit.write(
                    "finding_submit_modeb", call_id=call_id,
                    title=finding.title, endpoint=finding.endpoint,
                )
                assert self.router is not None

                async def action_handler(action_env):
                    sub = await call_tool_via_bridge(action_env.tool, action_env.args)
                    return sub.get("output", "")

                async def ask_handler(ask_env):
                    return await self._do_ask_user(ask_env, agent="validator")

                from zero.orchestrator.router import default_validator_driver

                async def driver(sub_loop):
                    return await default_validator_driver(
                        sub_loop, action_handler=action_handler, ask_handler=ask_handler,
                    )

                res = await self.router.handle_finding_submit(finding, drive_validator=driver)
                return {"ok": True, "output": res.observation_to_pentest}

            if name not in self.registry:
                obs = f"unknown tool: {name!r}"
                self.audit.write("tool_unknown_modeb", tool=name, call_id=call_id)
                return {"ok": False, "output": obs}
            tool = self.registry.get(name)
            await self._emit(
                ToolProposal(
                    tool=name, args=args,
                    risk_level=tool.risk_level.value, call_id=call_id,
                )
            )
            self.audit.write(
                "tool_proposal_modeb", tool=name, args=args,
                risk_level=tool.risk_level.value, call_id=call_id, source="mcp",
            )
            req = ApprovalRequest(
                tool=name, args=args, risk_level=tool.risk_level, call_id=call_id,
            )
            allowed = await self.gate.check(req)
            self.audit.write("approval_decision_modeb", call_id=call_id, allowed=allowed)
            if not allowed:
                obs = "user denied tool call"
                await self._emit(ToolObservation(
                    tool=name, call_id=call_id, ok=False, output=obs,
                ))
                return {"ok": False, "output": obs}
            result: ToolResult = await self._invoke_tool(tool, args, session_id="modeb")
            await self._emit(ToolObservation(
                tool=name, call_id=call_id, ok=result.ok,
                output=result.output, truncated=result.truncated,
            ))
            self.audit.write(
                "tool_observation_modeb",
                tool=name, call_id=call_id, ok=result.ok,
                output=result.output[:4000], truncated=result.truncated, source="mcp",
            )
            return {
                "ok": result.ok, "output": result.output,
                "truncated": result.truncated, "meta": result.meta,
            }

        async def ask_via_bridge(
            question: str, options: list[str], free_form: bool, blocking: bool
        ) -> str:
            from zero.llm.envelope import AskUserEnvelope

            env = AskUserEnvelope(
                question=question, options=options,
                free_form=free_form, blocking=blocking,
            )
            return await self._do_ask_user(env, agent="pentest")

        handlers = BridgeHandlers(
            catalog=self.registry.catalog(),
            call_tool=call_tool_via_bridge,
            ask_user=ask_via_bridge,
        )
        bridge = BridgeServer(handlers)
        await bridge.start()
        self.audit.write("mcp_bridge_started", url=bridge.base_url)
        auth_token = secrets.token_urlsafe(24)

        prompt_pieces = []
        if project_ctx:
            prompt_pieces.append(project_ctx)
        prompt_pieces.append(
            "You are running in zero as the Pentest agent (Mode B / MCP)."
            " Use the MCP tools registered under server 'zero' for every action."
            " Do not use built-in web tools — they have been disabled."
            " When you have a finding, call the `finding_submit` MCP tool."
        )
        if self.cfg.goal:
            prompt_pieces.append("\n\nGoal: " + self.cfg.goal)
        prompt = "\n\n".join(prompt_pieces)

        try:
            from zero.llm.claude_code import ModeBConfig

            be = ClaudeCodeBackendModeB(ModeBConfig(model=self.cfg.model))
            result_text = await be.run_session(
                prompt=prompt,
                bridge_url=bridge.base_url,
                auth_token=auth_token,
            )
            await self._emit(AgentTextDelta(text=f"\n[final] {result_text}\n"))
            await self._emit(AgentTextEnd())
            self.audit.write("final", answer=result_text, source="mcp")
        except Exception as e:  # noqa: BLE001
            await self._emit(AgentTextDelta(text=f"\n[mode-b error] {e}\n"))
            await self._emit(AgentTextEnd())
            self.audit.write("mode_b_error", error=str(e))
        finally:
            await bridge.stop()
            self.audit.write("mcp_bridge_stopped")
            await self._emit(SessionEnd())

    async def _agent_main(self, project_ctx: str | None) -> None:
        """Interactive session loop — Claude-Code-style.

        Lifecycle:
          1. If --goal was passed, seed it as the first user message.
             Otherwise sit idle and wait for the user to type something.
          2. Run the agent until it emits a `final` envelope (or runs out
             of turns).
          3. After the final, *don't* exit — go back to (1) and wait for
             the next user message. Conversation history is preserved so
             the agent remembers context across exchanges.
          4. Only exit when the user closes the UI (/exit or double Ctrl+C).
        """
        loop = build_pentest_loop(
            backend=self.backend,
            tools=self.registry.catalog(),
            project_context=project_ctx,
            max_turns=self.cfg.max_turns,
            session=self._make_session("pentest"),
        )
        self._active_loop = loop  # for the status bar's context-window usage
        if self.cfg.goal:
            loop.add_user_message(self.cfg.goal)
            self.audit.write("user_message", text=self.cfg.goal, source="cli")
            await self._emit(GoalBanner(text=self.cfg.goal))

        await self._emit_status(turn=0, mode="a")

        # Tracks whether the agent just produced a final and we should
        # wait for a new user message before running any more turns.
        needs_user_input = self.cfg.goal is None
        consecutive_turn_errors = 0

        try:
            while True:
                # ---- Idle phase: block on user input if there's nothing
                # to act on (no --goal at startup, or the last exchange
                # produced a final).
                if needs_user_input:
                    await self._emit(AgentTextDelta(text="\n"))
                    await self._emit(AgentTextDelta(text="ready — type a goal / question:"))
                    await self._emit(AgentTextEnd())
                    msg = await self.user_queue.get()
                    loop.add_user_message(msg)
                    self.audit.write("user_message", text=msg, source="ui")
                    # Fresh turn budget for the new exchange so a long
                    # chat doesn't hit the cap.
                    loop._turn = 0
                    needs_user_input = False

                # ---- Working phase: drain any extra user input typed
                # while we were idle / working, then run one turn.
                while not self.user_queue.empty():
                    extra = self.user_queue.get_nowait()
                    loop.add_user_message(extra)
                    self.audit.write("user_message", text=extra, source="ui")
                self._user_input_event.clear()

                turn_task = asyncio.create_task(self._do_turn(loop), name="agent_turn")
                self._current_turn_task = turn_task
                try:
                    env = await turn_task
                except asyncio.CancelledError:
                    await self._emit(TurnCancelled())
                    self.audit.write("turn_cancelled")
                    loop.add_observation(
                        tool="(system)", ok=False, output="interrupted_by_user"
                    )
                    # After a cancel, go back to waiting for user input
                    # — the user pressed Ctrl+C to redirect the agent,
                    # they probably want to type something.
                    needs_user_input = True
                    continue
                except Exception as e:  # noqa: BLE001
                    # Don't let a backend error / invalid-JSON envelope silently
                    # kill the agent task. Feed it back and retry; only surrender
                    # to the operator after several consecutive failures.
                    consecutive_turn_errors += 1
                    self.audit.write(
                        "turn_error",
                        error=f"{type(e).__name__}: {e}",
                        consecutive=consecutive_turn_errors,
                    )
                    loop.add_observation(
                        tool="(system)", ok=False,
                        output=(
                            f"your previous turn failed: {e}. "
                            "Reply with EXACTLY ONE valid JSON envelope (action / ask_user / "
                            "final) — no prose, no markdown fence, no trailing data."
                        ),
                    )
                    if consecutive_turn_errors >= _MAX_CONSECUTIVE_TURN_ERRORS:
                        await self._emit(AgentTextDelta(
                            text=(
                                f"\n[turn error ×{consecutive_turn_errors}] "
                                f"{type(e).__name__}: {e}\n   Type a message to continue, or /exit.\n"
                            )
                        ))
                        await self._emit(AgentTextEnd())
                        consecutive_turn_errors = 0
                        needs_user_input = True
                    else:
                        await self._emit(AgentTextDelta(
                            text=f"\n[turn error] {type(e).__name__}: {e} — retrying…\n"
                        ))
                        await self._emit(AgentTextEnd())
                    continue
                finally:
                    self._current_turn_task = None
                # Turn succeeded — reset the failure streak.
                consecutive_turn_errors = 0

                if isinstance(env, FinalEnvelope):
                    await self._emit(AgentTextDelta(text=f"\n[final] {env.answer}\n"))
                    await self._emit(AgentTextEnd())
                    self.audit.write("final", answer=env.answer)
                    needs_user_input = True
                    continue  # back to idle phase, NOT exit
                if isinstance(env, AskUserEnvelope):
                    answer = await self._do_ask_user(env)
                    loop.add_ask_user_answer(answer)
                    self.audit.write(
                        "ask_user_answered", question=env.question, answer=answer
                    )
                    continue
                if isinstance(env, (ActionEnvelope, ActionsEnvelope)):
                    await self._do_action(loop, env)
                    continue
        except asyncio.CancelledError:
            # UI requested shutdown.
            pass
        finally:
            # Give in-flight background validations a brief grace to finish so
            # the summary reflects them, then cancel any stragglers.
            if self._validation_tasks:
                try:
                    await asyncio.wait(set(self._validation_tasks), timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                for t in list(self._validation_tasks):
                    t.cancel()
            self._total_turns = max(self._total_turns, getattr(loop, "_turn", 0))
            await self._emit(
                SessionSummary(
                    confirmed=list(self._session_findings["confirmed"]),
                    rejected=list(self._session_findings["rejected"]),
                    pending=list(self._session_findings["pending"]),
                    findings_dir=str(self.findings_store.root),
                    audit_path=str(self.audit.path),
                    turns=self._total_turns,
                )
            )
            await self._emit(SessionEnd())

    async def _do_turn(self, loop, *, agent: str = "pentest", mode: str = "a") -> Any:
        # Emit BEFORE the LLM call so the user sees the agent is working
        # during the (potentially multi-second) wait. loop.turn updates
        # inside loop.step(), so we render `current_turn + 1`.
        await self._emit(TurnStart(agent=agent, turn=loop.turn + 1))
        step = await loop.step()
        env = step.envelope
        # The FULL thought is always written to thinking.log here, regardless of
        # how much (if any) of it we show in the chat.
        self._log_thinking(agent, loop.turn, env)
        if isinstance(env, (ActionEnvelope, ActionsEnvelope)) and env.thought:
            shown = self._thought_for_chat(env.thought)
            if shown:
                # A concise reasoning summary for this step (Codex/Claude-Code
                # style). The full thought is always in thinking.log.
                await self._emit(ReasoningSummary(agent=agent, turn=loop.turn, text=shown))
        await self._emit_status(turn=loop.turn, mode=mode)
        self.audit.write(
            "agent_step",
            turn=loop.turn,
            agent=agent,
            envelope_type=env.__class__.__name__,
            raw=step.raw_text,
            usage=step.usage,
        )
        return env

    def _thought_for_chat(self, thought: str) -> str:
        """How much of the agent's free-form thought to show in the chat, per
        `--thought-display`. The full thought is always in thinking.log."""
        from zero.ui.render import short_reason

        mode = self.cfg.thought_display
        if mode == "off":
            return ""
        if mode == "full":
            return thought
        # "condensed" (default): the agent's reasoning headline. Take the first
        # line (agents are told to lead `thought` with a short headline), then
        # short_reason trims it to one clean sentence — so it works whether the
        # model separates the headline with a newline or a period.
        first_line = thought.strip().splitlines()[0] if thought.strip() else ""
        return short_reason(first_line)

    async def _handle_one_agent_call(self, loop, call: ActionEnvelope) -> None:
        # finding_submit is orchestrator-intercepted (M6).
        if is_finding_submit(call.tool):
            await self._handle_finding_submit(loop, call, uuid.uuid4().hex[:8])
            return
        result = await self._run_one_tool(
            call.tool, call.args, reason=call.thought, session_id="agent"
        )
        loop.add_observation(call.tool, result.ok, result.output)

    async def _do_action(self, loop, env) -> None:
        if isinstance(env, ActionsEnvelope):
            # finding_submit mutates shared state — run the batch sequentially.
            for call in env.calls:
                await self._handle_one_agent_call(loop, call)
            return
        await self._handle_one_agent_call(loop, env)

    def _log_thinking(self, agent: str, turn: int, env: Any) -> None:
        """Append one readable reasoning entry for an agent step (orchestrator
        or any subagent) to `.pentest/thinking.log`."""
        thought = ""
        action = ""
        if isinstance(env, ActionEnvelope):
            thought = env.thought
            summary = json.dumps(env.args, ensure_ascii=False)
            if len(summary) > 200:
                summary = summary[:200] + "…"
            action = f"action: {env.tool}({summary})"
        elif isinstance(env, ActionsEnvelope):
            thought = env.thought
            action = "actions: " + ", ".join(c.tool for c in env.calls)
        elif isinstance(env, AskUserEnvelope):
            action = f"ask_user: {env.question}"
        elif isinstance(env, FinalEnvelope):
            ans = env.answer if len(env.answer) <= 400 else env.answer[:400] + "…"
            action = f"final: {ans}"
        try:
            self.thinking.write(agent=agent, turn=turn, thought=thought, action=action)
        except Exception:  # noqa: BLE001 — logging must never break a run
            pass

    async def _run_one_tool(
        self, tool_name: str, args: dict, *, label: str = "", reason: str = "",
        session_id: str = "orchestrator",
    ) -> ToolResult:
        """Propose → gate → invoke → observe one registry tool. Shared by the
        pentest loop (mode a), the orchestrator's own recon (mode c), and every
        subagent's tool calls. `label` attributes the call to a subagent in the
        UI + approval prompt (empty for the top-level agent — preserving mode-a
        output byte-for-byte). `session_id` isolates the caller's persistent
        python_exec interpreter (one per actor)."""
        call_id = uuid.uuid4().hex[:8]
        if tool_name not in self.registry:
            obs = f"unknown tool: {tool_name!r}"
            await self._emit(ToolObservation(tool=tool_name, call_id=call_id, ok=False, output=obs, label=label))
            self.audit.write("tool_unknown", tool=tool_name, call_id=call_id, label=label)
            return ToolResult(ok=False, output=obs)

        tool = self.registry.get(tool_name)
        await self._emit(
            ToolProposal(
                tool=tool_name, args=args, risk_level=tool.risk_level.value,
                call_id=call_id, label=label,
            )
        )
        self.audit.write(
            "tool_proposal", tool=tool_name, args=args,
            risk_level=tool.risk_level.value, call_id=call_id, label=label,
        )

        if not getattr(tool, "requires_approval", True):
            # Local, no-side-effect tools (burp_traffic) bypass the gate.
            allowed = True
        else:
            req = ApprovalRequest(
                tool=tool_name, args=args, risk_level=tool.risk_level, call_id=call_id,
                label=label, host_exec=getattr(tool, "runs_on_host", False), reason=reason,
            )
            allowed = await self.gate.check(req)
        self.audit.write("approval_decision", call_id=call_id, allowed=allowed, label=label)
        if not allowed:
            obs = "user denied the tool call"
            await self._emit(ToolObservation(tool=tool_name, call_id=call_id, ok=False, output=obs, label=label))
            return ToolResult(ok=False, output=obs)

        # Tell the UI the tool is now executing so a long run (e.g. a 300s ffuf in a
        # batch) shows a live "running" indicator instead of looking hung. Prefer the
        # per-call timeout from args, falling back to the tool class's wall-clock cap.
        tv = args.get("timeout_seconds") if isinstance(args, dict) else None
        timeout = int(tv) if isinstance(tv, (int, float)) and tv > 0 else int(
            getattr(tool, "timeout_seconds", 0) or 0
        )
        await self._emit(
            ToolRunning(tool=tool_name, call_id=call_id, label=label, timeout_seconds=timeout)
        )
        result = await self._invoke_tool(tool, args, session_id=session_id)
        # If the sandbox was re-created mid-call (RPC recovery), tell the agent ONCE so it
        # doesn't assume its in-container state (python_exec globals, python_bg jobs, files
        # written only under /work) survived. Host-side state (.pentest/, zero_files/) did.
        if self._sandbox_reset_pending:
            self._sandbox_reset_pending = False
            result = ToolResult(
                ok=result.ok,
                output=(
                    "[system: the sandbox was lost and re-created as a fresh container; "
                    "in-container session state (python_exec globals/imports, python_bg "
                    "jobs, /work-only files) was RESET — re-establish anything you relied "
                    "on. Files under your files-root and findings are intact.]\n"
                    + result.output
                ),
                truncated=result.truncated,
                meta=result.meta,
            )
        await self._emit(
            ToolObservation(
                tool=tool_name, call_id=call_id,
                ok=result.ok, output=result.output, truncated=result.truncated, label=label,
            )
        )
        # Surface a plan update as a checklist (Claude-Code TodoWrite). Labelled
        # (subagent) todos are routed out of the main chat by the UI.
        if tool_name == "todo_write" and result.ok:
            await self._emit(TodoUpdate(items=list(args.get("items", [])), label=label))
        self.audit.write(
            "tool_observation", tool=tool_name, call_id=call_id,
            ok=result.ok, output=result.output[:4000], truncated=result.truncated, label=label,
        )
        return result

    # ===================================================== orchestrated mode (c)

    def _recon_catalog(self) -> list[dict]:
        """The safe+active registry tools an agent recons with (no finding_submit,
        no dispatch). Shared by the orchestrator and the Ralph analyzer roles."""
        return [
            t for t in self.registry.catalog()
            if t["risk_level"] in ("safe", "active") and t["name"] != "finding_submit"
        ]

    def _orchestrator_catalog(self) -> list[dict]:
        """Orchestrator tool catalog: recon (safe+active) registry tools + the
        intercepted-only dispatch / memory-inspect / custom-tool-mgmt tools. The
        latter are NOT in default_registry, so modes a/b never see them; they're
        handled in _do_orchestrator_action and never run()/remoted."""
        disp = [
            {
                "name": t.name,
                "description": t.description,
                "risk_level": t.risk_level.value,
                "schema": t.json_schema(),
            }
            for t in dispatch_tools() + inspect_tools() + custom_mgmt_tools()
        ]
        return self._recon_catalog() + disp

    async def _drive_agent_to_final(
        self, loop, *, agent_label: str, session_id: str, deadline_seconds: int | None = None
    ) -> str:
        """Drive a simple (non-orchestrating) agent loop to its `final`, running any
        tool calls, and return the final answer text. Used by Ralph's analyzer /
        analyzer_validator. Unattended: an `ask_user` is auto-answered to proceed.
        Bounded by the loop's own turn cap plus a wall-clock deadline; on repeated
        backend errors it returns a short diagnostic string instead of crashing."""
        start = time.monotonic()
        deadline = start + (deadline_seconds or self.cfg.subagent_deadline_seconds)
        consecutive = 0
        while True:
            if time.monotonic() > deadline:
                return f"(deadline reached after {loop.turn} turns; no final emitted)"
            try:
                env = await self._do_turn(loop, agent=agent_label, mode="ralph")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a backend glitch must not crash the loop
                consecutive += 1
                self.audit.write(
                    "ralph_agent_turn_error", agent=agent_label,
                    error=f"{type(e).__name__}: {e}", consecutive=consecutive,
                )
                if consecutive >= _MAX_CONSECUTIVE_TURN_ERRORS:
                    return f"(stopped after {consecutive} backend errors: {type(e).__name__}: {e})"
                loop.add_observation(
                    "(system)", False,
                    f"your previous turn failed: {e}. Reply with EXACTLY ONE JSON envelope.",
                )
                continue
            consecutive = 0
            if isinstance(env, FinalEnvelope):
                return env.answer
            if isinstance(env, AskUserEnvelope):
                loop.add_ask_user_answer(
                    "No operator is available (Ralph unattended planning). Proceed"
                    " autonomously with your best judgment and emit your final."
                )
                continue
            if isinstance(env, ActionEnvelope):
                result = await self._run_one_tool(
                    env.tool, env.args, reason=env.thought, session_id=session_id
                )
                loop.add_observation(env.tool, result.ok, result.output)
                continue
            if isinstance(env, ActionsEnvelope):
                async def _one(c, _env=env):
                    return await self._run_one_tool(
                        c.tool, c.args, reason=c.thought or _env.thought, session_id=session_id
                    )
                results = await self._run_action_batch(env.calls, _one)
                for c, r in zip(env.calls, results):
                    loop.add_observation(c.tool, r.ok, r.output)
                continue

    async def _run_orchestrated_pass(
        self, *, seed_messages: list[str], max_turns: int, session_label: str
    ) -> str:
        """Drive ONE bounded orchestrated pass to finalize / turn-cap and return the
        orchestrator's closing summary. Reuses the exact same per-turn machinery as
        `_orchestrator_main` (_do_turn, _do_orchestrator_action, dispatch handling,
        SubagentManager) but WITHOUT the interactive idle / operator-input loop — it
        terminates and returns instead of going idle. This is Ralph's per-iteration
        worker; mode c's own loop is unchanged."""
        self._go_idle = False
        self._last_finalize_summary = ""
        # Per-pass strict-validation counters so the finalize guard is scoped to THIS
        # iteration, not the whole run.
        self._pentester_candidates = 0
        self._validators_dispatched = 0
        self.subagents = SubagentManager(
            max_concurrent=self.cfg.max_concurrent_subagents,
            max_total=self.cfg.max_total_subagents,
            runner=self._run_subagent,
            emit=self._emit,
        )
        loop = build_orchestrator_loop(
            backend=self.backend,
            tools=self._orchestrator_catalog(),
            project_context=self._project_ctx,
            max_turns=max_turns,
            session=self._make_session(session_label),
        )
        self._active_loop = loop
        base_system_prompt = loop.system_prompt
        for msg in seed_messages:
            loop.add_user_message(msg)
        await self._emit_status(turn=0, mode="ralph")

        final_summary = ""
        consecutive_turn_errors = 0
        try:
            while True:
                loop.system_prompt = base_system_prompt + _orchestrator_turn_note(
                    loop.turn, loop.max_turns
                )
                try:
                    env = await self._do_turn(loop, agent="orchestrator", mode="ralph")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    consecutive_turn_errors += 1
                    self.audit.write(
                        "turn_error", error=f"{type(e).__name__}: {e}",
                        consecutive=consecutive_turn_errors,
                    )
                    loop.add_observation(
                        "(system)", False,
                        f"your previous turn failed: {e}. Reply with EXACTLY ONE valid JSON"
                        " envelope (action / actions / final).",
                    )
                    if consecutive_turn_errors >= _MAX_CONSECUTIVE_TURN_ERRORS:
                        final_summary = f"(pass aborted after {consecutive_turn_errors} errors: {e})"
                        break
                    continue
                consecutive_turn_errors = 0

                if isinstance(env, FinalEnvelope):
                    if loop.turn >= loop.max_turns:
                        await self._emit_report(
                            loop,
                            summary=f"Ralph iteration hit the {loop.max_turns}-turn budget.",
                        )
                    final_summary = env.answer
                    break
                if isinstance(env, AskUserEnvelope):
                    answer = (
                        "No operator is available (Ralph unattended iteration). Proceed"
                        " autonomously: dispatch/validate as needed and call finalize when"
                        " this task is adequately covered."
                    )
                    loop.add_ask_user_answer(answer)
                    self.audit.write("ask_user_auto_answered", question=env.question)
                    continue
                if isinstance(env, (ActionEnvelope, ActionsEnvelope)):
                    await self._do_orchestrator_action(loop, env)
                    if self._go_idle:
                        # finalize fired and already wrote the report; the pass is done.
                        self._go_idle = False
                        final_summary = (
                            self._last_finalize_summary
                            or getattr(env, "thought", "")
                            or "iteration finalized."
                        )
                        break
                    continue
        finally:
            # Always let this pass's subagents finish so nothing leaks into the next
            # iteration (their findings are what the completion check reads).
            if self.subagents is not None:
                try:
                    await asyncio.wait_for(
                        self.subagents.await_("all", timeout=None), timeout=600
                    )
                except Exception:  # noqa: BLE001
                    pass
        return final_summary

    async def _orchestrator_main(self, project_ctx: str | None) -> None:
        """Senior-pentester Orchestrator loop (M8): recon directly, dispatch
        concurrent pentester/validator subagents, chain results, then finalize.
        Same idle/working shape as `_agent_main`."""
        self._project_ctx = project_ctx
        self._go_idle = False
        self.subagents = SubagentManager(
            max_concurrent=self.cfg.max_concurrent_subagents,
            max_total=self.cfg.max_total_subagents,
            runner=self._run_subagent,
            emit=self._emit,
        )
        loop = build_orchestrator_loop(
            backend=self.backend,
            tools=self._orchestrator_catalog(),
            project_context=project_ctx,
            max_turns=self.cfg.max_turns,
            session=self._make_session("orchestrator"),
        )
        self._active_loop = loop  # for the status bar's context-window usage
        base_system_prompt = loop.system_prompt
        # Resume from the orchestrator's own memory if a prior turn/session saved
        # one (its session_id is the stable "orchestrator", so this persists across
        # finalize→idle→next prompt). Seed as a user message, not the system prompt,
        # which is rewritten every turn (and to keep the prompt cacheable).
        own_memory = read_memory("orchestrator")
        if own_memory:
            loop.add_user_message(_seed_memory_message(own_memory))
        if self.cfg.goal:
            loop.add_user_message(self.cfg.goal)
            self.audit.write("user_message", text=self.cfg.goal, source="cli")
            await self._emit(GoalBanner(text=self.cfg.goal))

        await self._emit_status(turn=0, mode="c")
        needs_user_input = self.cfg.goal is None
        # A single bad turn (e.g. the model emitted invalid JSON) shouldn't make
        # the orchestrator surrender to the operator — feed the error back and let
        # it retry. Only after several consecutive failures do we go idle.
        consecutive_turn_errors = 0

        try:
            while True:
                if needs_user_input:
                    await self._emit(AgentTextDelta(text="\n"))
                    await self._emit(AgentTextDelta(text="ready — describe the engagement / target:"))
                    await self._emit(AgentTextEnd())
                    msg = await self.user_queue.get()
                    loop.add_user_message(msg)
                    self.audit.write("user_message", text=msg, source="ui")
                    loop._turn = 0
                    needs_user_input = False

                while not self.user_queue.empty():
                    extra = self.user_queue.get_nowait()
                    loop.add_user_message(extra)
                    self.audit.write("user_message", text=extra, source="ui")
                # All pending operator input is now in the conversation.
                self._user_input_event.clear()

                # Turn awareness: refresh the live turns-remaining note each turn
                # so the orchestrator paces itself toward `finalize`.
                loop.system_prompt = base_system_prompt + _orchestrator_turn_note(
                    loop.turn, loop.max_turns
                )

                turn_task = asyncio.create_task(
                    self._do_turn(loop, agent="orchestrator", mode="c"), name="orchestrator_turn"
                )
                self._current_turn_task = turn_task
                try:
                    env = await turn_task
                except asyncio.CancelledError:
                    await self._emit(TurnCancelled())
                    self.audit.write("turn_cancelled")
                    loop.add_observation(tool="(system)", ok=False, output="interrupted_by_user")
                    needs_user_input = True
                    continue
                except Exception as e:  # noqa: BLE001
                    # A backend timeout / `claude -p` failure / invalid-JSON
                    # envelope must NOT silently kill the orchestrator task (that
                    # leaves the UI alive but inert — the gruyere hang). Feed the
                    # error back as an observation and let the model RETRY on its
                    # own; only surrender to the operator after several consecutive
                    # failures so a transient JSON glitch self-heals instead of
                    # looking like a crash.
                    consecutive_turn_errors += 1
                    self.audit.write(
                        "turn_error",
                        error=f"{type(e).__name__}: {e}",
                        consecutive=consecutive_turn_errors,
                    )
                    loop.add_observation(
                        tool="(system)", ok=False,
                        output=(
                            f"your previous turn failed: {e}. "
                            "Reply with EXACTLY ONE valid JSON envelope (action / ask_user / "
                            "final) — no prose, no markdown fence, no trailing data."
                        ),
                    )
                    if consecutive_turn_errors >= _MAX_CONSECUTIVE_TURN_ERRORS:
                        await self._emit(AgentTextDelta(
                            text=(
                                f"\n[turn error ×{consecutive_turn_errors}] "
                                f"{type(e).__name__}: {e}\n"
                                "   Giving up this line — type a message to continue,"
                                " or /exit to quit.\n"
                            )
                        ))
                        await self._emit(AgentTextEnd())
                        consecutive_turn_errors = 0
                        needs_user_input = True
                    else:
                        await self._emit(AgentTextDelta(
                            text=f"\n[turn error] {type(e).__name__}: {e} — retrying…\n"
                        ))
                        await self._emit(AgentTextEnd())
                    continue
                finally:
                    self._current_turn_task = None
                # A turn completed without raising — reset the failure streak.
                consecutive_turn_errors = 0

                if isinstance(env, FinalEnvelope):
                    if loop.turn >= loop.max_turns:
                        # Turn budget reached. End the engagement cleanly: drain
                        # subagents, write the report, then go idle — never leave
                        # a run at the limit without a report (the bare turn-cap
                        # `final` from the loop would otherwise do exactly that).
                        await self._emit(AgentTextDelta(
                            text=(
                                f"\n[turn budget reached: {loop.max_turns} turns] "
                                "waiting for running subagents to finish, then finalizing.\n"
                            )
                        ))
                        await self._emit(AgentTextEnd())
                        self.audit.write(
                            "turn_limit_reached", turns=loop.turn, max_turns=loop.max_turns
                        )
                        await self._emit_report(
                            loop,
                            summary=(
                                f"Engagement stopped at the {loop.max_turns}-turn budget; "
                                "reporting what was confirmed/validated so far."
                            ),
                        )
                        needs_user_input = True
                        continue
                    await self._emit(AgentTextDelta(text=f"\n[final] {env.answer}\n"))
                    await self._emit(AgentTextEnd())
                    self.audit.write("final", answer=env.answer)
                    needs_user_input = True
                    continue
                if isinstance(env, AskUserEnvelope):
                    answer = await self._do_ask_user(env, agent="orchestrator")
                    loop.add_ask_user_answer(answer)
                    self.audit.write("ask_user_answered", question=env.question, answer=answer)
                    continue
                if isinstance(env, (ActionEnvelope, ActionsEnvelope)):
                    await self._do_orchestrator_action(loop, env)
                    if self._go_idle:
                        # finalize produced a report; wait for the next prompt
                        # instead of closing the session.
                        self._go_idle = False
                        needs_user_input = True
                    continue
        except asyncio.CancelledError:
            pass
        finally:
            # The session only ends when the UI exits (/exit or Ctrl+C); just
            # stop any in-flight subagents so they don't leak.
            if self.subagents is not None:
                await self.subagents.cancel_all()

    async def _do_orchestrator_action(self, loop, env) -> None:
        if isinstance(env, ActionsEnvelope):
            # Run the batch through the concurrent executor so a slow recon call does
            # NOT block the dispatches behind it. (Regression seen on Guild: a
            # python_exec recursive glob batched ahead of 3 dispatch_pentester calls
            # held them up ~10 min until the RPC died — pentesters "failed to spin".)
            # python_exec/pip_install are serialized by the executor; dispatch/inspect
            # only mutate shared bookkeeping synchronously (no await between read and
            # write), so concurrent execution is safe.
            await self._run_action_batch(
                env.calls, lambda c: self._handle_one_orch_call(loop, c)
            )
            return
        await self._handle_one_orch_call(loop, env)

    async def _handle_one_orch_call(self, loop, env: ActionEnvelope) -> None:
        if is_dispatch_tool(env.tool):
            obs = await self._handle_dispatch(loop, env)
            if obs is not None:
                loop.add_observation(env.tool, True, obs)
            return
        if is_inspect_tool(env.tool):
            obs = await self._handle_inspect(env)
            loop.add_observation(env.tool, True, obs)
            return
        if is_custom_mgmt_tool(env.tool):
            obs = await self._handle_custom_mgmt(env)
            loop.add_observation(env.tool, True, obs)
            return
        # A recon tool the orchestrator runs directly.
        result = await self._run_one_tool(env.tool, env.args, reason=env.thought)
        loop.add_observation(env.tool, result.ok, result.output)

    async def _handle_inspect(self, env: ActionEnvelope) -> str:
        """Orchestrator-only memory inspection. Reads any subagent's memory file
        (host-side) — handled here, like the dispatch tools, so it never gets
        remoted into the sandbox."""
        if env.tool == "list_memories":
            root = memory_root()
            ids = sorted(p.stem for p in root.glob("*.md")) if root.exists() else []
            await self._emit(ActivityLine(text=f"  ▸ listing memories ({len(ids)})"))
            self.audit.write("list_memories", ids=ids)
            if not ids:
                return "no agent memories saved yet."
            return "agent memories on record: " + ", ".join(ids)
        # read_subagent_memory
        try:
            a = ReadSubagentMemoryArgs(**env.args)
        except Exception as e:  # noqa: BLE001
            return f"bad read_subagent_memory args: {e}"
        body = read_memory(a.agent_id)
        await self._emit(ActivityLine(text=f"  ▸ reading memory of {a.agent_id}"))
        self.audit.write("read_subagent_memory", agent_id=a.agent_id, found=bool(body))
        if not body:
            return f"no memory on record for {a.agent_id!r} (it may not have saved any)."
        return f"=== memory of {a.agent_id} ===\n{body}"

    async def _handle_custom_mgmt(self, env: ActionEnvelope) -> str:
        """Orchestrator-only custom-tool management (create/modify/delete). Like
        the dispatch tools it is handled here — the scripts are written host-side
        into the files-root jail (mounted into the sandbox), never remoted. Wrapped
        broadly so a bad script (SyntaxError, bad name) can never crash the loop."""
        name = (env.args or {}).get("name", "")
        try:
            if env.tool == "custom_tool_delete":
                deleted = delete_tool(name)
                await self._emit(ActivityLine(text=f"  ▸ deleting custom tool {name!r}"))
                self.audit.write("custom_tool_delete", name=name, deleted=deleted)
                return (
                    f"deleted custom tool {name!r}."
                    if deleted
                    else f"no custom tool named {name!r} to delete."
                )
            # create / modify share the same write path (write_tool overwrites).
            description = (env.args or {}).get("description", "")
            body = (env.args or {}).get("script_body", "")
            p = write_tool(name, description, body)
            verb = "modified" if env.tool == "custom_tool_modify" else "created"
            await self._emit(ActivityLine(text=f"  ▸ {verb} custom tool {p.stem!r}"))
            self.audit.write("custom_tool_" + verb, name=p.stem)
            return (
                f"{verb} custom tool {p.stem!r}. Test it with custom_tool_call"
                " before relying on it; your subagents can now list and call it."
            )
        except SyntaxError as e:
            self.audit.write("custom_tool_error", name=name, error=f"SyntaxError: {e}")
            return f"script for {name!r} does not compile — fix and retry: {e}"
        except (ValueError, OSError) as e:
            self.audit.write("custom_tool_error", name=name, error=f"{type(e).__name__}: {e}")
            return f"could not save custom tool {name!r}: {e}"

    async def _handle_dispatch(self, loop, env: ActionEnvelope) -> str | None:
        """Handle an orchestrator dispatch tool. Returns the observation string
        for the orchestrator loop, or None when the session is ending."""
        assert self.subagents is not None
        tool, args = env.tool, env.args

        # Hard total-subagent budget (--max-total-subagents): refuse new dispatches once
        # the run has spawned its quota, BEFORE any per-tool bookkeeping. Counts both
        # pentesters and validators. 0 ⇒ unlimited (at_total_capacity always False).
        if tool in ("dispatch_pentester", "dispatch_validator") and self.subagents.at_total_capacity():
            return (
                f"subagent budget reached ({self.cfg.max_total_subagents} total subagents this "
                f"run) — '{tool}' refused. Do NOT dispatch more: await any running subagents, "
                "validate the strongest candidates already in hand, then call `finalize`."
            )

        if tool == "dispatch_pentester":
            try:
                a = DispatchPentesterArgs(**args)
            except Exception as e:  # noqa: BLE001
                return f"bad dispatch_pentester args: {e}"
            # Turn awareness: near the budget limit, stop opening NEW attack
            # ideas — keep the remaining turn(s) for awaiting + finalizing.
            if loop.max_turns - loop.turn <= 1:
                return (
                    "turn budget nearly exhausted — NOT dispatching a new pentester. "
                    "Stop searching for new vulnerabilities: await any running subagents, "
                    "validate the strongest candidates, then call `finalize`."
                )
            sid = self.subagents.spawn(
                "pentester", target=a.target, idea=a.attack_idea,
                context=a.context, max_turns=a.max_turns,
            )
            self.audit.write("dispatch_pentester", subagent_id=sid, target=a.target, idea=a.attack_idea)
            await self._emit(ActivityLine(
                text=f'  ▸ pentester {sid} → "{a.attack_idea}" @ {a.target}'
            ))
            await self._emit_status()
            return (
                f'dispatched pentester {sid} on "{a.target}" — idea: {a.attack_idea}. '
                f"running now: {self.subagents.running_summary()}. dispatch more independent "
                f"ideas, or call await_subagents to collect results."
            )

        if tool == "dispatch_validator":
            try:
                a = DispatchValidatorArgs(**args)
            except Exception as e:  # noqa: BLE001
                return f"bad dispatch_validator args: {e}"
            finding = a.to_finding()
            h = dedup_hash(finding)
            prior = self.findings_store.lookup_by_hash(h)
            if prior is not None or h in self._inflight_hashes:
                dup = prior["id"] if prior is not None else None
                return (
                    f"validator NOT dispatched — duplicate of {dup}; already on record/in-flight."
                    if dup else
                    "validator NOT dispatched — a matching finding is already being validated."
                )
            self._inflight_hashes.add(h)
            self._validations_pending += 1
            self._validators_dispatched += 1
            sid = self.subagents.spawn(
                "validator", target=finding.endpoint, idea=finding.title, finding=finding,
            )
            self.audit.write(
                "dispatch_validator", subagent_id=sid, title=finding.title,
                source=a.source_subagent,
            )
            await self._emit(ActivityLine(
                text=f'  ▸ validating {sid} → "{finding.title}"'
            ))
            await self._emit_status()
            return (
                f'dispatched validator {sid} for finding "{finding.title}" '
                f'(source {a.source_subagent or "?"}). the verdict surfaces via await_subagents '
                f"and as a finding card."
            )

        if tool == "await_subagents":
            try:
                a = AwaitSubagentsArgs(**args)
            except Exception as e:  # noqa: BLE001
                return f"bad await_subagents args: {e}"
            # Guard against a permanent hang: with NOTHING running and no completed
            # result queued, `await_("any", timeout=None)` blocks forever on the empty
            # done-queue (no subagent will ever complete). This bites on resume — the
            # re-seeded orchestrator memory may claim a subagent is "running" from a
            # PRIOR process whose subagents died with it, so the model awaits a ghost.
            # spawn() registers in `_running` synchronously, so an empty manager here
            # means nothing was actually dispatched: return immediately instead.
            p, v = self.subagents.running_counts()
            if p == 0 and v == 0 and not self.subagents.has_pending_results():
                await self._emit(ActivityLine(text="  ▸ no subagents running — nothing to await"))
                return (
                    "no subagents are running (none dispatched, or any from a prior "
                    "session are gone). Dispatch a pentester/validator first, THEN call "
                    "await_subagents — do not await when nothing is in flight."
                )
            await self._emit(ActivityLine(
                text=f"  ▸ waiting for subagents ({self.subagents.running_summary()})"
            ))
            # Act as soon as ONE subagent finishes — don't block for the whole
            # batch. Return the first completion(s) so the orchestrator can react
            # and dispatch follow-ups while the rest keep running. (finalize
            # still drains everything via await_("all").)
            #
            # The wait also races against operator input: if the user types
            # something while we're blocked, we return early (with whatever
            # finished) so the orchestrator can read + respond to the message
            # instead of freezing the chat until subagents complete.
            results, interrupted = await self._await_subagents_interruptible(
                "any", a.timeout
            )
            # Track candidate findings pentesters surfaced — finalize will refuse
            # to close while any remain unvalidated.
            self._pentester_candidates += sum(
                len(r.candidate_findings) for r in results if r.kind == "pentester"
            )
            await self._emit_status()
            obs = self.subagents.format_results(results)
            if interrupted:
                obs += (
                    "\n\n[operator interjected] The operator sent you a message "
                    "while you were waiting — it follows as the next user message. "
                    "Read it and respond / adjust course, then call await_subagents "
                    "again to keep collecting from the still-running subagents."
                )
            return obs

        if tool == "list_subagents":
            await self._emit(ActivityLine(text="  ▸ listing subagents"))
            return self.subagents.format_snapshot()

        if tool == "finalize":
            try:
                a = FinalizeArgs(**args)
            except Exception as e:  # noqa: BLE001
                return f"bad finalize args: {e}"
            # Strict validation: you cannot finalize a "solved/confirmed" claim on
            # candidate findings that no Validator ever checked. The orchestrator
            # may NOT self-confirm — only a Validator subagent confirms a finding.
            if self._pentester_candidates > 0 and self._validators_dispatched == 0:
                return (
                    f"finalize REFUSED: your pentester(s) returned "
                    f"{self._pentester_candidates} candidate finding(s) and NO validator has "
                    "checked any of them. You cannot confirm a finding yourself — your own "
                    "recon/login checks do not count as validation. Call `dispatch_validator` "
                    "for each real candidate, await the verdict, THEN finalize."
                )
            await self._emit(ActivityLine(text="  ▸ finalizing — writing report"))
            # Stash the substantive summary so the Ralph pass returns IT (not the bare
            # action `thought`) — this is what flows to progress.txt and the per-task
            # evidence dossier, so it must carry the orchestrator's actual account.
            self._last_finalize_summary = a.summary
            report_path = await self._emit_report(loop, summary=a.summary)
            self._go_idle = True  # show report, then wait for the next prompt
            return (
                f"report written to {report_path} and shown in the chat. The engagement"
                " stays open — the user may ask about a finding or give you another goal."
            )

        return f"unknown dispatch tool: {tool!r}"

    async def _await_subagents_interruptible(
        self, which: str, timeout: float | None
    ) -> tuple[list[SubagentResult], bool]:
        """Wait for subagent completion(s), but return early if the operator
        submits a message. Returns (results, interrupted_by_user).

        On interrupt the running subagents keep going — we only stop *waiting*,
        so the orchestrator's loop can drain the user message and respond. Any
        subagents that finished in the meantime are still returned."""
        assert self.subagents is not None
        await_task = asyncio.create_task(self.subagents.await_(which, timeout))
        user_task = asyncio.create_task(self._user_input_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {await_task, user_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            await_task.cancel()
            user_task.cancel()
            raise
        if await_task in done:
            user_task.cancel()
            return await_task.result(), False
        # Operator interjected. Stop waiting; subagents keep running.
        await_task.cancel()
        try:
            await await_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return self.subagents.collect_done(), True

    # ------------------------------------------------------------ subagent runners

    def _autowrite_memory(self, session_id: str, content: str) -> None:
        """Harness-side safety net: persist an actor's end-state to its memory file
        so `read_subagent_memory` always works — even when the model never called
        `memory_write` (in practice it rarely does). MERGES rather than clobbers: if
        the agent kept its own running memory, we append the end-state below it so
        nothing is lost. Best-effort; never crash a run."""
        try:
            existing = read_memory(session_id).strip()
            if existing and content not in existing:
                content = f"{existing}\n\n---\n{content}"
            write_memory(session_id, content)
        except Exception as e:  # noqa: BLE001 — memory persistence must not break a run
            self.audit.write("memory_autowrite_failed", session_id=session_id, error=str(e))

    async def _run_action_batch(self, calls, run_one, *, concurrent: bool = True):
        """Execute an `actions` batch in ONE turn. Independent calls run concurrently
        (one LLM round-trip for the whole batch — the win); calls that share the
        actor's stateful interpreter (`_SERIAL_BATCH_TOOLS`) run sequentially in
        listed order. `run_one(call)` is the per-call handler; results are returned in
        call order. Set `concurrent=False` to force fully-sequential execution (used by
        the orchestrator, whose intercepted dispatch/inspect tools mutate shared state)."""
        results: list[Any] = [None] * len(calls)
        if not concurrent:
            for i, c in enumerate(calls):
                results[i] = await run_one(c)
            return results

        async def _do(i: int) -> None:
            results[i] = await run_one(calls[i])

        conc = [i for i, c in enumerate(calls) if c.tool not in _SERIAL_BATCH_TOOLS]
        serial = [i for i, c in enumerate(calls) if c.tool in _SERIAL_BATCH_TOOLS]
        if conc:
            await asyncio.gather(*(_do(i) for i in conc))
        for i in serial:  # after the concurrent ones, in listed order
            await _do(i)
        return results

    async def _run_subagent(self, rec) -> SubagentResult:
        """Injected into SubagentManager: build + drive one subagent loop."""
        if rec.kind == "validator":
            return await self._run_validator_subagent(rec)
        return await self._run_pentester_subagent(rec)

    async def _run_pentester_subagent(self, rec) -> SubagentResult:
        backend = (
            self._subagent_backend_factory(rec)
            if self._subagent_backend_factory is not None
            else self.subagent_backend
        )
        tools = [
            t for t in self.registry.catalog()
            if not is_dispatch_tool(t["name"]) and t["name"] != "finding_submit"
        ]
        loop = build_pentest_loop(
            backend, tools, project_context=self._project_ctx,
            # Pentesters get their OWN (smaller) turn budget — they no longer
            # inherit the orchestrator's large max_turns, which let a single
            # idea run 40+ turns. An explicit dispatch max_turns still wins.
            max_turns=rec.max_turns or self.cfg.pentester_max_turns,
            session=self._make_session(f"pentester-{rec.id}"),
        )
        rec.loop = loop
        brief = (
            f"You are a focused pentester subagent. TARGET: {rec.target}\n"
            f"ATTACK IDEA: {rec.idea}\n"
        )
        # You do NOT see the operator's goal or the orchestrator's conversation —
        # so the engagement goal (which carries the base URL/host) is seeded here.
        if self.cfg.goal:
            brief += f"ENGAGEMENT GOAL (from the operator): {self.cfg.goal}\n"
        if rec.context:
            brief += f"CONTEXT from the orchestrator (creds/tokens/prior results to use): {rec.context}\n"
        brief += (
            "Investigate ONLY this idea against this target — it is ONE link of a larger "
            "chain that the orchestrator is sequencing, NOT the whole kill chain. You have a "
            "BOUNDED turn budget, so go deep on this single step and produce its one concrete "
            "artifact (a leaked file, a recovered/forged token, a working payload, a confirmed "
            "injection); do NOT try to carry the attack end-to-end — report your artifact in "
            "`discovered` and the orchestrator will dispatch the next step seeded with it. "
            "Do not submit findings yourself; "
            "when done, reply with a `final` whose `answer` is a JSON object: "
            '{"status": "EXPLOITED" | "NO_FINDING" | "INCOMPLETE", "summary": "...", '
            '"candidate_findings": [{"title":..., "summary":..., "endpoint":..., "method":..., '
            '"cwe":..., "severity_hint":..., "evidence":..., "payload_shape":...}], '
            '"discovered": ["creds/tokens/notes to chain"]}.\n'
            "The `summary` is the ONLY thing the orchestrator reads to decide what to do next, so "
            "make it useful:\n"
            "- If you EXPLOITED something, summarize the bug and how you proved it.\n"
            "- If status is NO_FINDING, do NOT just say 'nothing found'. Explain WHAT YOU TESTED "
            "(the concrete payloads / endpoints / parameters you tried) and WHY each did not work "
            "(the response/observation that ruled it out). Use NO_FINDING ONLY if you completed the "
            "decisive test(s) for the assigned hypothesis and the evidence ruled it out.\n"
            "- If you did not complete every step needed to decide the assigned hypothesis, status "
            "MUST be INCOMPLETE, not NO_FINDING. In the summary state `Reason incomplete`, "
            "`Completed`, `Remaining`, and `Exact next step` so the orchestrator can inspect the "
            "reason and continue the work.\n"
            "If ANY payload looked PROMISING or got "
            "close — a partial success, an interesting error, a reflection that was almost "
            "exploitable, an auth check that nearly passed — include the EXACT payload (or the full "
            "request / exploit script) VERBATIM in the summary so the orchestrator can refine and "
            "retry it. Do NOT paraphrase or shorten a promising payload: paste it as-is. This lets "
            "the orchestrator improve a near-miss instead of blindly re-dispatching the same idea.\n"
            f"The `summary` budget is large (up to {SUMMARY_MAX_CHARS} characters) — use it: paste "
            "full promising payloads, raw request/response snippets, or a working/partial exploit "
            "script verbatim rather than truncating them.\n"
            "Don't tunnel-vision: if an approach isn't working after a few attempts, switch "
            "technique rather than repeating variations of the same failing step; if nothing pans "
            "out after decisive testing, conclude NO_FINDING. If the task remains unfinished, "
            "conclude INCOMPLETE with the reason and exact continuation step."
        )
        loop.add_user_message(brief)
        # If this actor saved memory earlier this run, seed it so it resumes
        # instead of starting blank (and so a crashed/re-dispatched id continues).
        prior_memory = read_memory(rec.id)
        if prior_memory:
            loop.add_user_message(f"## Your memory from earlier this run\n\n{prior_memory}")
        label = f"pentester[{rec.id} {rec.target}]"
        start = time.monotonic()
        deadline = start + self.cfg.subagent_deadline_seconds
        # A single backend error (e.g. claude `error_max_turns` from a momentary
        # native-tool-use drift, or a transient `claude -p` failure) must NOT kill the
        # whole subagent and discard its work — like the orchestrator, feed the error
        # back and retry the turn, giving up only after several consecutive failures.
        consecutive_errors = 0
        while True:
            # Wall-clock backstop, checked between turns: the per-call
            # `--subagent-timeout` only bounds ONE `claude -p` call, so a
            # subagent that keeps iterating had no total cap. Stop here rather
            # than let it run for an hour on the same failing idea.
            if time.monotonic() > deadline:
                elapsed = time.monotonic() - start
                self.audit.write(
                    "subagent_deadline", subagent_id=rec.id, target=rec.target,
                    turns=loop.turn, elapsed=round(elapsed),
                )
                self._autowrite_memory(
                    rec.id,
                    f"# pentester {rec.id} — {rec.target}\nidea: {rec.idea}\n\n"
                    f"STATUS: ERROR (wall-clock deadline {self.cfg.subagent_deadline_seconds}s "
                    f"after {loop.turn} turn(s), no conclusion).",
                )
                return SubagentResult(
                    subagent_id=rec.id, kind="pentester", target=rec.target, idea=rec.idea,
                    status="ERROR",
                    summary=(
                        f"stopped: wall-clock deadline ({self.cfg.subagent_deadline_seconds}s) "
                        f"reached after {loop.turn} turn(s) without a conclusion."
                    ),
                    turns=loop.turn, duration=elapsed,
                )
            try:
                step = await loop.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a backend glitch must not kill the subagent
                consecutive_errors += 1
                self.audit.write(
                    "subagent_turn_error", subagent_id=rec.id,
                    error=f"{type(e).__name__}: {e}", consecutive=consecutive_errors,
                )
                if consecutive_errors >= _MAX_CONSECUTIVE_TURN_ERRORS:
                    elapsed = time.monotonic() - start
                    self._autowrite_memory(
                        rec.id,
                        f"# pentester {rec.id} — {rec.target}\nidea: {rec.idea}\n\n"
                        f"STATUS: ERROR ({consecutive_errors} consecutive backend errors; "
                        f"last: {type(e).__name__}: {e}).",
                    )
                    return SubagentResult(
                        subagent_id=rec.id, kind="pentester", target=rec.target, idea=rec.idea,
                        status="ERROR",
                        summary=(
                            f"stopped after {consecutive_errors} consecutive backend errors "
                            f"(last: {type(e).__name__}: {e})."
                        ),
                        turns=loop.turn, duration=elapsed,
                    )
                # Feed the error back and retry the turn (don't lose progress).
                loop.add_observation(
                    "(system)", False,
                    f"your previous turn failed: {e}. Reply with EXACTLY ONE JSON envelope "
                    "AS TEXT (action / ask_user / final) — do NOT make a native tool call; "
                    "name the tool inside the envelope and the runtime runs it.",
                )
                continue
            consecutive_errors = 0
            env = step.envelope
            self._log_thinking(f"pentester {rec.id} [{rec.target}]", loop.turn, env)
            await self._emit(
                SubagentProgress(subagent_id=rec.id, status_text=env.__class__.__name__, turn=loop.turn)
            )
            if isinstance(env, FinalEnvelope):
                parsed = parse_pentester_result(env.answer)
                # Safety net: persist the authoritative end-state to this subagent's
                # memory so the orchestrator can recover it via read_subagent_memory
                # even though the model itself almost never calls memory_write.
                disc = parsed.get("discovered") or []
                cands = parsed.get("candidate_findings") or []
                self._autowrite_memory(
                    rec.id,
                    f"# pentester {rec.id} — {rec.target}\nidea: {rec.idea}\n\n"
                    f"STATUS: {parsed['status']}\n\n## summary\n{parsed['summary']}\n\n"
                    "## discovered\n" + ("\n".join(f"- {d}" for d in disc) or "(none)") + "\n\n"
                    "## candidate findings\n"
                    + ("\n".join(f"- {c.get('title', '?')}" for c in cands) or "(none)"),
                )
                return SubagentResult(
                    subagent_id=rec.id, kind="pentester", target=rec.target, idea=rec.idea,
                    status=parsed["status"], summary=parsed["summary"],
                    candidate_findings=parsed["candidate_findings"], discovered=parsed["discovered"],
                    turns=loop.turn, duration=time.monotonic() - start,
                )
            if isinstance(env, AskUserEnvelope):
                answer = await self._do_ask_user(env, agent=f"pentester {rec.id}")
                loop.add_ask_user_answer(answer)
                continue
            if isinstance(env, ActionEnvelope):
                result = await self._run_one_tool(
                    env.tool, env.args, label=label, reason=env.thought, session_id=rec.id
                )
                loop.add_observation(env.tool, result.ok, result.output)
                continue
            if isinstance(env, ActionsEnvelope):
                async def _one(c, _env=env):
                    return await self._run_one_tool(
                        c.tool, c.args, label=label, reason=c.thought or _env.thought,
                        session_id=rec.id,
                    )
                results = await self._run_action_batch(env.calls, _one)
                for c, r in zip(env.calls, results):
                    loop.add_observation(c.tool, r.ok, r.output)
                continue

    async def _run_validator_subagent(self, rec) -> SubagentResult:
        start = time.monotonic()
        finding = rec.finding
        # Key memory/python_exec off the clean actor id (rec.id), not the verbose
        # UI label, so the orchestrator can read it via read_subagent_memory(rec.id).
        prior_memory = read_memory(rec.id)
        seed = (
            f"## Your memory from earlier this run\n\n{prior_memory}" if prior_memory else None
        )
        verdict, ids = await self._run_validation(
            finding, dedup_hash(finding),
            label=f"validator[{rec.id} {rec.target}]",
            session_id=rec.id,
            seed_memory=seed,
            record=rec,
        )
        return SubagentResult(
            subagent_id=rec.id, kind="validator", target=rec.target, idea=rec.idea,
            status=verdict, summary=f"validator verdict: {verdict}",
            finding_ids=ids, turns=getattr(rec.loop, "turn", 0),
            duration=time.monotonic() - start,
        )

    async def _emit_report(self, loop, *, summary: str) -> str:
        """Wait for in-flight subagents to FINISH, write the report file, and emit
        the report to the chat. Does NOT end the session — the orchestrator goes
        idle afterwards and waits for the next prompt. Returns the report path.
        May be called multiple times (each `finalize` produces a fresh report).

        We do NOT cancel running subagents here: finalize (or hitting the turn
        budget) waits for them to complete first, then reports. Subagents are
        turn-capped so they terminate on their own; a generous ceiling guards
        against a genuinely stuck one."""
        if self.subagents is not None:
            try:
                # timeout=None: wait for every subagent to actually complete.
                # The outer ceiling is only a backstop against a hung subagent.
                await asyncio.wait_for(self.subagents.await_("all", timeout=None), timeout=600)
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(self.drain_validations(), timeout=10)
        except Exception:  # noqa: BLE001
            pass

        attempts = list(self.subagents.attempts) if self.subagents is not None else []
        report_path = ""
        try:
            p = write_report(
                self.findings_store, self.cfg.cwd / ".pentest" / "report.md", attempts=attempts
            )
            report_path = str(p)
        except Exception as e:  # noqa: BLE001
            self.audit.write("report_error", error=str(e))
        self.audit.write("finalize", summary=summary[:500], report_path=report_path)

        failed = [
            {"subagent_id": a.subagent_id, "target": a.target, "idea": a.idea, "status": a.status}
            for a in attempts
            if a.kind == "pentester" and a.status in ("NO_FINDING", "ERROR")
        ]
        self._total_turns = max(self._total_turns, getattr(loop, "_turn", 0))
        await self._emit(
            SessionSummary(
                confirmed=list(self._session_findings["confirmed"]),
                rejected=list(self._session_findings["rejected"]),
                pending=list(self._session_findings["pending"]),
                failed_attempts=failed,
                findings_dir=str(self.findings_store.root),
                audit_path=str(self.audit.path),
                turns=self._total_turns,
                report_path=report_path,
            )
        )
        return report_path

    async def _handle_finding_submit(self, loop, env: ActionEnvelope, call_id: str) -> None:
        """Pentest agent submitted a finding.

        Validation runs in a BACKGROUND task so the pentest loop never blocks
        on the Validator (which can take many turns). The agent gets an
        immediate "queued for validation" ack and keeps working; the verdict
        surfaces later as a Finding card + a status-counter update.
        """
        try:
            finding: FindingModel = args_to_finding(env.args)
        except Exception as e:  # noqa: BLE001
            obs = f"finding_submit args invalid: {e}"
            await self._emit(ToolObservation(tool=env.tool, call_id=call_id, ok=False, output=obs))
            loop.add_observation(env.tool, False, obs)
            return

        await self._emit(
            ToolProposal(
                tool=env.tool, args=env.args, risk_level="safe", call_id=call_id
            )
        )
        self.audit.write(
            "finding_submit", call_id=call_id, title=finding.title, endpoint=finding.endpoint
        )
        assert self.router is not None

        # Dedup BEFORE spawning a validator. A finding already on disk, or one
        # currently being validated this session (not yet persisted), is a
        # duplicate — this collapses the resubmit-the-same-bug loop that made
        # earlier runs look frozen.
        h = dedup_hash(finding)
        prior = self.findings_store.lookup_by_hash(h)
        if prior is not None or h in self._inflight_hashes:
            dup_of = prior["id"] if prior is not None else None
            await self._emit(
                FindingEvent(
                    verdict="duplicate",
                    title=finding.title,
                    endpoint=finding.endpoint,
                    method=finding.method,
                    cwe=finding.cwe,
                    duplicate_of=dup_of,
                )
            )
            self.audit.write("finding_duplicate", duplicate_of=dup_of, hash_=finding.title)
            obs = (
                f"duplicate of {dup_of}; already validated — no new finding written."
                if dup_of is not None
                else "duplicate — a matching finding is already being validated; skipped."
            )
            await self._emit(ToolObservation(tool=env.tool, call_id=call_id, ok=True, output=obs))
            loop.add_observation(env.tool, True, obs)
            return

        # Queue async validation; ack the pentest agent immediately.
        self._inflight_hashes.add(h)
        self._validations_pending += 1
        await self._emit_status()
        obs = (
            f"finding accepted; queued for validation "
            f"({self._validations_pending} pending). Continue testing other "
            f"surfaces — the verdict will appear as a finding card when ready."
        )
        await self._emit(ToolObservation(tool=env.tool, call_id=call_id, ok=True, output=obs))
        loop.add_observation(env.tool, True, obs)

        task = asyncio.create_task(
            self._run_validation(finding, h), name=f"validate-{call_id}"
        )
        self._validation_tasks.add(task)
        task.add_done_callback(self._validation_tasks.discard)

    async def _run_validation(
        self,
        finding: FindingModel,
        dedup_h: str,
        *,
        label: str = "validator",
        session_id: str = "validator",
        seed_memory: str | None = None,
        record: Any = None,
    ) -> tuple[str, list[str]]:
        """Drive the Validator and surface the verdict to the UI. Returns
        (verdict, finding_ids). Used both as a mode-a background task and as the
        orchestrated validator subagent runner.

        `session_id` keys this validator's isolated python_exec + memory (a clean
        actor id like its subagent `rec.id`, distinct from the verbose UI `label`);
        `seed_memory` is its own prior memory injected at the start of its loop."""
        assert self.router is not None

        async def action_handler(action_env: ActionEnvelope) -> str:
            """Validator's tool calls flow through the same gate + registry,
            prefixed with `label` (e.g. `validator` or `validator[#5 /x]`)."""
            sub_call_id = uuid.uuid4().hex[:8]
            if action_env.tool not in self.registry:
                return f"unknown tool: {action_env.tool!r}"
            tool = self.registry.get(action_env.tool)
            await self._emit(
                ToolProposal(
                    tool=action_env.tool, args=action_env.args,
                    risk_level=tool.risk_level.value, call_id=sub_call_id, label=label,
                )
            )
            if not getattr(tool, "requires_approval", True):
                allowed = True  # local, no-side-effect tools bypass the gate
            else:
                req = ApprovalRequest(
                    tool=action_env.tool, args=action_env.args,
                    risk_level=tool.risk_level, call_id=sub_call_id, label=label,
                    host_exec=getattr(tool, "runs_on_host", False),
                    reason=action_env.thought,
                )
                allowed = await self.gate.check(req)
            self.audit.write(
                "validator_approval", call_id=sub_call_id, allowed=allowed,
            )
            if not allowed:
                return "user denied tool call"
            result = await self._invoke_tool(
                tool, action_env.args, session_id=session_id
            )
            await self._emit(
                ToolObservation(
                    tool=action_env.tool, call_id=sub_call_id,
                    ok=result.ok, output=result.output, truncated=result.truncated, label=label,
                )
            )
            return result.output

        async def ask_handler(ask_env: AskUserEnvelope) -> str:
            return await self._do_ask_user(ask_env, agent="validator")

        validator_deadline = time.monotonic() + self.cfg.subagent_deadline_seconds

        async def driver(sub_loop):
            # Expose the validator's live loop on its record so the running view can
            # show its context-window usage (pentesters set rec.loop directly; the
            # validator loop is built inside the router, so we capture it here).
            if record is not None:
                record.loop = sub_loop
            return await default_validator_driver(
                sub_loop,
                action_handler=action_handler,
                ask_handler=ask_handler,
                on_step=lambda turn, env: self._log_thinking(label, turn, env),
                deadline=validator_deadline,
            )

        try:
            res = await self.router.handle_finding_submit(
                finding, drive_validator=driver, extra_user_message=seed_memory
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — never let a background task crash the session
            self.audit.write("validator_crashed", error=f"{type(e).__name__}: {e}")
            await self._emit(
                FindingEvent(
                    verdict="pending",
                    title=finding.title,
                    endpoint=finding.endpoint,
                    method=finding.method,
                    cwe=finding.cwe,
                    rationale=f"validation could not complete: {e}",
                )
            )
            return ("pending", [])
        finally:
            self._validations_pending = max(0, self._validations_pending - 1)
            self._inflight_hashes.discard(dedup_h)

        # Surface to UI + audit.
        if res.duplicate:
            await self._emit(
                FindingEvent(
                    verdict="duplicate",
                    title=finding.title,
                    endpoint=finding.endpoint,
                    method=finding.method,
                    cwe=finding.cwe,
                    duplicate_of=res.duplicate_of,
                )
            )
            self.audit.write(
                "finding_duplicate", duplicate_of=res.duplicate_of, hash_=finding.title
            )
            await self._emit_status()
            return ("duplicate", [res.duplicate_of] if res.duplicate_of else [])

        verdict = "pending"
        finding_ids: list[str] = []
        if res.record is not None:
            v = res.record.validation
            verdict = v.verdict if v else "pending"
            finding_ids = [res.record.id]
            # Safety net: persist this validator's end-state to its memory (keyed by
            # session_id, == rec.id for the orchestrated subagent) so the orchestrator
            # can recover it even when the model never called memory_write.
            self._autowrite_memory(
                session_id,
                f"# validator — {finding.title}\n\nVERDICT: {verdict}\n\n"
                f"## rationale\n{(v.rationale if v else '')}\n\n"
                f"## reproducer\n{(v.reproducer if v else '')}",
            )
            # Resolve the on-disk path so the UI card can show it.
            file_path = self._find_record_path(res.record.id, verdict)
            await self._emit(
                FindingEvent(
                    verdict=verdict,
                    finding_id=res.record.id,
                    title=res.record.finding.title,
                    endpoint=res.record.finding.endpoint,
                    method=res.record.finding.method,
                    cwe=(v.cwe if v and v.cwe else res.record.finding.cwe),
                    cvss_score=(v.cvss40_score if v else 0.0),
                    cvss_vector=(v.cvss40_vector if v else ""),
                    rationale=(v.rationale if v else ""),
                    reproducer=(v.reproducer if v else ""),
                    file_path=file_path,
                )
            )
            self._session_findings.setdefault(verdict, []).append(
                {
                    "id": res.record.id,
                    "title": res.record.finding.title,
                    "cvss_score": (v.cvss40_score if v else 0.0),
                    "file_path": file_path,
                }
            )
            self.audit.write(
                "finding_persisted",
                id=res.record.id,
                verdict=verdict,
                cvss=(v.cvss40_score if v else None),
                file_path=file_path,
            )
            if verdict == "confirmed":
                self._validations_confirmed += 1
            elif verdict == "rejected":
                self._validations_rejected += 1
        await self._emit_status()
        return (verdict, finding_ids)

    async def drain_validations(self) -> None:
        """Await all in-flight background validations. Used on shutdown and
        by tests that need the verdict before asserting."""
        while self._validation_tasks:
            await asyncio.gather(*list(self._validation_tasks), return_exceptions=True)

    def _find_record_path(self, finding_id: str, verdict: str) -> str:
        """Return the str path of <findings_dir>/<verdict>/<id>.md."""
        p = self.findings_store.dirs.get(verdict)
        if p is None:
            return ""
        candidate = p / f"{finding_id}.md"
        return str(candidate)

    async def _invoke_tool(self, tool: Tool, args: dict, *, session_id: str = "orchestrator"):
        from zero.tools.base import ToolResult

        # python_exec keeps a PERSISTENT interpreter per session, and the memory
        # tools key their file off the actor; inject the caller's session id
        # (infra, not LLM-set) so each actor — orchestrator, each pentester/
        # validator subagent — gets an isolated session and its OWN memory file.
        if tool.name in (
            "python_exec", "memory_read", "memory_write", "todo_read", "todo_write",
        ):
            args = {**args, "session_id": session_id}
        try:
            parsed = tool.Args(**args)
        except ValidationError as e:
            return ToolResult(ok=False, output=f"args validation error: {e}")
        host = _extract_target_host(args)
        if host and not self.rate_limiter.disabled():
            await self.rate_limiter.acquire(host)
        try:
            result = await tool.run(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # A tool that raises (e.g. a NUL in a payload → subprocess "embedded
            # null byte" ValueError) must NOT crash the caller — for a subagent
            # that would silently kill the whole investigation. Return a clean
            # failed observation so the agent sees the error and can adapt.
            self.audit.write("tool_exception", tool=tool.name, error=f"{type(e).__name__}: {e}")
            return ToolResult(ok=False, output=f"tool error: {type(e).__name__}: {e}")
        # Neutralize NUL bytes in tool output BEFORE it enters the transcript. A
        # binary HTTP body / font dumped by python_exec or curl_raw can contain
        # `\x00`; left in, it flows into the next turn's `claude -p <prompt>` argv
        # and raises "embedded null byte" at spawn, killing the subagent. We
        # replace each run with a visible `[NUL*N]` marker (not silent deletion —
        # the presence of null bytes can itself be the finding) so the
        # transcript/audit/UI stay clean and informative. The backend applies the
        # same guard at the spawn boundary as a backstop.
        if result.output and "\x00" in result.output:
            from zero.llm.claude_code import neutralize_nul

            result.output = neutralize_nul(result.output)
        return result

    async def _do_ask_user(self, env: AskUserEnvelope, *, agent: str = "pentest") -> str:
        decision = self.ask_governor.evaluate(agent, env)
        if decision.refusal is not None:
            self.audit.write("ask_user_refused", agent=agent, reason=decision.refusal)
            return decision.refusal
        if decision.answer is not None:
            self.audit.write(
                "ask_user_short_circuit", agent=agent,
                question=env.question, answer=decision.answer,
            )
            return decision.answer

        ask_id = uuid.uuid4().hex[:8]
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._ask_user_replies[ask_id] = fut
        await self._emit(
            AskUser(
                question=env.question,
                options=env.options,
                free_form=env.free_form,
                blocking=env.blocking,
                ask_id=ask_id,
            )
        )
        self.audit.write(
            "ask_user", ask_id=ask_id, agent=agent,
            question=env.question, options=env.options,
        )
        try:
            answer = await fut
        finally:
            self._ask_user_replies.pop(ask_id, None)
        self.ask_governor.record_answer(agent, env, answer)
        return answer

    # ---------------------------------------------------- UI <-> orchestrator

    async def _emit(self, event: UIEvent) -> None:
        await self.ui_queue.put(event)

    async def _emit_status(self, *, turn: int | None = None, mode: str | None = None) -> None:
        """Emit a Status carrying the live approval + validation counters.

        Pass turn/mode when the agent advances; counter-only callers omit
        them and the last shown turn/mode is reused.
        """
        if turn is not None:
            self._status_turn = turn
        if mode is not None:
            self._status_mode = mode
        rp, rv = self.subagents.running_counts() if self.subagents is not None else (0, 0)
        await self._emit(
            Status(
                turn=self._status_turn,
                mode=self._status_mode,
                pending_approvals=self._approvals_pending,
                validations_pending=self._validations_pending,
                validations_confirmed=self._validations_confirmed,
                validations_rejected=self._validations_rejected,
                running_pentesters=rp,
                running_validators=rv,
                # The top-level agent's (orchestrator / mode-a) context-window usage.
                context_tokens=getattr(self._active_loop, "context_tokens", 0),
                context_window=self.cfg.context_window,
            )
        )

    async def _prompt_approval(self, req: ApprovalRequest) -> Decision:
        call_id = req.call_id
        self._approvals_pending += 1
        try:
            fut: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
            self._approval_replies[call_id] = fut
            await self._emit(
                ApprovalPrompt(
                    tool=req.tool,
                    args=req.args,
                    risk_level=req.risk_level.value,
                    call_id=call_id,
                    label=req.label,
                    reason=req.reason,
                )
            )
            return await fut
        finally:
            self._approval_replies.pop(call_id, None)
            self._approvals_pending = max(0, self._approvals_pending - 1)

    # Public entry points the UI calls.

    def submit_user_text(self, text: str) -> None:
        if text.strip():
            self.user_queue.put_nowait(text)
            # Wake a blocking await_subagents so the orchestrator can pick up
            # this message instead of staying frozen until subagents finish.
            self._user_input_event.set()

    def request_cancel_turn(self) -> bool:
        """Cancel the in-flight agent turn. Returns True if there was one."""
        t = self._current_turn_task
        if t and not t.done():
            t.cancel()
            return True
        return False

    def submit_approval(self, call_id: str, decision: Decision) -> None:
        fut = self._approval_replies.get(call_id)
        if fut and not fut.done():
            fut.set_result(decision)

    def submit_ask_user_answer(self, ask_id: str, answer: str) -> None:
        fut = self._ask_user_replies.get(ask_id)
        if fut and not fut.done():
            fut.set_result(answer)


def _extract_target_host(args: dict) -> str | None:
    """Pull a host name out of common tool-args shapes for rate limiting."""
    from urllib.parse import urlparse

    candidates = []
    for k in ("url", "target"):
        v = args.get(k)
        if isinstance(v, str):
            candidates.append(v)
    for v in candidates:
        try:
            p = urlparse(v if "://" in v else f"http://{v}")
        except ValueError:
            continue
        if p.hostname:
            return p.hostname.lower()
    return None
