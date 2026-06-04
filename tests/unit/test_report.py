from pathlib import Path

from zero.findings.dedup import dedup_hash
from zero.findings.report import write_report
from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import FindingsStore, new_finding_id


def test_report_lists_confirmed(tmp_path: Path):
    s = FindingsStore(tmp_path)
    for i in range(2):
        f = Finding(
            title=f"Bug {i}", summary="...", endpoint=f"/x/{i}",
            method="GET", cwe="CWE-79", evidence="...",
        )
        v = Validation(verdict="confirmed", rationale="r", reproducer="repro",
                       cvss40_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N",
                       cvss40_score=7.5, cwe="CWE-79")
        rec = FindingRecord(id=new_finding_id(), finding=f, validation=v, dedup_hash=dedup_hash(f))
        s.promote(rec, "confirmed")
    out = write_report(s, tmp_path / "report.md")
    text = out.read_text(encoding="utf-8")
    assert "**2 confirmed**" in text
    assert "## Found & approved" in text
    assert "Bug 0" in text and "Bug 1" in text
    assert "CVSS 4.0" in text


def test_report_empty(tmp_path: Path):
    s = FindingsStore(tmp_path)
    out = write_report(s, tmp_path / "r.md")
    text = out.read_text(encoding="utf-8")
    assert "_(no confirmed findings)_" in text
    # The clean-report sections are always present.
    assert "## Tested & rejected" in text
    assert "## Tested but didn't work" in text
