"""Regression: `await_subagents` must never hang when nothing is in flight.

Reproduces a real deadlock: on resume in a folder with a prior `.pentest/`, mode c
re-seeds the orchestrator's persisted memory, which may claim a subagent is
"running/dispatched" from a PROCESS THAT ALREADY DIED. The model then calls
`await_subagents` without dispatching, and `await_("any", timeout=None)` blocks
forever on the empty done-queue — permanent hang under --yolo (no operator to
interrupt it).

Fix A: `await_subagents` returns immediately when the manager is empty.
Fix B: the re-seeded memory is framed so the model re-dispatches instead of awaiting
       ghosts.
"""
from __future__ import annotations

import asyncio

from zero.llm.envelope import ActionEnvelope
from zero.orchestrator.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    _seed_memory_message,
)
from zero.orchestrator.subagents import SubagentManager, SubagentResult


def _orch(tmp_path) -> Orchestrator:
    cfg = OrchestratorConfig(cwd=tmp_path, goal="g", backend="echo", yolo=True)
    return Orchestrator(cfg)


def _empty_manager(orch: Orchestrator) -> SubagentManager:
    async def _never(rec):  # a runner that is never invoked in the empty case
        raise AssertionError("runner should not run")
    return SubagentManager(max_concurrent=4, max_total=10, runner=_never, emit=orch._emit)


# --------------------------------------------------------------------- Fix A

async def test_await_subagents_returns_immediately_when_nothing_running(tmp_path):
    orch = _orch(tmp_path)
    orch.subagents = _empty_manager(orch)
    env = ActionEnvelope(tool="await_subagents", args={}, thought="collecting the validator")

    # The bug was an infinite block here; wait_for turns a regression into a failure.
    obs = await asyncio.wait_for(orch._handle_dispatch(None, env), timeout=5)

    assert "no subagents" in obs.lower()
    assert orch.subagents.running_counts() == (0, 0)


async def test_await_subagents_still_awaits_a_real_dispatch(tmp_path):
    """The guard must NOT short-circuit when a subagent was actually dispatched —
    spawn() registers in `_running` synchronously, so a genuine dispatch→await path
    returns the real result, not the 'nothing to await' message."""
    orch = _orch(tmp_path)

    async def _runner(rec) -> SubagentResult:
        return SubagentResult(
            subagent_id=rec.id, kind=rec.kind, target=rec.target, idea=rec.idea,
            status="NO_FINDING", summary="ran to completion",
        )
    orch.subagents = SubagentManager(max_concurrent=4, max_total=10, runner=_runner, emit=orch._emit)
    orch.subagents.spawn("pentester", target="t", idea="probe")

    env = ActionEnvelope(tool="await_subagents", args={}, thought="collect")
    obs = await asyncio.wait_for(orch._handle_dispatch(None, env), timeout=5)

    assert "no subagents" not in obs.lower()
    assert "ran to completion" in obs


# --------------------------------------------------------------------- Fix B

def test_seed_memory_message_warns_about_stale_subagents():
    memory = "## CONFIRMED (validator running)\n- Validator antonietta_1 dispatched."
    msg = _seed_memory_message(memory)

    assert memory in msg                                   # original memory preserved
    assert "NO LONGER running" in msg                      # the anti-ghost warning
    assert "do NOT call await_subagents" in msg
    assert "re-dispatch" in msg.lower()
