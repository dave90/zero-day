"""Install Python packages into the sandbox so `python_exec` can import them.

A subagent / the orchestrator hits this when it needs a library that isn't baked
into the image (e.g. a niche parser). It runs pip non-interactively with a HARD
timeout — important now that `python_exec` itself has no per-call timeout: a pip
install that stalled inside `python_exec` would wedge the persistent session
forever. Packages install into the same interpreter environment the python_exec
workers use, so after a successful install the agent can `import` them straight
away (the worker invalidates its import caches each call).
"""
from __future__ import annotations

import asyncio
import re
import sys

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult

# A conservative pip requirement specifier: must start alphanumeric, then the
# chars that appear in names/extras/version pins. This blocks pip option
# injection (e.g. a "package" of `--index-url …` or `-r/etc/x`) since args are
# passed via exec (no shell), so only the leading-dash / flag risk matters.
_SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]<>=!~,. ]*$")
_MAX_PACKAGES = 20


class PipInstallArgs(BaseModel):
    packages: list[str] = Field(
        description="PyPI requirement specs to install, e.g. ['pdfminer.six', 'pypdf==4.2.0']"
    )
    timeout_seconds: int = Field(default=300, ge=10, le=900)


class PipInstallTool(Tool):
    name = "pip_install"
    description = (
        "Install Python packages (pip) into the sandbox so `python_exec` can import"
        " them. Use this instead of running pip inside python_exec. Returns pip's"
        " output; installed packages persist for the rest of the session."
    )
    risk_level = RiskLevel.ACTIVE  # runs pip (network); sandboxed in --container
    runs_on_host = True            # runs on the box → gated on host, auto in sandbox
    Args = PipInstallArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PipInstallArgs)
        pkgs = [p.strip() for p in args.packages if p.strip()]
        if not pkgs:
            return ToolResult(ok=False, output="no packages given")
        if len(pkgs) > _MAX_PACKAGES:
            return ToolResult(ok=False, output=f"too many packages (max {_MAX_PACKAGES})")
        bad = [p for p in pkgs if not _SPEC_RE.match(p)]
        if bad:
            return ToolResult(
                ok=False,
                output=f"rejected unsafe package spec(s): {bad!r} "
                "(names must start alphanumeric; no flags / options)",
            )
        argv = [
            sys.executable, "-m", "pip", "install",
            "--break-system-packages",   # Debian/Kali PEP-668 externally-managed env
            "--no-input",                # never block on a prompt
            "--disable-pip-version-check",
            *pkgs,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            return ToolResult(ok=False, output=f"failed to spawn pip: {e}")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=args.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                ok=False,
                output=f"pip install timed out after {args.timeout_seconds}s "
                f"(packages: {', '.join(pkgs)})",
            )
        text = out.decode("utf-8", errors="replace")
        ok = proc.returncode == 0
        if ok:
            text += "\n[installed — import them in python_exec now]"
        return ToolResult(ok=ok, output=text, meta={"exit_code": proc.returncode})
