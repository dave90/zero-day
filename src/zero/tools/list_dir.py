"""List a directory — a plain listing of sub-directories and files.

Jailed to the files root (see `_fsroot`): the agent can only list inside it, never the
wider host filesystem.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from zero.tools._fsroot import PathOutsideRoot, resolve_in_root
from zero.tools.base import RiskLevel, Tool, ToolResult

_MAX_ENTRIES_CAP = 5000


class ListDirArgs(BaseModel):
    path: str = Field(default=".", description="directory to list, relative to the files root")
    recursive: bool = Field(default=False, description="descend into sub-directories")
    max_entries: int = Field(default=1000, ge=1, le=_MAX_ENTRIES_CAP,
                             description="cap the number of entries returned")


def _fmt(entry: Path, root: Path, is_dir: bool) -> str:
    rel = entry.relative_to(root).as_posix()
    return rel + "/" if is_dir else rel


class ListDirTool(Tool):
    name = "list_dir"
    description = (
        "List a directory: its sub-directories (trailing '/') and files, sorted, dirs"
        " first. Hidden entries (e.g. .env, .git) are included. Set recursive=true to"
        " walk the whole tree. Read-only and jailed to the files root."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False  # read-only, jailed listing — never prompt
    Args = ListDirArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ListDirArgs)
        try:
            target, root = resolve_in_root(args.path)
        except PathOutsideRoot as e:
            return ToolResult(ok=False, output=str(e))
        if not target.exists():
            return ToolResult(ok=False, output=f"path not found: {args.path}")
        if not target.is_dir():
            return ToolResult(ok=False, output=f"not a directory: {args.path}")

        dirs: list[str] = []
        files: list[str] = []
        truncated = False
        try:
            if args.recursive:
                for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
                    dirnames.sort()
                    base = Path(dirpath)
                    for d in sorted(dirnames):
                        dirs.append(_fmt(base / d, root, True))
                    for f in sorted(filenames):
                        files.append(_fmt(base / f, root, False))
                    if len(dirs) + len(files) > args.max_entries:
                        truncated = True
                        break
            else:
                for child in sorted(target.iterdir(), key=lambda c: c.name):
                    (dirs if child.is_dir() else files).append(_fmt(child, root, child.is_dir()))
        except PermissionError as e:
            return ToolResult(ok=False, output=f"permission denied: {e}")

        entries = dirs + files
        if len(entries) > args.max_entries:
            entries = entries[: args.max_entries]
            truncated = True
        body = "\n".join(entries) if entries else "(empty)"
        if truncated:
            body += f"\n… (truncated at {args.max_entries} entries; raise max_entries)"
        return ToolResult(ok=True, output=body, truncated=truncated)
