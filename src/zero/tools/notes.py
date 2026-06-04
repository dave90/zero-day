"""Scratchpad notes the agent can append to and read back."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult


class NotesArgs(BaseModel):
    action: Literal["append", "read"] = Field(description="append a new entry or read all entries")
    text: str | None = Field(default=None, description="required when action=append")


class NotesTool(Tool):
    name = "notes"
    description = "Scratchpad. append (writes a line) | read (returns full file)."
    risk_level = RiskLevel.SAFE
    requires_approval = False  # local scratchpad, no side effects — never prompt
    Args = NotesArgs

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (Path.cwd() / ".pentest" / "notes.md")
        self._lock = asyncio.Lock()

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, NotesArgs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if args.action == "append":
            if not args.text:
                return ToolResult(ok=False, output="text required for append")
            async with self._lock:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(args.text.rstrip() + "\n")
            return ToolResult(ok=True, output=f"appended {len(args.text)} chars")
        # read
        if not self.path.exists():
            return ToolResult(ok=True, output="")
        return ToolResult(ok=True, output=self.path.read_text(encoding="utf-8"))
