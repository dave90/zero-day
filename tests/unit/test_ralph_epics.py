"""Offline: the two-phase (epics → tasks) planning flow (scripted backends).

Drives RalphDriver._build_epics / _build_prd directly with a scripted Orchestrator
backend that plays the analyzer and analyzer_validator in call order. No real `claude`.
Proves: (1) the epic discussion converges/ proceeds like the PRD discussion,
(2) the task phase tags every task with a valid epic_id and persists the epics,
(3) the validator (NOT a code gate) can reject a draft that leaves an epic unserved
and accept the revised one, and (4) --no-ralph-epics is a zero-cost no-op.
"""
from __future__ import annotations

import json
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ralph.driver import RalphDriver
from zero.ralph.prd import Epic


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


def _final(payload: dict) -> str:
    return json.dumps({"type": "final", "answer": json.dumps(payload)})


def _epics(epics: list[dict]) -> str:
    return _final({"goal": "g", "epics": epics})


def _tasks(tasks: list[dict]) -> str:
    return _final({"goal": "g", "tasks": tasks})


def _make_orch(tmp_path, backend, **cfg_kw) -> Orchestrator:
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="break the app", backend="echo", ralph=True, yolo=True, **cfg_kw
    )
    orch = Orchestrator(cfg)
    orch.backend = backend
    return orch


# ------------------------------------------------------------------ epic phase

async def test_epic_discussion_converges_on_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = ScriptedBackend([
        _epics([{"id": "e1", "title": "Uploads"}]),                         # analyzer v1
        _final({"approved": False, "feedback": "no tally area", "missing": ["tally"]}),  # review r1
        _epics([{"id": "e1", "title": "Uploads"}, {"id": "e2", "title": "Tally"}]),  # analyzer v2
        _final({"approved": True, "feedback": "", "missing": []}),           # review r2
    ])
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=3)
    epics = await RalphDriver(orch, None)._build_epics("break the app")
    assert [e.id for e in epics] == ["e1", "e2"]     # revised epics adopted
    assert backend.calls == 4                        # bounded, stopped on approval


async def test_epic_phase_proceeds_when_not_converged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = ScriptedBackend([
        _epics([{"id": "e1", "title": "Uploads"}]),                # analyzer v1
        _final({"approved": False, "feedback": "never happy"}),    # review r1 (last)
    ])
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=1)
    epics = await RalphDriver(orch, None)._build_epics("break the app")
    assert [e.id for e in epics] == ["e1"]           # proceed with latest, no infinite loop
    assert backend.calls == 2


async def test_disabled_toggle_returns_no_epics_without_backend_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = ScriptedBackend([])                    # would raise if the phase ran
    orch = _make_orch(tmp_path, backend, ralph_epics=False, ralph_discussion_rounds=3)
    epics = await RalphDriver(orch, None)._build_epics("break the app")
    assert epics == []
    assert backend.calls == 0                        # zero-cost no-op


# ------------------------------------------------------------------ task phase

async def test_task_phase_tags_epic_id_and_persists_epics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    epics = [Epic(id="e1", title="Uploads"), Epic(id="e2", title="Tally")]
    backend = ScriptedBackend([
        _tasks([{"id": "t1", "epic_id": "e1", "title": "a"},
                {"id": "t2", "epic_id": "e2", "title": "b"}]),   # analyzer tasks
        _final({"approved": True}),                              # review approves
    ])
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=1)
    prd = await RalphDriver(orch, None)._build_prd("break the app", epics)
    assert backend.calls == 2
    assert [e.id for e in prd.epics] == ["e1", "e2"]            # epics persisted onto the PRD
    epic_ids = {e.id for e in prd.epics}
    assert all(t.epic_id in epic_ids for t in prd.tasks)       # traceability
    assert prd.epic_progress("e1") == (0, 1) and prd.epic_progress("e2") == (0, 1)


async def test_validator_rejects_unserved_epic_then_accepts(tmp_path, monkeypatch):
    """Validator-only coverage: a task draft that leaves an epic unserved is rejected
    by the VALIDATOR (not a code gate); the revised draft that covers it is accepted."""
    monkeypatch.chdir(tmp_path)
    epics = [Epic(id="e1", title="Uploads"), Epic(id="e2", title="Tally")]
    backend = ScriptedBackend([
        _tasks([{"id": "t1", "epic_id": "e1", "title": "a"}]),          # v1: e2 unserved
        _final({"approved": False, "feedback": "epic e2 has no task", "missing": ["e2"]}),
        _tasks([{"id": "t1", "epic_id": "e1", "title": "a"},
                {"id": "t2", "epic_id": "e2", "title": "b"}]),          # v2: both covered
        _final({"approved": True}),
    ])
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=3)
    prd = await RalphDriver(orch, None)._build_prd("break the app", epics)
    assert backend.calls == 4                                   # rejected v1, accepted v2
    assert prd.epic_progress("e2")[1] == 1                      # e2 now has a task
    assert {t.epic_id for t in prd.tasks} == {"e1", "e2"}
