import json

import pytest

from zero.llm.backend import Message
from zero.llm.claude_code import (
    DEFAULT_DISALLOWED_TOOLS,
    _extract_claude_error,
    build_mode_a_argv,
    build_prompt,
    build_user_prompt,
    parse_claude_json,
    scrub_claude_code_env,
)


def _argval(argv, flag):
    return argv[argv.index(flag) + 1]


def test_mode_a_argv_keeps_big_strings_off_the_command_line():
    """Windows fix ([WinError 206], command line capped at 32 KB): neither the system
    prompt nor the conversation body may be argv elements. The system prompt goes to a
    FILE (`--system-prompt-file`) and the body is piped on STDIN (`--print`, no `-p
    <body>` positional). --setting-sources still drops the repo CLAUDE.md."""
    argv = build_mode_a_argv(
        binary="claude", max_turns=5, disallowed_tools=(),
        system_prompt_path="/tmp/sys.md", setting_sources="user",
    )
    assert "--print" in argv
    assert "-p" not in argv                 # no inline prompt positional/flag
    assert "--system-prompt" not in argv     # the inline (huge-argv) variant is gone
    assert _argval(argv, "--system-prompt-file") == "/tmp/sys.md"
    assert _argval(argv, "--setting-sources") == "user"
    # Nothing on the command line should be a large string (the body/system prompt).
    assert all(len(a) < 200 for a in argv), argv


def test_mode_a_argv_omits_identity_flags_when_unset():
    argv = build_mode_a_argv(binary="claude", max_turns=5, disallowed_tools=())
    assert "--system-prompt-file" not in argv
    assert "--system-prompt" not in argv
    assert "--setting-sources" not in argv


def test_mode_a_argv_session_create():
    """Create turn: pin the session UUID (--session-id), pass the identity/settings, and
    a debug display name (-n). NOT --resume."""
    argv = build_mode_a_argv(
        binary="claude", max_turns=4, disallowed_tools=("LSP",),
        system_prompt_path="/tmp/sys.md", setting_sources="user", lock_down_tools=True,
        session_id="UUID-1", session_name="pentester-rosa_1", resume=False,
    )
    assert _argval(argv, "--session-id") == "UUID-1"
    assert _argval(argv, "-n") == "pentester-rosa_1"
    assert "--resume" not in argv
    assert _argval(argv, "--system-prompt-file") == "/tmp/sys.md"
    assert _argval(argv, "--setting-sources") == "user"
    assert "--allowedTools" in argv  # lock-down on the create turn


def test_mode_a_argv_session_resume():
    """Resume turn: continue the session (--resume), and DROP the identity/settings/name
    (they live in the session) while KEEPING the lock-down + model."""
    argv = build_mode_a_argv(
        binary="claude", max_turns=4, disallowed_tools=("LSP",),
        system_prompt_path="", setting_sources="", lock_down_tools=True,
        model="claude-opus-4-8[1m]", session_id="UUID-1", resume=True,
    )
    assert _argval(argv, "--resume") == "UUID-1"
    assert "--session-id" not in argv
    assert "--system-prompt-file" not in argv
    assert "--setting-sources" not in argv
    assert "-n" not in argv
    assert _argval(argv, "--model") == "claude-opus-4-8[1m]"
    assert "--allowedTools" in argv  # lock-down still enforced on resume


def test_build_user_prompt_omits_catalog_when_no_tools():
    """The resume body carries only the new conversation — no '## Available tools'
    section (the catalog was sent once on the create turn)."""
    from zero.llm.backend import Message

    body = build_user_prompt([Message(role="observation", content="HTTP 200")], [])
    assert "## Available tools" not in body
    assert "HTTP 200" in body
    # ...but with tools present (the create turn) the catalog IS included.
    with_tools = build_user_prompt(
        [], [{"name": "curl_raw", "risk_level": "active", "description": "d"}]
    )
    assert "## Available tools" in with_tools and "curl_raw" in with_tools


def test_build_user_prompt_excludes_system_prompt():
    tools = [{"name": "t", "description": "d", "risk_level": "safe", "schema": {"type": "object"}}]
    p = build_user_prompt([Message(role="user", content="hi")], tools)
    assert "## Available tools" in p and "[user] hi" in p
    assert "envelope JSON object" in p


def test_backend_config_defaults_to_user_setting_source():
    from zero.llm.claude_code import ClaudeCodeConfig

    assert ClaudeCodeConfig().setting_sources == "user"  # no project/local CLAUDE.md leak


def test_backend_locks_down_all_real_tools_by_default():
    """The envelope protocol breaks if the model can call real tools (it tries the
    harness/MCP tools, finds our pentest tools 'not wired', and gives up). Lock-down
    must strip built-ins, MCP, and skills so only envelope text is possible."""
    from zero.llm.claude_code import ClaudeCodeConfig

    assert ClaudeCodeConfig().lock_down_tools is True
    argv = build_mode_a_argv(
        binary="claude", max_turns=5, disallowed_tools=(),
        lock_down_tools=True,
    )
    # An EMPTY WHITELIST `--allowedTools ""` is what actually grants zero tools.
    # `--tools ""` does NOT work (greedy variadic; the empty value leaves Claude
    # Code's defaults — incl. LSP — enabled), so it must NOT be the disable we rely on.
    assert "--allowedTools" in argv and argv[argv.index("--allowedTools") + 1] == ""
    assert "--tools" not in argv                # the ineffective flag must not be used
    assert "--strict-mcp-config" in argv      # no MCP servers (Google, etc.)
    assert "--disable-slash-commands" in argv  # no skills


def test_lock_down_omitted_when_disabled():
    argv = build_mode_a_argv(
        binary="claude", max_turns=5, disallowed_tools=(),
        lock_down_tools=False,
    )
    assert "--allowedTools" not in argv and "--strict-mcp-config" not in argv


def test_mode_a_argv_pins_model():
    argv = build_mode_a_argv(
        binary="claude", max_turns=5, disallowed_tools=(),
        model="claude-opus-4-8[1m]",
    )
    assert _argval(argv, "--model") == "claude-opus-4-8[1m]"


def test_mode_a_argv_no_model_flag_when_empty():
    argv = build_mode_a_argv(binary="claude", max_turns=5, disallowed_tools=())
    assert "--model" not in argv


def test_orchestrator_config_defaults_to_opus_1m():
    from zero.orchestrator.orchestrator import OrchestratorConfig
    from pathlib import Path

    assert OrchestratorConfig(cwd=Path(".")).model == "claude-opus-4-8[1m]"


def test_parse_result_field():
    out = json.dumps({"result": '{"type":"final","answer":"hi"}', "usage": {"input_tokens": 5}})
    resp = parse_claude_json(out)
    assert "final" in resp.text
    assert resp.usage["input_tokens"] == 5


def test_parse_text_fallback():
    out = json.dumps({"text": "hello"})
    resp = parse_claude_json(out)
    assert resp.text == "hello"


def test_parse_rejects_non_json():
    with pytest.raises(RuntimeError):
        parse_claude_json("not json")


def test_parse_rejects_missing_text():
    out = json.dumps({"usage": {}})
    with pytest.raises(RuntimeError):
        parse_claude_json(out)


def test_build_prompt_includes_tools_and_history():
    tools = [{"name": "t", "description": "d", "risk_level": "safe", "schema": {"type": "object"}}]
    msgs = [Message(role="user", content="hi")]
    p = build_prompt("SYS", msgs, tools)
    assert "SYS" in p
    assert "## Available tools" in p
    assert "- t (risk=safe): d" in p
    assert "[user] hi" in p
    assert "envelope JSON object" in p


def test_build_prompt_no_history():
    p = build_prompt("SYS", [], [])
    assert "## Conversation" not in p


# --- argv assembly ----------------------------------------------------------


def test_mode_a_argv_includes_disallowed_builtins():
    argv = build_mode_a_argv(
        binary="/usr/bin/claude",
        max_turns=5,
        disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
    )
    assert "--disallowedTools" in argv
    i = argv.index("--disallowedTools")
    spec = argv[i + 1]
    # The big offenders that previously caused error_max_turns must be present.
    # `LSP`/`ToolSearch`/`Monitor` are the newer built-ins the original list MISSED —
    # `LSP` is the one a subagent fell back to and called (the live-reproduced Guild
    # bug), so guard them explicitly to stop the recurrence.
    # `ScheduleWakeup`/`SendMessage`/`Workflow` are the Claude Code 2.1.x agent built-ins
    # the orchestrator fell back to in the juice-shop regression failure.
    for required in (
        "Bash", "Read", "Write", "WebFetch", "WebSearch",
        "LSP", "ToolSearch", "Monitor",
        "ScheduleWakeup", "SendMessage", "Workflow",
    ):
        assert required in spec, f"{required} missing from disallowed list"


def test_close_proc_transport_is_safe_on_none_and_missing():
    from zero.llm.claude_code import close_proc_transport

    # None proc (spawn failed before assignment) → no-op, no raise.
    close_proc_transport(None)

    # A proc-like object whose transport.close() raises must be swallowed.
    class _T:
        def close(self):
            raise RuntimeError("Event loop is closed")

    class _P:
        _transport = _T()

    close_proc_transport(_P())  # must not raise

    # A proc with a normal transport gets closed exactly once.
    calls = []

    class _T2:
        def close(self):
            calls.append(1)

    class _P2:
        _transport = _T2()

    close_proc_transport(_P2())
    assert calls == [1]


def test_scrub_claude_code_env_drops_agent_session_markers():
    # Regression: a `claude -p` spawned from INSIDE a Claude Code session inherited these
    # and attached to the parent agent runtime, picking up ScheduleWakeup/SendMessage/
    # Workflow instead of our pentest envelope (the juice-shop blind-regression failure).
    dirty = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SSE_PORT": "16972",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "AI_AGENT": "claude-code_2-1-187_agent",
        # auth + ordinary vars MUST survive (OAuth/subscription auth depends on them)
        "ANTHROPIC_API_KEY": "sk-keep",
        "PATH": "/usr/bin",
        "HOME": "/home/u",
    }
    clean = scrub_claude_code_env(dirty)
    for leaked in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_CHILD_SESSION",
                   "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT"):
        assert leaked not in clean, f"{leaked} should be scrubbed"
    assert clean["ANTHROPIC_API_KEY"] == "sk-keep"  # auth preserved
    assert clean["PATH"] == "/usr/bin" and clean["HOME"] == "/home/u"


def test_scrub_claude_code_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SSE_PORT", "9999")
    monkeypatch.setenv("ZERO_KEEP_ME", "yes")
    clean = scrub_claude_code_env()
    assert "CLAUDE_CODE_SSE_PORT" not in clean
    assert clean.get("ZERO_KEEP_ME") == "yes"


def test_mode_a_internal_max_turns_has_small_headroom():
    # claude's INTERNAL per-call --max-turns is a SMALL budget (not 1): we drive the
    # multi-turn loop ourselves (one envelope per call), but under lock-down a stray
    # native tool_use attempt at --max-turns 1 instantly trips error_max_turns and
    # fails the whole call (the live-reproduced Guild bug). A few turns of headroom let
    # that rare attempt fail harmlessly and the model recover to text. Must stay small
    # so it can't run a real internal agentic loop.
    from zero.llm.claude_code import ClaudeCodeConfig

    assert 2 <= ClaudeCodeConfig.max_turns <= 6
    assert ClaudeCodeConfig.lock_down_tools is True  # the precondition that makes it safe


def test_mode_a_argv_no_disallow_when_empty():
    argv = build_mode_a_argv(
        binary="claude", max_turns=5, disallowed_tools=()
    )
    assert "--disallowedTools" not in argv


def test_neutralize_nul_marks_runs_with_counts():
    """Regression (Blueprint_Heist): a NUL byte flowing into the prompt (from a binary
    tool observation) raises `ValueError: embedded null byte` at create_subprocess_exec
    if it ever reaches argv. The body now travels on STDIN (a byte stream tolerates
    NUL), but we still neutralize it — claude/node can mishandle NUL, and the marker
    keeps the security-relevant fact that N null bytes appeared here."""
    from zero.llm.claude_code import neutralize_nul

    out = neutralize_nul("decoded body:\x00\x00 GET /x" + "\x00")
    assert "\x00" not in out
    assert "decoded body:" in out          # surrounding content preserved
    assert "[NUL*2]" in out and "[NUL*1]" in out  # null runs marked, with counts


def test_neutralize_nul_helper():
    from zero.llm.claude_code import neutralize_nul

    assert neutralize_nul("a\x00b\x00") == "a[NUL*1]b[NUL*1]"
    assert neutralize_nul("x\x00\x00\x00y") == "x[NUL*3]y"   # runs collapse, count kept
    assert neutralize_nul("clean") == "clean"


# --- error parsing ----------------------------------------------------------


def test_extract_claude_error_max_turns():
    raw = json.dumps(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": 2,
            "stop_reason": "tool_use",
            "duration_ms": 8544,
        }
    )
    msg = _extract_claude_error(raw)
    assert "error_max_turns" in msg
    assert "tool_use" in msg
    assert "num_turns=2" in msg


def test_extract_claude_error_returns_empty_for_normal_response():
    raw = json.dumps({"result": '{"type":"final","answer":"ok"}'})
    assert _extract_claude_error(raw) == ""


def test_extract_claude_error_ignores_non_json():
    assert _extract_claude_error("not json") == ""
    assert _extract_claude_error("") == ""


def test_parse_claude_json_raises_on_is_error():
    raw = json.dumps(
        {
            "subtype": "error_max_turns",
            "is_error": True,
            "num_turns": 2,
            "stop_reason": "tool_use",
        }
    )
    with pytest.raises(RuntimeError) as exc:
        parse_claude_json(raw)
    # The clean message must surface — not just a giant JSON dump.
    assert "error_max_turns" in str(exc.value)
    # And it must be SHORT (< 200 chars), not 1KB of JSON.
    assert len(str(exc.value)) < 200, str(exc.value)


# --- error_max_turns retry (the gruyere freeze regression) ------------------
#
# Real runs died with `claude -p exit 1: error_max_turns` because claude's
# internal turn counter tripped the cap. The backend must now retry ONCE with
# extra headroom before giving up, so a single inflated-turn count doesn't
# crash a whole validation (which would park the finding pending for no reason).


class _FakeProc:
    def __init__(self, stdout: str, returncode: int) -> None:
        self._stdout = stdout.encode()
        self.returncode = returncode

    async def communicate(self, input=None):  # noqa: A002 — match asyncio's signature
        # The prompt body is now piped on STDIN (Windows command-line-length fix), so
        # the backend calls communicate(input=...). Accept and ignore it.
        return self._stdout, b""

    def kill(self):  # pragma: no cover - only hit on timeout
        pass

    async def wait(self):  # pragma: no cover
        return 0


_ERR_MAX_TURNS = json.dumps(
    {"subtype": "error_max_turns", "is_error": True, "num_turns": 6, "stop_reason": "tool_use"}
)
_OK_FINAL = json.dumps({"result": '{"type":"final","answer":"ok"}'})


def _patch_exec(monkeypatch, responses):
    """Patch create_subprocess_exec + which; record the --max-turns per call.

    `responses` is a list of (stdout, returncode) returned in order.
    Returns the list that captures the --max-turns value of each call.
    """
    from zero.llm import claude_code as cc

    monkeypatch.setattr(cc.shutil, "which", lambda b: "/usr/bin/claude")
    seen_caps: list[str] = []
    seq = list(responses)

    async def fake_exec(*argv, **kwargs):
        caps = argv[argv.index("--max-turns") + 1]
        seen_caps.append(caps)
        stdout, rc = seq.pop(0)
        return _FakeProc(stdout, rc)

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)
    return seen_caps


async def test_run_retries_once_on_error_max_turns(monkeypatch):
    from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

    caps = _patch_exec(monkeypatch, [(_ERR_MAX_TURNS, 1), (_OK_FINAL, 0)])
    backend = ClaudeCodeBackend(ClaudeCodeConfig(max_turns=10, max_turns_retry_bonus=20))
    resp = await backend.run("sys", [Message(role="user", content="hi")], [])
    assert "final" in resp.text
    # Retried once, with MORE turns the second time.
    assert caps == ["10", "30"]


async def test_run_raises_after_retry_exhausted(monkeypatch):
    from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

    caps = _patch_exec(monkeypatch, [(_ERR_MAX_TURNS, 1), (_ERR_MAX_TURNS, 1)])
    # explicit bonus so the test is independent of the (now small) default.
    backend = ClaudeCodeBackend(ClaudeCodeConfig(max_turns=10, max_turns_retry_bonus=20))
    with pytest.raises(RuntimeError) as exc:
        await backend.run("sys", [Message(role="user", content="hi")], [])
    assert "error_max_turns" in str(exc.value)
    assert caps == ["10", "30"]  # tried exactly twice, then gave up


async def test_run_no_retry_when_disabled(monkeypatch):
    from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

    caps = _patch_exec(monkeypatch, [(_ERR_MAX_TURNS, 1)])
    backend = ClaudeCodeBackend(ClaudeCodeConfig(max_turns=10, retry_on_max_turns=False))
    with pytest.raises(RuntimeError):
        await backend.run("sys", [Message(role="user", content="hi")], [])
    assert caps == ["10"]  # no retry


async def test_run_non_max_turns_error_fails_fast(monkeypatch):
    """A different error (e.g. context length) must NOT be retried."""
    from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

    other = json.dumps({"subtype": "error_other", "is_error": True})
    caps = _patch_exec(monkeypatch, [(other, 1)])
    backend = ClaudeCodeBackend(ClaudeCodeConfig(max_turns=10))
    with pytest.raises(RuntimeError):
        await backend.run("sys", [Message(role="user", content="hi")], [])
    assert caps == ["10"]  # failed fast, no retry


async def test_run_keeps_command_line_tiny_with_huge_prompt(monkeypatch):
    """Windows regression ([WinError 206], CreateProcess caps the command line at
    32,767 chars). No matter how large the system prompt + conversation grow, NOTHING
    large may reach argv: the body goes on STDIN, the system prompt to a temp FILE.
    This is the cheap always-run guard for the bug that broke Windows after a few turns.
    """
    from zero.llm import claude_code as cc
    from zero.llm.claude_code import ClaudeCodeBackend, ClaudeCodeConfig

    monkeypatch.setattr(cc.shutil, "which", lambda b: "/usr/bin/claude")
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["got_stdin_pipe"] = kwargs.get("stdin") is not None
        return _FakeProc(_OK_FINAL, 0)

    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", fake_exec)

    huge = "A" * 100_000  # would be ~3x the Windows cap if it landed on argv
    backend = ClaudeCodeBackend(ClaudeCodeConfig(retry_on_max_turns=False))
    await backend.run(
        system_prompt=huge,
        messages=[Message(role="user", content=huge)],
        tools=[{"name": "t", "description": huge, "risk_level": "safe", "schema": {}}],
    )
    argv = captured["argv"]
    assert captured["got_stdin_pipe"], "prompt body must be piped on stdin"
    assert huge not in argv                       # the inline string never appears
    assert all(len(a) < 1000 for a in argv), [len(a) for a in argv]
    assert sum(len(a) for a in argv) < 4000       # whole command line << 32,767
    # The system prompt is referenced by FILE path, not inlined.
    assert "--system-prompt-file" in argv and "--system-prompt" not in argv
