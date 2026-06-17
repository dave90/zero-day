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
# `--max-turns`, so even a single internal Read/Bash/WebFetch/LSP triggers
# `error_max_turns` and exits non-zero. Disabling them forces claude to
# respond with text only, which is exactly what our envelope protocol wants.
#
# This list is belt-and-suspenders BEHIND the empty `--allowedTools ""` whitelist
# (see build_mode_a_argv): the whitelist is the primary disable, this denylist
# names any built-in that must NEVER be callable even if the whitelist regresses.
# The list MUST stay exhaustive w.r.t. the installed Claude Code build — the real
# Guild bug was a subagent that fell back to the ONE built-in we'd forgotten here
# (`LSP`), called it, tripped `error_max_turns (stop=tool_use)`, and confabulated
# "the pentest tooling isn't wired up". Every name below is a current Claude Code
# built-in; add new ones as the CLI grows (the unit-test guard asserts the
# load-bearing ones are present). Pass ONLY valid names — a bogus name makes
# `claude` exit non-zero with empty stdout.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash", "BashOutput", "KillShell",
    "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch",
    "Task", "TodoWrite", "NotebookEdit",
    # Newer built-ins the original list missed — `LSP` is the one that bit us.
    "LSP", "ToolSearch", "Monitor", "Skill", "SlashCommand",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "PushNotification", "RemoteTrigger", "DesignSync", "Agent",
)


# `claude -p` runs Claude Code with ITS OWN system prompt ("You are Claude Code…"
# + your cwd/git/CLAUDE.md), so a subagent launched from inside this repo read the
# zero dev CLAUDE.md (walked up from the engagement dir) and concluded it was "a
# Claude Code dev session over the zero repo" — refusing to act as a pentester and
# claiming its only tools were Skill/Workflow/Read. We fix the identity leak by:
#   * --system-prompt  : REPLACE Claude Code's identity with OUR agent prompt (this
#                        also drops the default dynamic cwd/env/git/memory sections),
#   * --setting-sources: load USER settings only, dropping project/local so the
#                        repo's CLAUDE.md is never auto-discovered for the agent.
# (We avoid `--bare`, which would force ANTHROPIC_API_KEY and break OAuth auth.)
_DEFAULT_SETTING_SOURCES = "user"


@dataclass
class ClaudeCodeConfig:
    binary: str = "claude"
    # Pin the model for every `claude -p` call (alias like 'opus'/'sonnet' or a full
    # id like 'claude-opus-4-8[1m]' for the 1M-context variant). None ⇒ use the
    # account default. Set by the orchestrator from --model.
    model: str | None = None
    # Which Claude Code setting sources to load. "user" drops project/local so the
    # engagement repo's CLAUDE.md doesn't leak into the agent's context.
    setting_sources: str = _DEFAULT_SETTING_SOURCES
    # Mode A is an ENVELOPE protocol: the agent must NOT call any real tool — it
    # emits one JSON envelope naming one of OUR pentest tools (text), which the
    # orchestrator then executes. But `claude -p` otherwise exposes its full
    # harness (Bash/Read/Workflow/Skill/ToolSearch/Cron/Task/Monitor/LSP) AND the
    # user's MCP servers (Google, …). A capable model then tries to CALL those,
    # finds our pentest tools aren't real callable tools, and concludes "the
    # pentest tools aren't wired in" — refusing to proceed (real Guild bug). So we
    # strip EVERY real tool: --allowedTools "" (empty whitelist ⇒ grant ZERO tools),
    # --strict-mcp-config (no MCP servers, since Mode A passes no --mcp-config),
    # --disable-slash-commands (no skills), plus the DEFAULT_DISALLOWED_TOOLS denylist
    # as a backstop. NOTE: an empty `--tools ""` does NOT work — `--tools <tools...>`
    # is a greedy variadic and the empty value leaves Claude Code's defaults enabled
    # (that was the bug: subagents still saw LSP and called it). The empty *whitelist*
    # `--allowedTools ""` is the reliable disable. With zero tools the model can only
    # respond with text == our envelope.
    lock_down_tools: bool = True
    # claude's INTERNAL agentic-turn cap for one `claude -p` call. We drive the
    # multi-turn loop OURSELVES (one envelope per call), so the model never needs an
    # internal loop. We keep a SMALL budget rather than 1: under lock-down no tool is
    # callable, but a capable model on a large/insistent prompt can still EMIT a stray
    # native tool_use attempt — with `--max-turns 1` that single attempt instantly
    # trips `error_max_turns (stop=tool_use)` and fails the whole call (the live-
    # reproduced Guild bug). A few turns of headroom let that rare stray attempt fail
    # harmlessly (the tool isn't callable) and the model recover to a text envelope on
    # the next internal step. The old `10` was a pre-lock-down workaround for when real
    # tools WERE present and consumed internal turns.
    max_turns: int = 4
    timeout_seconds: int = 300
    disallowed_tools: tuple[str, ...] = DEFAULT_DISALLOWED_TOOLS
    # On `error_max_turns`, retry once with `max_turns_retry_bonus` extra internal
    # turns before giving up. Kept SMALL (was 20): a big bonus would reopen the
    # internal agentic loop on the retry — the very thing that lets the model drift
    # into a native tool call and confabulate "tools unavailable". 1→3 is enough to
    # clear a transient accounting trip without giving it room to loop.
    retry_on_max_turns: bool = True
    max_turns_retry_bonus: int = 2


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
    max_turns: int,
    disallowed_tools: tuple[str, ...],
    system_prompt_path: str = "",
    setting_sources: str = "",
    lock_down_tools: bool = False,
    model: str = "",
) -> list[str]:
    """Pure-function argv assembly — easy to unit test.

    The two BIG strings — the conversation/tools body and the system prompt — are
    deliberately NOT argv elements:
      * the body is fed to claude on STDIN (`--print` with no positional prompt), and
      * the system prompt is written to a temp file passed via `--system-prompt-file`.
    This is required for Windows: argv there goes through `CreateProcess`, whose total
    command line is capped at 32,767 chars — and our body (tool catalog ~18 KB + a
    growing transcript) blows past that within a few turns, raising
    `[WinError 206] The filename or extension is too long`. POSIX has a much larger
    ARG_MAX so it only ever bit Windows, but stdin/file keeps the command line tiny on
    every platform. `--system-prompt-file` still REPLACES Claude Code's own identity so
    the agent IS our pentester/validator. `setting_sources` (e.g. "user") drops
    project/local settings so the repo's CLAUDE.md doesn't leak. `lock_down_tools`
    strips every real tool via an empty `--allowedTools ""` whitelist (+ no MCP + no
    skills + a denylist backstop) — see ClaudeCodeConfig.lock_down_tools (and why
    `--tools ""` does NOT work)."""
    argv = [
        binary,
        "--print",  # non-interactive; the prompt body arrives on STDIN, not argv
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]
    if model:
        argv += ["--model", model]
    if system_prompt_path:
        argv += ["--system-prompt-file", system_prompt_path]
    if setting_sources:
        argv += ["--setting-sources", setting_sources]
    if lock_down_tools:
        # No real tools at all → the only thing the model can do is reply with text,
        # which is exactly our envelope. `--allowedTools ""` is an EMPTY WHITELIST
        # (grant zero tools) — the reliable disable; `--tools ""` does NOT work (the
        # empty value to the greedy variadic flag leaves the defaults enabled, so the
        # model still saw/called LSP — the Guild bug). Then no MCP servers, no skills.
        argv += ["--allowedTools", "", "--strict-mcp-config", "--disable-slash-commands"]
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
        # Our agent prompt goes to --system-prompt (see _DEFAULT_SETTING_SOURCES note);
        # the `-p` body carries only the dynamic tools + conversation.
        prompt = build_user_prompt(messages, tools)

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
            out_text, returncode, err = await self._run_once(
                binary, prompt, max_turns, system_prompt
            )
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
        self, binary: str, prompt: str, max_turns: int, system_prompt: str = ""
    ) -> tuple[str, int | None, str]:
        """Run one `claude -p` invocation. Returns (stdout, returncode, stderr).

        The big strings are kept OFF the command line (Windows `CreateProcess` caps it
        at 32,767 chars → `[WinError 206]`): the system prompt goes to a temp file
        (`--system-prompt-file`) and the conversation body is piped on STDIN."""
        sp_path = ""
        if system_prompt:
            # Temp file deleted in `finally`. NamedTemporaryFile(delete=False) so the
            # child process can open it by name on Windows (can't share an open handle).
            with tempfile.NamedTemporaryFile(
                "w", suffix=".md", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(neutralize_nul(system_prompt))
                sp_path = fh.name
        try:
            argv = build_mode_a_argv(
                binary=binary,
                max_turns=max_turns,
                disallowed_tools=self.cfg.disallowed_tools,
                system_prompt_path=sp_path,
                setting_sources=self.cfg.setting_sources,
                lock_down_tools=self.cfg.lock_down_tools,
                model=self.cfg.model or "",
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_bytes = neutralize_nul(prompt).encode("utf-8")
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=self.cfg.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(
                    f"claude -p timed out after {self.cfg.timeout_seconds}s"
                )
            return (
                stdout.decode("utf-8", errors="replace").strip(),
                proc.returncode,
                stderr.decode("utf-8", errors="replace").strip(),
            )
        finally:
            if sp_path:
                try:
                    os.unlink(sp_path)
                except OSError:
                    pass


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


def build_user_prompt(
    messages: list[Message],
    tools: list[dict[str, Any]],
) -> str:
    """The `-p` body: tool catalog + history (no system prompt — that goes to
    `--system-prompt` so the agent's identity isn't a mere user message under Claude
    Code's own system prompt)."""
    parts = ["## Available tools", _format_tools(tools), ""]
    if messages:
        parts.append("## Conversation")
        for m in messages:
            parts.append(f"[{m.role}] {m.content}")
        parts.append("")
    parts.append("Respond with exactly one envelope JSON object now.")
    return "\n".join(parts)


def build_prompt(
    system_prompt: str,
    messages: list[Message],
    tools: list[dict[str, Any]],
) -> str:
    """Concatenate system + tool catalog + history into a single prompt string.

    Retained for the live e2e tests; the live `run()` path now sends the system
    prompt via `--system-prompt` and uses `build_user_prompt` for the `-p` body."""
    parts = [system_prompt.rstrip(), "", build_user_prompt(messages, tools)]
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
    model: str | None = None  # pin the model (e.g. 'claude-opus-4-8[1m]'); None ⇒ default


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
    mcp_config_path: Path,
    disallowed_builtin_tools: tuple[str, ...],
    model: str = "",
) -> list[str]:
    """Pure-function assembly of the Mode B `claude` argv.

    Like Mode A, the prompt is fed on STDIN (`--print`, no positional prompt) rather
    than as an argv element, so a large goal+project-context body can't overflow the
    Windows `CreateProcess` command-line limit (`[WinError 206]`)."""
    argv = [
        binary,
        "--print",  # prompt arrives on STDIN
        "--output-format", "json",
        "--mcp-config", str(mcp_config_path),
    ]
    if model:
        argv += ["--model", model]
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
                mcp_config_path=cfg_path,
                disallowed_builtin_tools=self.cfg.disallowed_builtin_tools,
                model=self.cfg.model or "",
            )
            env = {**os.environ}
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=neutralize_nul(prompt).encode("utf-8")),
                    timeout=self.cfg.timeout_seconds,
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
