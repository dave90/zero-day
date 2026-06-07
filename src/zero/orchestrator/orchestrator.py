"""Top-level coordinator.

Owns the AuditLog, the ApprovalGate, the tool registry, the agent loop, and
the UI queue. Runs the agent as one asyncio.Task and the UI as another;
they communicate through an asyncio.Queue of UIEvents and a separate queue
of user inputs.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zero.agents.pentest import build_pentest_loop
from zero.agents.orchestrator_agent import build_orchestrator_loop, parse_pentester_result
from zero.llm.backend import LLMBackend
from zero.llm.envelope import (
    ActionEnvelope,
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
    dispatch_tools,
    is_dispatch_tool,
)
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
    SessionEnd,
    SessionSummary,
    Status,
    SubagentProgress,
    ToolObservation,
    ToolProposal,
    TurnCancelled,
    TurnStart,
    UIEvent,
)


# How many consecutive failed turns (invalid-JSON envelope, backend error) the
# agent loop tolerates — feeding the error back and retrying — before it gives up
# and waits for the operator. Lets a transient model glitch self-heal instead of
# reading as a crashed/idle session.
_MAX_CONSECUTIVE_TURN_ERRORS = 3


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
    backend: str = "claude"  # production default; "echo" is a test-only fixture
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
    validator_max_turns: int = 40          # Validator AgentLoop investigation steps
    validator_claude_max_turns: int = 60   # per-call `claude -p --max-turns`
    # Per-call `claude -p` wall-clock timeout shared by ALL agent backends — the
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
    # turns. The 900s `--subagent-timeout` is only PER `claude -p` call — a
    # subagent that keeps finding "one more thing to try" has no total cap
    # without this. Bounds a runaway subagent regardless of turn count.
    subagent_deadline_seconds: int = 1200
    # Orchestrated mode (M8): cap on concurrently-running subagents.
    max_concurrent_subagents: int = 10
    # M4 sandbox knobs. Secure by default: run tools INSIDE the container, and
    # proxy ALL of them there (not just python_exec). Falls back to host (with
    # the approval guard) if Docker isn't available. Use --no-container to opt
    # into host execution.
    use_container: bool = True
    container_image: str = "zero-sandbox:latest"
    host_network: bool = False
    burp_dir: Path | None = None
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


class Orchestrator:
    def __init__(self, cfg: OrchestratorConfig) -> None:
        self.cfg = cfg
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
        # Orchestrated mode (M8): the subagent manager, project context, an
        # optional per-subagent backend factory (tests inject scripted backends),
        # and the finalize latch.
        self.subagents: SubagentManager | None = None
        self._project_ctx: str | None = None
        self._subagent_backend_factory: Any = None
        # Set by the `finalize` tool: emit the report, then go idle and wait for
        # the next user prompt (Claude-Code style — the session does NOT close).
        self._go_idle = False

    # ------------------------------------------------------------------ setup

    def _build_registry(self) -> ToolRegistry:
        reg = default_registry()
        # Apply scope-host blocklist to the WebFetchTool instance.
        if self.cfg.scope_hosts:
            wf = reg.get("web_fetch")
            assert isinstance(wf, WebFetchTool)
            wf.blocked_hosts = {h.lower() for h in self.cfg.scope_hosts}
        return reg

    def _build_backend(self) -> LLMBackend:
        if self.cfg.backend == "echo":
            from zero.llm.echo import EchoBackend

            return EchoBackend()
        if self.cfg.backend == "claude":
            from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

            # Same per-call timeout as the subagents (`--llm-timeout`).
            return ClaudeCodeBackend(
                ClaudeCodeConfig(timeout_seconds=self.cfg.llm_timeout_seconds)
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
                ClaudeCodeConfig(timeout_seconds=self.cfg.llm_timeout_seconds)
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
                )
            )
        return self.backend

    # ------------------------------------------------------------- public api

    async def run_interactive(self) -> None:
        """Spawn agent + UI as concurrent tasks; await UI exit."""
        from zero.ui.app import run_ui

        project_ctx = project_context_for(self.cfg.cwd)
        self.audit.write(
            "session_start",
            cwd=str(self.cfg.cwd),
            backend=self.cfg.backend,
            goal=self.cfg.goal,
            has_project_context=project_ctx is not None,
            use_container=self.cfg.use_container,
            execution_mode=self.cfg.execution_mode,
        )

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

        # Router needs the (possibly remote-swapped) registry + project ctx.
        # The Validator runs on its own backend with a generous turn budget.
        self.router = AgentRouter(
            store=self.findings_store,
            backend=self.validator_backend,
            tools=self.registry.catalog(),
            project_context=project_ctx,
            validator_max_turns=self.cfg.validator_max_turns,
            goal=self.cfg.goal,
        )

        if self.cfg.execution_mode == "b":
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
            await self._teardown_container()
            self.audit.write("session_end")
            self.audit.close()
            self.thinking.close()

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

    async def _start_container_and_swap_tools(self) -> None:
        from zero.container.manager import ContainerManager, ContainerSpec
        from zero.container.rpc import ContainerRPC
        from zero.tools.remote import wrap_with_remote

        spec = ContainerSpec(
            image=self.cfg.container_image,
            cwd=self.cfg.cwd,
            burp_dir_host=self.cfg.burp_dir,
            cpus=self.cfg.container_cpus,
            memory=self.cfg.container_memory,
            host_network=self.cfg.host_network,
        )
        self._container = ContainerManager(spec)
        await self._container.start()
        self.audit.write(
            "container_started",
            name=self._container.name,
            image=spec.image,
            rpc_url=self._container.rpc_base_url(),
        )
        self._rpc = ContainerRPC(self._container.rpc_base_url())
        await self._rpc.connect()

        # Always proxy python_exec (the M4 acceptance test). With
        # --remote-all-tools, proxy the whole registry so every call lands in
        # the sandbox.
        names_to_remote = (
            self.registry.names() if self.cfg.remote_all_tools else ["python_exec"]
        )
        for name in names_to_remote:
            local = self.registry.get(name)
            self.registry.replace(wrap_with_remote(local, self._rpc))
        self.audit.write("tools_remoted", names=names_to_remote)

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

        from zero.tools.base import ToolResult

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
            result: ToolResult = await self._invoke_tool(tool, args)
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
            be = ClaudeCodeBackendModeB()
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
        )
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
                if isinstance(env, ActionEnvelope):
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
        if isinstance(env, ActionEnvelope) and env.thought:
            shown = self._thought_for_chat(env.thought)
            if shown:
                await self._emit(AgentTextDelta(text=shown))
                await self._emit(AgentTextEnd())
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
        return short_reason(thought)  # "condensed" (default): first sentence

    async def _do_action(self, loop, env: ActionEnvelope) -> None:
        # finding_submit is orchestrator-intercepted (M6).
        if is_finding_submit(env.tool):
            await self._handle_finding_submit(loop, env, uuid.uuid4().hex[:8])
            return
        result = await self._run_one_tool(env.tool, env.args, reason=env.thought)
        loop.add_observation(env.tool, result.ok, result.output)

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
        self, tool_name: str, args: dict, *, label: str = "", reason: str = ""
    ) -> ToolResult:
        """Propose → gate → invoke → observe one registry tool. Shared by the
        pentest loop (mode a), the orchestrator's own recon (mode c), and every
        subagent's tool calls. `label` attributes the call to a subagent in the
        UI + approval prompt (empty for the top-level agent — preserving mode-a
        output byte-for-byte)."""
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

        result = await self._invoke_tool(tool, args)
        await self._emit(
            ToolObservation(
                tool=tool_name, call_id=call_id,
                ok=result.ok, output=result.output, truncated=result.truncated, label=label,
            )
        )
        self.audit.write(
            "tool_observation", tool=tool_name, call_id=call_id,
            ok=result.ok, output=result.output[:4000], truncated=result.truncated, label=label,
        )
        return result

    # ===================================================== orchestrated mode (c)

    async def _orchestrator_main(self, project_ctx: str | None) -> None:
        """Senior-pentester Orchestrator loop (M8): recon directly, dispatch
        concurrent pentester/validator subagents, chain results, then finalize.
        Same idle/working shape as `_agent_main`."""
        self._project_ctx = project_ctx
        self._go_idle = False
        self.subagents = SubagentManager(
            max_concurrent=self.cfg.max_concurrent_subagents,
            runner=self._run_subagent,
            emit=self._emit,
        )
        # Orchestrator catalog: recon (safe+active) registry tools + dispatch
        # tools. Dispatch tools are NOT in default_registry, so modes a/b never
        # see them.
        recon = [
            t for t in self.registry.catalog()
            if t["risk_level"] in ("safe", "active") and t["name"] != "finding_submit"
        ]
        disp = [
            {
                "name": t.name,
                "description": t.description,
                "risk_level": t.risk_level.value,
                "schema": t.json_schema(),
            }
            for t in dispatch_tools()
        ]
        loop = build_orchestrator_loop(
            backend=self.backend,
            tools=recon + disp,
            project_context=project_ctx,
            max_turns=self.cfg.max_turns,
        )
        base_system_prompt = loop.system_prompt
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
                if isinstance(env, ActionEnvelope):
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

    async def _do_orchestrator_action(self, loop, env: ActionEnvelope) -> None:
        if is_dispatch_tool(env.tool):
            obs = await self._handle_dispatch(loop, env)
            if obs is not None:
                loop.add_observation(env.tool, True, obs)
            return
        # A recon tool the orchestrator runs directly.
        result = await self._run_one_tool(env.tool, env.args, reason=env.thought)
        loop.add_observation(env.tool, result.ok, result.output)

    async def _handle_dispatch(self, loop, env: ActionEnvelope) -> str | None:
        """Handle an orchestrator dispatch tool. Returns the observation string
        for the orchestrator loop, or None when the session is ending."""
        assert self.subagents is not None
        tool, args = env.tool, env.args

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
            "Investigate ONLY this idea against this target. Do not submit findings yourself; "
            "when done, reply with a `final` whose `answer` is a JSON object: "
            '{"status": "EXPLOITED" | "NO_FINDING", "summary": "...", '
            '"candidate_findings": [{"title":..., "summary":..., "endpoint":..., "method":..., '
            '"cwe":..., "severity_hint":..., "evidence":..., "payload_shape":...}], '
            '"discovered": ["creds/tokens/notes to chain"]}.\n'
            "The `summary` is the ONLY thing the orchestrator reads to decide what to do next, so "
            "make it useful:\n"
            "- If you EXPLOITED something, summarize the bug and how you proved it.\n"
            "- If status is NO_FINDING, do NOT just say 'nothing found'. Explain WHAT YOU TESTED "
            "(the concrete payloads / endpoints / parameters you tried) and WHY each did not work "
            "(the response/observation that ruled it out). If ANY payload looked PROMISING or got "
            "close — a partial success, an interesting error, a reflection that was almost "
            "exploitable, an auth check that nearly passed — include the EXACT payload (or the full "
            "request / exploit script) VERBATIM in the summary so the orchestrator can refine and "
            "retry it. Do NOT paraphrase or shorten a promising payload: paste it as-is. This lets "
            "the orchestrator improve a near-miss instead of blindly re-dispatching the same idea.\n"
            f"The `summary` budget is large (up to {SUMMARY_MAX_CHARS} characters) — use it: paste "
            "full promising payloads, raw request/response snippets, or a working/partial exploit "
            "script verbatim rather than truncating them."
        )
        loop.add_user_message(brief)
        label = f"pentester[{rec.id} {rec.target}]"
        start = time.monotonic()
        deadline = start + self.cfg.subagent_deadline_seconds
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
                return SubagentResult(
                    subagent_id=rec.id, kind="pentester", target=rec.target, idea=rec.idea,
                    status="ERROR",
                    summary=(
                        f"stopped: wall-clock deadline ({self.cfg.subagent_deadline_seconds}s) "
                        f"reached after {loop.turn} turn(s) without a conclusion."
                    ),
                    turns=loop.turn, duration=elapsed,
                )
            step = await loop.step()
            env = step.envelope
            self._log_thinking(f"pentester {rec.id} [{rec.target}]", loop.turn, env)
            await self._emit(
                SubagentProgress(subagent_id=rec.id, status_text=env.__class__.__name__, turn=loop.turn)
            )
            if isinstance(env, FinalEnvelope):
                parsed = parse_pentester_result(env.answer)
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
                result = await self._run_one_tool(env.tool, env.args, label=label, reason=env.thought)
                loop.add_observation(env.tool, result.ok, result.output)
                continue

    async def _run_validator_subagent(self, rec) -> SubagentResult:
        start = time.monotonic()
        finding = rec.finding
        verdict, ids = await self._run_validation(
            finding, dedup_hash(finding), label=f"validator[{rec.id} {rec.target}]"
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
        self, finding: FindingModel, dedup_h: str, *, label: str = "validator"
    ) -> tuple[str, list[str]]:
        """Drive the Validator and surface the verdict to the UI. Returns
        (verdict, finding_ids). Used both as a mode-a background task and as the
        orchestrated validator subagent runner."""
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
            result = await self._invoke_tool(tool, action_env.args)
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
            return await default_validator_driver(
                sub_loop,
                action_handler=action_handler,
                ask_handler=ask_handler,
                on_step=lambda turn, env: self._log_thinking(label, turn, env),
                deadline=validator_deadline,
            )

        try:
            res = await self.router.handle_finding_submit(finding, drive_validator=driver)
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

    async def _invoke_tool(self, tool: Tool, args: dict):
        from zero.tools.base import ToolResult

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
