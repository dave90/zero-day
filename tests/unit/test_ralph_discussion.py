"""Offline: the analyzer<->analyzer_validator PRD discussion (scripted backends).

Drives RalphDriver._build_prd directly with a scripted Orchestrator backend that
plays both the analyzer and the analyzer_validator in call order. No real `claude`.
"""
from __future__ import annotations

import json
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ralph.driver import RalphDriver


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


def _prd(tasks: list[dict]) -> str:
    return _final({"goal": "g", "tasks": tasks})


def _make_orch(tmp_path, backend, **cfg_kw) -> Orchestrator:
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="find a web bug", backend="echo", ralph=True, yolo=True, **cfg_kw
    )
    orch = Orchestrator(cfg)
    orch.backend = backend
    return orch


async def test_discussion_converges_on_approval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = ScriptedBackend(
        [
            _prd([{"id": "t1", "priority": 1, "title": "XSS"}]),               # analyzer v1
            _final({"approved": False, "feedback": "add SQLi", "missing": ["SQLi"]}),  # review r1
            _prd([{"id": "t1", "priority": 1, "title": "XSS"},
                  {"id": "t2", "priority": 1, "title": "SQLi"}]),              # analyzer v2
            _final({"approved": True, "feedback": "", "missing": []}),          # review r2
        ]
    )
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=3)
    prd = await RalphDriver(orch, None)._build_prd("find a web bug", [])
    assert [t.id for t in prd.tasks] == ["t1", "t2"]   # revised PRD adopted
    assert backend.calls == 4                          # bounded, stopped on approval


async def test_discussion_proceeds_when_not_converged(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    backend = ScriptedBackend(
        [
            _prd([{"id": "t1", "priority": 1, "title": "XSS"}]),               # analyzer v1
            _final({"approved": False, "feedback": "never happy"}),            # review r1 (last)
        ]
    )
    orch = _make_orch(tmp_path, backend, ralph_discussion_rounds=1)
    prd = await RalphDriver(orch, None)._build_prd("find a web bug", [])
    # Did not converge, but we proceed with the latest PRD rather than looping forever.
    assert [t.id for t in prd.tasks] == ["t1"]
    assert backend.calls == 2
