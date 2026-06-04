"""Dedup + validator handoff, with a stubbed validator driver."""
from __future__ import annotations

from pathlib import Path

from zero.findings.dedup import dedup_hash
from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import FindingsStore, new_finding_id
from zero.llm.echo import EchoBackend
from zero.orchestrator.router import AgentRouter


def _finding() -> Finding:
    return Finding(
        title="IDOR /orders/{id}", summary="cross-tenant read",
        endpoint="/orders/1041", method="GET", cwe="CWE-639",
        evidence="GET /orders/1041 returns user B's data",
    )


def _validation(verdict="confirmed") -> Validation:
    return Validation(
        verdict=verdict, rationale="reproduced", reproducer="login A; GET B's id",
        cvss40_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N",
        cvss40_score=8.7, cwe="CWE-639",
    )


async def _confirming_driver(loop):
    return _validation("confirmed")


async def _rejecting_driver(loop):
    return _validation("rejected")


async def _crashing_driver(loop):
    return None  # validator failed to produce a Validation


async def test_first_submission_persists_via_validator(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)
    res = await r.handle_finding_submit(_finding(), drive_validator=_confirming_driver)
    assert res.duplicate is False
    assert res.record is not None
    assert res.record.validation.verdict == "confirmed"
    assert (tmp_path / "confirmed" / f"{res.record.id}.md").exists()


async def test_second_identical_submission_is_duplicate(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)
    await r.handle_finding_submit(_finding(), drive_validator=_confirming_driver)
    res2 = await r.handle_finding_submit(_finding(), drive_validator=_confirming_driver)
    assert res2.duplicate is True
    assert res2.duplicate_of is not None
    assert res2.record is None
    # Only one confirmed file on disk.
    assert len(list((tmp_path / "confirmed").glob("*.md"))) == 1


async def test_rejected_lands_in_rejected_dir(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)
    res = await r.handle_finding_submit(_finding(), drive_validator=_rejecting_driver)
    assert res.record.validation.verdict == "rejected"
    assert (tmp_path / "rejected" / f"{res.record.id}.md").exists()
    assert not (tmp_path / "confirmed" / f"{res.record.id}.md").exists()


async def test_validator_crash_parks_pending(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)
    res = await r.handle_finding_submit(_finding(), drive_validator=_crashing_driver)
    assert res.record is not None
    assert res.record.validation is None
    assert (tmp_path / "pending" / f"{res.record.id}.md").exists()


async def test_feedback_includes_full_rationale_reproducer_cvss_and_next_hint(
    tmp_path: Path,
):
    """The Pentest agent must see actionable feedback, not just yes/no."""
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)

    rich = Validation(
        verdict="rejected",
        rationale=(
            "Tried the proposed payload with two distinct accounts (A=alice,"
            " B=bob) and the response for B was always B's own data — there is"
            " no cross-tenant impact. The original evidence used the same"
            " account for both requests."
        ),
        reproducer=(
            "1) login as alice, capture session\n"
            "2) GET /orders/<bob-order-id> with alice's session\n"
            "3) observe 403 Forbidden, NOT bob's data"
        ),
        cvss40_vector="",
        cvss40_score=0.0,
        cwe="CWE-639",
        additional_evidence="The endpoint enforces an `ownership_check` middleware (see server log line 'denied: alice→bob/orders/91').",
    )

    async def driver(_loop):
        return rich

    res = await r.handle_finding_submit(_finding(), drive_validator=driver)
    obs = res.observation_to_pentest

    # Verdict line + ID
    assert "VALIDATOR VERDICT: rejected" in obs
    assert res.record.id in obs
    # Full rationale (not truncated at 300 chars — the rationale here is > 200)
    assert "cross-tenant impact" in obs
    assert "ownership_check" in obs  # additional_evidence
    # Reproducer surfaces
    assert "403 Forbidden" in obs
    # Next-step hint for the rejected verdict
    assert "do NOT resubmit" in obs


async def test_feedback_for_confirmed_carries_cvss_and_next_hint(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)
    res = await r.handle_finding_submit(_finding(), drive_validator=_confirming_driver)
    obs = res.observation_to_pentest
    assert "VALIDATOR VERDICT: confirmed" in obs
    assert "CVSS 4.0" in obs and "CVSS:4.0/" in obs
    assert "Move on" in obs


async def test_feedback_for_pending_hints_at_gathering_evidence(tmp_path: Path):
    s = FindingsStore(tmp_path)
    r = AgentRouter(store=s, backend=EchoBackend(), tools=[], project_context=None)

    async def driver(_loop):
        return Validation(
            verdict="pending",
            rationale="Need an authenticated session to verify.",
            reproducer="(not run — no credentials)",
        )

    res = await r.handle_finding_submit(_finding(), drive_validator=driver)
    obs = res.observation_to_pentest
    assert "VALIDATOR VERDICT: pending" in obs
    assert "Gather the missing evidence" in obs
