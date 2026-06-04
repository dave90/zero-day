import json
from pathlib import Path

from zero.llm.replay import ReplayBackend
from zero.orchestrator.audit import AuditLog


async def test_replay_returns_recorded_envelopes(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    with AuditLog(p) as log:
        log.write("agent_step", turn=1, raw=json.dumps({"type": "action", "tool": "notes", "args": {}}))
        log.write("agent_step", turn=2, raw=json.dumps({"type": "final", "answer": "done"}))
    be = ReplayBackend(p)
    r1 = await be.run("sys", [], [])
    r2 = await be.run("sys", [], [])
    r3 = await be.run("sys", [], [])  # exhausted
    assert "notes" in r1.text
    assert "final" in r2.text
    assert "exhausted" in r3.text


async def test_replay_ignores_non_step_rows(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    with AuditLog(p) as log:
        log.write("session_start")
        log.write("agent_step", turn=1, raw='{"type":"final","answer":"x"}')
        log.write("tool_observation")
    be = ReplayBackend(p)
    r1 = await be.run("sys", [], [])
    assert "final" in r1.text
