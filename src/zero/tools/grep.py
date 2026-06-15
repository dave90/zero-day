"""Regex-search files under the files root.

Jailed to the files root (see `_fsroot`): the agent can only search inside it, never
the wider host filesystem.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from zero.tools._fsroot import PathOutsideRoot, resolve_in_root
from zero.tools.base import RiskLevel, Tool, ToolResult

_MAX_FILE_BYTES = 2_000_000   # skip files larger than this (bound memory)
_BINARY_SNIFF = 8192          # a NUL in the first this-many bytes ⇒ treat as binary
_OUTPUT_CAP = 200_000         # cap total output


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regular expression to search for")
    path: str = Field(default=".", description="file or directory to search, relative to the files root")
    recursive: bool = Field(default=True, description="descend into sub-directories when path is a dir")
    ignore_case: bool = Field(default=False, description="case-insensitive match")
    include: str | None = Field(default=None, description="only search files matching this glob (e.g. '*.py')")
    context: int = Field(default=0, ge=0, le=10, description="lines of context to show around each match")
    max_matches: int = Field(default=200, ge=1, le=2000, description="cap the number of matches returned")


def _iter_files(target: Path, recursive: bool, include: str | None):
    if target.is_file():
        yield target
        return
    if recursive:
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                if include and not fnmatch.fnmatch(name, include):
                    continue
                yield Path(dirpath) / name
    else:
        for child in sorted(target.iterdir(), key=lambda c: c.name):
            if child.is_file() and (not include or fnmatch.fnmatch(child.name, include)):
                yield child


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search files for a Python regular expression and return matching lines as"
        " 'path:lineno: line'. Searches a directory recursively by default; filter with"
        " an `include` glob (e.g. '*.py'), show `context` lines around hits. Binary"
        " files and very large files are skipped. Read-only and jailed to the files root."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False  # read-only, jailed search — never prompt
    Args = GrepArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, GrepArgs)
        try:
            rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        except re.error as e:
            return ToolResult(ok=False, output=f"invalid regex: {e}")
        try:
            target, root = resolve_in_root(args.path)
        except PathOutsideRoot as e:
            return ToolResult(ok=False, output=str(e))
        if not target.exists():
            return ToolResult(ok=False, output=f"path not found: {args.path}")

        out: list[str] = []
        matches = 0
        skipped_binary = 0
        skipped_large = 0
        truncated = False
        total_chars = 0

        for fpath in _iter_files(target, args.recursive, args.include):
            if matches >= args.max_matches:
                truncated = True
                break
            try:
                if fpath.stat().st_size > _MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
                data = fpath.read_bytes()
            except (PermissionError, OSError):
                continue
            if b"\x00" in data[:_BINARY_SNIFF]:
                skipped_binary += 1
                continue
            lines = data.decode("utf-8", "replace").splitlines()
            rel = fpath.relative_to(root).as_posix()
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                lo = max(0, i - args.context)
                hi = min(len(lines), i + args.context + 1)
                for j in range(lo, hi):
                    sep = ":" if j == i else "-"
                    rendered = f"{rel}:{j + 1}{sep} {lines[j]}"
                    out.append(rendered)
                    total_chars += len(rendered) + 1
                if args.context:
                    out.append("--")
                    total_chars += 3
                matches += 1
                if matches >= args.max_matches or total_chars >= _OUTPUT_CAP:
                    truncated = True
                    break
            if truncated:
                break

        notes = []
        if truncated:
            notes.append(f"truncated at {matches} matches / {_OUTPUT_CAP} chars")
        if skipped_binary:
            notes.append(f"skipped {skipped_binary} binary file(s)")
        if skipped_large:
            notes.append(f"skipped {skipped_large} file(s) >{_MAX_FILE_BYTES} bytes")

        if not out:
            tail = f"  [{'; '.join(notes)}]" if notes else ""
            return ToolResult(ok=True, output="(no matches)" + tail)
        body = "\n".join(out)
        if notes:
            body += "\n[" + "; ".join(notes) + "]"
        return ToolResult(ok=True, output=body, truncated=truncated)
