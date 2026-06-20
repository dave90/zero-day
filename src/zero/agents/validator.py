"""Validator agent: loads its prompt and drives the shared AgentLoop."""
from __future__ import annotations

import json
import re
from pathlib import Path

from zero.agents.base import AgentLoop
from zero.findings.schema import Finding, Validation
from zero.llm.backend import LLMBackend, Session
from zero.llm.envelope import FinalEnvelope

PROMPT_PATH = Path(__file__).parent / "prompts" / "validator_system.md"

# Spent when the validator hits its turn cap: force it to commit a verdict + the
# evidence gathered so far, instead of a bare "out of turns" (which would park the
# finding as pending with no reasoning).
VALIDATOR_WRAPUP_PROMPT = (
    "You have reached your turn limit — this is your LAST message. Emit your FINAL"
    " Validation envelope NOW (the required JSON). Commit to the verdict your"
    " evidence best supports (confirmed / rejected / pending), and put everything you"
    " established — reproduction steps, what you confirmed, and what remained"
    " unverified — in the reasoning. Do NOT return a bare 'out of turns' message."
)


# Periodic nudge (Claude-Code "<system-reminder>"): keep testing, keep memory current.
VALIDATOR_REMINDER = (
    "<system-reminder> Keep going — actually reproduce the finding with a tool call"
    " before you decide; don't return `pending` just because it's faster. Checkpoint"
    " with `memory_write` (what you reproduced, what's still unverified) and keep your"
    " `todo_write` plan current. </system-reminder>"
)


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
    session: Session | None = None,
) -> AgentLoop:
    return AgentLoop(
        backend=backend,
        system_prompt=load_system_prompt(project_context),
        tools=tools,
        max_turns=max_turns,
        wrapup_prompt=VALIDATOR_WRAPUP_PROMPT,
        reminder_text=VALIDATOR_REMINDER,
        reminder_every=6,
        session=session,
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
    """Parse the validator's `final.answer` into a Validation.

    The intended shape is a JSON-encoded Validation object (see validator_system.md).
    But a validator running on a persistent `--resume` session — where the detailed
    JSON-answer spec is back in the create-turn system prompt, not in the recent
    delta — often drifts to a PROSE verdict like "VALIDATION: CONFIRMED — …". That's
    a clearly-reached verdict and must NOT be discarded (which parks the finding as a
    misleading `pending`). So: try strict JSON first, then fall back to salvaging the
    verdict from prose, keeping the prose as the rationale. Mirrors the tolerant
    `parse_pentester_result`."""
    text = final.answer.strip()
    # Strip code fence if present.
    if text.startswith("```"):
        # naive fence strip
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip("`\n")
    # 1. Strict JSON — the intended format (keeps the rich structured fields).
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        obj = None
    if isinstance(obj, dict):
        try:
            return Validation(**obj)
        except Exception:  # noqa: BLE001
            pass  # fall through to prose salvage

    # 2. Prose fallback: extract a clearly-stated verdict so the validator's work
    #    isn't thrown away. Keep the full text as the rationale.
    verdict = _verdict_from_prose(text)
    if verdict is None:
        return None
    return Validation(
        verdict=verdict,
        rationale=text,
        reproducer="(validator emitted a prose verdict; see rationale and the "
        "validator's memory/tool log for the full reproduction)",
    )


def _verdict_from_prose(text: str) -> str | None:
    """Pull a `confirmed` / `rejected` / `pending` verdict out of a prose final.

    Prefers an explicit marker ("VERDICT: X" / "VALIDATION: X" / "VALIDATION — X")
    over a bare keyword, so a sentence that merely mentions a verdict word doesn't
    win over the validator's stated conclusion."""
    low = text.lower()
    m = re.search(
        r"(?:verdict|validation)\s*[:\-—\s]+\"?(confirmed|rejected|pending)", low
    )
    if m:
        return m.group(1)
    m = re.search(r"\b(confirmed|rejected|pending)\b", low)
    return m.group(1) if m else None
