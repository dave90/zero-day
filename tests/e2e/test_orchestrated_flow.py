"""End-to-end: drive the orchestrated (mode c) multi-agent pipeline offline.

A scripted Orchestrator backend dispatches a pentester, awaits it, dispatches a
validator on the pentester's candidate finding, awaits it, and finalizes. The
pentester + validator run as concurrent subagents on their own scripted
backends. No real `claude`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse, Message
from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.router import AgentRouter
from zero.orchestrator.subagents import SubagentManager, SubagentRecord, SubagentResult
from zero.ui.events import (
    AgentTextDelta,
    ApprovalPrompt,
    AskUser,
    Finding as FindingEvent,
    SessionEnd,
    SessionSummary,
    SubagentFinished,
    SubagentStarted,
)


class ScriptedBackend(LLMBackend):
    """Returns scripted envelopes in order; raises if exhausted."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def run(self, system_prompt, messages, tools):
        self.calls += 1
        if not self._script:
            raise AssertionError("scripted backend exhausted")
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return LLMResponse(text=entry)


# --- envelope builders -------------------------------------------------------

_FINDING = {
    "title": "Reflected XSS in /snippets uid",
    "summary": "uid reflected unescaped",
    "endpoint": "GET /snippets",
    "method": "GET",
    "cwe": "CWE-79",
    "severity_hint": "high",
    "evidence": "GET /snippets?uid=<script>alert(1)</script> reflected raw",
    "payload_shape": "uid=<script>",
}


def _action(tool: str, args: dict, thought: str = "t") -> str:
    return json.dumps({"type": "action", "tool": tool, "args": args, "thought": thought})


def _pentester_final() -> str:
    return json.dumps(
        {
            "type": "final",
            "answer": json.dumps(
                {
                    "status": "EXPLOITED",
                    "summary": "uid reflects <script> unescaped; XSS fires.",
                    "candidate_findings": [_FINDING],
                    "discovered": ["session cookie for user carol captured"],
                }
            ),
        }
    )


def _validator_final(verdict: str) -> str:
    return json.dumps(
        {
            "type": "final",
            "answer": json.dumps(
                {
                    "verdict": verdict,
                    "rationale": "reproduced the reflection with a benign marker",
                    "reproducer": "GET /snippets?uid=zz<script>alert(1)</script>",
                    "cvss40_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:P/VC:L/VI:L/VA:N/SC:N/SI:N/SA:N",
                    "cvss40_score": 5.1,
                    "cwe": "CWE-79",
                }
            ),
        }
    )


# --- harness -----------------------------------------------------------------


async def _drive(orch: Orchestrator, *, capture: list) -> None:
    while True:
        ev = await asyncio.wait_for(orch.ui_queue.get(), timeout=10)
        capture.append(ev)
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "auto")
        elif isinstance(ev, SessionSummary):
            # finalize now shows the report and the session stays OPEN; for the
            # test, the report is our completion signal.
            return
        elif isinstance(ev, SessionEnd):
            return


async def _run_orchestrated(orch: Orchestrator, validator_backend: LLMBackend, capture: list) -> None:
    orch.audit.write("session_start", cwd=str(orch.cfg.cwd), backend="echo", goal=orch.cfg.goal)
    orch.validator_backend = validator_backend
    orch.router = AgentRouter(
        store=orch.findings_store,
        backend=validator_backend,
        tools=orch.registry.catalog(),
        project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )
    agent_task = asyncio.create_task(orch._orchestrator_main(None))
    try:
        await asyncio.wait_for(_drive(orch, capture=capture), timeout=15)
    finally:
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()


def _make_orch(tmp_path: Path, orchestrator_backend, pentester_backend) -> Orchestrator:
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="find a web bug", backend="echo",
        execution_mode="c", yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    # Each pentester subagent gets a fresh scripted backend.
    orch._subagent_backend_factory = lambda rec: ScriptedBackend([pentester_backend()])
    return orch


async def test_orchestrated_confirms_finding_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "XSS in uid", "target": "/snippets"}),
            _action("await_subagents", {"which": "all"}),
            _action("dispatch_validator", {**_FINDING, "source_subagent": "#1"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "one confirmed XSS"}),
        ]
    )
    validator_backend = ScriptedBackend([_validator_final("confirmed")])
    orch = _make_orch(tmp_path, orchestrator_backend, _pentester_final)

    capture: list = []
    await _run_orchestrated(orch, validator_backend, capture)

    # Confirmed finding persisted.
    confirmed = list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
    assert len(confirmed) == 1

    # Subagent lifecycle events for both a pentester and a validator.
    starts = [e for e in capture if isinstance(e, SubagentStarted)]
    assert {s.subkind for s in starts} == {"pentester", "validator"}
    assert any(isinstance(e, SubagentFinished) and e.subkind == "pentester" for e in capture)

    # A confirmed finding card + a final summary with the report.
    cards = [e for e in capture if isinstance(e, FindingEvent)]
    assert any(c.verdict == "confirmed" for c in cards)
    summaries = [e for e in capture if isinstance(e, SessionSummary)]
    assert len(summaries) == 1
    assert len(summaries[0].confirmed) == 1
    assert summaries[0].report_path.endswith("report.md")

    # Report on disk separates found from tested.
    report = (tmp_path / ".pentest" / "report.md").read_text(encoding="utf-8")
    assert "## Found & approved" in report
    assert "Reflected XSS" in report

    # Audit chain.
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert "dispatch_pentester" in kinds
    assert "dispatch_validator" in kinds
    assert "finding_persisted" in kinds
    assert "finalize" in kinds


async def test_finalize_refused_until_candidate_is_validated(tmp_path: Path, monkeypatch):
    """Strict validation: the orchestrator cannot finalize a candidate finding it
    never sent to a Validator. It must dispatch_validator first. (Regression: a
    real run finalized 'LAB SOLVED' on a self-verified finding with no validator.)"""
    monkeypatch.chdir(tmp_path)
    # Orchestrator tries to finalize right after the pentester reports a candidate,
    # WITHOUT validating. The guard refuses; it then validates and finalizes.
    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "XSS in uid", "target": "/snippets"}),
            _action("await_subagents", {"which": "any"}),
            _action("finalize", {"summary": "solved, no need to validate"}),  # REFUSED
            _action("dispatch_validator", {**_FINDING, "source_subagent": "#1"}),
            _action("await_subagents", {"which": "any"}),
            _action("finalize", {"summary": "now validated"}),               # allowed
        ]
    )
    validator_backend = ScriptedBackend([_validator_final("confirmed")])
    orch = _make_orch(tmp_path, orchestrator_backend, _pentester_final)

    capture: list = []
    await _run_orchestrated(orch, validator_backend, capture)

    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    # The premature finalize did NOT persist/finalize; only the post-validation one did.
    assert "dispatch_validator" in kinds
    assert "finding_persisted" in kinds
    assert kinds.count("finalize") == 1  # the refused finalize never reached _emit_report
    confirmed = list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
    assert len(confirmed) == 1


def _pentester_noop_final() -> str:
    return json.dumps(
        {"type": "final", "answer": json.dumps({"status": "NO_FINDING", "summary": "nothing"})}
    )


async def test_concurrency_cap_limits_running_subagents(tmp_path: Path, monkeypatch):
    """With cap=2 and 4 dispatched pentesters, at most 2 run concurrently."""
    monkeypatch.chdir(tmp_path)
    gate = asyncio.Event()
    active = 0
    max_active = 0

    class GatedPentesterBackend(LLMBackend):
        async def run(self, system_prompt, messages, tools):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await gate.wait()
            active -= 1
            return LLMResponse(text=_pentester_noop_final())

    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "i0", "target": "/t0"}),
            _action("dispatch_pentester", {"attack_idea": "i1", "target": "/t1"}),
            _action("dispatch_pentester", {"attack_idea": "i2", "target": "/t2"}),
            _action("dispatch_pentester", {"attack_idea": "i3", "target": "/t3"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True,
        max_concurrent_subagents=2,
    )
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch._subagent_backend_factory = lambda rec: GatedPentesterBackend()
    orch.validator_backend = ScriptedBackend([])  # no validators here
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    orch.audit.write("session_start", cwd=str(tmp_path), backend="echo", goal="g")
    agent_task = asyncio.create_task(orch._orchestrator_main(None))
    try:
        await asyncio.sleep(0.15)  # let all 4 dispatch + 2 admitted hit the gate
        assert max_active <= 2, f"cap breached: {max_active} ran at once"
        assert active == 2
        gate.set()
        # finalize now leaves the loop idle (session stays open), so wait for the
        # subagents to finish rather than for the loop to end.
        for _ in range(500):
            if len(orch.subagents.attempts) == 4 and all(
                a.status != "running" for a in orch.subagents.attempts
            ):
                break
            await asyncio.sleep(0.01)
    finally:
        gate.set()
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()

    assert max_active == 2  # the cap was actually reached
    assert len(orch.subagents.attempts) == 4


async def test_no_deadlock_when_subagent_needs_approval_during_await(tmp_path: Path, monkeypatch):
    """A subagent that needs an (exploit) approval while the orchestrator is
    blocked in await_subagents must still complete — the UI keeps resolving
    approvals concurrently."""
    from pydantic import BaseModel

    from zero.tools.base import RiskLevel, Tool, ToolResult

    class _EmptyArgs(BaseModel):
        pass

    class FakeExploitTool(Tool):
        name = "fake_exploit"
        description = "a fake exploit tool for tests"
        risk_level = RiskLevel.EXPLOIT
        Args = _EmptyArgs

        async def run(self, args):
            return ToolResult(ok=True, output="pwned")

    monkeypatch.chdir(tmp_path)

    def pentester_script():
        return ScriptedBackend(
            [
                _action("fake_exploit", {}),       # needs approval (exploit, not auto)
                _pentester_noop_final(),
            ]
        )

    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "i", "target": "/t"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c")
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch._subagent_backend_factory = lambda rec: pentester_script()
    orch.registry.register(FakeExploitTool())
    orch.validator_backend = ScriptedBackend([])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    capture: list = []
    # _drive runs concurrently and approves the exploit prompt while the
    # orchestrator is blocked in await_subagents — proving no deadlock.
    await _run_orchestrated(orch, orch.validator_backend, capture)

    assert any(isinstance(e, ApprovalPrompt) for e in capture), "exploit call should have prompted"
    assert any(isinstance(e, SubagentFinished) for e in capture)
    assert any(isinstance(e, SessionSummary) for e in capture)


class _AlwaysActionBackend(LLMBackend):
    """Never finals — always proposes an action against a tool that isn't in the
    registry (so the call is a cheap no-op observation). Used to drive a subagent
    into its turn cap / wall-clock deadline deterministically."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, system_prompt, messages, tools):
        self.calls += 1
        return LLMResponse(text=_action("nonexistent_tool", {}))


async def test_operator_can_interject_while_subagents_run(tmp_path: Path, monkeypatch):
    """Regression: while the orchestrator was blocked in await_subagents, operator
    input was queued but never acted on — the chat looked frozen until the whole
    batch finished. The blocking await must now return early when the operator
    submits a message, WITHOUT cancelling the still-running subagents."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)

    block = asyncio.Event()

    async def runner(rec: SubagentRecord) -> SubagentResult:
        await block.wait()  # stay running until the test releases it
        return SubagentResult(
            subagent_id=rec.id, kind=rec.kind, target=rec.target, idea=rec.idea,
            status="NO_FINDING", summary="done",
        )

    orch.subagents = SubagentManager(max_concurrent=5, runner=runner, emit=orch._emit)
    orch.subagents.spawn("pentester", target="/t", idea="i")

    task = asyncio.create_task(orch._await_subagents_interruptible("any", None))
    await asyncio.sleep(0.05)
    assert not task.done(), "await should block while the subagent is still running"

    # Operator types something mid-flight.
    orch.submit_user_text("focus on the login form instead")

    results, interrupted = await asyncio.wait_for(task, timeout=2)
    assert interrupted is True
    assert results == []                                  # nothing finished yet
    assert orch.subagents.running_counts() == (1, 0)      # subagent kept running
    block.set()
    await asyncio.wait_for(orch.subagents.await_("all"), timeout=2)


async def test_pentester_uses_its_own_turn_budget_not_orchestrators(tmp_path: Path, monkeypatch):
    """Regression (3000s runs): pentesters inherited the orchestrator's large
    max_turns, so one idea could run 40+ turns. They must use the (smaller)
    pentester_max_turns instead."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c", yolo=True,
        max_turns=99, pentester_max_turns=2, subagent_deadline_seconds=9999,
    )
    orch = Orchestrator(cfg)
    backend = _AlwaysActionBackend()
    orch._subagent_backend_factory = lambda rec: backend

    rec = SubagentRecord(id="#1", kind="pentester", target="/t", idea="i")
    result = await asyncio.wait_for(orch._run_pentester_subagent(rec), timeout=5)

    assert result.turns == 2          # stopped at pentester_max_turns, not 99
    assert backend.calls == 2


async def test_pentester_stops_at_wall_clock_deadline(tmp_path: Path, monkeypatch):
    """Regression (3000s runs): a subagent that keeps iterating had no total
    wall-clock cap (only a per-`claude -p`-call timeout). With the deadline at 0
    it must bail before doing any work, with an ERROR result that explains why."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c", yolo=True,
        pentester_max_turns=99, subagent_deadline_seconds=0,
    )
    orch = Orchestrator(cfg)
    backend = _AlwaysActionBackend()
    orch._subagent_backend_factory = lambda rec: backend

    rec = SubagentRecord(id="#1", kind="pentester", target="/t", idea="i")
    result = await asyncio.wait_for(orch._run_pentester_subagent(rec), timeout=5)

    assert result.status == "ERROR"
    assert "deadline" in result.summary
    assert backend.calls == 0  # tripped before the first LLM call


async def test_turn_error_does_not_kill_orchestrator(tmp_path: Path, monkeypatch):
    """Regression (gruyere 'blocked' hang): a backend error on a turn used to
    escape _orchestrator_main and silently kill the agent task, leaving the UI
    alive but inert. It must instead surface the error and go idle for the next
    prompt — the engagement stays open and recovers."""
    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend(
        [
            RuntimeError("claude -p exit 1: boom"),          # turn 1 blows up
            _action("finalize", {"summary": "recovered"}),   # after the operator re-prompts
        ]
    )
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch.validator_backend = ScriptedBackend([])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    capture: list = []
    saw_error = False

    async def drive() -> None:
        nonlocal saw_error
        while True:
            ev = await asyncio.wait_for(orch.ui_queue.get(), timeout=10)
            capture.append(ev)
            if isinstance(ev, AgentTextDelta) and "[turn error]" in ev.text:
                saw_error = True
                orch.submit_user_text("try again")  # re-prompt to continue
            elif isinstance(ev, SessionSummary):
                return

    orch.audit.write("session_start", cwd=str(tmp_path), backend="echo", goal="g")
    agent_task = asyncio.create_task(orch._orchestrator_main(None))
    try:
        await asyncio.wait_for(drive(), timeout=15)
    finally:
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()

    assert saw_error, "the backend error should have surfaced as a [turn error] line"
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert "turn_error" in kinds
    # The loop survived and ran the next turn (finalize) instead of dying.
    assert any(isinstance(e, SessionSummary) for e in capture)


def test_default_execution_mode_is_orchestrated():
    cfg = OrchestratorConfig(cwd=Path("."))
    assert cfg.execution_mode == "c"
    assert cfg.max_concurrent_subagents == 10
    assert cfg.max_turns == 10  # orchestrator turn budget (turn awareness)
    assert cfg.subagent_timeout_seconds == 900  # higher per-call timeout for subagents
    assert cfg.pentester_max_turns == 14   # pentesters don't inherit the big max_turns
    assert cfg.subagent_deadline_seconds == 1200  # total wall-clock cap per subagent


def test_subagent_timeout_applies_to_subagent_backends_not_orchestrator(tmp_path: Path):
    """--subagent-timeout sets the per-call claude timeout on the pentester +
    validator backends, while the orchestrator's own backend keeps the default."""
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="claude", execution_mode="c", subagent_timeout_seconds=1234,
    )
    orch = Orchestrator(cfg)
    assert orch.subagent_backend.cfg.timeout_seconds == 1234   # pentester subagents
    assert orch.validator_backend.cfg.timeout_seconds == 1234  # validator subagents
    assert orch.backend.cfg.timeout_seconds == 300             # orchestrator's own (default)


async def test_turn_limit_ends_after_waiting_for_running_subagent(tmp_path: Path, monkeypatch):
    """When the orchestrator hits its turn budget it must END cleanly: a
    still-running subagent is awaited to COMPLETION (never cancelled), then the
    report is written. The orchestrator never calls `finalize` here — the cap
    does it. (Regression for the 'aware of turns + ends at the limit + waits for
    subagents' behavior.)"""
    monkeypatch.chdir(tmp_path)
    release = asyncio.Event()

    class GatedPentester(LLMBackend):
        async def run(self, system_prompt, messages, tools):
            await release.wait()  # stay "running" until the orchestrator waits for us
            return LLMResponse(text=_pentester_noop_final())

    # max_turns=3: turn 1 dispatches (allowed), turns 2-3 are filler, turn 4 hits
    # the cap. No `finalize` in the script — the turn limit must finalize for us.
    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "i", "target": "/t"}),
            _action("list_subagents", {}),
            _action("list_subagents", {}),
        ]
    )
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="g", backend="echo", execution_mode="c",
        yolo=True, max_turns=3,
    )
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch._subagent_backend_factory = lambda rec: GatedPentester()
    orch.validator_backend = ScriptedBackend([])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    capture: list = []

    async def drive() -> None:
        while True:
            ev = await asyncio.wait_for(orch.ui_queue.get(), timeout=10)
            capture.append(ev)
            if isinstance(ev, ApprovalPrompt):
                orch.submit_approval(ev.call_id, Decision.YES)
            elif isinstance(ev, AgentTextDelta) and "turn budget reached" in ev.text:
                # The orchestrator is now about to wait for the running subagent.
                # Releasing it proves the engagement waits-then-completes (rather
                # than hanging forever or cancelling the subagent).
                release.set()
            elif isinstance(ev, (SessionSummary, SessionEnd)):
                return

    orch.audit.write("session_start", cwd=str(tmp_path), backend="echo", goal="g")
    agent_task = asyncio.create_task(orch._orchestrator_main(None))
    try:
        await asyncio.wait_for(drive(), timeout=15)
    finally:
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()

    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    # Ended because of the turn budget, and wrote the report.
    assert "turn_limit_reached" in kinds
    assert (tmp_path / ".pentest" / "report.md").exists()
    # The running subagent was awaited to completion, not abandoned/cancelled.
    assert any(isinstance(e, SubagentFinished) for e in capture)
    assert len(orch.subagents.attempts) == 1
    assert all(a.status != "running" for a in orch.subagents.attempts)
