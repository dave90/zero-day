"""ClaudeCodeBackend — Mode A and Mode B.

Mode A shells out per turn:

    claude -p "$PROMPT" --output-format json --max-turns 1

The prompt is the system prompt + serialized history + tool catalog.

Mode B (M8) runs `claude` once per session with an MCP server registered
(`--mcp-config`) and the built-in WebSearch/WebFetch tools disabled. The
MCP server (zero.llm.mcp_server) calls back to the orchestrator's bridge
so the ApprovalGate / audit / UI loop participates exactly as in Mode A.
Mode B is driven by ClaudeCodeBackendModeB (separate class) because it
isn't turn-based at the LLM level — the orchestrator hands the agent
control to claude until it produces a final answer.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse, Message


# claude's built-in tools that we must disable in Mode A: we route every
# action through our envelope, but if any built-in is left enabled, claude
# may decide to call ITS tool — and each tool-use cycle counts against
# `--max-turns`, so even a single internal Read/Bash/WebFetch triggers
# `error_max_turns` and exits non-zero. Disabling them forces claude to
# respond with text only, which is exactly what our envelope protocol
# wants.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell",
    "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "Task", "TodoWrite", "NotebookEdit",
)


@dataclass
class ClaudeCodeConfig:
    binary: str = "claude"
    # 10 leaves headroom for claude's internal turn accounting (cache
    # lookups, system messages, retries). We expect exactly one envelope
    # per call, but the internal turn counter inflates fast on long
    # prompts; reports of `error_max_turns` came in even at 5. With
    # built-in tools disabled (see DEFAULT_DISALLOWED_TOOLS) the extra
    # turns are never used to execute anything, just bookkeeping.
    max_turns: int = 10
    timeout_seconds: int = 300
    disallowed_tools: tuple[str, ...] = DEFAULT_DISALLOWED_TOOLS
    # On `error_max_turns`, retry once with `max_turns_retry_bonus` extra
    # internal turns before giving up — claude's internal turn counter can
    # trip the cap on long prompts even with built-in tools disabled.
    retry_on_max_turns: bool = True
    max_turns_retry_bonus: int = 20


_NUL_RUN = re.compile("\x00+")


def neutralize_nul(s: str) -> str:
    """Replace raw NUL bytes with a visible, count-preserving marker `[NUL*N]`.

    A string containing `\\x00` raises `ValueError: embedded null byte` the moment
    it's passed to `create_subprocess_exec` (argv elements are NUL-terminated C
    strings — a raw NUL can never be carried there regardless). Tool output can
    contain NULs (binary HTTP bodies, fonts, null-byte reflections) and those flow
    into the next turn's prompt via the transcript, crashing the spawn before the
    turn starts. We don't silently drop them: the marker keeps the *information
    that N null bytes appeared here* — which is itself security-relevant — without
    the raw byte that breaks the spawn."""
    if "\x00" not in s:
        return s
    return _NUL_RUN.sub(lambda m: f"[NUL*{len(m.group())}]", s)


def build_mode_a_argv(
    *,
    binary: str,
    prompt: str,
    max_turns: int,
    disallowed_tools: tuple[str, ...],
) -> list[str]:
    """Pure-function argv assembly — easy to unit test."""
    argv = [
        binary,
        "-p", neutralize_nul(prompt),
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]
    if disallowed_tools:
        argv += ["--disallowedTools", ",".join(disallowed_tools)]
    return argv


class ClaudeCodeBackend(LLMBackend):
    def __init__(self, cfg: ClaudeCodeConfig | None = None) -> None:
        self.cfg = cfg or ClaudeCodeConfig()

    async def run(
        self,
        system_prompt: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        binary = shutil.which(self.cfg.binary)
        if not binary:
            raise FileNotFoundError(
                f"`{self.cfg.binary}` not on PATH; install Claude Code CLI or"
                " run with --backend echo."
            )
        prompt = build_prompt(system_prompt, messages, tools)

        # First attempt at the configured cap. claude's internal turn
        # counter inflates on long prompts and can trip `error_max_turns`
        # even though we never let it execute a tool — so on that specific
        # failure, retry ONCE with extra headroom rather than crashing the
        # whole turn (which, for the validator, would park a finding as
        # pending for no good reason). Any other error fails fast.
        caps = [self.cfg.max_turns]
        if self.cfg.retry_on_max_turns:
            caps.append(self.cfg.max_turns + self.cfg.max_turns_retry_bonus)
        last_error: str | None = None
        for attempt, max_turns in enumerate(caps):
            out_text, returncode, err = await self._run_once(binary, prompt, max_turns)
            if returncode == 0:
                return parse_claude_json(out_text)
            # claude often reports structured errors on stdout even with
            # exit != 0. Extract a tight one-liner instead of dumping the
            # full JSON blob (which can be 1KB+).
            detail = _extract_claude_error(out_text) or err or "(no output)"
            last_error = f"claude -p exit {returncode}: {detail}"
            is_max_turns = "error_max_turns" in detail
            if is_max_turns and attempt + 1 < len(caps):
                continue  # retry with more turns
            raise RuntimeError(last_error)
        # Unreachable (the loop always returns or raises), but keeps types happy.
        raise RuntimeError(last_error or "claude -p failed")

    async def _run_once(
        self, binary: str, prompt: str, max_turns: int
    ) -> tuple[str, int | None, str]:
        """Run one `claude -p` invocation. Returns (stdout, returncode, stderr)."""
        argv = build_mode_a_argv(
            binary=binary,
            prompt=prompt,
            max_turns=max_turns,
            disallowed_tools=self.cfg.disallowed_tools,
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.cfg.timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise TimeoutError(f"claude -p timed out after {self.cfg.timeout_seconds}s")
        return (
            stdout.decode("utf-8", errors="replace").strip(),
            proc.returncode,
            stderr.decode("utf-8", errors="replace").strip(),
        )


def _extract_claude_error(stdout: str) -> str:
    """Reduce a claude error JSON to a short human-readable string.

    Example input:
      {"type":"result","subtype":"error_max_turns","duration_ms":8544,
       "is_error":true,"num_turns":2,"stop_reason":"tool_use",...}

    Returns: "error_max_turns (stop=tool_use, num_turns=2)"

    If `stdout` isn't a recognized error JSON, returns "" so the caller
    falls back to stderr.
    """
    if not stdout:
        return ""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    if not isinstance(obj, dict) or not obj.get("is_error"):
        return ""
    subtype = obj.get("subtype", "error")
    stop = obj.get("stop_reason", "")
    turns = obj.get("num_turns", "")
    bits = [subtype]
    if stop:
        bits.append(f"stop={stop}")
    if turns:
        bits.append(f"num_turns={turns}")
    return f"{bits[0]} (" + ", ".join(bits[1:]) + ")" if len(bits) > 1 else bits[0]


def parse_claude_json(stdout: str) -> LLMResponse:
    """Parse `claude -p --output-format json` output."""
    try:
        obj = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"claude returned non-JSON: {e}: {stdout!r}")

    # If claude reported a structured error (is_error=true), don't try
    # to interpret it as a response — surface it as a clean error so the
    # router can park the finding with a meaningful reason.
    if isinstance(obj, dict) and obj.get("is_error"):
        detail = _extract_claude_error(stdout) or "unknown error"
        raise RuntimeError(f"claude error: {detail}")

    # The CLI's JSON shape historically has `result` or `text` plus a usage block.
    text: str = ""
    for key in ("result", "text", "content", "completion"):
        v = obj.get(key)
        if isinstance(v, str) and v:
            text = v
            break
    if not text:
        raise RuntimeError(f"could not find response text in claude output: {obj!r}")
    usage = obj.get("usage") or {}
    return LLMResponse(text=text, usage=usage if isinstance(usage, dict) else {}, raw=obj)


def build_prompt(
    system_prompt: str,
    messages: list[Message],
    tools: list[dict[str, Any]],
) -> str:
    """Concatenate system + tool catalog + history into a single prompt string."""
    parts = [system_prompt.rstrip(), "", "## Available tools", _format_tools(tools), ""]
    if messages:
        parts.append("## Conversation")
        for m in messages:
            parts.append(f"[{m.role}] {m.content}")
        parts.append("")
    parts.append("Respond with exactly one envelope JSON object now.")
    return "\n".join(parts)


def _format_tools(tools: list[dict[str, Any]]) -> str:
    lines = []
    for t in tools:
        lines.append(
            f"- {t['name']} (risk={t['risk_level']}): {t['description']}"
        )
        schema = t.get("schema")
        if schema:
            lines.append(f"  args schema: {json.dumps(schema, separators=(',', ':'))}")
    return "\n".join(lines) if lines else "(no tools available)"


# ----------------------------------------------------------------- Mode B


@dataclass
class ModeBConfig:
    binary: str = "claude"
    timeout_seconds: int = 3600
    disallowed_builtin_tools: tuple[str, ...] = ("WebSearch", "WebFetch")


def build_mcp_config(
    *, bridge_url: str, auth_token: str, server_module: str = "zero.llm.mcp_server"
) -> dict[str, Any]:
    """Construct the JSON that `claude --mcp-config` consumes.

    See Anthropic's MCP docs: each server entry is a `command` + `args` +
    optional `env`. We launch ourselves via `python -m zero.llm.mcp_server`.
    """
    return {
        "mcpServers": {
            "zero": {
                "command": sys.executable,
                "args": ["-m", server_module],
                "env": {
                    "ZERO_MCP_BRIDGE_URL": bridge_url,
                    "ZERO_MCP_AUTH_TOKEN": auth_token,
                },
            }
        }
    }


def build_mode_b_argv(
    *,
    binary: str,
    prompt: str,
    mcp_config_path: Path,
    disallowed_builtin_tools: tuple[str, ...],
) -> list[str]:
    """Pure-function assembly of the Mode B `claude` argv."""
    argv = [
        binary,
        "-p", neutralize_nul(prompt),
        "--output-format", "json",
        "--mcp-config", str(mcp_config_path),
    ]
    if disallowed_builtin_tools:
        argv += ["--disallowedTools", ",".join(disallowed_builtin_tools)]
    return argv


class ClaudeCodeBackendModeB:
    """Drive `claude` in delegated mode.

    Unlike Mode A this isn't an LLMBackend — the agent loop isn't
    turn-based here. The orchestrator hands the goal + project context to
    `run_session()` once; we launch claude, claude drives the conversation
    autonomously through MCP tool calls (which round-trip through our
    bridge), and we return the final answer.
    """

    def __init__(self, cfg: ModeBConfig | None = None) -> None:
        self.cfg = cfg or ModeBConfig()

    async def run_session(
        self,
        *,
        prompt: str,
        bridge_url: str,
        auth_token: str = "",
    ) -> str:
        binary = shutil.which(self.cfg.binary)
        if not binary:
            raise FileNotFoundError(
                f"`{self.cfg.binary}` not on PATH; required for Mode B"
            )
        mcp_cfg = build_mcp_config(bridge_url=bridge_url, auth_token=auth_token)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(mcp_cfg, fh)
            cfg_path = Path(fh.name)
        try:
            argv = build_mode_b_argv(
                binary=binary,
                prompt=prompt,
                mcp_config_path=cfg_path,
                disallowed_builtin_tools=self.cfg.disallowed_builtin_tools,
            )
            env = {**os.environ}
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cfg.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(
                    f"claude (Mode B) timed out after {self.cfg.timeout_seconds}s"
                )
            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"claude (Mode B) exit {proc.returncode}: {err.strip()}"
                )
            resp = parse_claude_json(stdout.decode("utf-8", errors="replace"))
            return resp.text
        finally:
            try:
                cfg_path.unlink()
            except OSError:
                pass
