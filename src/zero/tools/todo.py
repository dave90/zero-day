"""todo_write / todo_read — each agent's structured, visible plan (Claude-Code
TodoWrite). A short checklist the agent maintains so it tracks multi-step work and
the operator can see progress.

Modelled on the memory tools (`tools/memory.py`): per-actor, host-side, keyed purely
by the injected `session_id` (the LLM never names another actor's file), excluded from
sandbox remoting. The list is REPLACED on each write (not appended). Files live under
`<.pentest>/todo/<session_id>.json`, a sibling of the memory store.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from zero.tools._memroot import memory_root, safe_name
from zero.tools.base import RiskLevel, Tool, ToolResult

_STATUS = ("pending", "in_progress", "done")


def todo_root() -> Path:
    """Sibling of the memory store: `<.pentest>/todo`."""
    return memory_root().parent / "todo"


def path_for(session_id: str) -> Path:
    return todo_root() / f"{safe_name(session_id)}.json"


class TodoItem(BaseModel):
    step: str = Field(description="the task, imperative and short")
    status: Literal["pending", "in_progress", "done"] = "pending"


def read_todo_items(session_id: str) -> list[dict]:
    p = path_for(session_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def write_todo_items(session_id: str, items: list[dict]) -> Path:
    p = path_for(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def format_todo(items: list[dict]) -> str:
    """Render a checklist as text (for `todo_read` and the post-tool reminder)."""
    if not items:
        return "(no plan yet)"
    glyph = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    return "\n".join(f"{glyph.get(i.get('status', 'pending'), '[ ]')} {i.get('step', '?')}" for i in items)


class TodoWriteArgs(BaseModel):
    items: list[TodoItem] = Field(
        description="the FULL plan as a checklist; this REPLACES the previous list"
    )
    # Managed automatically by the orchestrator. You do not need to set this.
    session_id: str = Field(default="_default")


class TodoReadArgs(BaseModel):
    # Managed automatically by the orchestrator. You do not need to set this.
    session_id: str = Field(default="_default")


class TodoWriteTool(Tool):
    name = "todo_write"
    description = (
        "Maintain your task plan as a checklist (steps with status pending/in_progress/"
        "done). Call it when you start multi-step work and update it as you go — mark a"
        " step in_progress before you work it and done right after, and add steps you"
        " discover. This REPLACES the whole list each time, so send the full plan. It"
        " keeps you on track and shows the operator your progress."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False
    runs_on_host = False
    Args = TodoWriteArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, TodoWriteArgs)
        items = [i.model_dump() for i in args.items]
        write_todo_items(args.session_id, items)
        return ToolResult(ok=True, output="plan updated:\n" + format_todo(items))


class TodoReadTool(Tool):
    name = "todo_read"
    description = "Read your current task plan (the checklist you saved with todo_write)."
    risk_level = RiskLevel.SAFE
    requires_approval = False
    runs_on_host = False
    Args = TodoReadArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, TodoReadArgs)
        return ToolResult(ok=True, output=format_todo(read_todo_items(args.session_id)))
