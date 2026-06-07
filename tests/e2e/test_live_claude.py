"""Live `claude -p` integration tests.

These actually invoke the user's Claude Code CLI — they're slow (each turn
is several seconds), cost tokens, and require `claude` to be on PATH and
authenticated. Gated by both:

  * the `live_claude` marker (so you can run them on demand: `-m live_claude`)
  * a skipif when `claude` isn't available, so the suite stays green on
    machines without it

Run only these:
    uv run pytest -q -m live_claude tests/e2e/test_live_claude.py

Skip these explicitly:
    uv run pytest -q -m "not live_claude"
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from zero.llm.backend import Message
from zero.llm.claude_code import ClaudeCodeBackend, build_prompt
from zero.llm.envelope import (
    ActionEnvelope,
    AskUserEnvelope,
    FinalEnvelope,
    parse_envelope,
)


pytestmark = [
    pytest.mark.live,          # token-spending live run — excluded from the default suite
    pytest.mark.live_claude,
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="claude CLI not on PATH",
    ),
]


SIMPLE_SYSTEM = """\
You are an envelope-only assistant. Reply with EXACTLY one JSON object,
no prose, no markdown fence. Allowed shapes:

  {"type": "action",   "tool": "<name>", "args": {...}, "thought": "..."}
  {"type": "ask_user", "question": "...", "options": [...], "free_form": true, "blocking": true}
  {"type": "final",    "answer": "..."}
"""


async def test_claude_returns_well_formed_envelope():
    """Smoke test: the real `claude -p` returns text that we can parse as
    an envelope using the same machinery the agent loop uses."""
    backend = ClaudeCodeBackend()
    resp = await backend.run(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(
                role="user",
                content=(
                    "Reply with a `final` envelope whose answer is the"
                    " literal string 'pong'. Nothing else."
                ),
            )
        ],
        tools=[],
    )
    assert resp.text, "claude returned empty text"
    env = parse_envelope(resp.text)
    assert isinstance(env, FinalEnvelope), f"got {type(env).__name__}: {resp.text!r}"
    # Don't assert the exact answer text — model wording drifts. Just
    # require that it was an envelope of the right type with a non-empty
    # answer.
    assert env.answer.strip(), f"empty answer: {env}"


async def test_claude_uses_tool_when_available():
    """Given a tool catalog and a goal that obviously calls for a tool,
    claude should emit an `action` envelope referencing it."""
    tools = [
        {
            "name": "web_search",
            "description": "search the public web for a query",
            "risk_level": "safe",
            "schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query"],
            },
        }
    ]
    backend = ClaudeCodeBackend()
    resp = await backend.run(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(
                role="user",
                content=(
                    "Find recent writeups about CVE-2021-44228 (Log4Shell)."
                    " Use the `web_search` tool."
                ),
            )
        ],
        tools=tools,
    )
    env = parse_envelope(resp.text)
    assert isinstance(env, ActionEnvelope), f"got {type(env).__name__}: {resp.text!r}"
    assert env.tool == "web_search", f"expected web_search tool, got: {env.tool}"
    assert env.args.get("query")


async def test_disallowed_builtins_prevent_max_turns_error():
    """Regression: previously, prompts that tempted claude to use its
    built-in tools (Bash/Read/WebFetch) triggered `error_max_turns` and
    `claude -p exit 1`. With our DEFAULT_DISALLOWED_TOOLS list, claude
    can't reach for its own tools and must respond with an envelope.

    The validator's seed message is exactly this shape — "investigate
    this finding" — and was the recurring crash point in real runs.
    """
    backend = ClaudeCodeBackend()
    resp = await backend.run(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(
                role="user",
                content=(
                    "Investigate this finding and decide whether it's real:\n"
                    "  endpoint=/api/orders/1041 method=GET cwe=CWE-639\n"
                    "  evidence: 'returns user B's data when logged in as A'\n"
                    "Reply with a `final` envelope whose answer is the literal"
                    " string 'rejected: not enough evidence'."
                ),
            )
        ],
        tools=[],
    )
    env = parse_envelope(resp.text)
    assert isinstance(env, FinalEnvelope), f"got {type(env).__name__}: {resp.text!r}"
    assert env.answer.strip(), f"empty answer: {env}"


async def test_build_prompt_round_trips_through_claude():
    """End-to-end of the prompt-builder — feed claude what the agent loop
    would actually feed it, ensure response is parseable."""
    prompt = build_prompt(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(role="user", content="Reply with `final` envelope, answer='ok'."),
        ],
        tools=[],
    )
    # Drive ClaudeCodeBackend.run() but with a single user message that's
    # already been wrapped by build_prompt — this is the path the agent
    # loop takes.
    backend = ClaudeCodeBackend()
    # Bypass build_prompt by passing prompt verbatim as the only user
    # message; build_prompt will wrap it once more, which is wasteful but
    # tests the worst case for prompt length too.
    resp = await asyncio.wait_for(
        backend.run(
            system_prompt="",
            messages=[Message(role="user", content=prompt)],
            tools=[],
        ),
        timeout=120,
    )
    env = parse_envelope(resp.text)
    assert isinstance(env, (FinalEnvelope, ActionEnvelope, AskUserEnvelope))
