"""Tests for AgentLoop using the EchoBackend + a custom-script backend."""
from __future__ import annotations

import json

import pytest

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, LLMResponse, Message, Session
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


class SessionAwareBackend(LLMBackend):
    """Records, per call, whether the session was already `started` (resuming) and the
    exact `messages` it received. Mimics ClaudeCodeBackend's contract: a successful
    create flips `session.started=True`. Optionally raises on a chosen call index to
    exercise session-loss recovery."""

    def __init__(self, responses: list[str], *, fail_on_call: int | None = None) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[bool, list[Message]]] = []
        self._fail_on = fail_on_call
        self._n = 0

    async def run(self, system_prompt, messages, tools, *, session: Session | None = None):
        started_before = bool(session and session.started)
        self.calls.append((started_before, list(messages)))
        n, self._n = self._n, self._n + 1
        if self._fail_on is not None and n == self._fail_on:
            raise RuntimeError("simulated session lost")
        if session is not None and not session.started:
            session.started = True  # create succeeded → next call resumes
        if not self._responses:
            raise AssertionError("script exhausted")
        return LLMResponse(text=self._responses.pop(0))


_ACT = json.dumps({"type": "action", "tool": "x", "args": {}})
_FIN = json.dumps({"type": "final", "answer": "ok"})


async def test_session_sends_full_then_delta_only():
    """With a Session, turn 1 sends the full history (create) and later turns send ONLY
    the messages appended since — claude holds the rest. The model's own assistant
    replies and already-sent messages are never re-shipped."""
    backend = SessionAwareBackend([_ACT, _FIN])
    sess = Session(id="UUID-1", name="pentester-rosa_1")
    loop = AgentLoop(backend, system_prompt="sys", tools=[], session=sess)
    loop.add_user_message("brief")

    await loop.step()                       # turn 1 → create
    loop.add_observation("x", True, "OBS1")
    await loop.step()                       # turn 2 → resume

    # Turn 1: not started yet, full history (just the brief so far).
    assert backend.calls[0][0] is False
    assert [m.content for m in backend.calls[0][1]] == ["brief"]
    # Turn 2: session started, delta is ONLY the new observation.
    assert backend.calls[1][0] is True
    delta = backend.calls[1][1]
    assert any("OBS1" in m.content for m in delta)
    assert all("brief" not in m.content for m in delta)      # not re-sent
    assert all(m.role != "assistant" for m in delta)         # claude's reply not re-sent
    assert sess.started is True


async def test_session_none_sends_full_history_each_turn():
    """The legacy (stateless) path is unchanged: every call gets the whole transcript."""
    backend = SessionAwareBackend([_ACT, _FIN])
    loop = AgentLoop(backend, system_prompt="sys", tools=[], session=None)
    loop.add_user_message("brief")
    await loop.step()
    loop.add_observation("x", True, "OBS1")
    await loop.step()
    # Turn 2 sees brief + assistant reply + observation (the full grown history).
    assert len(backend.calls[1][1]) == 3
    assert any("brief" in m.content for m in backend.calls[1][1])


async def test_session_recovery_regenerates_id_on_failure():
    """If a call fails (e.g. the create's `--session-id` was claimed but errored, or a
    resume lost the session), the loop gives the session a FRESH id and drops back to a
    full create — so the retry can't hit 'Session ID already in use' and replays the
    whole transcript."""
    backend = SessionAwareBackend([_ACT], fail_on_call=1)
    sess = Session(id="UUID-2")
    loop = AgentLoop(backend, system_prompt="sys", tools=[], session=sess)
    loop.add_user_message("brief")
    await loop.step()                       # call 0: create OK
    loop.add_observation("x", True, "OBS1")
    with pytest.raises(RuntimeError):
        await loop.step()                   # call 1: resume → raises
    assert sess.id != "UUID-2"              # fresh id ⇒ retry create can't clash
    assert sess.started is False            # recovery: re-create next time
    assert loop._sent_idx == 0              # next send replays the full transcript


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
