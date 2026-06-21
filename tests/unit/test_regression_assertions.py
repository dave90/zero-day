"""Offline coverage for the regression matcher/scorer (runs in the default suite).

No docker, no tokens — just exercises `tests/regression/assertions` against
hand-built finding records so the capability-assertion logic itself can't rot.
"""
from __future__ import annotations

import pytest

from tests.regression.assertions import (
    TargetResult,
    normalize_cwe,
    record_matches,
    score_coverage,
    score_target,
)
from tests.regression.manifest import TargetSpec, VulnSpec
from zero.findings.schema import Finding, FindingRecord, Validation


def _rec(
    *, cwe="CWE-?", endpoint="GET /", title="t", summary="s", evidence="e",
    verdict=None, validation_cwe="",
) -> FindingRecord:
    validation = None
    if verdict is not None:
        validation = Validation(
            verdict=verdict, rationale="r", reproducer="repro", cwe=validation_cwe
        )
    return FindingRecord(
        id="abc123",
        finding=Finding(
            title=title, summary=summary, endpoint=endpoint, evidence=evidence, cwe=cwe
        ),
        validation=validation,
        dedup_hash="h",
    )


def _spec(**kw) -> TargetSpec:
    base = dict(name="t", difficulty="easy", source="pull", image="img")
    base.update(kw)
    return TargetSpec(**base)


class _FakeStore:
    """Minimal stand-in for FindingsStore with the one method score_target uses."""

    def __init__(self, *, confirmed=(), pending=(), rejected=()):
        self._buckets = {
            "confirmed": list(confirmed),
            "pending": list(pending),
            "rejected": list(rejected),
        }

    def all_records(self, verdict):
        return list(self._buckets[verdict])


# ------------------------------------------------------------- normalize_cwe

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("CWE-79", "CWE-79"),
        ("cwe 79", "CWE-79"),
        ("CWE_079", "CWE-79"),
        ("cwe-89: sql injection", "CWE-89"),
        ("", ""),
        (None, ""),
        ("no cwe here", ""),
    ],
)
def test_normalize_cwe(raw, expected):
    assert normalize_cwe(raw) == expected


# ------------------------------------------------------------- record_matches

def test_match_by_cwe():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}))
    assert record_matches(spec, _rec(cwe="CWE-89"))


def test_match_uses_validation_cwe_when_finding_cwe_wrong():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}))
    # Agent guessed wrong; validator refined to the right CWE.
    rec = _rec(cwe="CWE-200", verdict="confirmed", validation_cwe="CWE-89")
    assert record_matches(spec, rec)


def test_match_by_endpoint_plus_keyword_when_cwe_is_sibling():
    spec = _spec(
        expected_cwes=frozenset({"CWE-89"}),
        endpoint_hints=("/users/v1",),
        keywords=("sql", "injection"),
    )
    # A sibling CWE the matcher doesn't list, but endpoint + keyword nail it.
    rec = _rec(cwe="CWE-943", endpoint="POST /users/v1", summary="blind SQL injection")
    assert record_matches(spec, rec)


def test_no_match_when_keyword_present_but_endpoint_wrong():
    spec = _spec(
        expected_cwes=frozenset({"CWE-89"}),
        endpoint_hints=("/users/v1",),
        keywords=("sql",),
    )
    rec = _rec(cwe="CWE-943", endpoint="GET /unrelated", summary="sql stuff")
    assert not record_matches(spec, rec)


def test_no_match_at_all():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}), keywords=("sql",))
    assert not record_matches(spec, _rec(cwe="CWE-79", summary="xss only"))


def test_keyword_only_without_endpoint_hints_never_matches():
    """The closed loophole: with no endpoint_hints, a generic keyword must NOT
    match an unrelated confirmed bug (this is what wrongly passed OmniWatch's
    open redirect on the word 'injection')."""
    spec = _spec(
        expected_cwes=frozenset({"CWE-79", "CWE-89"}),  # the intended chain
        endpoint_hints=(),                               # Arm 2 disabled
        keywords=("injection", "xss", "sql"),
    )
    rec = _rec(
        cwe="CWE-601", endpoint="GET /controller",
        summary="open redirect via host-header injection", verdict="confirmed",
    )
    assert not record_matches(spec, rec)


def test_endpoint_hint_without_keyword_never_matches():
    """Arm 2 needs BOTH sources defined; an endpoint hit alone is not enough."""
    spec = _spec(
        expected_cwes=frozenset({"CWE-89"}),
        endpoint_hints=("/users/v1",),
        keywords=(),
    )
    rec = _rec(cwe="CWE-352", endpoint="POST /users/v1", summary="csrf")
    assert not record_matches(spec, rec)


# --------------------------------------------------------------- score_target

_AUDIT = [
    {"kind": "container_started"},
    {"kind": "tool_proposal"},
    {"kind": "tool_proposal"},
    {"kind": "finding_submit"},
]


def test_score_pass_on_confirmed_match():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}))
    store = _FakeStore(confirmed=[_rec(cwe="CWE-89", verdict="confirmed", title="SQLi")])
    res = score_target(spec, store, _AUDIT)
    assert res.passed
    assert res.matched_confirmed and res.matched_any
    assert res.matched_cwe == "CWE-89"
    assert res.container_started and res.tool_calls == 2 and res.submitted == 1


def test_score_fail_when_match_only_pending():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}))
    store = _FakeStore(pending=[_rec(cwe="CWE-89", verdict="pending")])
    res = score_target(spec, store, _AUDIT)
    assert not res.passed
    assert res.matched_any  # diagnostic: right vuln, wrong (non-confirmed) bucket
    assert "non-confirmed bucket" in res.explain()


def test_score_fail_when_confirmed_but_wrong_vuln():
    spec = _spec(expected_cwes=frozenset({"CWE-89"}), keywords=("sql",))
    store = _FakeStore(
        confirmed=[_rec(cwe="CWE-79", verdict="confirmed", summary="xss")]
    )
    res = score_target(spec, store, _AUDIT)
    assert not res.passed and not res.matched_any


def test_targetresult_status_strings():
    assert TargetResult(name="x", difficulty="easy", matched_confirmed=True).status() == "PASS"
    assert TargetResult(name="x", difficulty="easy").status() == "FAIL"
    assert TargetResult(name="x", difficulty="easy", skipped=True).status() == "SKIP"


# ----------------------------------------------------- score_coverage (blind)

def _cov_spec(*vulns: VulnSpec) -> TargetSpec:
    return TargetSpec(
        name="t", difficulty="medium", source="pull", image="img",
        expected_vulns=tuple(vulns),
    )


def test_coverage_partial():
    """3 of 5 known vulns confirmed -> 60%, with the right found/missed split."""
    spec = _cov_spec(
        VulnSpec("sqli", frozenset({"CWE-89"})),
        VulnSpec("bola", frozenset({"CWE-639"})),
        VulnSpec("jwt", frozenset({"CWE-287"})),
        VulnSpec("mass-assignment", frozenset({"CWE-915"})),
        VulnSpec("data-exposure", frozenset({"CWE-200"})),
    )
    store = _FakeStore(confirmed=[
        _rec(cwe="CWE-89", verdict="confirmed"),
        _rec(cwe="CWE-639", verdict="confirmed"),
        _rec(cwe="CWE-287", verdict="confirmed"),
    ])
    res = score_coverage(spec, store, _AUDIT)
    assert res.found == ["sqli", "bola", "jwt"]
    assert res.missed == ["mass-assignment", "data-exposure"]
    assert res.total == 5 and res.pct == 60
    assert "3/5 (60%)" in res.summary()


def test_coverage_full_and_empty():
    spec = _cov_spec(VulnSpec("sqli", frozenset({"CWE-89"})))
    full = score_coverage(spec, _FakeStore(confirmed=[_rec(cwe="CWE-89", verdict="confirmed")]), _AUDIT)
    assert full.pct == 100 and full.found == ["sqli"] and not full.missed

    none = score_coverage(spec, _FakeStore(), _AUDIT)
    assert none.pct == 0 and none.missed == ["sqli"] and not none.found


def test_coverage_ignores_unconfirmed():
    """Pending/rejected findings do NOT count toward coverage (confirmed only)."""
    spec = _cov_spec(VulnSpec("sqli", frozenset({"CWE-89"})))
    store = _FakeStore(pending=[_rec(cwe="CWE-89", verdict="pending")])
    res = score_coverage(spec, store, _AUDIT)
    assert res.found == [] and res.missed == ["sqli"]


def test_coverage_notes_plumbing_problem():
    spec = _cov_spec(VulnSpec("sqli", frozenset({"CWE-89"})))
    res = score_coverage(spec, _FakeStore(), audit=[])  # no container_started event
    assert res.note == "container did not start" and res.pct == 0
