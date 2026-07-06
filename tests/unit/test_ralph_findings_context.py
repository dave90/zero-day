"""Offline: prior findings (confirmed/rejected/pending) are fed into each iteration.

Each Ralph iteration must be able to REUSE confirmed work, SKIP rejected dead-ends,
and CONTINUE pending leads — so the carried-forward findings store is rendered into
the orchestrator's iteration seed (and the completion judge).
"""
from __future__ import annotations

from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import new_finding_id
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ralph.driver import RalphDriver
from zero.ralph.prd import PRD, PRDTask


def _rec(title, endpoint, *, verdict=None, reproducer="", rationale="", notes=""):
    validation = None
    if verdict in ("confirmed", "rejected"):
        validation = Validation(verdict=verdict, rationale=rationale, reproducer=reproducer)
    return FindingRecord(
        id=new_finding_id(),
        finding=Finding(
            title=title, summary="s", endpoint=endpoint, method="GET",
            cwe="CWE-79", evidence="e", notes=notes,
        ),
        validation=validation,
        dedup_hash=title,
    )


def _driver(tmp_path) -> RalphDriver:
    orch = Orchestrator(OrchestratorConfig(cwd=tmp_path, backend="echo", ralph=True))
    return RalphDriver(orch, None)


def test_findings_context_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = _driver(tmp_path)._findings_context()
    assert "none recorded yet" in ctx


def test_findings_context_renders_all_three_verdicts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    drv = _driver(tmp_path)
    store = drv.orch.findings_store
    long_repro = "GET /saveprofile " + "A" * 900  # > 600 → must truncate
    store.promote(_rec("Stored XSS", "POST /saveprofile",
                       verdict="confirmed", reproducer=long_repro), "confirmed")
    store.promote(_rec("SQLi in id", "GET /q",
                       verdict="rejected", rationale="param is integer-cast server-side"),
                  "rejected")
    store.submit_pending(_rec("SSRF maybe", "GET /fetch",
                              notes="need an outbound listener to confirm"))

    ctx = drv._findings_context()
    # All three sections present.
    assert "### Confirmed" in ctx and "### Rejected" in ctx and "### Pending" in ctx
    assert "1 confirmed · 1 rejected · 1 pending" in ctx
    # Confirmed carries its reproducer, truncated (full 900-char body not present).
    assert "reproducer:" in ctx
    assert "A" * 900 not in ctx and " …" in ctx
    # Rejected explains WHY (so the next pass won't retry it).
    assert "integer-cast" in ctx
    # Pending carries the gap (from finding.notes when no validation).
    assert "outbound listener" in ctx


def test_iteration_seed_embeds_findings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    drv = _driver(tmp_path)
    drv.orch.findings_store.promote(
        _rec("Stored XSS", "POST /saveprofile", verdict="confirmed", reproducer="payload"),
        "confirmed",
    )
    seed = drv._iteration_seed(2, PRD(tasks=[PRDTask(id="t1", title="x")]), "earlier journal")
    assert "## Findings so far" in seed
    assert "Stored XSS" in seed
    assert "earlier journal" in seed  # progress still there too
