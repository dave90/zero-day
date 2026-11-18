"""Offline contract checks for the opt-in live matrix; no target/model is contacted."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "matrix", Path(__file__).resolve().parents[2] / "tests/live/matrix.py"
)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_matrix_keeps_models_modes_and_profiles_separate():
    manifest = {"models": [{"id": "synthetic-model-for-test", "declared_support": True}]}
    cases = matrix.expand(manifest)
    baseline = [c for c in cases if c["variant"] == "baseline"]
    assert len(baseline) == 24
    assert {c["profile"] for c in baseline} == {"P1", "P2", "P3"}
    assert {c["mode"] for c in baseline} == {"single", "orchestrated", "swarm", "ralph"}
    assert all(c["config"]["backend"] == "codex" and c["config"]["container"] for c in cases)
    assert not any(c["config"]["scope_host"] for c in cases)
    assert len(matrix.expand(manifest, smoke=True)) == 1
    assert all(c["config"]["ralph_iterations"] == 2 for c in cases if c["mode"] == "ralph")


def test_capability_needs_independent_verdict_and_reproducer():
    candidate = {
        "verdict": "pending",
        "finding": {"cwe": "CWE-79", "endpoint": "/snippets.gtl", "evidence": "uid=marker"},
        "validation": None,
    }
    assert not matrix.known_confirmed([candidate])
    confirmed = {
        **candidate,
        "verdict": "confirmed",
        "validation": {
            "cwe": "CWE-79",
            "rationale": "Independently retested",
            "reproducer": "GET snippets.gtl?uid=marker",
        },
    }
    assert matrix.known_confirmed([confirmed])
    assert not matrix.known_confirmed(
        [{**confirmed, "validation": {**confirmed["validation"], "rationale": ""}}]
    )
