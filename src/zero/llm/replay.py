"""Backend that replays recorded `assistant` messages from an audit log.

Extracts every `agent_step` record's `raw` field in order and returns them
on successive `run()` calls. When the script is exhausted, returns a final
envelope so the loop terminates cleanly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse, Message
from zero.orchestrator.audit import read_audit


class ReplayBackend(LLMBackend):
    def __init__(self, audit_path: Path) -> None:
        rows = read_audit(audit_path)
        self._script: list[str] = [
            r["raw"] for r in rows if r.get("kind") == "agent_step" and r.get("raw")
        ]
        self._idx = 0

    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        session: Any = None,  # accepted + ignored; replay is index-driven
    ) -> LLMResponse:
        if self._idx >= len(self._script):
            return LLMResponse(
                text=json.dumps(
                    {"type": "final", "answer": "(replay exhausted)"},
                ),
            )
        text = self._script[self._idx]
        self._idx += 1
        return LLMResponse(text=text)
