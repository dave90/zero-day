"""Custom tools — orchestrator-authored Python scripts, reusable by every agent.

Five tools split by authority, mirroring the dispatch/registry split already in
the codebase:

* `custom_tool_create` / `custom_tool_modify` / `custom_tool_delete` are
  ORCHESTRATOR-ONLY. Like the dispatch tools (`dispatch.py`) they are NOT in
  `default_registry`; the orchestrator appends them to its own catalog and
  intercepts them (`is_custom_mgmt_tool` + `_handle_custom_mgmt`), so subagents
  never see them and the `run()` bodies here are sentinels that never fire.
* `custom_tool_list` / `custom_tool_call` are normal registry tools, so they
  flow to the orchestrator AND every pentester/validator subagent, and are
  remoted into the sandbox like `python_exec`.

A custom tool is one self-describing script (`_customtools`): its module
docstring documents what it does and what JSON args it reads from stdin. `call`
runs `python <script>` with the caller's args as JSON on stdin and captures the
output — there is no per-tool schema; the docstring (surfaced by `list`) tells
the agent how to call it.
"""
from __future__ import annotations

import asyncio
import json
import sys

from pydantic import BaseModel, Field

from zero.tools._customtools import list_tools, path_for, read_tool
from zero.tools._fsroot import PathOutsideRoot
from zero.tools.base import RiskLevel, Tool, ToolResult

# Cap captured output so a chatty script can't blow up the transcript (mirrors
# the bounded output of the other exec tools).
_MAX_OUTPUT = 200_000


# --- custom_tool_list (all agents) -------------------------------------------


class CustomToolListArgs(BaseModel):
    pass


class CustomToolListTool(Tool):
    name = "custom_tool_list"
    description = (
        "List the reusable custom tools available in this engagement, with each"
        " tool's description/usage (from its script docstring). Call this to"
        " discover tools the orchestrator has authored, then run one with"
        " custom_tool_call."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False  # read-only over the jailed store — never prompt
    Args = CustomToolListArgs

    async def run(self, args: BaseModel) -> ToolResult:
        tools = list_tools()
        if not tools:
            return ToolResult(ok=True, output="(no custom tools defined yet)")
        blocks = [f"### {t['name']}\n{t['description']}" for t in tools]
        return ToolResult(ok=True, output="\n\n".join(blocks))


# --- custom_tool_call (all agents) -------------------------------------------


class CustomToolCallArgs(BaseModel):
    name: str = Field(description="the custom tool to run (see custom_tool_list)")
    args: dict = Field(
        default_factory=dict,
        description="arguments for the tool, passed to its script as JSON on stdin"
        " (see the tool's described usage)",
    )
    timeout_seconds: int = Field(default=120, ge=1, le=900)


class CustomToolCallTool(Tool):
    name = "custom_tool_call"
    description = (
        "Run a custom tool by name. Its `args` are delivered to the script as a"
        " JSON object on stdin; the script's stdout is returned. Use"
        " custom_tool_list first to see the tool's usage."
    )
    risk_level = RiskLevel.ACTIVE  # executes author-supplied code; sandboxed in --container
    runs_on_host = True            # runs on the box → gated on host, auto in sandbox
    Args = CustomToolCallArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, CustomToolCallArgs)
        if read_tool(args.name) is None:
            return ToolResult(
                ok=False,
                output=f"no such custom tool: {args.name!r} (use custom_tool_list)",
            )
        try:
            script = path_for(args.name)
        except (ValueError, PathOutsideRoot) as e:
            return ToolResult(ok=False, output=f"bad tool name: {e}")

        payload = json.dumps(args.args).encode("utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as e:
            return ToolResult(ok=False, output=f"failed to spawn custom tool: {e}")
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(input=payload), timeout=args.timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(
                ok=False,
                output=f"custom tool {args.name!r} timed out after {args.timeout_seconds}s",
            )
        text = out.decode("utf-8", errors="replace")
        truncated = len(text) > _MAX_OUTPUT
        if truncated:
            text = text[:_MAX_OUTPUT] + "\n… (output truncated)"
        ok = proc.returncode == 0
        if not ok:
            text = f"[custom tool exited {proc.returncode}]\n{text}"
        return ToolResult(ok=ok, output=text, truncated=truncated,
                          meta={"exit_code": proc.returncode})


# --- custom_tool_create / modify / delete (orchestrator-only, intercepted) ---


_SENTINEL = "custom-tool management is handled by the orchestrator, not run directly."


class _CustomMgmtTool(Tool):
    """Base for the orchestrator-only management tools. Like `_OrchestratorTool`
    in dispatch.py: intercepted by the orchestrator, the `run()` body never
    fires (returns an instructive error if a non-orchestrator agent ever has it)."""

    risk_level = RiskLevel.SAFE  # writes a script file to the jail; the risk is at call time

    async def run(self, args: BaseModel) -> ToolResult:  # pragma: no cover - never called
        return ToolResult(ok=False, output=_SENTINEL)


class CustomToolCreateArgs(BaseModel):
    name: str = Field(description="snake_case tool name, e.g. 'jwt_forge'")
    description: str = Field(
        description="what the tool does, when to use it, and what JSON args it reads"
        " from stdin — this becomes the script's docstring and is shown to subagents"
    )
    script_body: str = Field(
        description="the Python source (without the docstring). Read JSON args from"
        " stdin (json.load(sys.stdin)) and print results to stdout."
    )


class CustomToolCreateTool(_CustomMgmtTool):
    name = "custom_tool_create"
    description = (
        "Create a reusable custom tool (a Python script) that you and your"
        " subagents can run with custom_tool_call. The description becomes the"
        " script's docstring (its usage). The script is rejected if it doesn't"
        " compile. Test it with custom_tool_call before relying on it. Persists"
        " across runs."
    )
    Args = CustomToolCreateArgs


class CustomToolModifyArgs(BaseModel):
    name: str = Field(description="the existing tool to overwrite")
    description: str = Field(description="the new description/usage (becomes the docstring)")
    script_body: str = Field(description="the new Python source (replaces the old)")


class CustomToolModifyTool(_CustomMgmtTool):
    name = "custom_tool_modify"
    description = (
        "Replace an existing custom tool's script and description. Rejected if the"
        " new script doesn't compile. Re-test with custom_tool_call afterward."
    )
    Args = CustomToolModifyArgs


class CustomToolDeleteArgs(BaseModel):
    name: str = Field(description="the custom tool to delete")


class CustomToolDeleteTool(_CustomMgmtTool):
    name = "custom_tool_delete"
    description = "Delete a custom tool by name."
    Args = CustomToolDeleteArgs


# --- helpers (mirror dispatch.py's contract) ---------------------------------


CUSTOM_MGMT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        CustomToolCreateTool.name,
        CustomToolModifyTool.name,
        CustomToolDeleteTool.name,
    }
)


def is_custom_mgmt_tool(tool_name: str) -> bool:
    return tool_name in CUSTOM_MGMT_TOOL_NAMES


def custom_mgmt_tools() -> list[Tool]:
    """The orchestrator-only custom-tool management tools, instantiated. Appended
    to the orchestrator's catalog (NOT registered in `default_registry`, so
    subagents and modes a/b never see them); intercepted, never `run()`."""
    return [CustomToolCreateTool(), CustomToolModifyTool(), CustomToolDeleteTool()]
