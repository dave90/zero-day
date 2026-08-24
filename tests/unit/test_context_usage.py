"""Per-agent context-window usage: token math, loop tracking, and formatting."""
import json

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, LLMResponse, context_tokens_from_usage
from zero.ui.render import fmt_context, fmt_tokens


def test_context_tokens_sums_input_and_both_cache_tiers():
    usage = {
        "input_tokens": 1000,
        "cache_creation_input_tokens": 2000,
        "cache_read_input_tokens": 5000,
        "output_tokens": 9999,  # NOT counted — it's the reply, not the next prompt
    }
    assert context_tokens_from_usage(usage) == 8000


def test_context_tokens_tolerates_missing_or_empty():
    assert context_tokens_from_usage(None) == 0
    assert context_tokens_from_usage({}) == 0
    assert context_tokens_from_usage({"input_tokens": 42}) == 42


def test_fmt_tokens_compact():
    assert fmt_tokens(940) == "940"
    assert fmt_tokens(48_000) == "48k"
    assert fmt_tokens(1_200_000) == "1.2M"


def test_fmt_context_pct_and_empty_window():
    assert fmt_context(48_000, 200_000) == "ctx 48k/200k (24%)"
    assert fmt_context(5_000, 0) == ""           # no window → no segment
    assert fmt_context(500_000, 200_000) == "ctx 500k/200k (100%)"  # clamped


class _UsageBackend(LLMBackend):
    """Returns a final envelope with a populated usage block."""

    async def run(self, system_prompt, messages, tools):
        env = json.dumps({"type": "final", "answer": "done"})
        return LLMResponse(text=env, usage={
            "input_tokens": 3000, "cache_read_input_tokens": 12000,
        })


async def test_agent_loop_tracks_context_tokens():
    loop = AgentLoop(_UsageBackend(), system_prompt="sys", tools=[])
    assert loop.context_tokens == 0   # before any call
    await loop.step()
    assert loop.context_tokens == 15000  # 3000 + 12000


class _SnapshotBackend(LLMBackend):
    async def run(self, system_prompt, messages, tools):
        env = json.dumps({"type": "final", "answer": "done"})
        return LLMResponse(
            text=env,
            usage={"input_tokens": 900_000},  # cumulative turn/billing usage
            context_tokens=13_268,
            context_window=258_400,
        )


async def test_agent_loop_prefers_explicit_provider_context_snapshot():
    loop = AgentLoop(_SnapshotBackend(), system_prompt="sys", tools=[])
    await loop.step()
    assert loop.context_tokens == 13_268
    assert loop.context_window == 258_400
