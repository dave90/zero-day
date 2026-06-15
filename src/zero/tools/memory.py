"""memory_read / memory_write — each agent's own bounded scratchpad.

Every actor (the orchestrator, each pentester / validator subagent) gets ONE
markdown file it reads at the start of its run and OVERWRITES as it works — a
running summary of what it is doing, what it has tried, what worked, and the next
step. Unlike the transcript it never grows: `memory_write` REPLACES the file.

The file is chosen purely from the per-actor `session_id` the orchestrator injects
into the args (the same mechanism `python_exec` uses; the LLM never sets it), so an
agent can only ever touch its own memory. These tools run on the HOST (they are
excluded from sandbox remoting) and write under `.pentest/memory` — see `_memroot`.
The orchestrator reads any subagent's memory via the separate, orchestrator-only
`read_subagent_memory` / `list_memories` tools (see `dispatch.py`).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from zero.tools._memroot import path_for
from zero.tools.base import RiskLevel, Tool, ToolResult


def read_memory(session_id: str) -> str:
    """Host-side read of an actor's memory file. Returns "" when none exists yet
    (a fresh agent has no memory — that is not an error). Shared by the agent
    `memory_read` tool and the orchestrator's `read_subagent_memory` handler."""
    p = path_for(session_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"[memory unreadable: {e}]"


def write_memory(session_id: str, content: str) -> Path:
    p = path_for(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    # errors="replace" so arbitrary bytes the agent pasted (a raw response body)
    # can never crash the write; replace-mode keeps the file bounded.
    p.write_text(content, encoding="utf-8", errors="replace")
    return p


class MemoryWriteArgs(BaseModel):
    content: str = Field(
        description="the FULL new contents of your memory; this REPLACES the file (it is not appended)"
    )
    # Managed automatically by the orchestrator (one isolated memory per agent).
    # You do not need to set this.
    session_id: str = Field(default="_default")


class MemoryReadArgs(BaseModel):
    # Managed automatically by the orchestrator. You do not need to set this.
    session_id: str = Field(default="_default")


class MemoryWriteTool(Tool):
    name = "memory_write"
    description = (
        "Save your working memory — a concise running summary of THIS investigation"
        " (what you've tried, what worked/failed, recovered creds/tokens/payloads, and"
        " the next step). This REPLACES your memory file (it does NOT append), so write"
        " the whole thing each time and keep it tight — prune stale detail rather than"
        " letting it grow. Update it whenever you learn something worth not losing or"
        " your context is filling up; it survives even if you crash or run out of turns,"
        " and the orchestrator can read it to recover your progress."
    )
    risk_level = RiskLevel.SAFE  # writes only to .pentest/memory on the host
    requires_approval = False    # no side effect beyond the local scratchpad — never prompt
    runs_on_host = False         # confined to .pentest/memory; not arbitrary host code
    Args = MemoryWriteArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, MemoryWriteArgs)
        p = write_memory(args.session_id, args.content)
        return ToolResult(ok=True, output=f"memory saved ({len(args.content)} chars) → {p.name}")


class MemoryReadTool(Tool):
    name = "memory_read"
    description = (
        "Load your working memory — the running summary you saved earlier with"
        " memory_write. Call this at the START of your run to pick up where you (or a"
        " prior session) left off. Returns empty if you have not written any yet."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False
    runs_on_host = False
    Args = MemoryReadArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, MemoryReadArgs)
        body = read_memory(args.session_id)
        if not body:
            return ToolResult(ok=True, output="(your memory is empty — nothing saved yet)")
        return ToolResult(ok=True, output=body)
