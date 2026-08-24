"""ClaudeCodeBackend — Mode A and Mode B.

Mode A shells out per turn:

    claude -p "$PROMPT" --output-format stream-json --verbose --max-turns 1

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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zero.llm.backend import (
    LLMBackend,
    LLMResponse,
    Message,
    Session,
    context_tokens_from_usage,
)
from zero.llm.llm_trace import trace_call


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
    # NB: NOT "SlashCommand" — that's not a real tool name (slash commands are killed by
    # the `--disable-slash-commands` flag), and listing it makes claude warn on EVERY call
    # `Permission deny rule "SlashCommand" matches no known tool`. Only real built-ins here.
    "LSP", "ToolSearch", "Monitor", "Skill",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "CronCreate", "CronDelete", "CronList",
    "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "PushNotification", "RemoteTrigger", "DesignSync", "Agent",
    # Newer agent-harness built-ins (Claude Code 2.1.x). `ScheduleWakeup`/`SendMessage`/
    # `Workflow` were the ones a child `claude` (spawned from inside a Claude Code
    # session) fell back to when the parent agent runtime leaked in — the model reported
    # them as "the only executable tools", couldn't reach our pentest envelope tools, and
    # gave up on turn 1 (the juice-shop regression failure). Scrubbing the parent env
    # (see scrub_claude_code_env) removes the leak; these keep the denylist exhaustive.
    "ScheduleWakeup", "SendMessage", "Workflow",
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
    # id like 'claude-opus-5[1m]' for the 1M-context variant). None ⇒ use the
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
    # Native-tool-drift RE-STEER budget. Under lock-down the model sometimes reaches for a
    # NATIVE tool (Read/Grep/Bash), hits "No such tool available", and confabulates a
    # giving-up final instead of emitting our envelope (worst on source-review prompts —
    # the Swiss Post run burned dozens of subagent turns this way). Rather than fail the
    # whole turn, the backend RESUMES the same claude session with a terse "envelope only"
    # correction up to this many times, so the model — now seeing its own failed attempt —
    # recovers to a real envelope. 0 ⇒ raise immediately (the old, lossy behavior).
    max_native_resteers: int = 2


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


# Env vars that mark a process as running *inside* a Claude Code agent session. When we
# spawn `claude -p` from within one (e.g. running the regression suite from a Claude Code
# session, or any nested invocation), the child INHERITS these and attaches to the parent
# agent runtime — picking up the parent's harness tools (ScheduleWakeup/SendMessage/
# Workflow/…) over an SSE/MCP side channel that our `--allowedTools ""` lockdown and
# `--system-prompt-file` override do NOT cover. The orchestrator then sees Claude Code's
# tools instead of our pentest envelope and bails on turn 1 (the juice-shop regression
# failure: only the FIRST engagement attached, the rest ran clean). Scrubbing them makes
# every spawned `claude` hermetic — identical behaviour whether launched from a bare
# terminal or from inside Claude Code. We drop the whole CLAUDE_CODE_*/CLAUDECODE family
# (incl. SSE_PORT, CHILD_SESSION, ENTRYPOINT, SSE/MCP handles) plus the generic AI_AGENT
# marker; ANTHROPIC_*/auth vars are deliberately KEPT so OAuth/subscription auth still works.
_CLAUDE_CODE_ENV_PREFIXES = ("CLAUDE_CODE_",)
_CLAUDE_CODE_ENV_EXACT = frozenset({"CLAUDECODE", "AI_AGENT"})


def scrub_claude_code_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `env` (default os.environ) with the Claude Code agent-session
    markers removed, so a spawned `claude -p` cannot attach to a parent agent runtime."""
    src = os.environ if env is None else env
    return {
        k: v for k, v in src.items()
        if k not in _CLAUDE_CODE_ENV_EXACT
        and not any(k.startswith(p) for p in _CLAUDE_CODE_ENV_PREFIXES)
    }


def close_proc_transport(proc) -> None:
    """Explicitly close a finished subprocess's transport while the loop is still alive.

    Otherwise the transport can be garbage-collected AFTER its event loop has closed —
    `BaseSubprocessTransport.__del__` then calls `loop.call_soon` on the dead loop and
    raises `RuntimeError: Event loop is closed`. It's harmless (the process already
    exited) but surfaces as a noisy PytestUnraisableExceptionWarning under the regression
    suite, which runs hundreds of `claude` subprocesses across many short-lived per-test
    loops. Idempotent and never raises."""
    if proc is None:
        return
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass


def build_mode_a_argv(
    *,
    binary: str,
    max_turns: int,
    disallowed_tools: tuple[str, ...],
    system_prompt_path: str = "",
    setting_sources: str = "",
    lock_down_tools: bool = False,
    model: str = "",
    session_id: str = "",
    session_name: str = "",
    resume: bool = False,
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
        # Complete assistant messages carry per-request usage, and the JSONL stream
        # also exposes `system/compact_boundary`. The one-shot JSON result only has
        # cumulative usage for the whole invocation, which is wrong after compaction.
        "--output-format", "stream-json",
        "--verbose",
        "--max-turns", str(max_turns),
    ]
    if model:
        argv += ["--model", model]
    # Persistent-session flags. On `resume` we CONTINUE an existing claude session
    # (`--resume <uuid>`) and send only the new conversation delta on stdin — the
    # system prompt, settings, and tool catalog were all fixed on the create turn and
    # live in the session, so they are deliberately NOT re-sent here. On create we
    # pin the session id (`--session-id <uuid>`, must be a valid UUID) and pass the
    # identity/settings; `-n` is a human-readable label for debugging only.
    if resume and session_id:
        argv += ["--resume", session_id]
    elif session_id:
        argv += ["--session-id", session_id]
        if session_name:
            argv += ["-n", session_name]
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
        *,
        session: Session | None = None,
    ) -> LLMResponse:
        binary = shutil.which(self.cfg.binary)
        if not binary:
            raise FileNotFoundError(
                f"`{self.cfg.binary}` not on PATH; install Claude Code CLI or"
                " run with --backend echo."
            )
        # Session mode: when continuing an existing claude session (`--resume`),
        # `messages` is only the NEW delta, the system prompt + tool catalog are
        # already in the session, so we omit both (no `--system-prompt-file`, empty
        # tools ⇒ build_user_prompt drops the catalog). On the create turn (or with no
        # session at all) we send the full body + identity, pinning `--session-id`.
        resuming = session is not None and session.started
        started_as_create = session is not None and not resuming
        body_tools = [] if resuming else tools
        sys_for_call = "" if resuming else system_prompt
        prompt = build_user_prompt(messages, body_tools)

        # Native-tool-drift RE-STEER loop. Under lock-down the model sometimes tries to
        # call a tool NATIVELY (Claude Code's own Read/Grep/Bash), hits "No such tool
        # available", and confabulates a giving-up final instead of emitting our envelope —
        # worst on source-review prompts, which is exactly where it bit us. Instead of
        # failing the whole turn, we re-steer IN-BACKEND: RESUME the same claude session
        # with a terse "envelope only" correction so the model — now seeing its own failed
        # native attempt — converts to a real envelope. Bounded by `max_native_resteers`;
        # only when that is exhausted do we surface the retryable error to the agent loop.
        for resteer in range(self.cfg.max_native_resteers + 1):
            resp, drift, detail = await self._run_turn(
                binary, prompt, sys_for_call, session, resuming
            )
            if resp is not None:
                if session is not None:
                    session.started = True  # this call created/continued the session
                return resp
            if not drift:
                raise RuntimeError(detail)  # a hard (non-drift) error — surface as-is
            if resteer < self.cfg.max_native_resteers:
                # The drifting call already created/continued the session in claude, so
                # resume IT with only the correction delta (drop system prompt + catalog);
                # the model sees its own failed native attempt plus the fix. Sessionless
                # backends can't resume, so re-send the full body with the correction.
                if session is not None:
                    session.started = True
                    resuming = True
                    sys_for_call = ""
                    prompt = build_user_prompt(
                        [Message(role="user", content=_RESTEER_BODY)], []
                    )
                else:
                    prompt = build_user_prompt(
                        [*messages, Message(role="user", content=_RESTEER_BODY)], body_tools
                    )
                continue
            # Re-steers exhausted: void a create's claimed session id so the loop's NEXT
            # attempt is a clean create (a resume keeps its id), then surface the drift as
            # a retryable error naming the fix.
            if started_as_create and session is not None:
                session.id = str(uuid.uuid4())
                session.started = False
            raise RuntimeError(_DRIFT_ERROR_MSG)
        # Unreachable — the loop always returns or raises.
        raise RuntimeError(_DRIFT_ERROR_MSG)

    async def _run_turn(
        self,
        binary: str,
        prompt: str,
        sys_for_call: str,
        session: Session | None,
        resuming: bool,
    ) -> tuple[LLMResponse | None, bool, str | None]:
        """One logical LLM turn: the `claude -p` call plus the `error_max_turns` headroom
        ladder (claude's internal turn counter inflates on long prompts and can trip the
        cap even though we never let it execute a tool — so we retry once with more turns).

        Returns (resp, drift, detail):
          * (LLMResponse, False, None) — a clean answer to return
          * (None, True, reason)       — native-tool drift under lock-down (re-steerable)
          * (None, False, error)       — a hard error to surface
        """
        caps = [self.cfg.max_turns]
        if self.cfg.retry_on_max_turns:
            caps.append(self.cfg.max_turns + self.cfg.max_turns_retry_bonus)
        last_error: str | None = None
        for attempt, max_turns in enumerate(caps):
            out_text, returncode, err = await self._run_once(
                binary, prompt, max_turns, sys_for_call, session=session, resume=resuming
            )
            if returncode == 0:
                resp = parse_claude_stream_json(out_text)
                if self.cfg.lock_down_tools and _is_native_tool_drift(resp):
                    return None, True, "native tool_use under lock-down"
                return resp, False, None
            # claude often reports structured errors on stdout even with exit != 0.
            # Extract a tight one-liner instead of dumping the full JSON blob (1KB+).
            detail = _extract_claude_error(out_text) or err or "(no output)"
            last_error = f"claude -p exit {returncode}: {detail}"
            if "error_max_turns" in detail and attempt + 1 < len(caps):
                # A create that errored ALREADY claimed its `--session-id`, so retrying
                # with the same id fails "Session ID already in use". Give it a FRESH id
                # (stdin still carries the full seed) for a clean create. (Resume keeps id.)
                if session is not None and not resuming:
                    session.id = str(uuid.uuid4())
                continue  # retry with more turns
            break
        return None, False, last_error

    async def _run_once(
        self,
        binary: str,
        prompt: str,
        max_turns: int,
        system_prompt: str = "",
        *,
        session: Session | None = None,
        resume: bool = False,
    ) -> tuple[str, int | None, str]:
        """Run one `claude -p` invocation. Returns (stdout, returncode, stderr).

        The big strings are kept OFF the command line (Windows `CreateProcess` caps it
        at 32,767 chars → `[WinError 206]`): the system prompt goes to a temp file
        (`--system-prompt-file`) and the conversation body is piped on STDIN."""
        sp_path = ""
        proc = None
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
                # On resume the session already carries the settings; only pass
                # --setting-sources on the create turn (alongside --system-prompt-file).
                setting_sources="" if resume else self.cfg.setting_sources,
                lock_down_tools=self.cfg.lock_down_tools,
                model=self.cfg.model or "",
                session_id=session.id if session else "",
                session_name=session.name if session else "",
                resume=resume,
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=scrub_claude_code_env(),  # hermetic: no parent Claude Code agent attach
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
            out_s = stdout.decode("utf-8", errors="replace").strip()
            err_s = stderr.decode("utf-8", errors="replace").strip()
            # Full per-actor I/O trace (no-op unless --llm-log / ZERO_LLM_LOG_DIR).
            trace_call(
                session.name if session else "default",
                argv=argv, system_prompt=system_prompt, stdin_body=prompt,
                stdout=out_s, stderr=err_s, returncode=proc.returncode, resume=resume,
            )
            return (out_s, proc.returncode, err_s)
        finally:
            close_proc_transport(proc)  # avoid "Event loop is closed" on later GC
            if sp_path:
                try:
                    os.unlink(sp_path)
                except OSError:
                    pass


def _extract_claude_error(stdout: str) -> str:
    """Reduce a claude error JSON/JSONL stream to a short human-readable string.

    Example input:
      {"type":"result","subtype":"error_max_turns","duration_ms":8544,
       "is_error":true,"num_turns":2,"stop_reason":"tool_use",...}

    Returns: "error_max_turns (stop=tool_use, num_turns=2)"

    If `stdout` contains no recognized error object, returns "" so the caller
    falls back to stderr.
    """
    if not stdout:
        return ""
    errors = [obj for obj in _iter_claude_json(stdout) if obj.get("is_error")]
    if not errors:
        return ""
    obj = errors[-1]
    subtype = obj.get("subtype", "error")
    stop = obj.get("stop_reason", "")
    turns = obj.get("num_turns", "")
    bits = [subtype]
    if stop:
        bits.append(f"stop={stop}")
    if turns:
        bits.append(f"num_turns={turns}")
    return f"{bits[0]} (" + ", ".join(bits[1:]) + ")" if len(bits) > 1 else bits[0]


def _iter_claude_json(stdout: str):
    """Yield object lines from Claude's compact JSON or stream-JSON output."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


# Claude Code's own message when the model emits a native `tool_use` for a tool that
# isn't in the (empty) allow-list. Under our lock-down the model should NEVER call a
# tool natively — it must emit a JSON envelope naming one of OUR tools. When it drifts
# back into native tool_use it loops on this message and, given a few internal turns of
# headroom, "recovers" to a giving-up TEXT final ("total tooling outage — every tool
# returns 'No such tool available'"). That final has is_error=false, so it's returned as
# a normal answer and accepted — wasting the turn AND (in Ralph mode) poisoning
# progress.txt so later iterations give up too. We detect the signature and raise it as a
# retryable error so the agent loop re-steers the model back to the envelope.
_NATIVE_DRIFT_MARKER = "no such tool"


def looks_like_native_tool_drift(text: str) -> bool:
    """True if a lock-down response is actually the model confabulating about native
    tools being unavailable (Claude's built-in 'No such tool available' message) rather
    than emitting our envelope."""
    return _NATIVE_DRIFT_MARKER in (text or "").lower()


def _is_native_tool_drift(resp: LLMResponse) -> bool:
    """True when a lock-down turn 'succeeded' but is really native-tool drift: the model
    either quoted Claude's built-in 'No such tool available' (the confabulated giving-up
    final) OR the result carries a native `tool_use` stop_reason (the structured,
    language-independent signal that it reached for a native tool). Either way the turn
    must be re-steered to the envelope, not accepted as a real answer."""
    if looks_like_native_tool_drift(resp.text):
        return True
    raw = resp.raw or {}
    return raw.get("stop_reason") == "tool_use"


# The correction we RESUME the drifting claude session with: it sees its own failed native
# attempt, then this terse "envelope only" nudge, and converts to a real envelope. Kept
# short because it's a resume delta (the system prompt + tool catalog already live in the
# session). Names the common file-review tools since source review is where drift peaks.
_RESTEER_BODY = (
    "STOP — your previous reply tried to call a tool NATIVELY (Claude Code's own "
    "Read/Grep/Glob/Bash). Those tools DO NOT EXIST here; every native call returns "
    "'No such tool available'. The ONLY way to act is to emit EXACTLY ONE JSON envelope "
    "naming one of the tools from your instructions — e.g. "
    '{"type":"action","tool":"grep","args":{"pattern":"...","path":"..."}} to search, '
    '{"type":"action","tool":"read_file","args":{"path":"..."}} to read a file, or '
    '{"type":"final","answer":"..."} to finish. Re-issue your intended action NOW as one '
    "such envelope, with no prose and no native tool call."
)

# Surfaced to the agent loop only after re-steering is exhausted. Keeps the word
# "envelope" so the loop's own re-prompt (and the unit tests) can key off it.
_DRIFT_ERROR_MSG = (
    "claude kept calling tools NATIVELY under lock-down and reported 'no such tool "
    "available' instead of using the envelope, and did not recover after re-steering. Do "
    "NOT make native tool calls: reply with EXACTLY ONE JSON envelope naming one of the "
    "provided tools (or a final), and the runtime will run it."
)


def _numeric_usage(value: Any) -> dict[str, int]:
    """Keep the numeric token counters from a Claude usage object."""
    if not isinstance(value, dict):
        return {}
    return {
        str(key): int(amount)
        for key, amount in value.items()
        if isinstance(amount, (int, float))
    }


def _claude_message_text(message: dict[str, Any]) -> str:
    """Extract text blocks from one complete stream-json assistant message."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _claude_context_window(result: dict[str, Any], model: str) -> int | None:
    """Read the selected model's window from a Claude result when present."""
    model_usage = result.get("modelUsage") or result.get("model_usage")
    if not isinstance(model_usage, dict):
        return None
    entries: list[Any] = []
    if model and model in model_usage:
        entries.append(model_usage[model])
    entries.extend(value for key, value in model_usage.items() if key != model)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("contextWindow") or entry.get("context_window")
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


def parse_claude_stream_json(stdout: str) -> LLMResponse:
    """Parse CLI `stream-json` and select the current, not cumulative, usage.

    Claude's final result usage is cumulative across all internal model steps. A
    complete `assistant` event instead carries the usage for that API request, whose
    input/cache tiers are the live context snapshot. A `compact_boundary` discards any
    earlier candidate so only a following assistant request can repopulate the value.
    """
    objects = list(_iter_claude_json(stdout))
    if not objects:
        raise RuntimeError(f"claude returned non-JSON stream: {stdout!r}")

    result: dict[str, Any] = {}
    assistant_text = ""
    assistant_usage: dict[str, int] = {}
    assistant_model = ""
    assistant_stop_reason = ""
    compacted = False

    for obj in objects:
        if obj.get("is_error"):
            detail = _extract_claude_error(stdout) or "unknown error"
            raise RuntimeError(f"claude error: {detail}")
        msg_type = obj.get("type")
        if msg_type == "system" and obj.get("subtype") == "compact_boundary":
            compacted = True
            # Usage observed before the newest boundary describes the old, large
            # context (or the summarization request), never the replacement context.
            assistant_usage = {}
            assistant_model = ""
            assistant_stop_reason = ""
            continue
        if msg_type == "assistant" and obj.get("parent_tool_use_id") in (None, ""):
            message = obj.get("message")
            if not isinstance(message, dict):
                continue
            usage = _numeric_usage(message.get("usage"))
            if usage:
                assistant_usage = usage
            model = message.get("model")
            if isinstance(model, str):
                assistant_model = model
            stop_reason = message.get("stop_reason")
            if isinstance(stop_reason, str):
                assistant_stop_reason = stop_reason
            text = _claude_message_text(message)
            if text:
                assistant_text = text
            continue
        if msg_type == "result" or any(
            isinstance(obj.get(key), str) for key in ("result", "completion")
        ):
            result = obj

    # The result's clean text remains authoritative; assistant blocks are a fallback.
    text = ""
    for key in ("result", "text", "content", "completion"):
        value = result.get(key)
        if isinstance(value, str) and value:
            text = value
            break
    text = text or assistant_text
    if not text:
        raise RuntimeError(f"could not find response text in claude stream: {stdout[:500]!r}")

    result_usage = _numeric_usage(result.get("usage"))
    if assistant_usage:
        context_tokens = context_tokens_from_usage(assistant_usage)
        usage_source = "assistant"
    elif compacted:
        # Do not fall back to cumulative result usage after a boundary: until another
        # API request tokenizes the replacement context, its exact size is unknown.
        context_tokens = 0
        usage_source = "awaiting_post_compaction"
    else:
        # Compatibility for older/fake CLI output that only emits one result object.
        context_tokens = context_tokens_from_usage(result_usage)
        usage_source = "result_fallback"

    # Keep billing/audit usage separate from context occupancy. The result is
    # cumulative for this CLI invocation; use it when present, while the explicit
    # `context_tokens` above drives the UI from the latest assistant request.
    usage = result_usage or assistant_usage

    raw = dict(result or objects[-1])
    raw["context_compacted"] = compacted
    raw["context_usage_source"] = usage_source
    if assistant_stop_reason and not raw.get("stop_reason"):
        raw["stop_reason"] = assistant_stop_reason
    context_window = _claude_context_window(result, assistant_model)
    return LLMResponse(
        text=text,
        usage=usage,
        raw=raw,
        context_tokens=context_tokens,
        context_window=context_window,
    )


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
    Code's own system prompt).

    `tools` empty ⇒ the `## Available tools` section is omitted entirely. That's the
    session-`--resume` path: the catalog was sent ONCE on the create turn and lives in
    claude's session, so later turns send only the new conversation delta."""
    parts: list[str] = []
    if tools:
        parts += ["## Available tools", _format_tools(tools)]
        # Custom tools are dynamic engagement data (orchestrator-authored scripts in
        # the files-root store), NOT static registry entries — so they're surfaced
        # here, in the same create-turn-only catalog block, rather than baked into
        # `tools`. This is the single render point shared by both backends and every
        # agent, so each freshly-dispatched subagent (and the orchestrator on a fresh
        # rerun) sees the live inventory up front without spending a turn on
        # custom_tool_list. Gated on `if tools:` so it follows the catalog's
        # create-turn-only lifetime (omitted on --resume deltas).
        custom = _format_custom_tools()
        if custom:
            parts.append(custom)
        parts.append("")
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


def _format_custom_tools() -> str:
    """Render the orchestrator-authored custom tools as a `### Custom tools`
    subsection of the catalog, or "" when none exist.

    Read from the files-root store host-side (the LLM loop always runs on the host),
    so this also surfaces tools authored in a PRIOR run on a rerun — the store
    persists under `zero_files/` across runs and survives `zero reset`. Framed
    explicitly as NOT envelope-callable: they're run via `custom_tool_call`, so the
    model doesn't try to emit them as a direct tool action. Wrapped broadly — a bad
    store read must never crash prompt-building."""
    try:
        from zero.tools._customtools import list_tools

        custom = list_tools()
    except Exception:  # noqa: BLE001 - prompt-building must never fail on a store read
        return ""
    if not custom:
        return ""
    lines = [
        "### Custom tools",
        "Reusable tools the orchestrator authored for this engagement. These are NOT"
        " envelope tools — do not name one as an action `tool`. Run one with"
        " `custom_tool_call` (its `args` are delivered to the script as JSON on"
        " stdin, per the usage below); `custom_tool_list` refreshes this list.",
    ]
    lines += [f"- {t['name']}: {t['description']}" for t in custom]
    return "\n".join(lines)


# ----------------------------------------------------------------- Mode B


@dataclass
class ModeBConfig:
    binary: str = "claude"
    timeout_seconds: int = 3600
    disallowed_builtin_tools: tuple[str, ...] = ("WebSearch", "WebFetch")
    model: str | None = None  # pin the model (e.g. 'claude-opus-5[1m]'); None ⇒ default


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
        proc = None
        try:
            argv = build_mode_b_argv(
                binary=binary,
                mcp_config_path=cfg_path,
                disallowed_builtin_tools=self.cfg.disallowed_builtin_tools,
                model=self.cfg.model or "",
            )
            # Scrub Claude Code agent-session markers (see scrub_claude_code_env): a
            # Mode B `claude` spawned from inside a Claude Code session would otherwise
            # attach to the parent agent runtime and pick up its harness tools.
            env = scrub_claude_code_env()
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
            out_s = stdout.decode("utf-8", errors="replace")
            err_s = stderr.decode("utf-8", errors="replace")
            trace_call(
                "modeb", argv=argv, system_prompt="", stdin_body=prompt,
                stdout=out_s, stderr=err_s, returncode=proc.returncode, resume=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"claude (Mode B) exit {proc.returncode}: {err_s.strip()}"
                )
            resp = parse_claude_json(out_s)
            return resp.text
        finally:
            close_proc_transport(proc)
            try:
                cfg_path.unlink()
            except OSError:
                pass
