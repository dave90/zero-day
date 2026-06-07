"""EchoBackend — scripted responses, test-only.

This is NOT a user-facing backend: `--backend` only exposes `claude`, and
`zero` defaults to the Claude Code backend inside the Docker sandbox. The
EchoBackend exists solely as an offline fake so the test suite can drive the
agent loop / orchestrator / router without an LLM. Tests construct
`OrchestratorConfig(backend="echo")` directly, which the orchestrator factory
still honors.

Returns envelopes from a pre-loaded script in order. When the script is
exhausted, returns a `final` envelope summarizing the run.
"""
from __future__ import annotations

import json
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse, Message


DEFAULT_SCRIPT: list[dict[str, Any]] = [
    {
        "type": "action",
        "tool": "web_search",
        "args": {"query": "target recon: common web app entry points", "max_results": 3},
        "thought": "Let me orient before I attack.",
    },
    {
        "type": "ask_user",
        "question": "Which area should I focus on first?",
        "options": ["auth", "orders", "search"],
        "free_form": True,
        "blocking": True,
    },
    {
        "type": "action",
        "tool": "web_search",
        "args": {"query": "OWASP top 10 2024", "max_results": 3},
        "thought": "Refreshing my memory on common web bugs.",
    },
    {
        "type": "final",
        "answer": "Demo complete. The EchoBackend ran through a web_search, an"
                  " ask_user, and a web_search to exercise the UI surface.",
    },
]


class EchoBackend(LLMBackend):
    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self._script = list(script if script is not None else DEFAULT_SCRIPT)
        self._idx = 0

    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        if self._idx >= len(self._script):
            env: dict[str, Any] = {"type": "final", "answer": "(echo backend exhausted)"}
        else:
            env = self._script[self._idx]
            self._idx += 1
        return LLMResponse(text=json.dumps(env), usage={"input_tokens": 0, "output_tokens": 0})
