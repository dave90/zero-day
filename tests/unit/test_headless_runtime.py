"""Frontend-neutral lifecycle regressions without a model, network, or terminal renderer."""

import asyncio
import builtins

import pytest

from zero.llm.echo import EchoBackend
from zero.orchestrator.approval import ApprovalRequest
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.ownership import WorkspaceOwnership
from zero.tools.base import RiskLevel
from zero.ui.events import (
    ApprovalPrompt,
    InteractionResolved,
    RuntimeFailure,
    RuntimeState,
    SessionEnd,
)


@pytest.mark.parametrize("mode", ["a", "c"])
async def test_headless_is_independent_of_terminal_and_cleans_up(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(
        OrchestratorConfig(
            cwd=tmp_path,
            backend="echo",
            execution_mode=mode,
            use_container=False,
            goal="offline goal",
        )
    )
    orch.backend = EchoBackend([{"type": "final", "answer": "offline answer"}])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert name != "zero.ui.app", "Headless execution imported the terminal renderer"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    events = []

    async def frontend(runtime):
        async with asyncio.timeout(3):
            while True:
                event = await runtime.ui_queue.get()
                events.append(event)
                if isinstance(event, RuntimeState) and event.lifecycle == "idle":
                    return

    await orch.run_headless(frontend)
    assert any(isinstance(e, RuntimeState) and e.execution_location == "host" for e in events)
    with WorkspaceOwnership(tmp_path):
        pass


async def test_runtime_task_failure_surfaces_and_releases_ownership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    orch = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False))
    seen = []

    async def fail(_):
        raise RuntimeError("synthetic runtime failure")

    orch._orchestrator_main = fail

    async def frontend(runtime):
        while True:
            event = await asyncio.wait_for(runtime.ui_queue.get(), 3)
            seen.append(event)
            if isinstance(event, SessionEnd):
                return

    with pytest.raises(RuntimeError, match="synthetic runtime failure"):
        await orch.run_headless(frontend)
    assert any(isinstance(e, RuntimeFailure) for e in seen)
    with WorkspaceOwnership(tmp_path):
        with pytest.raises(RuntimeError, match="already in use"):
            with WorkspaceOwnership(tmp_path):
                pass


async def test_cancelled_operator_wait_expires_with_source_metadata(tmp_path):
    orch = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False))
    task = asyncio.create_task(
        orch._prompt_approval(
            ApprovalRequest(
                tool="python_exec",
                args={"code": "pass"},
                risk_level=RiskLevel.EXPLOIT,
                call_id="request",
                host_exec=True,
                actor_id="actual-actor",
            )
        )
    )
    event = await orch.ui_queue.get()
    assert isinstance(event, ApprovalPrompt)
    assert event.actor_id == "actual-actor"
    assert event.host_exec and event.execution_location == "host"
    assert not event.session_grant_allowed
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert isinstance(await orch.ui_queue.get(), InteractionResolved)
    assert not orch._approval_replies
    orch.audit.close()
    orch.thinking.close()


async def test_late_child_event_keeps_original_goal_provenance(tmp_path):
    from zero.ui.events import AgentTextDelta

    orch = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False))
    orch._run_context.set("first-goal")
    release = asyncio.Event()

    async def child():
        await release.wait()
        await orch._emit(AgentTextDelta(agent="validator", text="Late independent result"))

    task = asyncio.create_task(child())
    orch.submit_user_text("Follow up", run_id="second-goal")
    orch.adopt_user_run(await orch.user_queue.get())
    await orch._emit(AgentTextDelta(text="New goal response"))
    release.set()
    await task
    current = await orch.ui_queue.get()
    late = await orch.ui_queue.get()
    assert current._run_id == "second-goal"
    assert late._run_id == "first-goal"
    orch.audit.close()
    orch.thinking.close()
