"""Analyzer agent: recons the target and produces the Ralph PRD.

Same builder shape as `pentest.py` / `orchestrator_agent.py` — a prompt file plus a
`build_*_loop`. Parsing of its `final` lives in `zero.ralph.prd.parse_prd`.
"""
from __future__ import annotations

from pathlib import Path

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, Session

PROMPT_PATH = Path(__file__).parent / "prompts" / "analyzer_system.md"

# Spent when the analyzer hits its turn cap: force it to emit the PRD it has so far
# rather than a bare "out of turns" (which would yield an empty backlog).
ANALYZER_WRAPUP_PROMPT = (
    "You have reached your turn limit — this is your LAST message. Emit your FINAL"
    " PRD now: a `final` whose `answer` is ONLY the JSON object"
    ' {"goal":..., "tasks":[...]}. Include every task you have identified so far;'
    " do NOT return a bare 'out of turns' message."
)

ANALYZER_REMINDER = (
    "<system-reminder> Keep recon focused on PLANNING, not exploiting — enumerate"
    " the surface, then produce the prioritized task backlog. When you have enough"
    " to plan, emit the PRD JSON in a `final`. </system-reminder>"
)


def load_system_prompt(project_context: str | None = None) -> str:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    if project_context:
        return body + "\n\n## Project context\n\n" + project_context + "\n"
    return body


def build_analyzer_loop(
    backend: LLMBackend,
    tools: list[dict],
    *,
    project_context: str | None = None,
    max_turns: int = 8,
    session: Session | None = None,
) -> AgentLoop:
    return AgentLoop(
        backend=backend,
        system_prompt=load_system_prompt(project_context),
        tools=tools,
        max_turns=max_turns,
        wrapup_prompt=ANALYZER_WRAPUP_PROMPT,
        reminder_text=ANALYZER_REMINDER,
        reminder_every=4,
        session=session,
    )
