"""The orchestrator must decompose a kill chain into single provable steps and
sequence them itself, rather than handing a whole end-to-end chain to one
budget-limited subagent. We assert the guidance is present in (a) the orchestrator
system prompt and (b) the per-dispatch pentester brief — the two places the agents
actually read it."""
import json

from zero.agents.orchestrator_agent import load_system_prompt
from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.subagents import SubagentRecord


def test_orchestrator_prompt_teaches_single_step_decomposition():
    body = load_system_prompt().lower()
    # Scope each dispatch to one provable step, not a whole chain.
    assert "provable step" in body or "smallest provable unit" in body
    assert "never a whole chain" in body or "not the whole" in body or "one link" in body
    # The orchestrator sequences the chain itself, seeding each step with the prior artifact.
    assert "sequence the chain yourself" in body
    assert "seeded with it" in body or "seeded with" in body
    # Names the exact antipattern (numbered multi-step plan in one attack_idea) and
    # gives a concrete BAD vs GOOD example so the rule isn't merely abstract.
    assert "step 1" in body and "step 2" in body  # the BAD example's numbered plan
    assert "✗ bad" in body and "✓ good" in body
    # A blunt hard limit reinforcing one step per dispatch.
    assert "one step per dispatch" in body


class _ImmediateFinal(LLMBackend):
    async def run(self, system_prompt, messages, tools):
        return LLMResponse(text=json.dumps({
            "type": "final",
            "answer": json.dumps({"status": "NO_FINDING", "summary": "n/a"}),
        }))


async def test_pentester_brief_says_single_link_not_whole_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", execution_mode="c", yolo=True)
    orch = Orchestrator(cfg)
    orch._subagent_backend_factory = lambda rec: _ImmediateFinal()

    rec = SubagentRecord(id="gino_1", kind="pentester", target="https://h/x", idea="recover the secret")
    await orch._run_pentester_subagent(rec)

    brief = next(m.content for m in rec.loop.history if m.role == "user")
    assert "ONE link of a larger chain" in brief
    assert "do NOT try to carry the attack end-to-end" in brief
    assert "the orchestrator will dispatch the next step seeded with it" in brief
