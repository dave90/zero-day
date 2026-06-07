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
