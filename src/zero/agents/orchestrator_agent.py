"""Orchestrator agent: the senior pentester that dispatches subagents (M8)."""
from __future__ import annotations

import json
from pathlib import Path

from zero.agents.base import AgentLoop
from zero.llm.backend import LLMBackend, Session

PROMPT_PATH = Path(__file__).parent / "prompts" / "orchestrator_system.md"

# Periodic nudge (Claude-Code "<system-reminder>"): hold the big picture, keep the
# plan + memory current, validate before finalizing, pivot off dead ends.
ORCHESTRATOR_REMINDER = (
    "<system-reminder> Keep the engagement moving: update your `todo_write` plan and"
    " `memory_write` map of the chain so far, validate strong candidates (a Validator"
    " verdict — your own recon doesn't count), and pivot off dead ends rather than"
    " re-dispatching the same idea. </system-reminder>"
)


def load_system_prompt(project_context: str | None = None) -> str:
    body = PROMPT_PATH.read_text(encoding="utf-8")
    if project_context:
        return body + "\n\n## Project context\n\n" + project_context + "\n"
    return body


def build_orchestrator_loop(
    backend: LLMBackend,
    tools: list[dict],
    *,
    project_context: str | None = None,
    max_turns: int = 20,
    session: Session | None = None,
) -> AgentLoop:
    return AgentLoop(
        backend=backend,
        system_prompt=load_system_prompt(project_context),
        tools=tools,
        max_turns=max_turns,
        reminder_text=ORCHESTRATOR_REMINDER,
        reminder_every=3,
        session=session,
    )


def parse_pentester_result(text: str) -> dict:
    """Tolerantly parse a pentester subagent's final answer.

    Expected JSON: {status, summary, candidate_findings:[...], discovered:[...]}.
    Falls back to treating the whole answer as an INCOMPLETE summary: malformed
    output proves neither that the task finished nor that the hypothesis was ruled
    out, but it still must not crash the orchestrator."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip("`\n")
    try:
        obj = json.loads(t)
        if not isinstance(obj, dict):
            raise ValueError("not an object")
    except Exception:  # noqa: BLE001
        return {
            "status": "INCOMPLETE",
            "summary": text.strip()[:1000],
            "candidate_findings": [],
            "discovered": [],
        }
    raw_status = obj.get("status")
    status = raw_status.strip().upper() if isinstance(raw_status, str) else ""
    if status not in {"EXPLOITED", "NO_FINDING", "INCOMPLETE"}:
        # A missing, null, or unknown status is uncertain, not a completed result.
        status = "INCOMPLETE"
    return {
        "status": status,
        "summary": str(obj.get("summary", "")),
        "candidate_findings": list(obj.get("candidate_findings") or []),
        "discovered": [str(d) for d in (obj.get("discovered") or [])],
    }
