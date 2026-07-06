"""Offline: --ralph-resume continues a prior run from .ralph/ without re-planning."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.router import AgentRouter
from zero.ralph import state
from zero.ralph.driver import RalphDriver
from zero.ralph.prd import PRD, PRDTask, load_prd, save_prd


class ScriptedBackend(LLMBackend):
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls = 0

    async def run(self, system_prompt, messages, tools):
        self.calls += 1
        if not self._script:
            raise AssertionError("scripted backend exhausted")
        entry = self._script.pop(0)
        return LLMResponse(text=entry)


def _action(tool: str, args: dict) -> str:
    return json.dumps({"type": "action", "tool": tool, "args": args, "thought": "t"})


def _final(payload: dict) -> str:
    return json.dumps({"type": "final", "answer": json.dumps(payload)})


async def test_ralph_resume_continues_without_replanning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Prior run left: a PRD (t1 done, t2 pending) and one snapshot (.pentest_1).
    save_prd(
        state.prd_path(tmp_path),
        PRD(goal="g", tasks=[
            PRDTask(id="t1", priority=1, title="a", status="completed"),
            PRDTask(id="t2", priority=2, title="b", status="pending"),
        ]),
    )
    (tmp_path / ".ralph_logs" / ".pentest_1").mkdir(parents=True)

    # Resume backend plays ONLY the loop (finalize + completion judge) — NO analyzer
    # discussion. If planning ran, the script would be exhausted / mis-ordered.
    backend = ScriptedBackend([
        _action("finalize", {"summary": "t2 exhausted, nothing exploitable"}),
        _final({"verdicts": [{"task_id": "t2", "completed": True, "reason": "covered"}]}),
    ])

    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="g", backend="echo",
        ralph=True, ralph_resume=True, ralph_iterations=1,
        ralph_approve_prd=True,  # must be SKIPPED on resume (no operator input here)
        ralph_discussion_rounds=1, yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = backend
    orch.router = AgentRouter(
        store=orch.findings_store, backend=ScriptedBackend([]),
        tools=orch.registry.catalog(), project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )

    await asyncio.wait_for(RalphDriver(orch, None).run(), timeout=30)
    orch.audit.close()

    # 1. No re-planning + no approval prompt: only the 2 loop calls were made.
    assert backend.calls == 2

    # 2. Prior task state honoured; the remaining task got completed.
    prd = load_prd(state.prd_path(tmp_path))
    assert prd.by_id("t1").status == "completed"
    assert prd.by_id("t2").status == "completed"

    # 3. Iteration numbering CONTINUED (snapshot .pentest_2), prior one untouched.
    assert (tmp_path / ".ralph_logs" / ".pentest_2").is_dir()
    assert (tmp_path / ".ralph_logs" / ".pentest_1").is_dir()

    # 4. Journal + audit record it as iteration 2.
    assert "Iteration 2" in state.progress_path(tmp_path).read_text(encoding="utf-8")
    iters = [r for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")
             if r["kind"] == "ralph_iteration_done"]
    assert [r["iteration"] for r in iters] == [2]
