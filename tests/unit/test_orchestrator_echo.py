"""End-to-end: drive the Orchestrator agent loop (no UI) with the EchoBackend.

The UI runs in its own asyncio task in production; here we replace it with a
lightweight stand-in that drains UIEvents and responds to ApprovalPrompt /
AskUser events automatically.

After the agent's interactive refactor it no longer exits on a `final` — it
loops back to wait for the next user message — so the fake UI must
actively shut down the session (mimicking the user typing `/exit`) once
it observes the final.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.events import AgentTextDelta, ApprovalPrompt, AskUser, SessionEnd


async def _drive_until_final(orch: Orchestrator) -> None:
    """Auto-approve everything and exit as soon as the agent emits a final."""
    while True:
        ev = await orch.ui_queue.get()
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "auth")
        elif isinstance(ev, AgentTextDelta) and "[final]" in ev.text:
            return
        elif isinstance(ev, SessionEnd):
            return


async def test_full_echo_run_writes_expected_audit(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, goal="hello", backend="echo")
    orch = Orchestrator(cfg)

    orch.audit.write(
        "session_start",
        cwd=str(cfg.cwd),
        backend=cfg.backend,
        goal=cfg.goal,
        has_project_context=False,
    )
    agent_task = asyncio.create_task(orch._agent_main(None))
    ui_task = asyncio.create_task(_drive_until_final(orch))
    try:
        await asyncio.wait_for(ui_task, timeout=20)
    finally:
        # Mimic /exit: cancel the agent's idle-wait so the session ends.
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.write("session_end")
        orch.audit.close()

    rows = read_audit(tmp_path / ".pentest" / "audit.jsonl")
    kinds = [r["kind"] for r in rows]
    assert kinds[0] == "session_start"
    assert "user_message" in kinds
    assert "tool_proposal" in kinds
    assert "tool_observation" in kinds
    assert "ask_user" in kinds
    assert "ask_user_answered" in kinds
    assert "final" in kinds
    assert kinds[-1] == "session_end"


async def test_agent_loops_back_to_idle_after_final(tmp_path: Path, monkeypatch):
    """After a final, the agent must NOT exit — it should wait for the
    next user message. This is the Claude-Code-style interactive
    behavior. Regression: previously `_agent_main` `break`d on final
    and the orchestrator emitted SessionEnd unconditionally."""
    import json

    from zero.llm.backend import LLMBackend, LLMResponse

    class TwoFinalsBackend(LLMBackend):
        """Reply with a final twice — once per user message."""

        def __init__(self) -> None:
            self.calls = 0

        async def run(self, sys_p, msgs, tools):
            self.calls += 1
            return LLMResponse(
                text=json.dumps({"type": "final", "answer": f"reply {self.calls}"})
            )

    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, goal="first goal", backend="echo")
    orch = Orchestrator(cfg)
    backend = TwoFinalsBackend()
    orch.backend = backend

    finals_seen: list[str] = []

    async def drive_two_exchanges(orch: Orchestrator) -> None:
        async for ev_text in _stream_final_texts(orch, target_count=2):
            finals_seen.append(ev_text)
            if len(finals_seen) == 1:
                # The agent must now be back in idle. Type a follow-up;
                # if the agent had exited, this would be ignored and the
                # second final never arrives.
                orch.submit_user_text("follow-up question")

    orch.audit.write("session_start", goal=cfg.goal)
    agent_task = asyncio.create_task(orch._agent_main(None))
    ui_task = asyncio.create_task(drive_two_exchanges(orch))
    try:
        await asyncio.wait_for(ui_task, timeout=20)
    finally:
        agent_task.cancel()
        try:
            await asyncio.wait_for(agent_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        orch.audit.write("session_end")
        orch.audit.close()

    assert len(finals_seen) == 2, f"only saw {finals_seen!r}"
    assert backend.calls == 2

    rows = read_audit(tmp_path / ".pentest" / "audit.jsonl")
    kinds = [r["kind"] for r in rows]
    assert kinds.count("user_message") == 2  # --goal + the follow-up
    assert kinds.count("final") == 2


async def _stream_final_texts(orch: Orchestrator, *, target_count: int):
    """Yield the answer text of each `[final]` event until target_count reached."""
    seen = 0
    while seen < target_count:
        ev = await orch.ui_queue.get()
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "x")
        elif isinstance(ev, AgentTextDelta) and "[final]" in ev.text:
            seen += 1
            yield ev.text
        elif isinstance(ev, SessionEnd):
            return
