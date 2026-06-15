"""Tests for AgentLoop using the EchoBackend + a custom-script backend."""
from __future__ import annotations

import json
from typing import Any

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, LLMResponse, Message
from zero.llm.echo import EchoBackend
from zero.llm.envelope import (
    ActionEnvelope,
    AskUserEnvelope,
    FinalEnvelope,
)


class ScriptedBackend(LLMBackend):
    """Returns a list of raw responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def run(self, system_prompt, messages, tools):
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("script exhausted")
        return LLMResponse(text=self._responses.pop(0))


async def test_periodic_reminder_injected_every_n_turns():
    """The Claude-Code-style <system-reminder> is injected before the model call
    every `reminder_every` turns (and never on the first turn)."""
    act = json.dumps({"type": "action", "tool": "x", "args": {}})
    backend = ScriptedBackend([act, act, act, act])
    loop = AgentLoop(
        backend, system_prompt="sys", tools=[],
        reminder_text="<system-reminder> checkpoint </system-reminder>",
        reminder_every=2,
    )
    for _ in range(4):
        await loop.step()

    def count(msgs):
        return sum("checkpoint" in m.content for m in msgs)

    # backend.calls[i] = messages seen on turn i+1; reminders persist in history, so
    # the COUNT bumps on the turns where a new one is injected (2 and 4), not 1 or 3.
    assert count(backend.calls[0]) == 0   # turn 1 — never
    assert count(backend.calls[1]) == 1   # turn 2 — injected
    assert count(backend.calls[2]) == 1   # turn 3 — no new one
    assert count(backend.calls[3]) == 2   # turn 4 — second injected


async def test_no_reminder_when_disabled():
    act = json.dumps({"type": "action", "tool": "x", "args": {}})
    backend = ScriptedBackend([act, act, act])
    loop = AgentLoop(backend, system_prompt="sys", tools=[])  # reminder_every=0 default
    for _ in range(3):
        await loop.step()
    assert all(not any("system-reminder" in m.content for m in msgs) for msgs in backend.calls)


async def test_echo_first_step_is_web_search_action():
    loop = AgentLoop(EchoBackend(), system_prompt="sys", tools=[])
    step = await loop.step()
    assert isinstance(step.envelope, ActionEnvelope)
    assert step.envelope.tool == "web_search"


async def test_loop_walks_through_echo_script_until_final():
    loop = AgentLoop(EchoBackend(), system_prompt="sys", tools=[])
    seen_types: list[str] = []
    while True:
        step = await loop.step()
        seen_types.append(type(step.envelope).__name__)
        if isinstance(step.envelope, FinalEnvelope):
            break
        # Pretend each step's effect happened.
        if isinstance(step.envelope, ActionEnvelope):
            loop.add_observation(step.envelope.tool, True, "ok")
        elif isinstance(step.envelope, AskUserEnvelope):
            loop.add_ask_user_answer("auth")
    assert "ActionEnvelope" in seen_types
    assert "AskUserEnvelope" in seen_types
    assert seen_types[-1] == "FinalEnvelope"


async def test_turn_cap_returns_final():
    loop = AgentLoop(EchoBackend(), system_prompt="sys", tools=[], max_turns=0)
    step = await loop.step()
    assert isinstance(step.envelope, FinalEnvelope)
    assert "turn cap" in step.envelope.answer


async def test_turn_cap_triggers_wrapup_handoff():
    """With a wrapup_prompt, the turn cap spends ONE last call to hand off a rich
    summary instead of the bare 'turn cap reached' (the Blueprint_Heist fix: the
    pentester's recovered secret must reach the orchestrator)."""
    handoff = json.dumps({"type": "final", "answer": "secret=IM_Sup3r_K3y; forged admin JWT eyJ..."})
    backend = ScriptedBackend([handoff])
    loop = AgentLoop(backend, system_prompt="sys", tools=[], max_turns=0,
                     wrapup_prompt="WRAP IT UP")
    step = await loop.step()
    assert isinstance(step.envelope, FinalEnvelope)
    assert "IM_Sup3r_K3y" in step.envelope.answer        # the intel survived
    assert "turn cap reached" not in step.envelope.answer
    # The wrap-up prompt was injected before the final call.
    assert any(m.role == "user" and m.content == "WRAP IT UP" for m in loop.history)
    assert len(backend.calls) == 1


async def test_wrapup_only_happens_once():
    """If the handoff call somehow doesn't end the run, a second step falls back to
    the bare cap final rather than looping on the wrap-up call forever."""
    handoff = json.dumps({"type": "final", "answer": "done"})
    backend = ScriptedBackend([handoff])  # only ONE response available
    loop = AgentLoop(backend, system_prompt="sys", tools=[], max_turns=0,
                     wrapup_prompt="WRAP IT UP")
    await loop.step()                       # consumes the one handoff response
    step2 = await loop.step()               # must NOT call the backend again
    assert isinstance(step2.envelope, FinalEnvelope)
    assert "turn cap reached" in step2.envelope.answer
    assert len(backend.calls) == 1


async def test_wrapup_coerces_action_to_final():
    """If the agent tries to ACT in its handoff turn (it's out of budget), keep its
    text as the final instead of running another tool."""
    action = json.dumps({"type": "action", "tool": "web_search",
                         "args": {"query": "x"}, "thought": "I still found secret=ABC"})
    backend = ScriptedBackend([action])
    loop = AgentLoop(backend, system_prompt="sys", tools=[], max_turns=0,
                     wrapup_prompt="WRAP IT UP")
    step = await loop.step()
    assert isinstance(step.envelope, FinalEnvelope)
    assert "secret=ABC" in step.envelope.answer


async def test_retry_on_malformed_envelope():
    good = json.dumps({"type": "final", "answer": "ok"})
    backend = ScriptedBackend(["this is not JSON", good])
    loop = AgentLoop(backend, system_prompt="sys", tools=[])
    step = await loop.step()
    assert isinstance(step.envelope, FinalEnvelope)
    # Two backend calls were made (initial + retry).
    assert len(backend.calls) == 2


async def test_history_grows_per_turn():
    loop = AgentLoop(EchoBackend(), system_prompt="sys", tools=[])
    loop.add_user_message("hello")
    assert any(m.role == "user" for m in loop.history)
    await loop.step()
    assert any(m.role == "assistant" for m in loop.history)
