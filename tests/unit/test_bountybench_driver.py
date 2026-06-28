"""Offline, free unit tests for the BountyBench `zero` driver.

These exercise the pure/collection logic of `benchmarks/bountybench/zero_driver.py`
against a seeded engagement tree — no orchestrator, no Docker, no tokens. The live
end-to-end path (driving a real `zero` run) is covered by the opt-in pilot, not here.
"""
from __future__ import annotations

import json

from benchmarks.bountybench.bb_overlay.zero_driver import (
    EngagementResult,
    _find_exploit_script,
    _render_pentest_md,
    _sum_usage,
    build_bountybench_goal,
    collect_engagement_result,
    synthesize_exploit_from_finding,
)
from zero.findings.dedup import dedup_hash
from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import FindingsStore


def _make_record(title: str, cwe: str, *, verdict: str = "confirmed") -> FindingRecord:
    finding = Finding(
        title=title,
        summary="seeded",
        endpoint="GET /api/x",
        cwe=cwe,
        evidence="curl http://target/api/x -> leaks data",
        payload_shape="idor/sequential-id",
    )
    return FindingRecord(
        id=title.lower().replace(" ", "")[:10],
        finding=finding,
        validation=Validation(
            verdict=verdict,
            rationale="seeded",
            reproducer="curl -s http://target/api/x | grep secret",
            cwe=cwe,
        ),
        dedup_hash=dedup_hash(finding),
    )


def test_render_pentest_md_includes_target_and_exploit_contract():
    md = _render_pentest_md(
        system="lunary", bounty_number=0, target_host="http://lunary:3333",
        writeup="IDOR in /v1/projects", cwe="CWE-639",
        goal="Find and prove one defect.", exploit_path="/zero_files/exploit.sh",
    )
    assert "http://lunary:3333" in md
    assert "/zero_files/exploit.sh" in md
    assert "CWE-639" in md
    # The exit-code contract BB scores on must be spelled out.
    assert "exit 0 when the vulnerability is present" in md


def test_render_pentest_md_omits_cwe_line_when_absent():
    md = _render_pentest_md(
        system="t", bounty_number=1, target_host="http://t", writeup="",
        cwe=None, goal="g", exploit_path="/zero_files/exploit.sh",
    )
    assert "hint" not in md.lower()
    assert "(no writeup provided)" in md


def test_render_pentest_md_points_to_verify_scripts_when_given():
    # BountyBench gives Exploit agents verifier files on disk and tells them to inspect
    # those files. For fairness, the prompt should point to the files without inlining
    # verifier bodies or adding extra grader-state hints.
    md = _render_pentest_md(
        system="mlflow", bounty_number=0, target_host="http://mlflow:5002",
        writeup="path traversal", cwe="CWE-23", goal="g",
        exploit_path="/work/exploit.sh",
        verify_files={
            "verify.sh": "docker exec mlflow test -f /tmp/mlflow/bounty0",
            "check_mlflow_flag.py": 'last == "pwned - mlflow"',
        },
    )
    assert "Verifier Files" in md
    assert "/zero_files/verify_files/" in md
    assert "/tmp/mlflow/bounty0" not in md
    assert "pwned - mlflow" not in md
    assert "`verify.sh`" in md and "`check_mlflow_flag.py`" in md


def test_render_pentest_md_no_verify_section_without_scripts():
    md = _render_pentest_md(
        system="t", bounty_number=0, target_host="http://t", writeup="",
        cwe=None, goal="g", exploit_path="/work/exploit.sh", verify_files=None,
    )
    assert "Verifier Files" not in md


def test_detect_goal_requires_broad_blind_endpoint_coverage():
    goal = build_bountybench_goal("detect", "http://target")
    assert "endpoint inventory" in goal
    assert "authorization as a matrix" in goal
    assert "state-changing methods" in goal
    assert "seeded accounts/roles" in goal
    assert "Do not stop at the first plausible bug" in goal
    # Keep Detect blind: the generic methodology must not smuggle grader specifics.
    assert "verify_files" not in goal
    assert "DELETE /v1/projects" not in goal
    assert "3e1d5bb2" not in goal


def test_sum_usage_sums_only_agent_step_events():
    audit = [
        {"kind": "session_start"},
        {"kind": "agent_step", "usage": {"input_tokens": 10, "output_tokens": 3}},
        {"kind": "agent_step", "usage": {"input_tokens": 5, "cache_read_input_tokens": 2}},
        {"kind": "tool_proposal", "usage": {"input_tokens": 999}},  # ignored
        {"kind": "agent_step"},  # no usage block — tolerated
    ]
    assert _sum_usage(audit) == {
        "input_tokens": 15,
        "output_tokens": 3,
        "cache_read_input_tokens": 2,
    }


def test_collect_engagement_result_reads_seeded_tree(tmp_path):
    # Seed findings.
    store = FindingsStore(tmp_path / ".pentest" / "findings")
    store.promote(_make_record("SQL Injection", "CWE-89"), "confirmed")
    store.promote(_make_record("Weak thing", "CWE-200", verdict="rejected"), "rejected")
    # Seed audit.
    audit_path = tmp_path / ".pentest" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        "\n".join(
            json.dumps(e)
            for e in [
                {"kind": "container_started"},
                {"kind": "tool_proposal"},
                {"kind": "tool_proposal"},
                {"kind": "agent_step", "usage": {"input_tokens": 7}},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Seed the agent's exploit.sh.
    (tmp_path / "zero_files").mkdir(parents=True, exist_ok=True)
    (tmp_path / "zero_files" / "exploit.sh").write_text("#!/bin/bash\nexit 0\n")

    res = collect_engagement_result(tmp_path)
    assert isinstance(res, EngagementResult)
    assert res.has_exploit
    assert res.found and res.best_finding().finding.cwe == "CWE-89"
    assert len(res.rejected) == 1
    assert res.container_started
    assert res.tool_calls == 2
    assert res.usage == {"input_tokens": 7}
    assert res.error is None


def test_collect_engagement_result_handles_empty_tree(tmp_path):
    res = collect_engagement_result(tmp_path, error="boom")
    assert not res.has_exploit
    assert not res.found
    assert res.tool_calls == 0
    assert res.error == "boom"


def test_synthesize_exploit_from_finding_uses_reproducer():
    rec = _make_record("IDOR", "CWE-639")
    script = synthesize_exploit_from_finding(rec)
    assert script is not None
    assert script.startswith("#!/bin/bash")
    assert "grep secret" in script  # reproducer body carried through
    assert "CWE-639" in script


def test_find_exploit_script_prefers_work_path_and_newest(tmp_path):
    # No script anywhere.
    assert _find_exploit_script(tmp_path) is None
    # Script in the jail (/zero_files) is found.
    zf = tmp_path / "zero_files"
    zf.mkdir()
    (zf / "exploit.sh").write_text("#!/bin/bash\n# jail\nexit 0\n")
    assert _find_exploit_script(tmp_path) == zf / "exploit.sh"
    # A newer script at the /work path (cwd/exploit.sh) wins.
    work = tmp_path / "exploit.sh"
    work.write_text("#!/bin/bash\n# work\nexit 0\n")
    import os
    os.utime(work, (10**10, 10**10))  # force newer mtime
    assert _find_exploit_script(tmp_path) == work


def test_find_exploit_script_ignores_empty(tmp_path):
    (tmp_path / "exploit.sh").write_text("")  # empty → ignored
    assert _find_exploit_script(tmp_path) is None


def test_synthesize_exploit_returns_none_without_repro():
    finding = Finding(title="t", summary="s", endpoint="GET /", evidence="")
    rec = FindingRecord(id="x", finding=finding, dedup_hash=dedup_hash(finding))
    assert synthesize_exploit_from_finding(rec) is None
