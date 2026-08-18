"""CodexCLIBackend — drive the OpenAI Codex CLI as a turn-based LLM backend.

Structurally a sibling of `ClaudeCodeBackend` (Mode A): one `codex exec` per turn,
the model replies with exactly one of OUR envelope JSON shapes (parsed downstream by
`envelope.py`), and the orchestrator runs the named pentest tool. The conversation is
held server-side by a persistent Codex *thread* (`codex exec resume <id>`), so after
the create turn we send only the new delta and let Codex cache the prefix — the same
token-saver the Claude backend gets from `--session-id`/`--resume`.

Differences from `claude -p` that shape this file:

  * **Prompt on STDIN.** `codex exec` with no positional PROMPT reads instructions
    from stdin (`Reading prompt from stdin...`). Same Windows win as Claude's
    `--print` (no big string on the command line → no `[WinError 206]`).
  * **Prompt-level instructions.** Codex has no `--system-prompt-file` equivalent. We
    set the supported `instructions` config override for the harness persona, then fold
    the detailed agent identity into the first stdin body on the create turn.
  * **Server-assigned session id.** Unlike Claude's settable `--session-id <uuid>`,
    Codex MINTS the id itself. We scrape it from the `thread.started` JSONL event on
    the create turn and overwrite `session.id` with it, then resume against that id.
  * **Output is JSONL + a last-message file.** `--json` streams events
    (`thread.started` → id, `turn.completed` → usage, `item.completed` → text) and
    `-o/--output-last-message <FILE>` writes the clean final message. We take the
    envelope text from the `-o` file (falling back to the last `agent_message`).
  * **Soft tool lock-down.** Codex's shell tool can't be hard-disabled the way
    Claude's `--allowedTools ""` empty whitelist removes every tool. We run it
    `read-only` + `approval_policy=never` (so a non-interactive turn never blocks)
    and rely on the envelope instruction to keep it text-only. Documented limitation.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zero.llm.backend import LLMBackend, LLMResponse, Message, Session

# Reuse the Claude backend's prompt builder + NUL scrubber verbatim — the body shape
# (tool catalog + `[role] content` transcript + the "respond with one envelope" tail)
# is backend-agnostic, and `neutralize_nul` is required for the same reason here:
# argv/stdin can't carry a raw NUL, and tool output (binary HTTP bodies) flows into
# the transcript.
from zero.llm.claude_code import build_user_prompt, close_proc_transport, neutralize_nul
from zero.llm.llm_trace import trace_call

# Codex's replacement instructions (passed via `-c instructions=…`, the Codex config
# key accepted by current CLI versions). Codex's stock persona ("you are a coding agent
# sharing this workspace") plus our read-only sandbox make it misread the situation as
# "I'm a locked-down coding assistant and these pentest tools aren't real, so I can't
# probe the target" — and FINALIZE on turn 1 without acting (the Codex analog of the
# Guild lock-down bug; it refused a dvwa engagement with "those pentest tools are not
# available … the workspace is read-only and network access is restricted"). Folding a
# preamble into the prompt body was NOT enough — Codex still trusted its own persona.
# Overriding instructions is: with this, Codex emits action envelopes normally.
# Kept to a SINGLE LINE (no leading '[' or '=') so the `-c` value can't be misread as
# TOML and stays tiny on the command line (Windows arg-length safe). It replaces only the
# persona; the detailed per-agent identity still rides in the prompt body. Backend-level
# (not in the shared agent prompts) so it covers EVERY Codex actor and never touches Claude.
_CODEX_INSTRUCTIONS = (
    "You output exactly ONE JSON envelope per turn for an external penetration-testing "
    "harness that executes the tool you name against the live target and returns the "
    "result as the next observation. You run no commands yourself and need no tools, "
    "shell, or network of your own. Your environment being sandboxed, read-only, or "
    "offline is expected and NEVER limits the tools the harness runs for you. Never "
    "refuse, stall, or finalize by claiming the tools are unavailable or the network is "
    "restricted; when unsure, emit an action envelope and read the observation."
)


@dataclass
class CodexCLIConfig:
    binary: str = "codex"
    # Pin the model for every `codex exec` call (e.g. a Codex/GPT model id). None ⇒
    # use Codex's configured default. Set by the orchestrator from --model — note that
    # with --backend codex you must pass a Codex model or omit --model (a Claude id
    # would be rejected by Codex), exactly mirroring ClaudeCodeConfig.model.
    model: str | None = None
    timeout_seconds: int = 300
    # Mode-A-style soft lock-down (Codex can't drop its shell tool entirely): keep it
    # read-only and never let a turn block on an approval prompt. We DON'T pass
    # --strict-config, so these `-c` overrides are tolerated on every subcommand and
    # extra keys are ignored rather than erroring.
    sandbox_mode: str = "read-only"
    approval_policy: str = "never"
    # Engagements run from a scratch dir that usually isn't a git repo; Codex refuses
    # to start outside one unless told it's fine.
    skip_git_repo_check: bool = True


def build_codex_argv(
    *,
    binary: str,
    last_message_path: str,
    sandbox_mode: str = "read-only",
    approval_policy: str = "never",
    skip_git_repo_check: bool = True,
    model: str = "",
    session_id: str = "",
    resume: bool = False,
    instructions: str = "",
) -> list[str]:
    """Pure-function argv assembly — easy to unit test.

    The prompt body is NOT here: it's piped on STDIN (no positional PROMPT), so a
    large tool-catalog+transcript body can never overflow the Windows `CreateProcess`
    command line. Create vs resume:

      create : codex exec [resume <id>]? --json [-m M] -c sandbox_mode=… \
               -c approval_policy=… --skip-git-repo-check -o <file>
      resume : codex exec resume <session_id> …same flags…   (NOTE: `exec resume` has
               no `-s/--sandbox` flag, so sandbox MUST be set via `-c sandbox_mode=…`,
               which is why we use `-c` for both create and resume rather than `-s`.)
    """
    argv = [binary, "exec"]
    if resume and session_id:
        argv += ["resume", session_id]
    argv += ["--json"]
    if model:
        argv += ["--model", model]
    # `-c key=value` is the universal config override. The value is parsed as TOML and
    # falls back to a literal string when that fails, so a bare `read-only`/`never`
    # (which isn't valid TOML) is taken as the string we want — no quoting needed once
    # it's a single argv element.
    if sandbox_mode:
        argv += ["-c", f"sandbox_mode={sandbox_mode}"]
    if approval_policy:
        argv += ["-c", f"approval_policy={approval_policy}"]
    # Replace Codex's coding-agent persona with our harness framing (the analog of
    # Claude's --system-prompt) so Codex doesn't refuse the engagement as if its own
    # sandbox limited the tools. Single-line value ⇒ can't be misread as TOML.
    if instructions:
        argv += ["-c", f"instructions={instructions}"]
    if skip_git_repo_check:
        argv += ["--skip-git-repo-check"]
    # The clean final message lands here; we read it back as the envelope text.
    argv += ["-o", last_message_path]
    return argv


def codex_model(model: str | None) -> str:
    """The model id to pass to `codex -m`, or "" to use Codex's own default.

    The orchestrator shares ONE `--model` across backends and its default is a Claude
    id (`claude-opus-5[1m]`) — which Codex rejects ("model is not supported when using
    Codex with a ChatGPT account"). So we drop any `claude*` id here: that makes
    `zero run` work out of the box (Codex default model) without forcing
    the user to pass `--model ''`. A non-Claude id is passed through verbatim so a user
    CAN pin a specific Codex model."""
    if not model or model.lower().startswith("claude"):
        return ""
    return model


def codex_configured_model() -> str:
    """Return the model selected by the Codex CLI config, if one is pinned.

    Zero deliberately drops its shared Claude model default for the Codex backend. In
    that case Codex resolves the model from ``$CODEX_HOME/config.toml`` (normally
    ``~/.codex/config.toml``). Reading the same value lets startup diagnostics report
    the model that will actually run instead of the discarded Claude placeholder.
    """
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        with (Path(home) / "config.toml").open("rb") as fh:
            config = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return ""

    model = config.get("model")
    return model if isinstance(model, str) else ""


def codex_effective_model(model: str | None) -> str:
    """Return the model name an invocation will use, suitable for diagnostics."""
    return codex_model(model) or codex_configured_model() or "codex-default"


def codex_context_window(model: str | None = None) -> int | None:
    """The active Codex model's context window (tokens), read at runtime from Codex's
    own model-metadata cache (`$CODEX_HOME/models_cache.json`, default `~/.codex`).

    Codex's live event stream doesn't carry the window, but Codex caches per-model
    metadata on disk (refreshed by the CLI) — that's the authoritative source. Returns
    the window for `model` when it matches a cached slug, else the largest cached window
    (current Codex models share one), or None when the cache is unreadable so the caller
    falls back. This is why zero shows a correct ctx% for Codex (≈272k) instead of the
    Claude-derived 1M/200k default."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    try:
        data = json.loads((Path(home) / "models_cache.json").read_text(encoding="utf-8"))
        models = data.get("models") or []
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    windows: dict[str, int] = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id") or m.get("model")
        cw = m.get("context_window") or m.get("max_context_window")
        if slug and isinstance(cw, int) and cw > 0:
            windows[str(slug)] = cw
    if not windows:
        return None
    effective_model = codex_effective_model(model)
    if effective_model:
        for slug, cw in windows.items():
            if effective_model == slug or effective_model.startswith(slug):
                return cw
    return max(windows.values())


def _extract_codex_error(stdout: str) -> str:
    """Pull a human-readable failure out of a `codex exec --json` stream.

    On failure Codex doesn't print to stderr (stderr only carries the "Reading prompt
    from stdin..." status line) — the real error is a `turn.failed`/`error` JSONL event
    on STDOUT. Returns "" if none is found so the caller falls back to stderr."""
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") in ("turn.failed", "error"):
            err = obj.get("error")
            msg = (err.get("message") if isinstance(err, dict) else None) or obj.get("message")
            if msg:
                return str(msg)[:500]
    return ""


def _map_usage(u: dict[str, Any] | None) -> dict[str, int]:
    """Map a Codex `turn.completed.usage` block to the Anthropic-style keys the rest of
    the system accounts on (`context_tokens_from_usage`).

    Codex reports `input_tokens` as the TOTAL prompt (cached + fresh) with
    `cached_input_tokens` the cached subset — whereas the Anthropic convention this
    codebase assumes is DISJOINT tiers (input excludes cache). So we split: fresh
    input = input − cached, cache_read = cached, cache_creation = 0 (Codex exposes no
    separate creation count). That keeps the context-window sum equal to Codex's total
    input_tokens (no double counting) and lets the resume turns show up as
    cache-read-dominated. Native keys are preserved verbatim in `LLMResponse.raw`."""
    if not u:
        return {}
    inp = int(u.get("input_tokens", 0) or 0)
    cached = int(u.get("cached_input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    return {
        "input_tokens": max(0, inp - cached),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": out,
    }


def parse_codex_output(last_message_text: str, stdout: str) -> LLMResponse:
    """Build an LLMResponse from a `codex exec --json` run.

    `last_message_text` is the content of the `-o/--output-last-message` file (the
    authoritative final message); `stdout` is the JSONL event stream from which we
    scrape the thread id (`thread.started`) and token usage (`turn.completed`). If the
    `-o` file came back empty for any reason, fall back to the last `agent_message`
    item in the stream so we still surface text rather than crashing the turn."""
    thread_id = ""
    usage_raw: dict[str, Any] = {}
    agent_msgs: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # Codex may interleave non-JSON status lines; ignore them.
        if not isinstance(obj, dict):
            continue
        t = obj.get("type")
        if t == "thread.started" and obj.get("thread_id"):
            thread_id = str(obj["thread_id"])
        elif t == "turn.completed" and isinstance(obj.get("usage"), dict):
            usage_raw = obj["usage"]
        elif t == "item.completed":
            item = obj.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
                agent_msgs.append(str(item["text"]))

    text = (last_message_text or "").strip() or (agent_msgs[-1].strip() if agent_msgs else "")
    if not text:
        # No agent message — surface a turn.failed/error event if Codex emitted one
        # (e.g. an API error) rather than a generic "no text" message.
        err = _extract_codex_error(stdout)
        if err:
            raise RuntimeError(f"codex turn failed: {err}")
        raise RuntimeError(
            f"could not find response text in codex output (stdout={stdout[:500]!r})"
        )
    return LLMResponse(
        text=text,
        usage=_map_usage(usage_raw),
        raw={"thread_id": thread_id, "usage": usage_raw},
    )


class CodexCLIBackend(LLMBackend):
    def __init__(self, cfg: CodexCLIConfig | None = None) -> None:
        self.cfg = cfg or CodexCLIConfig()

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
                f"`{self.cfg.binary}` not on PATH; install the OpenAI Codex CLI or"
                " run with --backend claude."
            )
        # Session mode mirrors the Claude backend: on resume `messages` is only the
        # NEW delta and the tool catalog + identity already live in the Codex thread,
        # so we drop both (empty tools ⇒ build_user_prompt omits the catalog; no
        # system-prompt fold). On create (or no session) we send the full body and
        # prepend our agent identity, since Codex has no separate system-prompt slot.
        resuming = session is not None and session.started
        body_tools: list[dict[str, Any]] = [] if resuming else tools
        body = build_user_prompt(messages, body_tools)
        if not resuming and system_prompt:
            # Codex has no system-prompt slot: fold the detailed agent identity into the
            # first turn's body. (The persona-level "you drive a harness, don't refuse"
            # framing is set separately via `-c instructions` — see _run_once.)
            body = f"[system]\n{system_prompt.rstrip()}\n\n{body}"

        last_msg, returncode, err, stdout = await self._run_once(
            binary, body, session=session, resume=resuming
        )
        if returncode != 0:
            # The real error is a turn.failed/error event on stdout — stderr only has
            # the "Reading prompt from stdin..." status line, which is useless on its own.
            detail = _extract_codex_error(stdout) or err or "(no output)"
            raise RuntimeError(f"codex exec exit {returncode}: {detail}")
        resp = parse_codex_output(last_msg, stdout)
        # Capture the server-minted thread id so the next turn can `resume` it. The
        # orchestrator pre-seeds `session.id` with a throwaway UUID; we overwrite it
        # with Codex's real id here. (On failure we raised above, leaving
        # session.started=False so the AgentLoop replays a fresh create.)
        if session is not None and not resuming:
            tid = (resp.raw or {}).get("thread_id")
            if tid:
                session.id = str(tid)
            session.started = True
        return resp

    async def _run_once(
        self,
        binary: str,
        body: str,
        *,
        session: Session | None = None,
        resume: bool = False,
    ) -> tuple[str, int | None, str, str]:
        """Run one `codex exec` invocation. Returns (last_message, returncode, stderr,
        stdout). The prompt body is piped on STDIN; the final message is written by
        Codex to a temp file (`-o`) we read back and then delete."""
        # NamedTemporaryFile(delete=False) so the child can open it by name on Windows
        # (an open handle can't be shared); we unlink it in `finally`.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            last_path = fh.name
        proc = None
        try:
            argv = build_codex_argv(
                binary=binary,
                last_message_path=last_path,
                sandbox_mode=self.cfg.sandbox_mode,
                approval_policy=self.cfg.approval_policy,
                skip_git_repo_check=self.cfg.skip_git_repo_check,
                model=codex_model(self.cfg.model),
                session_id=session.id if session else "",
                resume=resume,
                # Sent every turn (create + resume): tiny, idempotent, and guarantees the
                # persona override holds across the whole thread.
                instructions=_CODEX_INSTRUCTIONS,
            )
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_bytes = neutralize_nul(body).encode("utf-8")
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes), timeout=self.cfg.timeout_seconds
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(
                    f"codex exec timed out after {self.cfg.timeout_seconds}s"
                )
            last_msg = ""
            try:
                last_msg = Path(last_path).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                pass  # fall back to scraping agent_message from stdout in parse step
            stdout_s = stdout.decode("utf-8", errors="replace")
            err_s = stderr.decode("utf-8", errors="replace").strip()
            # Full per-agent I/O trace (no-op unless tracing enabled). Codex's "output"
            # is the final message PLUS the JSONL event stream — capture both. The
            # persona/system framing rides in `-c instructions`, so log that too.
            trace_call(
                session.name if session else "default",
                argv=argv, system_prompt=_CODEX_INSTRUCTIONS, stdin_body=body,
                stdout=f"[last_message]\n{last_msg}\n\n[jsonl event stream]\n{stdout_s}",
                stderr=err_s, returncode=proc.returncode, resume=resume,
            )
            return (last_msg, proc.returncode, err_s, stdout_s)
        finally:
            close_proc_transport(proc)  # avoid "Event loop is closed" on later GC
            try:
                os.unlink(last_path)
            except OSError:
                pass
