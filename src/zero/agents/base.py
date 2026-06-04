"""Shared ReAct loop primitives."""
from __future__ import annotations

from dataclasses import dataclass, field

from zero.llm.backend import LLMBackend, Message
from zero.llm.envelope import (
    Envelope,
    EnvelopeError,
    FinalEnvelope,
    RETRY_REMINDER,
    parse_envelope,
)


@dataclass
class StepResult:
    envelope: Envelope
    raw_text: str
    usage: dict[str, int] = field(default_factory=dict)


class AgentLoop:
    """One step at a time: ask the LLM for an envelope, hand it back.

    The caller drives the loop: feed the envelope to whatever subsystem
    handles it (approval gate + tool registry for `action`; UI for `ask_user`;
    end-of-run for `final`), then call `add_observation` / `add_user_message`
    and loop again.
    """

    def __init__(
        self,
        backend: LLMBackend,
        system_prompt: str,
        tools: list[dict],
        *,
        max_turns: int = 20,
    ) -> None:
        self.backend = backend
        self.system_prompt = system_prompt
        self.tools = tools
        self.max_turns = max_turns
        self.history: list[Message] = []
        self._turn = 0

    @property
    def turn(self) -> int:
        return self._turn

    def add_user_message(self, text: str) -> None:
        self.history.append(Message(role="user", content=text))

    def add_observation(self, tool: str, ok: bool, output: str) -> None:
        status = "ok" if ok else "error"
        self.history.append(
            Message(role="observation", content=f"[{tool} {status}] {output}")
        )

    def add_ask_user_answer(self, answer: str) -> None:
        self.history.append(Message(role="user", content=f"(ask_user answer) {answer}"))

    async def step(self) -> StepResult:
        """Ask the LLM for one envelope. Cancellable via asyncio.CancelledError."""
        if self._turn >= self.max_turns:
            return StepResult(
                envelope=FinalEnvelope(
                    answer=f"turn cap reached ({self.max_turns}); stopping."
                ),
                raw_text="",
            )
        self._turn += 1
        resp = await self.backend.run(self.system_prompt, self.history, self.tools)
        try:
            env = parse_envelope(resp.text)
        except EnvelopeError as e:
            self.history.append(Message(role="assistant", content=resp.text))
            self.history.append(
                Message(role="user", content=f"{RETRY_REMINDER}\n\nReason: {e}")
            )
            resp2 = await self.backend.run(self.system_prompt, self.history, self.tools)
            env = parse_envelope(resp2.text)
            self.history.append(Message(role="assistant", content=resp2.text))
            return StepResult(envelope=env, raw_text=resp2.text, usage=resp2.usage)
        self.history.append(Message(role="assistant", content=resp.text))
        return StepResult(envelope=env, raw_text=resp.text, usage=resp.usage)
