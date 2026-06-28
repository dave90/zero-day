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
    ActivityLine,
    AgentTextDelta,
    ApprovalPrompt,
    AskUser,
    Finding as FindingEvent,
    SessionEnd,
    SessionSummary,
    Status,
    SubagentFinished,
    SubagentStarted,
    TodoUpdate,
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


def _actions(calls: list[tuple[str, dict]], thought: str = "batch") -> str:
    return json.dumps({
        "type": "actions",
        "thought": thought,
        "calls": [{"tool": t, "args": a} for t, a in calls],
    })


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
    # 2 work turns + 1 final "handoff" call at the cap (so a pentester that runs
    # out of turns still hands its findings back instead of a bare cap message).
    assert backend.calls == 3


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


async def test_turn_error_auto_recovers_without_user_input(tmp_path: Path, monkeypatch):
    """Regression (gruyere): the orchestrator's turn produced INVALID JSON (twice,
    so `step()`'s own retry also failed → EnvelopeError). It used to either kill
    the task (silent hang) or give up to the operator (looked crashed). It must now
    feed the error back and RETRY on its own — no user input — and recover."""
    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend(
        [
            "this is not json {",                            # turn 1, attempt 1
            "still not valid json",                          # turn 1, step() retry → EnvelopeError
            _action("finalize", {"summary": "recovered"}),   # turn 2, auto-retried (no user input)
        ]
    )
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend

    capture: list = []
    # _run_orchestrated answers prompts but NEVER submits user text, so reaching
    # `finalize` proves the orchestrator recovered on its own.
    await _run_orchestrated(orch, ScriptedBackend([]), capture)

    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert "turn_error" in kinds
    assert any(isinstance(e, SessionSummary) for e in capture)  # recovered → finalized


async def test_gives_up_after_max_consecutive_turn_errors(tmp_path: Path, monkeypatch):
    """After too many consecutive failed turns the orchestrator stops auto-retrying
    and surrenders to the operator (rather than burning turns/tokens forever)."""
    monkeypatch.chdir(tmp_path)
    # 3 failed turns × (attempt + step() retry) = 6 invalid responses → 3 turn_errors.
    orchestrator_backend = ScriptedBackend(["definitely not json {"] * 6)
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch.validator_backend = ScriptedBackend([])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    gave_up = False

    async def drive() -> None:
        nonlocal gave_up
        while True:
            ev = await asyncio.wait_for(orch.ui_queue.get(), timeout=10)
            if isinstance(ev, AgentTextDelta) and "Giving up" in ev.text:
                gave_up = True
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

    assert gave_up, "should surrender to the operator after repeated failures"
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert kinds.count("turn_error") == 3


async def test_dispatch_emits_activity_lines(tmp_path: Path, monkeypatch):
    """Dispatch/await/validate/finalize used to emit NOTHING to the chat (they go
    through _handle_dispatch, which never emits a ToolProposal). They must now each
    surface a concise `▸ …` ActivityLine so the chat isn't silent during waits."""
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

    activity = [e.text for e in capture if isinstance(e, ActivityLine)]
    assert any("▸ pentester" in t for t in activity)
    assert any("▸ waiting for subagents" in t for t in activity)
    assert any("▸ validating" in t for t in activity)
    assert any("▸ finalizing" in t for t in activity)
    # A subagent FINISHING now reaches the chat path (was suppressed before).
    assert any(isinstance(e, SubagentFinished) for e in capture)


async def test_subagent_survives_a_crashing_tool_call(tmp_path: Path, monkeypatch):
    """Regression (Blueprint_Heist): a tool that raises (a NUL payload →
    'embedded null byte') used to crash the whole pentester subagent (status
    ERROR), losing the investigation. It must instead return a failed observation
    so the subagent keeps going and finishes normally."""
    from pydantic import BaseModel

    from zero.tools.base import RiskLevel, Tool, ToolResult

    class _Empty(BaseModel):
        pass

    class _NulTool(Tool):
        name = "boom"
        description = "raises embedded null byte"
        risk_level = RiskLevel.ACTIVE
        Args = _Empty

        async def run(self, args) -> ToolResult:
            raise ValueError("embedded null byte")

    monkeypatch.chdir(tmp_path)

    def pentester_script():
        return ScriptedBackend(
            [
                _action("boom", {}),           # crashes inside the tool…
                _pentester_noop_final(),        # …but the subagent must continue
            ]
        )

    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "x", "target": "/t"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch._subagent_backend_factory = lambda rec: pentester_script()
    orch.registry.register(_NulTool())
    orch.validator_backend = ScriptedBackend([])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
    )

    capture: list = []
    await _run_orchestrated(orch, orch.validator_backend, capture)

    finished = [e for e in capture if isinstance(e, SubagentFinished)]
    assert finished, "the subagent should have finished, not vanished"
    # Survived the crashing tool call → reached its own NO_FINDING final,
    # NOT killed with status ERROR.
    assert finished[0].status == "NO_FINDING"


async def test_subagent_memory_written_and_orchestrator_reads_it(tmp_path: Path, monkeypatch):
    """Per-agent memory: a pentester and a validator each write their working
    memory (keyed by their OWN actor id), and the orchestrator reads both back via
    read_subagent_memory. Locks in (a) host-side memory tools, (b) per-actor
    session_id injection, and (c) the validator keying on rec.id, not the UI label."""
    from zero.tools._memroot import path_for

    monkeypatch.chdir(tmp_path)
    # Deterministic subagent ids: pentester=gino_1, validator=gino_2.
    monkeypatch.setattr("zero.orchestrator.subagents.random.choice", lambda seq: "gino")

    def pentester_script():
        return ScriptedBackend(
            [
                _action("memory_write", {"content": "PENTEST MEMO: admin token=abc123"}),
                _pentester_final(),
            ]
        )

    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "XSS in uid", "target": "/snippets"}),
            _action("await_subagents", {"which": "all"}),
            _action("dispatch_validator", {**_FINDING, "source_subagent": "gino_1"}),
            _action("await_subagents", {"which": "all"}),
            _action("read_subagent_memory", {"agent_id": "gino_1"}),
            _action("read_subagent_memory", {"agent_id": "gino_2"}),
            _action("list_memories", {}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    validator_backend = ScriptedBackend(
        [
            _action("memory_write", {"content": "VALIDATOR MEMO: reproduced the reflection"}),
            _validator_final("confirmed"),
        ]
    )
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="find a web bug", backend="echo", execution_mode="c", yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend
    orch._subagent_backend_factory = lambda rec: pentester_script()

    capture: list = []
    await _run_orchestrated(orch, validator_backend, capture)

    # Each actor wrote to ITS OWN file (validator keyed by rec.id, not the label).
    assert "PENTEST MEMO: admin token=abc123" in path_for("gino_1").read_text(encoding="utf-8")
    assert "VALIDATOR MEMO: reproduced the reflection" in path_for("gino_2").read_text(encoding="utf-8")

    # The orchestrator's reads were handled (intercepted, host-side) and it finalized.
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    reads = [r for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")
             if r["kind"] == "read_subagent_memory"]
    assert {r["agent_id"] for r in reads} == {"gino_1", "gino_2"}
    assert all(r["found"] for r in reads)
    assert "list_memories" in kinds
    assert "finalize" in kinds
    assert any(isinstance(e, SessionSummary) for e in capture)


async def test_harness_autowrites_memory_when_model_never_does(tmp_path: Path, monkeypatch):
    """Phase A safety net: even though the scripted pentester + validator NEVER call
    memory_write, the harness persists each one's end-state to its memory file so the
    orchestrator can recover it. (Regression: in real runs agents read memory but
    write 0×, leaving files empty.)"""
    from zero.tools._memroot import path_for

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("zero.orchestrator.subagents.random.choice", lambda seq: "gino")

    orchestrator_backend = ScriptedBackend(
        [
            _action("dispatch_pentester", {"attack_idea": "XSS in uid", "target": "/snippets"}),
            _action("await_subagents", {"which": "all"}),
            _action("dispatch_validator", {**_FINDING, "source_subagent": "gino_1"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    validator_backend = ScriptedBackend([_validator_final("confirmed")])
    # Pentester just finals (EXPLOITED) — no memory_write anywhere.
    orch = _make_orch(tmp_path, orchestrator_backend, _pentester_final)

    capture: list = []
    await _run_orchestrated(orch, validator_backend, capture)

    pent_mem = path_for("gino_1").read_text(encoding="utf-8")
    assert "STATUS: EXPLOITED" in pent_mem
    assert "uid reflects" in pent_mem            # the pentester's summary landed
    val_mem = path_for("gino_2").read_text(encoding="utf-8")
    assert "VERDICT: confirmed" in val_mem       # validator end-state persisted too


async def test_pentester_actions_batch_runs_all_calls_in_one_turn(tmp_path: Path, monkeypatch):
    """Phase B: a subagent emits ONE `actions` envelope with two independent tool
    calls; both run and BOTH observations are appended in a single turn (one LLM
    round-trip), then it finals."""
    monkeypatch.chdir(tmp_path)

    seen: list[str] = []

    class _CountingBackend(LLMBackend):
        def __init__(self) -> None:
            self.calls = 0
            self._script = [
                _actions([("list_dir", {"path": "."}), ("grep", {"pattern": "x", "path": "."})]),
                _pentester_noop_final(),
            ]

        async def run(self, system_prompt, messages, tools):
            # Record observations the loop fed back so we can assert both landed.
            for m in messages:
                if getattr(m, "role", "") == "observation":
                    seen.append(m.content)
            self.calls += 1
            return LLMResponse(text=self._script[self.calls - 1])

    backend = _CountingBackend()
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch._subagent_backend_factory = lambda rec: backend

    rec = SubagentRecord(id="#1", kind="pentester", target="/t", idea="i")
    result = await asyncio.wait_for(orch._run_pentester_subagent(rec), timeout=5)

    assert result.status == "NO_FINDING"
    assert backend.calls == 2  # batch turn + final turn — NOT 3 (the two calls shared one turn)
    # On the 2nd LLM call the loop had fed back BOTH observations from the batch.
    blob = "\n".join(seen)
    assert "list_dir" in blob and "grep" in blob


async def test_orchestrator_todo_write_emits_todo_update(tmp_path: Path, monkeypatch):
    """Phase C: when the orchestrator calls todo_write, a TodoUpdate event reaches the
    UI (checklist) and the plan is persisted host-side."""
    from zero.tools.todo import read_todo_items

    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend(
        [
            _action("todo_write", {"items": [
                {"step": "recon", "status": "in_progress"},
                {"step": "exploit", "status": "pending"},
            ]}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    orch = _make_orch(tmp_path, orchestrator_backend, _pentester_noop_final)

    capture: list = []
    await _run_orchestrated(orch, ScriptedBackend([]), capture)

    todos = [e for e in capture if isinstance(e, TodoUpdate)]
    assert todos and any("recon" in i["step"] for i in todos[0].items)
    assert todos[0].label == ""  # orchestrator's plan → shown in chat
    # Persisted under the orchestrator's session.
    assert [i["step"] for i in read_todo_items("orchestrator")] == ["recon", "exploit"]


async def test_orchestrator_batch_dispatches_all_pentesters(tmp_path: Path, monkeypatch):
    """Phase B regression (Guild 'pentesters failed to spin'): several
    dispatch_pentester calls in ONE actions batch must all spawn — a batch runs
    through the concurrent executor, so they aren't serialized behind each other (or
    behind a slow recon call)."""
    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend(
        [
            _actions([
                ("dispatch_pentester", {"attack_idea": "reset", "target": "/forgetpassword"}),
                ("dispatch_pentester", {"attack_idea": "sqli", "target": "/login"}),
                ("dispatch_pentester", {"attack_idea": "cookie", "target": "/signup"}),
            ]),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "done"}),
        ]
    )
    orch = _make_orch(tmp_path, orchestrator_backend, _pentester_noop_final)

    capture: list = []
    await _run_orchestrated(orch, ScriptedBackend([]), capture)

    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert kinds.count("dispatch_pentester") == 3            # all three spawned
    assert len(orch.subagents.attempts) == 3
    assert all(a.status != "running" for a in orch.subagents.attempts)


async def test_status_carries_context_window(tmp_path: Path, monkeypatch):
    """The orchestrator's Status events carry the context-window size (and the
    active loop's token usage) for the UI's per-agent ctx display."""
    monkeypatch.chdir(tmp_path)
    orchestrator_backend = ScriptedBackend([_action("finalize", {"summary": "done"})])
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True,
        context_window=1_000_000,
    )
    orch = Orchestrator(cfg)
    orch.backend = orchestrator_backend

    capture: list = []
    await _run_orchestrated(orch, ScriptedBackend([]), capture)

    statuses = [e for e in capture if isinstance(e, Status)]
    assert statuses, "expected at least one Status event"
    assert all(s.context_window == 1_000_000 for s in statuses)  # wired from cfg


async def test_context_window_resolved_backend_aware(tmp_path: Path, monkeypatch):
    """When left on auto (None), the ctx denominator is resolved per backend at startup:
    Claude infers it from the model id; Codex reads its real window from the model cache.
    An explicit value is always honored."""
    # Claude '[1m]' model → 1M; plain model → 200k.
    o1m = Orchestrator(OrchestratorConfig(
        cwd=tmp_path, backend="claude", model="claude-opus-4-8[1m]", use_container=False))
    assert o1m.cfg.context_window == 1_000_000
    o200 = Orchestrator(OrchestratorConfig(
        cwd=tmp_path, backend="claude", model="claude-sonnet-4-6", use_container=False))
    assert o200.cfg.context_window == 200_000

    # Codex → read from a temp model cache (no dependency on the real ~/.codex).
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"models": [{"slug": "gpt-5.5", "context_window": 272000}]}),
        encoding="utf-8",
    )
    ocx = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="codex", use_container=False))
    assert ocx.cfg.context_window == 272000

    # Explicit value is left untouched.
    oexp = Orchestrator(OrchestratorConfig(
        cwd=tmp_path, backend="codex", context_window=50_000, use_container=False))
    assert oexp.cfg.context_window == 50_000


async def test_pentester_recovers_from_a_backend_turn_error(tmp_path: Path, monkeypatch):
    """A single backend error (e.g. claude error_max_turns) must NOT kill the
    subagent — it feeds the error back and retries the turn, then finishes normally."""
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    # First step raises (drift→error_max_turns); the retry returns a clean final.
    orch._subagent_backend_factory = lambda rec: ScriptedBackend(
        [RuntimeError("claude -p exit 1: error_max_turns (stop=tool_use, num_turns=4)"),
         _pentester_noop_final()]
    )
    rec = SubagentRecord(id="#1", kind="pentester", target="/t", idea="i")
    result = await asyncio.wait_for(orch._run_pentester_subagent(rec), timeout=5)
    assert result.status == "NO_FINDING"   # recovered, not ERROR
    assert result.turns >= 2               # the failed turn + the recovered one


async def test_pentester_gives_up_after_repeated_errors_and_keeps_memory(tmp_path: Path, monkeypatch):
    """If the backend keeps failing, the subagent ends ERROR gracefully (not a bare
    crash) AFTER the consecutive cap, and its end-state is persisted to memory."""
    from zero.tools.memory import read_memory

    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)

    class _AlwaysRaises(LLMBackend):
        async def run(self, system_prompt, messages, tools):
            raise RuntimeError("error_max_turns")

    orch._subagent_backend_factory = lambda rec: _AlwaysRaises()
    rec = SubagentRecord(id="gino_9", kind="pentester", target="/t", idea="i")
    result = await asyncio.wait_for(orch._run_pentester_subagent(rec), timeout=5)
    assert result.status == "ERROR"
    assert "consecutive" in result.summary
    assert "ERROR" in read_memory("gino_9")  # work/end-state not silently lost


async def test_validator_driver_recovers_from_a_turn_error(tmp_path: Path):
    """default_validator_driver must survive a backend error and still produce a
    verdict (rather than returning None / parking the finding as pending)."""
    from zero.agents.base import AgentLoop
    from zero.orchestrator.router import default_validator_driver

    backend = ScriptedBackend([RuntimeError("error_max_turns"), _validator_final("confirmed")])
    loop = AgentLoop(backend, system_prompt="s", tools=[])

    async def _noop_action(env):  # not reached (final-only script)
        return ""

    async def _noop_ask(env):
        return "auto"

    v = await default_validator_driver(loop, action_handler=_noop_action, ask_handler=_noop_ask)
    assert v is not None and v.verdict == "confirmed"


def test_thought_display_gating(tmp_path: Path):
    """`thought_display` controls how much of the thought reaches the chat; the
    full thought always goes to thinking.log (unaffected by this knob)."""
    thought = "uid reflects unescaped. Then I weaponize it against carlos."
    cond = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", thought_display="condensed"))
    assert cond._thought_for_chat(thought) == "uid reflects unescaped"
    off = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", thought_display="off"))
    assert off._thought_for_chat(thought) == ""
    full = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", thought_display="full"))
    assert full._thought_for_chat(thought) == thought


def test_default_execution_mode_is_orchestrated():
    cfg = OrchestratorConfig(cwd=Path("."))
    assert cfg.execution_mode == "c"
    assert cfg.max_concurrent_subagents == 10
    assert cfg.max_turns == 10  # orchestrator turn budget (turn awareness)
    assert cfg.llm_timeout_seconds == 900  # shared per-call timeout (orchestrator + subagents)
    assert cfg.pentester_max_turns == 14   # pentesters don't inherit the big max_turns
    assert cfg.subagent_deadline_seconds == 1200  # total wall-clock cap per subagent
    assert cfg.thought_display == "condensed"  # one-line thought in chat by default


def test_llm_timeout_is_shared_by_orchestrator_and_subagents(tmp_path: Path):
    """`--llm-timeout` sets the SAME per-call claude timeout on the orchestrator's
    own backend AND the pentester/validator subagent backends — one value for all."""
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="claude", execution_mode="c", llm_timeout_seconds=1234,
    )
    orch = Orchestrator(cfg)
    assert orch.backend.cfg.timeout_seconds == 1234            # orchestrator's own
    assert orch.subagent_backend.cfg.timeout_seconds == 1234   # pentester subagents
    assert orch.validator_backend.cfg.timeout_seconds == 1234  # validator subagents


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
