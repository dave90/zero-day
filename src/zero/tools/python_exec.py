"""Run a Python snippet in a subprocess with a wall-clock timeout.

Phase 1 stub: runs on the host. Moves into the Docker sandbox in M4.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult


class PythonExecArgs(BaseModel):
    code: str = Field(description="Python source executed with the host python interpreter")
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class PythonExecTool(Tool):
    name = "python_exec"
    description = "Run a Python snippet and return its stdout + stderr."
    risk_level = RiskLevel.ACTIVE  # arbitrary code; treat as ACTIVE on the host
    # Executes arbitrary code in a host subprocess — the gate will not
    # auto-approve it on the host (only when sandboxed via --container, or with
    # an explicit --allow-host-exec).
    runs_on_host = True
    Args = PythonExecArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PythonExecArgs)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(args.code)
            script = Path(fh.name)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=args.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(
                    ok=False,
                    output=f"timeout after {args.timeout_seconds}s",
                )
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            combined = out
            if err:
                combined += "\n--- stderr ---\n" + err
            return ToolResult(ok=proc.returncode == 0, output=combined)
        finally:
            try:
                script.unlink()
            except OSError:
                pass
