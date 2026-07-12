"""Ralph per-task evidence dossier — the fix for the P1-7 livelock.

Regression for a real production run where Ralph re-picked ONE task for 32
iterations: each fresh-session orchestrator inherited only a bare *claim* in
progress.txt (never the evidence), so the completion judge — handed an unverifiable
summary — rejected every time and the task never advanced.

The fix persists the orchestrator's actual work into a durable per-task dossier
(under `.ralph/`, never reset) that BOTH the validator (to verify) and the next
iteration (to build on) read. These offline tests, driven by scripted backends,
prove: (1) evidence persists across the reset and both directions carry it,
(2) a genuinely-missing-then-supplied task completes on the next pass, and
(3) a task whose evidence never satisfies the judge is NEVER force-completed.
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
from zero.ralph.dossier import append_evidence, read_dossier
from zero.ralph.driver import RalphDriver
from zero.ralph.prd import PRD, PRDTask, load_prd


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


def _action(tool: str, args: dict) -> str:
    return json.dumps({"type": "action", "tool": tool, "args": args, "thought": "t"})


def _final(payload: dict) -> str:
    return json.dumps({"type": "final", "answer": json.dumps(payload)})


def _make_orch(tmp_path: Path, orch_backend: LLMBackend, *, iterations: int) -> Orchestrator:
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="audit the vote path", backend="echo",
        ralph=True, ralph_iterations=iterations, ralph_approve_prd=False,
        ralph_discussion_rounds=1, ralph_epics=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    orch.backend = orch_backend
    orch.router = AgentRouter(
        store=orch.findings_store, backend=ScriptedBackend([]),
        tools=orch.registry.catalog(), project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )
    return orch


# --------------------------------------------------------------------------- (1)+(2)

async def test_dossier_persists_and_task_completes_next_pass(tmp_path: Path, monkeypatch):
    """Iter 1: orchestrator finalizes a thin claim → judge REJECTS with a specific
    'what's missing' note. Iter 2: orchestrator supplies it → judge ACCEPTS. The
    dossier accumulates BOTH iterations (survives the reset), and the task completes
    on the second pass instead of looping."""
    monkeypatch.chdir(tmp_path)
    orch_backend = ScriptedBackend([
        # phase 1 — PRD (one task), approved
        _final({"goal": "audit the vote path", "tasks": [
            {"id": "P1-7", "priority": 1, "title": "double-vote / replay state machine",
             "acceptanceCriteria": "each transition's idempotency guard named + replay adjudicated"}]}),
        _final({"approved": True}),
        # iter 1 — thin finalize, then judge rejects with a concrete gap
        _action("finalize", {"summary": "task P1-7: state machine looks fine"}),
        _final({"verdicts": [{"task_id": "P1-7", "completed": False,
                              "reason": "MISSING: trace confirm->partiallyDecrypted and name the "
                                        "idempotency guard + optimistic-lock version column"}]}),
        # iter 2 — finalize supplies the missing evidence, judge accepts
        _action("finalize", {"summary": "task P1-7: confirm->partiallyDecrypted guarded by "
                                        "isNotPartiallyDecrypted; @Version optimistic lock blocks "
                                        "concurrent double-submit; replayed confirmation rejected"}),
        _final({"verdicts": [{"task_id": "P1-7", "completed": True,
                              "reason": "verified the guard + version column in code"}]}),
    ])
    orch = _make_orch(tmp_path, orch_backend, iterations=2)

    await asyncio.wait_for(RalphDriver(orch, None).run(), timeout=30)
    orch.audit.close()

    # Task completed on pass 2 (not force-completed — the judge accepted supplied evidence).
    prd = load_prd(state.prd_path(tmp_path))
    assert prd.by_id("P1-7").status == "completed"
    assert prd.by_id("P1-7").passes == 2

    # The dossier lives under .ralph/ (never reset) and accumulated BOTH iterations.
    dossier = read_dossier(state.dossier_path(tmp_path, "P1-7"))
    assert "Iteration 1" in dossier and "Iteration 2" in dossier
    assert "state machine looks fine" in dossier          # iter-1 evidence survived the reset
    assert "isNotPartiallyDecrypted" in dossier            # iter-2 evidence appended

    # Exactly two iterations ran (no spinning).
    iters = [r for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")
             if r["kind"] == "ralph_iteration_done"]
    assert len(iters) == 2


# --------------------------------------------------------------------------- (3)

async def test_never_force_completes_a_task_that_stays_unverified(tmp_path: Path, monkeypatch):
    """If the judge is never satisfied, the task must NOT complete just because it has
    been worked repeatedly — it stays pending with an honest note, and the loop is
    bounded by ralph_iterations (no infinite re-pick)."""
    monkeypatch.chdir(tmp_path)
    orch_backend = ScriptedBackend([
        _final({"goal": "g", "tasks": [{"id": "P1-7", "priority": 1, "title": "state machine"}]}),
        _final({"approved": True}),
        # iter 1 — restate, judge rejects
        _action("finalize", {"summary": "P1-7 basically done"}),
        _final({"verdicts": [{"task_id": "P1-7", "completed": False, "reason": "still no evidence"}]}),
        # iter 2 — restate again, judge rejects again
        _action("finalize", {"summary": "P1-7 basically done, again"}),
        _final({"verdicts": [{"task_id": "P1-7", "completed": False, "reason": "still no evidence"}]}),
        # NOTE: no iteration-3 script — if the loop over-ran, the backend would raise.
    ])
    orch = _make_orch(tmp_path, orch_backend, iterations=2)

    await asyncio.wait_for(RalphDriver(orch, None).run(), timeout=30)
    orch.audit.close()

    prd = load_prd(state.prd_path(tmp_path))
    assert prd.by_id("P1-7").status == "pending"           # never force-completed
    assert prd.by_id("P1-7").passes == 2
    assert not prd.all_done()
    iters = [r for r in read_audit(tmp_path / ".pentest" / "audit.jsonl")
             if r["kind"] == "ralph_iteration_done"]
    assert len(iters) == 2                                  # bounded — did not run forever


# --------------------------------------------------------------------------- atomic tasks

def test_planning_briefs_steer_toward_atomic_tasks(tmp_path: Path):
    """Ralph's design is one-thing-per-iteration, so the analyzer brief must ask for
    atomic per-surface tasks and the critique brief must reject coarse buckets."""
    orch = _make_orch(tmp_path, ScriptedBackend([]), iterations=2)
    driver = RalphDriver(orch, None)

    analyzer = driver._analyzer_brief("break the app", [])
    assert "ATOMIC" in analyzer.upper()
    assert "one task per iteration" in analyzer.lower()

    prd = PRD(goal="g", tasks=[PRDTask(id="t1", title="Web-application testing of the site")])
    critique = driver._critique_brief("g", prd)
    assert "ATOMIC" in critique.upper()
    assert "reject" in critique.lower() and "bucket" in critique.lower()


# --------------------------------------------------------------------------- info-passing

async def test_resume_directive_carries_dossier_and_note_to_orchestrator(tmp_path: Path):
    """The seed for an iteration whose top task already has a dossier surfaces the
    validator's note as a directive AND embeds the accumulated evidence — so the fresh
    session builds on it instead of starting blank."""
    orch = _make_orch(tmp_path, ScriptedBackend([]), iterations=2)
    driver = RalphDriver(orch, None)
    state.ensure_dirs(tmp_path)
    append_evidence(state.dossier_path(tmp_path, "P1-7"), iteration=1,
                    summary="iter-1: examined VoteService.confirm()", memory_notes="grep isNotSentVote -> 2 hits")
    prd = PRD(goal="g", tasks=[PRDTask(
        id="P1-7", priority=1, title="state machine", status="pending",
        note="MISSING: name the idempotency guard on the confirm transition")])

    seed = driver._iteration_seed(2, prd, progress_text="")

    assert "RESUME: task P1-7" in seed
    assert "name the idempotency guard on the confirm transition" in seed   # the note as directive
    assert "examined VoteService.confirm()" in seed                          # the accumulated evidence
    # And the judge-side context carries the same dossier.
    assert "examined VoteService.confirm()" in driver._dossiers_context(prd)


async def test_orchestrator_memory_notes_captured_into_dossier(tmp_path: Path):
    """The orchestrator's about-to-be-wiped memory scratchpad is captured verbatim into
    the durable dossier (the richest evidence trail it already writes each pass)."""
    orch = _make_orch(tmp_path, ScriptedBackend([]), iterations=1)
    driver = RalphDriver(orch, None)
    mem = state.pentest_dir(tmp_path) / "memory" / "orchestrator.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text("read AuthService:53-75; PSS params pinned; replay blocked by @Version", encoding="utf-8")

    notes = driver._orchestrator_memory_notes()
    assert "PSS params pinned" in notes

    append_evidence(state.dossier_path(tmp_path, "P1-5"), iteration=1, summary="s", memory_notes=notes)
    assert "PSS params pinned" in read_dossier(state.dossier_path(tmp_path, "P1-5"))
