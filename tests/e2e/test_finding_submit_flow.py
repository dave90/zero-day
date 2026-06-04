"""End-to-end: drive the full finding_submit pipeline through the
orchestrator, with a scripted backend that plays both the Pentest agent
and the Validator agent in alternating turns.

Covered scenarios (these are the ones that were broken in real runs):

  * Happy path  : Pentest submits → Validator confirms → file written to
                  `.pentest/findings/confirmed/`; audit log carries the
                  full event chain.

  * Dedup       : a second identical submission produces a "duplicate of"
                  observation back to the Pentest agent and creates no
                  new file on disk.

  * Validator   : the validator's backend.run() raises (simulating
    failure     : `claude -p exit 1`). The finding must land in
                  `.pentest/findings/pending/` with the actual error
                  captured in the notes — not silently dropped.
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
from zero.ui.events import (
    AgentTextDelta,
    ApprovalPrompt,
    AskUser,
    Finding as FindingEvent,
    SessionEnd,
    ToolObservation,
)


class ScriptedOrErrorBackend(LLMBackend):
    """Returns a string per call in order. If an entry is an Exception
    instance, raises it (used to simulate `claude -p exit 1`)."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[list[Message]] = []

    async def run(self, system_prompt, messages, tools):
        self.calls.append(list(messages))
        if not self._script:
            raise AssertionError("script exhausted")
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return LLMResponse(text=entry)


# Envelope builders ----------------------------------------------------------


def _submit_action(*, title: str, endpoint: str, cwe: str, payload_shape: str) -> str:
    return json.dumps(
        {
            "type": "action",
            "tool": "finding_submit",
            "args": {
                "title": title,
                "summary": "demo summary",
                "endpoint": endpoint,
                "method": "GET",
                "cwe": cwe,
                "severity_hint": "medium",
                "evidence": "GET " + endpoint + " returns other user's data",
                "payload_shape": payload_shape,
            },
            "thought": "I have enough; submitting.",
        }
    )


def _validator_final(verdict: str) -> str:
    return json.dumps(
        {
            "type": "final",
            "answer": json.dumps(
                {
                    "verdict": verdict,
                    "rationale": "reproduced with two accounts",
                    "reproducer": "login as A; GET /x/B-id; observe B's data",
                    "cvss40_vector": (
                        "CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N"
                        "/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N"
                    ),
                    "cvss40_score": 7.5,
                    "cwe": "CWE-639",
                }
            ),
        }
    )


def _pentest_final(answer: str = "done") -> str:
    return json.dumps({"type": "final", "answer": answer})


# Fake UI: auto-approves and auto-answers asks -------------------------------


async def _drive_until_final(orch: Orchestrator, *, capture: list | None = None) -> None:
    """Auto-approve, auto-answer, return on the first [final] text.

    If `capture` is provided, every UIEvent observed is appended to it
    (lets callers assert on the event stream — e.g. presence of a
    finding card or session summary).
    """
    while True:
        ev = await orch.ui_queue.get()
        if capture is not None:
            capture.append(ev)
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "auto")
        elif isinstance(ev, AgentTextDelta) and "[final]" in ev.text:
            return
        elif isinstance(ev, SessionEnd):
            return


async def _run_with(
    orch: Orchestrator, *, capture: list | None = None, drain: bool = True
) -> None:
    """Same shape as the M1 echo e2e — bypasses run_interactive's UI.

    Validation now runs in BACKGROUND tasks, so after the pentest agent's
    `[final]` we explicitly `drain_validations()` to let verdicts land before
    the caller asserts (mirrors production, where the UI keeps draining).
    """
    orch.audit.write(
        "session_start",
        cwd=str(orch.cfg.cwd),
        backend=orch.cfg.backend,
        goal=orch.cfg.goal,
        has_project_context=False,
    )
    # The router is set up by `run_interactive`; do it manually here. The
    # Validator runs on its own (possibly separate) backend.
    orch.router = AgentRouter(
        store=orch.findings_store,
        backend=orch.validator_backend,
        tools=orch.registry.catalog(),
        project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )
    agent_task = asyncio.create_task(orch._agent_main(None))
    ui_task = asyncio.create_task(_drive_until_final(orch, capture=capture))
    try:
        await asyncio.wait_for(ui_task, timeout=10)
        if drain:
            await asyncio.wait_for(orch.drain_validations(), timeout=10)
    finally:
        # Simulate /exit so the agent's idle-wait unblocks. Drain a few
        # more events so callers can see the SessionSummary the agent
        # emits on the way out.
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        if capture is not None:
            while not orch.ui_queue.empty():
                capture.append(orch.ui_queue.get_nowait())
        orch.audit.write("session_end")
        orch.audit.close()


def _make_orch(
    tmp_path: Path,
    backend: LLMBackend,
    goal: str = "test",
    *,
    validator_backend: LLMBackend | None = None,
) -> Orchestrator:
    cfg = OrchestratorConfig(cwd=tmp_path, goal=goal, backend="echo")
    orch = Orchestrator(cfg)
    orch.backend = backend  # pentest agent's scripted backend
    # The Validator runs on its own scripted backend so async interleaving is
    # deterministic (a single shared script would race between the two loops).
    orch.validator_backend = validator_backend if validator_backend is not None else backend
    return orch


# Tests ----------------------------------------------------------------------


async def test_confirmed_finding_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(
                title="IDOR /api/x",
                endpoint="/api/x/1",
                cwe="CWE-639",
                payload_shape="http_replay/tampered-id",
            ),
            _pentest_final(),
        ]
    )
    validator = ScriptedOrErrorBackend([_validator_final("confirmed")])
    orch = _make_orch(tmp_path, pentest, goal="find IDOR", validator_backend=validator)
    await _run_with(orch)

    confirmed_dir = tmp_path / ".pentest" / "findings" / "confirmed"
    files = list(confirmed_dir.glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("---\n")
    assert "Validator verdict" in body
    assert "confirmed" in body
    assert "CVSS:4.0" in body

    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert "finding_submit" in kinds
    assert "finding_persisted" in kinds


async def test_rejected_finding_lands_in_rejected_dir(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(title="bogus", endpoint="/x", cwe="CWE-?", payload_shape="x"),
            _pentest_final(),
        ]
    )
    validator = ScriptedOrErrorBackend([_validator_final("rejected")])
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    await _run_with(orch)
    assert len(list((tmp_path / ".pentest" / "findings" / "rejected").glob("*.md"))) == 1
    assert not list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))


async def test_resubmission_is_dedup(tmp_path: Path, monkeypatch):
    """A second identical submission must NOT create a new file and must
    NOT re-invoke the validator (script would exhaust otherwise)."""
    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            # first submission
            _submit_action(
                title="dup", endpoint="/x/1", cwe="CWE-639",
                payload_shape="http_replay/x",
            ),
            # second submission — same dedup key (different numeric id is
            # normalized away by dedup.normalize_endpoint)
            _submit_action(
                title="dup", endpoint="/x/9999", cwe="CWE-639",
                payload_shape="http_replay/x",
            ),
            _pentest_final(),
        ]
    )
    # Exactly ONE validator turn available — the second submission must dedup
    # (via the in-flight guard or the on-disk record), so it never spawns a
    # second validation. If it did, this script would exhaust and raise.
    validator = ScriptedOrErrorBackend([_validator_final("confirmed")])
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    await _run_with(orch)

    files = list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
    assert len(files) == 1, "second submission must not create a new file"
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert kinds.count("finding_submit") == 2
    assert kinds.count("finding_persisted") == 1


async def test_validator_backend_failure_parks_pending_with_error_in_notes(
    tmp_path: Path, monkeypatch
):
    """Simulates `claude -p exit 1` for the validator: the finding must
    still land on disk (in pending/) with the actual error captured."""
    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(
                title="needs validation",
                endpoint="/x/1",
                cwe="CWE-79",
                payload_shape="http_replay/xss",
            ),
            _pentest_final(),
        ]
    )
    # The validator's backend call raises (simulates `claude -p exit 1`).
    validator = ScriptedOrErrorBackend(
        [RuntimeError("claude -p exit 1: context length exceeded")]
    )
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    await _run_with(orch)

    pending_files = list((tmp_path / ".pentest" / "findings" / "pending").glob("*.md"))
    assert len(pending_files) == 1
    body = pending_files[0].read_text(encoding="utf-8")
    # The actual error string must be in the notes — that's the whole
    # point of this test (without it, the user is blind to WHY validation
    # failed).
    assert "claude -p exit 1" in body
    assert "context length exceeded" in body
    # Nothing in confirmed/rejected.
    assert not list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
    assert not list((tmp_path / ".pentest" / "findings" / "rejected").glob("*.md"))


async def test_session_emits_rich_finding_card_and_summary(
    tmp_path: Path, monkeypatch
):
    """After a confirmed finding the orchestrator must emit:
       (a) a Finding event carrying ALL the fields the renderer needs to
           draw a full card (id, endpoint, CWE, CVSS, rationale, reproducer,
           file_path), and
       (b) a SessionSummary at the end listing the finding.
    Both are what the user sees at the bottom of the run."""
    from zero.ui.events import Finding as FindingEvent
    from zero.ui.events import SessionSummary

    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(
                title="Reflected XSS in error page",
                endpoint="/error/{path}",
                cwe="CWE-79",
                payload_shape="http_replay/xss",
            ),
            _pentest_final(),
        ]
    )
    validator = ScriptedOrErrorBackend([_validator_final("confirmed")])
    orch = _make_orch(tmp_path, pentest, goal="find xss", validator_backend=validator)
    capture: list = []
    await _run_with(orch, capture=capture)

    # (a) Finding event carries the full payload.
    findings = [e for e in capture if isinstance(e, FindingEvent)]
    assert len(findings) == 1
    f = findings[0]
    assert f.verdict == "confirmed"
    assert f.finding_id  # non-empty
    assert f.title == "Reflected XSS in error page"
    assert f.endpoint == "/error/{path}"
    # Validator may refine the CWE; the card shows the validator's value
    # when set. Our mock validator refines CWE-79 → CWE-639.
    assert f.cwe in ("CWE-79", "CWE-639")
    assert f.cvss_score > 0
    assert "CVSS:4.0/" in f.cvss_vector
    assert f.rationale  # validator gave one
    assert f.reproducer  # validator gave one
    assert f.file_path.endswith(f"{f.finding_id}.md")

    # (b) SessionSummary at the end lists it.
    summaries = [e for e in capture if isinstance(e, SessionSummary)]
    assert len(summaries) == 1
    s = summaries[0]
    assert len(s.confirmed) == 1
    assert s.confirmed[0]["id"] == f.finding_id
    assert s.confirmed[0]["title"] == f.title
    assert s.findings_dir
    assert s.audit_path


# ---------------------------------------------------------------------------
# Regression tests for the failures that froze the /tmp/gruyere run: the
# validator blocked the pentest loop and kept crashing on `error_max_turns`.
# (The `error_max_turns` RETRY itself is unit-tested in
# tests/unit/test_claude_code_backend.py; here we guard the orchestrator's
# async behaviour with fake backends.)
# ---------------------------------------------------------------------------


class GatedBackend(LLMBackend):
    """Blocks on an Event before returning — lets a test hold a validation
    'in flight' while it inspects orchestrator state."""

    def __init__(self, gate: asyncio.Event, response: str) -> None:
        self._gate = gate
        self._response = response
        self.calls = 0

    async def run(self, system_prompt, messages, tools):
        self.calls += 1
        await self._gate.wait()
        return LLMResponse(text=self._response)


class AlwaysMaxTurnsBackend(LLMBackend):
    """Every call fails the way the real `claude -p` did on gruyere."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, system_prompt, messages, tools):
        self.calls += 1
        raise RuntimeError(
            "claude -p exit 1: error_max_turns (stop=tool_use, num_turns=60)"
        )


def _setup_router(orch: Orchestrator) -> None:
    orch.audit.write(
        "session_start", cwd=str(orch.cfg.cwd), backend=orch.cfg.backend,
        goal=orch.cfg.goal, has_project_context=False,
    )
    orch.router = AgentRouter(
        store=orch.findings_store,
        backend=orch.validator_backend,
        tools=orch.registry.catalog(),
        project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )


async def test_validation_is_non_blocking(tmp_path: Path, monkeypatch):
    """The pentest agent must NOT block on the validator: it gets an immediate
    'queued for validation' ack and reaches its own final while the validator
    is still running. This is the core fix for the gruyere freeze."""
    monkeypatch.chdir(tmp_path)
    gate = asyncio.Event()
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(title="slow", endpoint="/x/1", cwe="CWE-79", payload_shape="x"),
            _pentest_final(),
        ]
    )
    validator = GatedBackend(gate, _validator_final("confirmed"))
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    _setup_router(orch)

    agent_task = asyncio.create_task(orch._agent_main(None))
    capture: list = []
    try:
        # Drive to the pentest agent's final — reached even though the
        # validator is still gated (i.e. it did NOT block).
        await asyncio.wait_for(_drive_until_final(orch, capture=capture), timeout=10)

        # The agent was acked immediately, the verdict has NOT arrived yet.
        acks = [
            e for e in capture
            if isinstance(e, ToolObservation) and "queued for validation" in e.output
        ]
        assert len(acks) == 1, "pentest agent was not acked immediately"
        assert not [e for e in capture if isinstance(e, FindingEvent)], (
            "a verdict card arrived before the validator was released — it blocked"
        )
        assert orch._validations_pending == 1

        # Release the validator; now the verdict lands.
        gate.set()
        await asyncio.wait_for(orch.drain_validations(), timeout=10)
        while not orch.ui_queue.empty():
            capture.append(orch.ui_queue.get_nowait())

        cards = [e for e in capture if isinstance(e, FindingEvent)]
        assert len(cards) == 1 and cards[0].verdict == "confirmed"
        assert orch._validations_pending == 0
        assert orch._validations_confirmed == 1
    finally:
        gate.set()
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()


async def test_persistent_error_max_turns_parks_pending_and_never_hangs(
    tmp_path: Path, monkeypatch
):
    """If the validator backend keeps failing with `error_max_turns`, the run
    must degrade gracefully: the finding is parked pending with the error in
    its notes, the pending counter returns to 0, and nothing hangs."""
    monkeypatch.chdir(tmp_path)
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(title="xss", endpoint="/x/1", cwe="CWE-79", payload_shape="x"),
            _pentest_final(),
        ]
    )
    validator = AlwaysMaxTurnsBackend()
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    capture: list = []

    # The whole thing must finish well within the timeout (no freeze).
    await asyncio.wait_for(_run_with(orch, capture=capture), timeout=15)

    pending = list((tmp_path / ".pentest" / "findings" / "pending").glob("*.md"))
    assert len(pending) == 1
    assert "error_max_turns" in pending[0].read_text(encoding="utf-8")
    # Counter didn't leak — the UI won't show a forever-growing pending count.
    assert orch._validations_pending == 0
    cards = [e for e in capture if isinstance(e, FindingEvent)]
    assert cards and cards[0].verdict == "pending"


async def test_inflight_dedup_prevents_concurrent_revalidation(
    tmp_path: Path, monkeypatch
):
    """Resubmitting the same finding while the first is still being validated
    must NOT spawn a second validator — it's reported duplicate immediately.
    Guards the in-flight-hash path that collapses the resubmit storm."""
    monkeypatch.chdir(tmp_path)
    gate = asyncio.Event()
    pentest = ScriptedOrErrorBackend(
        [
            _submit_action(title="dup", endpoint="/x/1", cwe="CWE-639", payload_shape="p"),
            # same dedup key (numeric id normalized away), submitted while #1
            # is still in flight (validator gated)
            _submit_action(title="dup", endpoint="/x/9999", cwe="CWE-639", payload_shape="p"),
            _pentest_final(),
        ]
    )
    validator = GatedBackend(gate, _validator_final("confirmed"))
    orch = _make_orch(tmp_path, pentest, validator_backend=validator)
    _setup_router(orch)

    agent_task = asyncio.create_task(orch._agent_main(None))
    capture: list = []
    try:
        await asyncio.wait_for(_drive_until_final(orch, capture=capture), timeout=10)

        # Exactly one validation in flight; the 2nd submit deduped.
        assert len(orch._validation_tasks) == 1, "second submit spawned a 2nd validator"
        dups = [
            e for e in capture
            if isinstance(e, FindingEvent) and e.verdict == "duplicate"
        ]
        assert len(dups) == 1

        gate.set()
        await asyncio.wait_for(orch.drain_validations(), timeout=10)
        files = list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
        assert len(files) == 1
    finally:
        gate.set()
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.close()
