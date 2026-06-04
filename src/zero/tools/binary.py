"""BinaryTool helper: shell out to a CLI binary with a wall-clock timeout.

Each concrete tool in this package implements `args_to_argv(args)` to
translate its pydantic Args into a CLI argv list. BinaryTool handles
subprocess spawning, timeout enforcement, stdout/stderr collection, output
truncation, and shutil.which() presence checks.
"""
from __future__ import annotations

import asyncio
import shutil
from abc import abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from zero.tools.base import Tool, ToolResult


DEFAULT_TIMEOUT = 300  # 5 min, per plan §M5
MAX_OUTPUT_BYTES = 200_000


class BinaryTool(Tool):
    binary: ClassVar[str] = ""  # e.g. "sqlmap"
    timeout_seconds: ClassVar[int] = DEFAULT_TIMEOUT
    # Shells out to a real host binary — gated as host execution (see Tool).
    runs_on_host = True

    @abstractmethod
    def args_to_argv(self, args: BaseModel) -> list[str]: ...

    async def run(self, args: BaseModel) -> ToolResult:
        if not self.binary:
            return ToolResult(ok=False, output=f"{self.name}: binary not configured")
        binary_path = shutil.which(self.binary)
        if binary_path is None:
            return ToolResult(
                ok=False,
                output=(
                    f"{self.binary} not on PATH. The host doesn't have it installed;"
                    " run with --container so it executes inside the Kali sandbox."
                ),
            )
        argv = [binary_path, *self.args_to_argv(args)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return ToolResult(ok=False, output=f"failed to spawn {self.binary}: {e}")

        timeout = getattr(args, "timeout_seconds", None) or self.timeout_seconds
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                ok=False,
                output=f"{self.binary} timed out after {timeout}s",
            )

        out_text = stdout.decode("utf-8", "replace")
        err_text = stderr.decode("utf-8", "replace")
        combined = out_text
        if err_text:
            combined += "\n--- stderr ---\n" + err_text

        truncated = False
        if len(combined) > MAX_OUTPUT_BYTES:
            combined = combined[:MAX_OUTPUT_BYTES] + f"\n… (truncated, {len(combined) - MAX_OUTPUT_BYTES} more bytes)"
            truncated = True

        return ToolResult(
            ok=proc.returncode == 0,
            output=combined,
            truncated=truncated,
            meta={"exit_code": proc.returncode, "argv_count": len(argv)},
        )
