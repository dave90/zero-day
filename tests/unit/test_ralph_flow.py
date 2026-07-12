"""Offline end-to-end Ralph loop with scripted backends (no real `claude`).

One iteration: analyzer drafts a PRD, the validator approves it, the per-iteration
orchestrator dispatches a pentester + validator and finalizes a confirmed finding,
the analyzer_validator marks the task complete, and the driver snapshots+resets
`.pentest/` and writes the cumulative report. Asserts the durable Ralph artifacts.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.router import AgentRouter
from zero.ralph import state
from zero.ralph.driver import RalphDriver
from zero.ralph.prd import load_prd


class ScriptedBackend(LLMBackend):
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


def _action(tool: str, args: dict) -> str:
    return json.dumps({"type": "action", "tool": tool, "args": args, "thought": "t"})


def _final(payload: dict) -> str:
    return json.dumps({"type": "final", "answer": json.dumps(payload)})


def _pentester_final() -> str:
    return _final({
        "status": "EXPLOITED",
        "summary": "uid reflects <script> unescaped; XSS fires.",
        "candidate_findings": [_FINDING],
        "discovered": [],
    })


def _validator_final(verdict: str) -> str:
    return _final({
        "verdict": verdict,
        "rationale": "reproduced the reflection with a benign marker",
        "reproducer": "GET /snippets?uid=zz<script>alert(1)</script>",
        "cvss40_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:P/VC:L/VI:L/VA:N/SC:N/SI:N/SA:N",
        "cvss40_score": 5.1,
        "cwe": "CWE-79",
    })


async def test_ralph_loop_end_to_end(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch_backend = ScriptedBackend(
        [
            # phase 1 — analyzer PRD, then validator approves it
            _final({"goal": "find a web bug", "tasks": [
                {"id": "t1", "priority": 1, "title": "XSS in /snippets",
                 "acceptanceCriteria": "uid param probed for reflection"}]}),
            _final({"approved": True, "feedback": "", "missing": []}),
            # phase 2 — iteration 1 orchestrated pass
            _action("dispatch_pentester", {"attack_idea": "XSS in uid", "target": "/snippets"}),
            _action("await_subagents", {"which": "all"}),
            _action("dispatch_validator", {**_FINDING, "source_subagent": "#1"}),
            _action("await_subagents", {"which": "all"}),
            _action("finalize", {"summary": "task t1: confirmed reflected XSS in /snippets"}),
            # completion judge
            _final({"verdicts": [{"task_id": "t1", "completed": True, "reason": "confirmed XSS"}]}),
        ]
    )
    validator_backend = ScriptedBackend([_validator_final("confirmed")])

    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="find a web bug", backend="echo",
        ralph=True, ralph_iterations=1, ralph_approve_prd=False,
        ralph_discussion_rounds=1, ralph_epics=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = orch_backend
    orch.validator_backend = validator_backend
    orch._subagent_backend_factory = lambda rec: ScriptedBackend([_pentester_final()])
    orch.router = AgentRouter(
        store=orch.findings_store,
        backend=validator_backend,
        tools=orch.registry.catalog(),
        project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )

    await asyncio.wait_for(RalphDriver(orch, None).run(), timeout=30)
    orch.audit.close()

    # 1. PRD task marked completed by the analyzer_validator.
    prd = load_prd(state.prd_path(tmp_path))
    assert prd is not None
    assert prd.by_id("t1").status == "completed"
    assert prd.by_id("t1").passes == 1

    # 2. Confirmed finding persisted and CARRIED FORWARD in the live .pentest/.
    confirmed = list((tmp_path / ".pentest" / "findings" / "confirmed").glob("*.md"))
    assert len(confirmed) == 1

    # 3. Progress journal written, naming the iteration + the confirmed finding.
    progress = state.progress_path(tmp_path).read_text(encoding="utf-8")
    assert "Iteration 1" in progress
    assert "Reflected XSS" in progress

    # 4. The .pentest/ snapshot for iteration 1 exists with the finding.
    snap = tmp_path / ".ralph_logs" / ".pentest_1"
    assert snap.is_dir()
    assert list((snap / "findings" / "confirmed").glob("*.md"))
    # memory was reset fresh after the snapshot.
    assert list((tmp_path / ".pentest" / "memory").iterdir()) == []

    # 5. Cumulative final report written.
    report = state.final_report_path(tmp_path).read_text(encoding="utf-8")
    assert "Reflected XSS" in report

    # 6. Audit trail records the loop completing.
    kinds = [r["kind"] for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")]
    assert "ralph_iteration_done" in kinds
    assert "ralph_done" in kinds


async def test_ralph_stops_early_when_all_tasks_complete(tmp_path: Path, monkeypatch):
    """With a single task completed in iteration 1, iteration 2 must not run even
    though ralph_iterations=3 (early-stop when the backlog is exhausted)."""
    monkeypatch.chdir(tmp_path)
    orch_backend = ScriptedBackend(
        [
            _final({"goal": "g", "tasks": [{"id": "t1", "priority": 1, "title": "probe"}]}),
            _final({"approved": True}),
            # iteration 1: orchestrator finalizes immediately (no candidate ⇒ no validator needed)
            _action("finalize", {"summary": "t1 exhausted, nothing exploitable"}),
            _final({"verdicts": [{"task_id": "t1", "completed": True, "reason": "exhausted"}]}),
            # NOTE: no script for an iteration 2 — if the loop ran again it would raise.
        ]
    )
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="g", backend="echo",
        ralph=True, ralph_iterations=3, ralph_approve_prd=False,
        ralph_discussion_rounds=1, ralph_epics=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = orch_backend
    orch.router = AgentRouter(
        store=orch.findings_store, backend=ScriptedBackend([]),
        tools=orch.registry.catalog(), project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )

    await asyncio.wait_for(RalphDriver(orch, None).run(), timeout=30)
    orch.audit.close()

    prd = load_prd(state.prd_path(tmp_path))
    assert prd.all_done()
    # Only iteration 1 ran (the early-stop kept us from consuming a 2nd pass).
    iters = [r for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")
             if r["kind"] == "ralph_iteration_done"]
    assert len(iters) == 1
    assert orch_backend.calls == 4  # analyzer, validator, finalize, judge — nothing more
