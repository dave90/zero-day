"""AgentRouter — owns the Pentest → Validator handoff.

The orchestrator delegates two responsibilities here:

1. On a `finding_submit` action from the Pentest agent: parse args, run
   dedup, either drop with a duplicate observation or spin up the
   Validator to investigate the finding.

2. Persist the result (FindingRecord) under `.pentest/findings/{verdict}/`
   and update the index.

The Validator runs as its own AgentLoop with a separate history.
Cross-agent isolation: nothing leaks except the structured `Finding` and
`Validation` objects.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from zero.agents.base import AgentLoop
from zero.agents.validator import (
    build_validator_loop,
    parse_validation,
    seed_with_finding,
)
from zero.findings.dedup import dedup_hash
from zero.findings.schema import Finding, FindingRecord, Validation, Verdict
from zero.findings.store import FindingsStore, new_finding_id
from zero.llm.backend import LLMBackend
from zero.llm.envelope import ActionEnvelope, AskUserEnvelope, FinalEnvelope


@dataclass
class HandoffResult:
    duplicate: bool = False
    duplicate_of: str | None = None
    record: FindingRecord | None = None
    observation_to_pentest: str = ""  # the message fed back into the Pentest loop


class AgentRouter:
    """Orchestrator-side state. One per session."""

    def __init__(
        self,
        store: FindingsStore,
        *,
        backend: LLMBackend,
        tools: list[dict],
        project_context: str | None,
        validator_max_turns: int = 40,
        goal: str | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.tools = tools
        self.project_context = project_context
        self.validator_max_turns = validator_max_turns
        # Operator goal (carries the base URL/host). The validator doesn't see
        # the orchestrator's conversation, so we seed it alongside the finding.
        self.goal = goal

    async def handle_finding_submit(
        self,
        finding: Finding,
        *,
        drive_validator: "ValidatorDriver",
    ) -> HandoffResult:
        h = dedup_hash(finding)
        prior = self.store.lookup_by_hash(h)
        if prior is not None:
            return HandoffResult(
                duplicate=True,
                duplicate_of=prior["id"],
                observation_to_pentest=(
                    f"duplicate of {prior['id']} (verdict={prior['verdict']}); "
                    "no new finding written."
                ),
            )

        # New finding → spin up the Validator. Exclude `finding_submit` from
        # the Validator's tools — the Validator produces a Validation, not
        # another Finding, and keeping finding_submit in scope would let it
        # recurse into another Validator pass.
        validator_tools = [t for t in self.tools if t.get("name") != "finding_submit"]
        loop = build_validator_loop(
            self.backend,
            validator_tools,
            project_context=self.project_context,
            max_turns=self.validator_max_turns,
        )
        seed_with_finding(loop, finding)
        if self.goal:
            loop.add_user_message(
                f"ENGAGEMENT GOAL (from the operator, for the base URL/host): {self.goal}"
            )
        validation: Validation | None = None
        validator_error: str | None = None
        try:
            validation = await drive_validator(loop)
        except Exception as e:  # noqa: BLE001 — surface any backend failure
            validator_error = f"{type(e).__name__}: {e}"
        if validation is None:
            # Validator failed (or produced no Validation); park as pending
            # with the error captured in the finding notes so the user can
            # see WHY validation didn't happen.
            note = "validator produced no Validation"
            if validator_error:
                note = f"validator error: {validator_error}"
            finding_with_note = finding.model_copy(
                update={
                    "notes": (finding.notes + "\n\n[router] " + note).strip()
                }
            )
            rec = FindingRecord(
                id=new_finding_id(),
                finding=finding_with_note,
                validation=None,
                dedup_hash=h,
            )
            path = self.store.submit_pending(rec)
            return HandoffResult(
                record=rec,
                observation_to_pentest=(
                    f"{note}; parked as pending at {path.name}."
                    " gather more evidence or retry."
                ),
            )

        rec = FindingRecord(
            id=new_finding_id(),
            finding=finding,
            validation=validation,
            dedup_hash=h,
        )
        verdict: Verdict = validation.verdict
        self.store.promote(rec, verdict)
        return HandoffResult(
            record=rec,
            observation_to_pentest=_format_validator_feedback(rec.id, validation),
        )


def _format_validator_feedback(rec_id: str, v: Validation) -> str:
    """Structured feedback the Pentest agent sees as a tool observation.

    The Pentest agent uses this to decide what to do next:
      - confirmed → move on to other surfaces (and dedup will block resubmits)
      - rejected  → revise approach; the rationale + reproducer explain what
                    the validator actually tried, so the Pentest agent can
                    address the gap (e.g. "rejected because evidence didn't
                    show cross-tenant impact — retry with two accounts")
      - pending   → gather more evidence; reproducer says what's missing
    """
    lines = [f"VALIDATOR VERDICT: {v.verdict}  (finding id={rec_id})"]
    if v.cvss40_score or v.cvss40_vector:
        lines.append(f"CVSS 4.0: {v.cvss40_score}  {v.cvss40_vector}")
    if v.cwe:
        lines.append(f"CWE: {v.cwe}")
    lines.append("")
    lines.append("Rationale:")
    lines.append(v.rationale.strip() or "(none provided)")
    if v.reproducer.strip():
        lines.append("")
        lines.append("Reproducer the validator used:")
        lines.append(v.reproducer.strip())
    if v.additional_evidence.strip():
        lines.append("")
        lines.append("Additional evidence the validator gathered:")
        lines.append(v.additional_evidence.strip())
    # Actionable next-step hint per verdict.
    lines.append("")
    if v.verdict == "rejected":
        lines.append(
            "NEXT: do NOT resubmit the same shape — the rationale above"
            " explains the gap. Address it, then submit a refined finding."
        )
    elif v.verdict == "pending":
        lines.append(
            "NEXT: the validator couldn't fully confirm. Gather the missing"
            " evidence implied by the rationale, then resubmit."
        )
    else:  # confirmed
        lines.append(
            "NEXT: confirmed and persisted. Move on to other attack surface;"
            " resubmitting the same shape will dedup."
        )
    return "\n".join(lines)


# Callable type: orchestrator-side driver that runs the validator's loop to
# completion (handling its own tool calls + approvals + ask_user). Returns
# the parsed Validation or None.
from typing import Any, Awaitable, Callable

ValidatorDriver = Callable[[AgentLoop], Awaitable[Validation | None]]


async def default_validator_driver(
    loop: AgentLoop,
    *,
    action_handler: Callable[[ActionEnvelope], Awaitable[str]],
    ask_handler: Callable[[AskUserEnvelope], Awaitable[str]],
    on_step: Callable[[int, Any], None] | None = None,
    deadline: float | None = None,
) -> Validation | None:
    """Generic driver: handle action / ask_user / final until done. Caller
    supplies the per-envelope handlers (so the orchestrator can wire the
    same ApprovalGate + audit log). `on_step(turn, envelope)` is an optional
    observer used to log the validator's reasoning.

    `deadline` is an optional `time.monotonic()` timestamp; when reached
    (checked between turns) the driver gives up and returns None, so the
    finding is parked as pending rather than letting the validator run
    unbounded."""
    while True:
        if deadline is not None and time.monotonic() > deadline:
            return None
        step = await loop.step()
        env = step.envelope
        if on_step is not None:
            on_step(loop.turn, env)
        if isinstance(env, FinalEnvelope):
            return parse_validation(env)
        if isinstance(env, AskUserEnvelope):
            answer = await ask_handler(env)
            loop.add_ask_user_answer(answer)
            continue
        if isinstance(env, ActionEnvelope):
            obs = await action_handler(env)
            loop.add_observation(env.tool, ok=True, output=obs)
            continue
