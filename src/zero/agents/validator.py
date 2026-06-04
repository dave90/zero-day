"""Validator agent: loads its prompt and drives the shared AgentLoop."""
from __future__ import annotations

import json
from pathlib import Path

from zero.agents.base import AgentLoop
from zero.findings.schema import Finding, Validation
from zero.llm.backend import LLMBackend
from zero.llm.envelope import FinalEnvelope

PROMPT_PATH = Path(__file__).parent / "prompts" / "validator_system.md"


def load_system_prompt(project_context: str | None = None) -> str:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    if project_context:
        return body + "\n\n## Project context\n\n" + project_context + "\n"
    return body


def build_validator_loop(
    backend: LLMBackend,
    tools: list[dict],
    *,
    project_context: str | None = None,
    max_turns: int = 40,
) -> AgentLoop:
    return AgentLoop(
        backend=backend,
        system_prompt=load_system_prompt(project_context),
        tools=tools,
        max_turns=max_turns,
    )


def seed_with_finding(loop: AgentLoop, finding: Finding) -> None:
    """Inject the Finding to validate as the first user message."""
    body = (
        "The Pentest agent submitted the following finding. Investigate it"
        " from scratch and produce a Validation in your final envelope.\n\n"
        + json.dumps(finding.model_dump(), indent=2)
    )
    loop.add_user_message(body)


def parse_validation(final: FinalEnvelope) -> Validation | None:
    """The validator's final.answer should be a JSON Validation object."""
    text = final.answer.strip()
    # Strip code fence if present.
    if text.startswith("```"):
        # naive fence strip
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip("`\n")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    try:
        return Validation(**obj)
    except Exception:  # noqa: BLE001
        return None
