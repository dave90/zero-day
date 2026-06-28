"""Live `codex exec` integration tests.

These actually invoke the OpenAI Codex CLI — slow, cost tokens, and require `codex`
on PATH and authenticated. Gated by:

  * the `live` marker (excluded from the default suite; opt in with `-m live`)
  * a `live_codex` marker so you can target just these
  * a skipif when `codex` isn't available, so the suite stays green without it

Run only these:
    uv run pytest -q -m live tests/e2e/test_live_codex.py
"""
from __future__ import annotations

import shutil

import pytest

from zero.llm.backend import Message, Session
from zero.llm.codex_cli import CodexCLIBackend
from zero.llm.envelope import (
    ActionEnvelope,
    ActionsEnvelope,
    AskUserEnvelope,
    FinalEnvelope,
    parse_envelope,
)

pytestmark = [
    pytest.mark.live,           # token-spending live run — excluded from the default suite
    pytest.mark.live_codex,
    pytest.mark.skipif(
        shutil.which("codex") is None,
        reason="codex CLI not on PATH",
    ),
]


SIMPLE_SYSTEM = """\
You are an envelope-only assistant. Reply with EXACTLY one JSON object,
no prose, no markdown fence. Do NOT run any shell commands. Allowed shapes:

  {"type": "action",   "tool": "<name>", "args": {...}, "thought": "..."}
  {"type": "ask_user", "question": "...", "options": [...], "free_form": true, "blocking": true}
  {"type": "final",    "answer": "..."}
"""


async def test_codex_returns_well_formed_envelope():
    """Smoke test: the real `codex exec` returns text we can parse as an envelope with
    the same machinery the agent loop uses."""
    backend = CodexCLIBackend()
    resp = await backend.run(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(
                role="user",
                content='Emit exactly: {"type":"final","answer":"pong"}',
            )
        ],
        tools=[],
    )
    env = parse_envelope(resp.text)
    assert isinstance(env, (ActionEnvelope, ActionsEnvelope, AskUserEnvelope, FinalEnvelope))


async def test_codex_session_resume_keeps_context_and_caches():
    """End-to-end of the session token-saver: drive a real Codex thread over three
    turns. Assert (a) Codex mints a thread id captured into the session and reused on
    resume, (b) a marker seeded on turn 1 survives to turn 3 (the thread persists
    across separate `codex exec` processes — we don't re-ship it), and (c) at least one
    resume turn serves its prefix from cache (cache_read_input_tokens > 0)."""
    backend = CodexCLIBackend()
    sess = Session(id="seed-uuid", name="codex-live")

    r1 = await backend.run(
        system_prompt=SIMPLE_SYSTEM,
        messages=[
            Message(
                role="user",
                content=(
                    "Remember the engagement marker ZK-9-OMEGA for later. "
                    'Reply now with exactly: {"type":"final","answer":"ack"}'
                ),
            )
        ],
        tools=[],
        session=sess,
    )
    parse_envelope(r1.text)
    assert sess.started is True
    # Codex assigned a real thread id (not our throwaway seed).
    thread_id = (r1.raw or {}).get("thread_id")
    assert thread_id and sess.id == thread_id and sess.id != "seed-uuid"

    r2 = await backend.run(
        SIMPLE_SYSTEM,
        [Message(role="observation", content="(noted)")],
        [],
        session=sess,
    )
    parse_envelope(r2.text)

    r3 = await backend.run(
        SIMPLE_SYSTEM,
        [
            Message(
                role="user",
                content=(
                    "Recall the exact engagement marker I gave you and put it verbatim "
                    'in the answer field, e.g. {"type":"final","answer":"<marker>"}'
                ),
            )
        ],
        [],
        session=sess,
    )
    assert "ZK-9-OMEGA" in (r3.text or ""), f"marker not recalled: {r3.text!r}"

    def cached(resp) -> int:
        return resp.usage.get("cache_read_input_tokens", 0)

    assert cached(r2) > 0 or cached(r3) > 0, (
        "no resume turn served its prefix from cache "
        f"(r2={r2.usage}, r3={r3.usage}) — Codex session caching may be broken"
    )
