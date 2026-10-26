"""LLM backend ABC.

A backend is given a system prompt, the conversation history, and the tool
catalog. It returns a single raw-string `LLMResponse` (the envelope text);
parsing into an action/ask_user/final envelope happens in `envelope.py`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal


@dataclass
class Message:
    role: Literal["user", "assistant", "system", "observation"]
    content: str


@dataclass
class Session:
    """A persistent `claude` conversation, keyed by a stable UUID.

    Lets the ClaudeCodeBackend talk to one claude session across many `claude -p`
    calls (`--session-id` to create, `--resume` to continue) so we send only the
    NEW messages each turn instead of re-shipping the whole transcript — claude
    holds the conversation and does the caching/context management itself. One
    Session per actor (orchestrator + each subagent). `started` flips to True after
    the create call; `name` is a human-readable label (`-n`) for debugging only.
    Backends that aren't session-aware (echo/scripted/replay) ignore it."""

    id: str
    name: str = ""
    started: bool = False


@dataclass
class LLMResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None
    # Exact active-context occupancy when a provider exposes it separately from
    # cumulative/billing usage. None preserves the legacy derivation from `usage`.
    context_tokens: int | None = None
    # Provider-reported effective context window for this session/turn. Most
    # backends leave this unset and the UI uses its configured fallback; CLI
    # backends fill it when their own runtime exposes a more accurate value.
    context_window: int | None = None


def context_tokens_from_usage(usage: dict[str, int] | None) -> int:
    """Tokens currently occupying the context window per an Anthropic `usage` block.

    That's the whole PROMPT we just sent — fresh input plus both cache tiers
    (`cache_creation_input_tokens` + `cache_read_input_tokens`); cached tokens still
    sit in the window. `output_tokens` is the reply, not part of the next prompt, so
    it's excluded. Missing/odd keys default to 0 so a backend with sparse usage (or
    the echo fixture) just reports a smaller number rather than raising."""
    if not usage:
        return 0
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    total = 0
    for k in keys:
        v = usage.get(k, 0)
        if isinstance(v, (int, float)):
            total += int(v)
    return total


class LLMBackend(ABC):
    def fork(self) -> LLMBackend:
        """Own one actor's request state. Stateless/CLI adapters can share themselves."""
        return self

    def set_actor(self, name: str) -> None:
        """Optional trace label, independent of persistent provider sessions."""

    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        session: Session | None = None,
    ) -> LLMResponse:
        """Run one turn. `session=None` (default) ⇒ stateless: `messages` is the FULL
        conversation and the backend re-sends it every call (legacy behavior, used by
        echo/scripted/replay fixtures). When a `Session` is given, a session-aware
        backend (ClaudeCodeBackend) creates/continues that claude session and treats
        `messages` as the DELTA to append — see Session. Backends that don't support
        sessions must accept and ignore the kwarg."""
        ...

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        session: Session | None = None,
    ) -> AsyncIterator[str]:
        """Optional: stream tokens. Default falls back to one-shot run."""
        resp = await self.run(system_prompt, messages, tools, session=session)
        yield resp.text


class BackendError(RuntimeError):
    """An already-classified backend failure; permanent errors must not be re-driven."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def terminal_backend_error(error: Exception) -> bool:
    return isinstance(error, BackendError) and not error.retryable
