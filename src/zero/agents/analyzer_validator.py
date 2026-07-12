"""Analyzer-Validator agent: reviews the PRD and judges task completion.

Builder shape mirrors `validator.py`. Parsing of its finals lives in
`zero.ralph.prd` (`parse_critique`, `parse_task_verdicts`).
"""
from __future__ import annotations

from pathlib import Path

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, Session

PROMPT_PATH = Path(__file__).parent / "prompts" / "analyzer_validator_system.md"

ANALYZER_VALIDATOR_WRAPUP_PROMPT = (
    "You have reached your turn limit — this is your LAST message. Emit your FINAL"
    " JSON verdict now (the shape the active job requires: an epic review or PRD review"
    ' {"approved":..., "feedback":..., "missing":[...]} or completion verdicts'
    ' {"verdicts":[...]}). Do NOT return a bare "out of turns" message.'
)

ANALYZER_VALIDATOR_REMINDER = (
    "<system-reminder> Be distrustful: check claims with a tool before you sign off."
    " Approve a PRD only when it truly covers the goal; mark a task complete only on"
    " real, exhausted coverage. </system-reminder>"
)


def load_system_prompt(project_context: str | None = None) -> str:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    if project_context:
        return body + "\n\n## Project context\n\n" + project_context + "\n"
    return body


def build_analyzer_validator_loop(
    backend: LLMBackend,
    tools: list[dict],
    *,
    project_context: str | None = None,
    max_turns: int = 6,
    session: Session | None = None,
) -> AgentLoop:
    return AgentLoop(
        backend=backend,
        system_prompt=load_system_prompt(project_context),
        tools=tools,
        max_turns=max_turns,
        wrapup_prompt=ANALYZER_VALIDATOR_WRAPUP_PROMPT,
        reminder_text=ANALYZER_VALIDATOR_REMINDER,
        reminder_every=4,
        session=session,
    )
