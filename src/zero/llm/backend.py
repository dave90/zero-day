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
