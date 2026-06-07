import json

import pytest

from zero.llm.backend import Message
from zero.llm.claude_code import (
    DEFAULT_DISALLOWED_TOOLS,
    _extract_claude_error,
    build_mode_a_argv,
    build_prompt,
    parse_claude_json,
)


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
        prompt="hello",
        max_turns=5,
        disallowed_tools=DEFAULT_DISALLOWED_TOOLS,
    )
    assert "--disallowedTools" in argv
    i = argv.index("--disallowedTools")
    spec = argv[i + 1]
    # The big offenders that previously caused error_max_turns must be present.
    for required in ("Bash", "Read", "Write", "WebFetch", "WebSearch"):
        assert required in spec, f"{required} missing from disallowed list"


def test_mode_a_argv_max_turns_default_above_one():
    # 1 is too tight — claude's internal accounting hits error_max_turns
    # even when our envelope-only protocol wouldn't need a second turn.
    from zero.llm.claude_code import ClaudeCodeConfig

    assert ClaudeCodeConfig.max_turns >= 3, (
        "max_turns=1 caused real-world error_max_turns crashes; keep > 1"
    )


def test_mode_a_argv_no_disallow_when_empty():
    argv = build_mode_a_argv(
        binary="claude", prompt="x", max_turns=5, disallowed_tools=()
    )
    assert "--disallowedTools" not in argv


def test_mode_a_argv_neutralizes_nul_in_prompt():
    """Regression (Blueprint_Heist): a NUL byte in the prompt (from a binary tool
    observation flowing into the transcript) raises `ValueError: embedded null
    byte` at `create_subprocess_exec`. The argv builder must neutralize it so the
    spawn can't crash — while keeping a visible marker of where it was."""
    prompt = "decoded body:\x00\x00 GET /x" + "\x00"
    argv = build_mode_a_argv(
        binary="claude", prompt=prompt, max_turns=5, disallowed_tools=()
    )
    assert all("\x00" not in a for a in argv), argv
    p = argv[argv.index("-p") + 1]
    assert "decoded body:" in p          # surrounding content preserved
    assert "[NUL*2]" in p and "[NUL*1]" in p  # the null runs are marked, with counts


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

    async def communicate(self):
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
    backend = ClaudeCodeBackend(ClaudeCodeConfig(max_turns=10))
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
