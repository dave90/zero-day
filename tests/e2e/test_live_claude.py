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
    ActionsEnvelope,
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


# --- the recurring class: subagents losing tool access under lock-down ---------
#
# This is the test the previous ones MISSED. `test_disallowed_builtins_prevent_
# max_turns_error` above uses SIMPLE_SYSTEM + a tiny prompt — which always passes
# even when the lock-down is broken. The bug only fires with the REAL, large agent
# system prompt + the FULL tool catalog + an insistent "act now" brief: that's what
# pushes a capable model to emit a stray native tool_use, which (if any built-in is
# still callable) trips `error_max_turns (stop=tool_use)` and kills the subagent —
# the Guild failure, reproduced. Run a few reps because the stray attempt is
# probabilistic; production needs it to never happen.

REPS = 3


def _real_catalog() -> list[dict]:
    """The exact tool catalog a pentester/validator subagent is handed (full
    default_registry minus the orchestrator-only dispatch tools + finding_submit)."""
    from zero.tools.registry import default_registry
    from zero.tools.dispatch import is_dispatch_tool

    return [
        t for t in default_registry().catalog()
        if not is_dispatch_tool(t["name"]) and t["name"] != "finding_submit"
    ]


# An insistent brief shaped like the orchestrator's real pentester seed — it tells
# the agent to ACT against a target NOW, which is what tempts the stray tool_use.
_INSISTENT_BRIEF = (
    "You are a focused pentester subagent. TARGET: http://10.10.10.10:8080/login\n"
    "ATTACK IDEA: test the /login POST (username, password) for SQL-injection auth "
    "bypass. Begin probing immediately with your tools and emit ONE action envelope "
    "now — do not ask questions, do not stall.\n"
    "Respond with exactly one envelope JSON object now."
)


@pytest.mark.parametrize(
    "load_prompt",
    [
        pytest.param(
            lambda: __import__(
                "zero.agents.pentest", fromlist=["load_system_prompt"]
            ).load_system_prompt(),
            id="pentester",
        ),
        pytest.param(
            lambda: __import__(
                "zero.agents.validator", fromlist=["load_system_prompt"]
            ).load_system_prompt(),
            id="validator",
        ),
    ],
)
async def test_locked_down_subagent_never_calls_native_tool(load_prompt):
    """Invariant: under the PRODUCTION lock-down, a subagent driven by the REAL
    agent system prompt + FULL catalog + an insistent "act now" brief must (a) not
    raise (no `error_max_turns` / `claude -p exit 1`), (b) return parseable envelope
    text, and (c) NEVER stop on a native tool_use. Failing any rep means the
    lock-down regressed and subagents will lose tool access again."""
    system_prompt = load_prompt()
    catalog = _real_catalog()
    backend = ClaudeCodeBackend()  # production config — no overrides

    for rep in range(REPS):
        try:
            resp = await backend.run(
                system_prompt=system_prompt,
                messages=[Message(role="user", content=_INSISTENT_BRIEF)],
                tools=catalog,
            )
        except RuntimeError as e:  # the backend raises on error_max_turns
            pytest.fail(
                f"rep {rep}: lock-down regressed — claude raised instead of emitting "
                f"an envelope (the subagent-lost-tools bug): {e}"
            )
        # The model must not have stopped on a native tool call — that means a
        # built-in was still callable (e.g. LSP), the exact failure mode.
        stop = (resp.raw or {}).get("stop_reason")
        assert stop != "tool_use", (
            f"rep {rep}: claude stopped on a NATIVE tool_use (a built-in is still "
            f"callable — lock-down regressed): raw={resp.raw!r}"
        )
        # And the text must be a usable envelope, like the agent loop expects
        # (single action, a batch of actions, an ask, or a final).
        env = parse_envelope(resp.text)
        assert isinstance(
            env, (ActionEnvelope, ActionsEnvelope, FinalEnvelope, AskUserEnvelope)
        ), f"rep {rep}: not an envelope: {resp.text!r}"


# --- persistent-session token saver (--session-id / --resume) ------------------


async def test_session_resume_keeps_context_and_shrinks_input():
    """End-to-end of the token fix: drive a real pentester AgentLoop with a persistent
    claude session over three turns. Assert (a) the resumed turns send FAR fewer input
    tokens than the create turn (we stop re-shipping the transcript), (b) claude retains
    the conversation across separate `claude -p` processes (a marker seeded on turn 1 is
    recalled on turn 3), and (c) the lock-down holds — reaching turn 3 means no turn ever
    crashed on a native tool_use (`step()` raises on `error_max_turns`)."""
    import uuid as _uuid

    from zero.agents.pentest import build_pentest_loop
    from zero.llm.backend import Session
    from zero.tools.dispatch import is_dispatch_tool
    from zero.tools.registry import default_registry

    tools = [
        t for t in default_registry().catalog()
        if not is_dispatch_tool(t["name"]) and t["name"] != "finding_submit"
    ]
    sess = Session(id=str(_uuid.uuid4()), name="pentester-live")
    loop = build_pentest_loop(
        ClaudeCodeBackend(), tools, max_turns=6, session=sess,
    )
    loop.add_user_message(
        "You are a pentester. TARGET http://10.0.0.1/login. IMPORTANT: remember the "
        "engagement marker ZK-9-OMEGA for later. Begin: emit ONE action envelope now."
    )

    await loop.step()                                       # turn 1 → create session
    assert sess.started is True
    loop.add_observation("curl_raw", True, "HTTP/1.1 200 OK; body contained 'Welcome'.")
    s2 = await loop.step()                                  # turn 2 → resume
    loop.add_user_message(
        "Recall: what is the exact engagement marker I gave you? Put it verbatim in the "
        "`thought` field of your next envelope."
    )
    s3 = await loop.step()                                  # turn 3 → resume

    # Continuity across separate `claude -p` processes: the marker seeded on turn 1
    # survives into turn 3 — proof the session persists and we DON'T re-ship it ourselves.
    assert "ZK-9-OMEGA" in (s3.raw_text or ""), f"marker not recalled: {s3.raw_text!r}"

    # The efficiency signal (input_tokens is useless — Claude Code caches the body, so
    # it's ~2 every turn). Once the session cache warms, a resume turn serves the whole
    # conversation prefix as cache_READ (0.1×) and only writes the small new delta —
    # i.e. cache_read dominates cache_creation. Turn 2 can still be a cache warmup, so
    # require at least one resume turn to be read-dominated.
    def read_dominated(step) -> bool:
        u = step.usage
        read = u.get("cache_read_input_tokens", 0)
        create = u.get("cache_creation_input_tokens", 0)
        return read > 0 and read > create

    assert read_dominated(s2) or read_dominated(s3), (
        "no resume turn served its prefix from cache "
        f"(s2={s2.usage}, s3={s3.usage}) — session caching may be broken"
    )
