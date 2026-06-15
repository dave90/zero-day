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
class LLMResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


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
    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...

    async def stream(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[str]:
        """Optional: stream tokens. Default falls back to one-shot run."""
        resp = await self.run(system_prompt, messages, tools)
        yield resp.text
