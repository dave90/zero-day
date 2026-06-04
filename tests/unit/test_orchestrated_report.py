"""The clean final report (M8) separates found & approved from tested/failed."""
from pathlib import Path

from zero.findings.dedup import dedup_hash
from zero.findings.report import write_report
from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import FindingsStore, new_finding_id
from zero.orchestrator.subagents import AttackAttempt


def _persist(store: FindingsStore, title: str, verdict: str):
    f = Finding(title=title, summary="s", endpoint=f"/{title}", method="GET", cwe="CWE-79", evidence="e")
    v = Validation(verdict=verdict, rationale=f"because {verdict}", reproducer="repro",
                   cvss40_score=5.0 if verdict == "confirmed" else 0.0)
    rec = FindingRecord(id=new_finding_id(), finding=f, validation=v, dedup_hash=dedup_hash(f))
    store.promote(rec, verdict)


def test_report_has_four_sections_found_vs_failed(tmp_path: Path):
    store = FindingsStore(tmp_path)
    _persist(store, "RealXSS", "confirmed")
    _persist(store, "FakeSQLi", "rejected")
    _persist(store, "MaybeIDOR", "pending")
    attempts = [
        AttackAttempt("#1", "pentester", "/login", "auth bypass", status="NO_FINDING", outcome="rate limited"),
        AttackAttempt("#2", "pentester", "/admin", "open redirect", status="ERROR", outcome="tool crashed"),
        AttackAttempt("#3", "validator", "/x", "RealXSS", status="confirmed"),  # not a failed attempt
    ]
    out = write_report(store, tmp_path / "report.md", attempts=attempts)
    text = out.read_text(encoding="utf-8")

    # All four sections present.
    assert "## Found & approved" in text
    assert "## Tested & rejected" in text
    assert "## Pending" in text
    assert "## Tested but didn't work" in text

    # Found & approved holds the confirmed finding; rejected/failed are elsewhere.
    found_idx = text.index("## Found & approved")
    rejected_idx = text.index("## Tested & rejected")
    failed_idx = text.index("## Tested but didn't work")
    assert found_idx < rejected_idx < failed_idx
    assert "RealXSS" in text[found_idx:rejected_idx]
    assert "FakeSQLi" in text[rejected_idx:]

    # Failed attempts list the pentester ideas (not the validator attempt).
    failed_section = text[failed_idx:]
    assert "auth bypass @ `/login`" in failed_section
    assert "open redirect @ `/admin`" in failed_section
    assert "RealXSS" not in failed_section  # the validator attempt isn't a "didn't work"
