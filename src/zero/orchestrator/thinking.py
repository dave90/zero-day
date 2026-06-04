"""Human-readable 'thinking' log.

One readable entry per agent step — the orchestrator and every subagent
(pentester / validator). It captures the agent's stated reasoning (the
envelope `thought`) and the action/question/answer it produced that turn, so an
operator can follow how the engagement reasoned without parsing the JSONL audit
log. Written to `.pentest/thinking.log`.
"""
from __future__ import annotations

import time
from pathlib import Path


class ThinkingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, *, agent: str, turn: int, thought: str = "", action: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        self._fh.write(f"[{stamp}] {agent} · turn {turn}\n")
        lines = thought.strip().splitlines()
        for i, line in enumerate(lines):
            prefix = "    thinking: " if i == 0 else "              "
            self._fh.write(f"{prefix}{line}\n")
        if action:
            self._fh.write(f"    → {action}\n")
        self._fh.write("\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
