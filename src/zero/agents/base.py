"""Shared ReAct loop primitives."""
from __future__ import annotations

from dataclasses import dataclass, field

from zero.llm.backend import LLMBackend, Message, context_tokens_from_usage
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
        wrapup_prompt: str | None = None,
        reminder_text: str | None = None,
        reminder_every: int = 0,
    ) -> None:
        self.backend = backend
        self.system_prompt = system_prompt
        self.tools = tools
        self.max_turns = max_turns
        # When the turn budget is hit, give the agent ONE last LLM call driven by
        # this prompt to hand off everything it found (secrets, tokens, partial
        # progress) instead of a bare "turn cap reached" that throws that intel
        # away. None ⇒ keep the bare cap final (orchestrator / tests).
        self.wrapup_prompt = wrapup_prompt
        # Periodic "<system-reminder>" injected every `reminder_every` turns (0 = off)
        # — the Claude-Code pattern: a lightweight nudge sitting right before the next
        # model call to keep memory checkpointed, the plan updated, and the agent from
        # stopping early. Off by default (orchestrator/tests opt in via the builders).
        self.reminder_text = reminder_text
        self.reminder_every = reminder_every
        self.history: list[Message] = []
        self._turn = 0
        self._wrapped_up = False
        # Tokens occupying this agent's context window as of its LAST LLM call (the
        # prompt size: input + both cache tiers). Updated in step(); read by the UI
        # (orchestrator status bar + the per-subagent running view) to show
        # used/window. 0 until the first call (and with the echo test fixture).
        self.context_tokens = 0

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
            # Out of turns. If a handoff prompt is set, spend ONE last call asking
            # the agent to summarize everything it found (secrets/tokens/partial
            # chain/working payloads) so that intel reaches whoever consumes its
            # result — rather than a bare cap message that discards it.
            if self.wrapup_prompt and not self._wrapped_up:
                self._wrapped_up = True
                self.history.append(Message(role="user", content=self.wrapup_prompt))
                resp = await self.backend.run(self.system_prompt, self.history, self.tools)
                self.context_tokens = context_tokens_from_usage(resp.usage)
                self.history.append(Message(role="assistant", content=resp.text))
                try:
                    env: Envelope = parse_envelope(resp.text)
                except EnvelopeError:
                    env = FinalEnvelope(answer=resp.text.strip())
                if not isinstance(env, FinalEnvelope):
                    # It tried to act, but the budget is spent — keep its text as
                    # the handoff rather than running another tool.
                    env = FinalEnvelope(answer=(getattr(env, "thought", "") or resp.text).strip())
                return StepResult(envelope=env, raw_text=resp.text, usage=resp.usage)
            return StepResult(
                envelope=FinalEnvelope(
                    answer=f"turn cap reached ({self.max_turns}); stopping."
                ),
                raw_text="",
            )
        self._turn += 1
        # Post-tool reminder (Claude-Code "<system-reminder>"): every N turns, drop a
        # lightweight nudge right before the model call. Throttled so it doesn't bloat
        # context, and never on the first turn.
        if (
            self.reminder_text
            and self.reminder_every > 0
            and self._turn > 1
            and self._turn % self.reminder_every == 0
        ):
            self.history.append(Message(role="user", content=self.reminder_text))
        resp = await self.backend.run(self.system_prompt, self.history, self.tools)
        self.context_tokens = context_tokens_from_usage(resp.usage)
        try:
            env = parse_envelope(resp.text)
        except EnvelopeError as e:
            self.history.append(Message(role="assistant", content=resp.text))
            self.history.append(
                Message(role="user", content=f"{RETRY_REMINDER}\n\nReason: {e}")
            )
            resp2 = await self.backend.run(self.system_prompt, self.history, self.tools)
            self.context_tokens = context_tokens_from_usage(resp2.usage)
            env = parse_envelope(resp2.text)
            self.history.append(Message(role="assistant", content=resp2.text))
            return StepResult(envelope=env, raw_text=resp2.text, usage=resp2.usage)
        self.history.append(Message(role="assistant", content=resp.text))
        return StepResult(envelope=env, raw_text=resp.text, usage=resp.usage)
