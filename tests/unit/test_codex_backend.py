"""Offline tests for CodexCLIBackend — no `codex` CLI, no tokens.

We monkeypatch `asyncio.create_subprocess_exec` (and `shutil.which`) so the tests
exercise argv assembly, stdin-vs-argv discipline, JSONL/last-message parsing, the
usage mapping, and session id capture without ever spawning Codex. The live behavior
is covered by `tests/e2e/test_live_codex.py` (`-m live`)."""
import json
from pathlib import Path

import pytest

from zero.llm.backend import Message, Session
from zero.llm.codex_cli import (
    CodexCLIBackend,
    _extract_codex_error,
    build_codex_argv,
    codex_context_window,
    codex_model,
    parse_codex_output,
)


def _argval(argv, flag):
    return argv[argv.index(flag) + 1]


# Real Codex `--json` event shapes captured from `codex exec` (v0.141.0).
_THREAD_ID = "019eeea9-0be8-7531-aa22-be6e9fafea6f"
_ENVELOPE = '{"type":"final","answer":"hi"}'


def _codex_stdout(thread_id=_THREAD_ID, text=_ENVELOPE, usage=None):
    usage = usage or {
        "input_tokens": 11448,
        "cached_input_tokens": 9600,
        "output_tokens": 35,
        "reasoning_output_tokens": 20,
    }
    lines = [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage},
    ]
    return "\n".join(json.dumps(o) for o in lines)


# ------------------------------------------------------------------ argv shape

def test_build_codex_argv_create():
    """Create turn: `codex exec --json` with the soft lock-down via `-c` overrides and
    the last-message file. No `resume`, no `-s` flag."""
    argv = build_codex_argv(binary="codex", last_message_path="/tmp/last.txt")
    assert argv[:3] == ["codex", "exec", "--json"]
    assert "resume" not in argv
    assert "-s" not in argv and "--sandbox" not in argv   # sandbox via -c, not -s
    assert "-c" in argv
    assert "sandbox_mode=read-only" in argv
    assert "approval_policy=never" in argv
    assert "--skip-git-repo-check" in argv
    assert _argval(argv, "-o") == "/tmp/last.txt"


def test_build_codex_argv_resume_uses_thread_id_and_no_sandbox_flag():
    """Resume turn: `codex exec resume <id>` — sandbox MUST come via `-c` because
    `exec resume` has no `-s/--sandbox` flag."""
    argv = build_codex_argv(
        binary="codex", last_message_path="/tmp/last.txt",
        session_id=_THREAD_ID, resume=True,
    )
    assert argv[:4] == ["codex", "exec", "resume", _THREAD_ID]
    assert "-s" not in argv and "--sandbox" not in argv
    assert "sandbox_mode=read-only" in argv
    assert _argval(argv, "-o") == "/tmp/last.txt"


def test_build_codex_argv_model_optional():
    assert "--model" not in build_codex_argv(binary="codex", last_message_path="/x")
    argv = build_codex_argv(binary="codex", last_message_path="/x", model="gpt-5-codex")
    assert _argval(argv, "--model") == "gpt-5-codex"


def test_codex_model_drops_claude_ids():
    """The shared --model default is a Claude id, which Codex rejects — it must NOT be
    passed to `codex -m` (else `--backend codex` fails out of the box). Non-Claude ids
    pass through so a user can pin a real Codex model."""
    assert codex_model("claude-opus-4-8[1m]") == ""
    assert codex_model("claude-sonnet-4-6") == ""
    assert codex_model(None) == ""
    assert codex_model("") == ""
    assert codex_model("gpt-5-codex") == "gpt-5-codex"


def _write_models_cache(tmp_path, models):
    import json as _j
    (tmp_path / "models_cache.json").write_text(_j.dumps({"models": models}), encoding="utf-8")


def test_codex_context_window_reads_cache(tmp_path, monkeypatch):
    """The window is read at runtime from Codex's own model cache, not hardcoded."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_models_cache(tmp_path, [
        {"slug": "gpt-5.5", "context_window": 272000},
        {"slug": "gpt-5.4-mini", "context_window": 128000},
    ])
    assert codex_context_window("gpt-5.5") == 272000      # exact model match
    assert codex_context_window("gpt-5.4-mini") == 128000
    assert codex_context_window() == 272000               # no model → largest cached


def test_codex_context_window_none_when_cache_missing(tmp_path, monkeypatch):
    """Unreadable cache → None so the caller falls back to a sane default."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # empty dir, no models_cache.json
    assert codex_context_window("gpt-5.5") is None


def test_extract_codex_error_reads_turn_failed():
    """The real error on a failed run is a JSONL event on stdout, not stderr."""
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"x"}',
        '{"type":"turn.failed","error":{"message":"The model is not supported."}}',
    ])
    assert "not supported" in _extract_codex_error(stdout)
    assert _extract_codex_error('{"type":"turn.completed","usage":{}}') == ""


# --------------------------------------------------------------- output parsing

def test_parse_codex_output_prefers_last_message_file():
    resp = parse_codex_output(_ENVELOPE, _codex_stdout())
    assert resp.text == _ENVELOPE
    assert (resp.raw or {})["thread_id"] == _THREAD_ID


def test_parse_codex_output_falls_back_to_agent_message():
    """If the `-o` file is empty, scrape the last agent_message from the JSONL."""
    resp = parse_codex_output("", _codex_stdout(text=_ENVELOPE))
    assert resp.text == _ENVELOPE


def test_parse_codex_output_raises_without_text():
    with pytest.raises(RuntimeError):
        parse_codex_output("", '{"type":"turn.completed","usage":{}}')


def test_parse_codex_output_tolerates_non_json_lines():
    noisy = "Reading prompt from stdin...\n" + _codex_stdout()
    resp = parse_codex_output(_ENVELOPE, noisy)
    assert resp.text == _ENVELOPE
    assert (resp.raw or {})["thread_id"] == _THREAD_ID


def test_usage_maps_cached_to_cache_read_without_double_counting():
    """Codex `input_tokens` is the TOTAL (cached included). We split it so the
    context-window sum equals that total and the cached portion shows as cache_read."""
    resp = parse_codex_output(_ENVELOPE, _codex_stdout())
    u = resp.usage
    assert u["cache_read_input_tokens"] == 9600
    assert u["cache_creation_input_tokens"] == 0
    assert u["input_tokens"] == 11448 - 9600      # fresh = total − cached
    assert u["output_tokens"] == 35
    # The native block is preserved for debugging.
    assert (resp.raw or {})["usage"]["cached_input_tokens"] == 9600


# ------------------------------------------------------- subprocess-level (run)

class _FakeProc:
    def __init__(self, stdout: str, returncode: int, captured: dict) -> None:
        self._stdout = stdout.encode()
        self.returncode = returncode
        self._captured = captured

    async def communicate(self, input=None):  # noqa: A002 — match asyncio's signature
        self._captured["stdin"] = (input or b"").decode("utf-8", errors="replace")
        return self._stdout, b""

    def kill(self):  # pragma: no cover - only on timeout
        pass

    async def wait(self):  # pragma: no cover
        return 0


def _patch_exec(monkeypatch, *, stdout, last_message, returncode=0):
    """Patch create_subprocess_exec + which. The fake writes `last_message` to the
    `-o` path Codex was told to use (so the backend reads it back), and returns
    `stdout` as the JSONL stream. Captures argv/stdin for assertions."""
    from zero.llm import codex_cli as cx

    monkeypatch.setattr(cx.shutil, "which", lambda b: "/usr/bin/codex")
    captured: dict = {}

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["got_stdin_pipe"] = kwargs.get("stdin") is not None
        out_path = argv[argv.index("-o") + 1]
        Path(out_path).write_text(last_message, encoding="utf-8")
        return _FakeProc(stdout, returncode, captured)

    monkeypatch.setattr(cx.asyncio, "create_subprocess_exec", fake_exec)
    return captured


async def test_run_keeps_command_line_tiny_with_huge_prompt(monkeypatch):
    """Windows regression ([WinError 206]): no matter how large the system prompt +
    conversation + catalog grow, nothing big reaches argv — it's all piped on STDIN."""
    captured = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    huge = "A" * 100_000
    backend = CodexCLIBackend()
    await backend.run(
        system_prompt=huge,
        messages=[Message(role="user", content=huge)],
        tools=[{"name": "t", "description": huge, "risk_level": "safe", "schema": {}}],
    )
    argv = captured["argv"]
    assert captured["got_stdin_pipe"], "prompt body must be piped on stdin"
    assert huge not in argv
    assert all(len(a) < 1000 for a in argv), [len(a) for a in argv]
    assert sum(len(a) for a in argv) < 4000       # whole command line << 32,767


async def test_run_drops_claude_model_from_argv(monkeypatch):
    """A Claude model id (the shared --model default) must never reach `codex -m`."""
    from zero.llm.codex_cli import CodexCLIConfig

    captured = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    backend = CodexCLIBackend(CodexCLIConfig(model="claude-opus-4-8[1m]"))
    await backend.run("sys", [Message(role="user", content="hi")], [])
    assert "--model" not in captured["argv"]
    assert "claude-opus-4-8[1m]" not in captured["argv"]


async def test_run_nonzero_exit_surfaces_jsonl_error(monkeypatch):
    """On failure the RuntimeError carries the turn.failed message from stdout, not the
    useless 'Reading prompt from stdin...' stderr line."""
    stdout = '{"type":"turn.failed","error":{"message":"model is not supported"}}'
    _patch_exec(monkeypatch, stdout=stdout, last_message="", returncode=1)
    backend = CodexCLIBackend()
    with pytest.raises(RuntimeError, match="model is not supported"):
        await backend.run("sys", [Message(role="user", content="hi")], [])


async def test_session_create_captures_thread_id(monkeypatch):
    """On create the backend overwrites the throwaway session UUID with Codex's real
    thread id and marks the session started, so the next turn can resume it."""
    _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    backend = CodexCLIBackend()
    sess = Session(id="throwaway-uuid", name="pentester-1")
    await backend.run("sys", [Message(role="user", content="hi")], [], session=sess)
    assert sess.id == _THREAD_ID
    assert sess.started is True


async def test_session_resume_builds_resume_argv_with_captured_id(monkeypatch):
    """After create, a started session drives `codex exec resume <thread_id>` and
    folds neither the system prompt nor the tool catalog into the body."""
    captured = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    backend = CodexCLIBackend()
    sess = Session(id=_THREAD_ID, name="pentester-1", started=True)
    await backend.run(
        "SYSTEM-IDENTITY",
        [Message(role="observation", content="OBS-DELTA")],
        [{"name": "t", "description": "d", "risk_level": "safe", "schema": {}}],
        session=sess,
    )
    argv = list(captured["argv"])
    assert argv[:4] == ["/usr/bin/codex", "exec", "resume", _THREAD_ID]
    # On resume neither the identity nor the catalog is re-sent (they live in the
    # thread); only the new delta is piped on stdin.
    body = captured["stdin"]
    assert "SYSTEM-IDENTITY" not in body
    assert "## Available tools" not in body
    assert "OBS-DELTA" in body


async def test_session_create_folds_system_prompt_into_body(monkeypatch):
    """On the create turn (no native system-prompt slot) the identity is prepended to
    the stdin body and the tool catalog is included."""
    captured = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    backend = CodexCLIBackend()
    sess = Session(id="throwaway", name="pentester-1")
    await backend.run(
        "SYSTEM-IDENTITY",
        [Message(role="user", content="begin")],
        [{"name": "t", "description": "d", "risk_level": "safe", "schema": {}}],
        session=sess,
    )
    body = captured["stdin"]
    assert "SYSTEM-IDENTITY" in body
    assert "## Available tools" in body


def _codex_instr(argv):
    argv = list(argv)
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("instructions="):
            return argv[i + 1].split("=", 1)[1]
    return None


async def test_instructions_override_persona_on_create_and_resume(monkeypatch):
    """The persona-replacement (Codex's analog of Claude's --system-prompt) that stops
    Codex refusing as if its own sandbox limited the tools is passed via
    `-c instructions` on EVERY turn (create + resume), so the override holds across
    the whole thread. It is a single line (safe as a `-c` value / on the command line)."""
    # create
    captured = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    backend = CodexCLIBackend()
    sess = Session(id="seed", name="orch")
    await backend.run("SYS", [Message(role="user", content="go")], [], session=sess)
    bi = _codex_instr(captured["argv"])
    assert bi and "never" in bi.lower() and "\n" not in bi
    assert not any("base_instructions=" in str(a) for a in captured["argv"])
    # resume — still applied
    captured2 = _patch_exec(monkeypatch, stdout=_codex_stdout(), last_message=_ENVELOPE)
    await backend.run("SYS", [Message(role="observation", content="obs")], [], session=sess)
    assert _codex_instr(captured2["argv"]) == bi
