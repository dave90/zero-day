"""Read a file's content.

Jailed to the files root (see `_fsroot`): the agent can only read inside it, never the
wider host filesystem.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from zero.tools._fsroot import PathOutsideRoot, resolve_in_root
from zero.tools.base import RiskLevel, Tool, ToolResult

_MAX_BYTES_CAP = 1_000_000


class ReadFileArgs(BaseModel):
    path: str = Field(description="file to read, relative to the files root")
    max_bytes: int = Field(default=200_000, ge=1, le=_MAX_BYTES_CAP,
                           description="read at most this many bytes")
    offset: int = Field(default=0, ge=0, description="start reading at this byte offset (paging)")


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a file's content (decoded as UTF-8, undecodable bytes replaced). Use"
        " offset + max_bytes to page through large files. Read-only and jailed to the"
        " files root."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False  # read-only, jailed read — never prompt
    Args = ReadFileArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, ReadFileArgs)
        try:
            target, _root = resolve_in_root(args.path)
        except PathOutsideRoot as e:
            return ToolResult(ok=False, output=str(e))
        if not target.exists():
            return ToolResult(ok=False, output=f"file not found: {args.path}")
        if target.is_dir():
            return ToolResult(ok=False, output=f"is a directory, not a file: {args.path}")

        try:
            size = target.stat().st_size
            with target.open("rb") as fh:
                if args.offset:
                    fh.seek(args.offset)
                raw = fh.read(args.max_bytes)
        except PermissionError as e:
            return ToolResult(ok=False, output=f"permission denied: {e}")
        except OSError as e:
            return ToolResult(ok=False, output=f"read error: {e}")

        truncated = args.offset + len(raw) < size
        text = raw.decode("utf-8", "replace")
        if truncated:
            nxt = args.offset + len(raw)
            text += (
                f"\n… (truncated: read bytes {args.offset}–{nxt} of {size}; "
                f"continue with offset={nxt} or raise max_bytes)"
            )
        return ToolResult(ok=True, output=text, truncated=truncated)
