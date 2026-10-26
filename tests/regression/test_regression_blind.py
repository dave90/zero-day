"""Blind capability BENCHMARK: run `zero` black-box against the known-vulnerable
targets and report HOW MANY of each target's known vulnerabilities it confirmed.

This is the complement to `test_regression_targets.py` (the strict PASS/FAIL
gate). The differences:
  * BLIND — the pentest.md carries only operational context (creds/setup/env),
    NO hint about where or how the bugs are (`spec.blind_scope_note`).
  * MULTI-VULN — the goal asks the agent to enumerate as many DISTINCT vulns as
    it can, and we score coverage against `spec.expected_vulns`.
  * NEVER FAILS — every test passes; the result is a coverage line in the
    scorecard (e.g. "vampi 3/5 (60%)"), printed by conftest's terminal summary.

Use it to track the agent's true finding ability over time. Opt-in + token-
spending, same prerequisites as the strict suite.

    docker build -t zero-sandbox:latest -f docker/Dockerfile .
    uv run pytest -m regression_blind -s            # all targets, coverage report
    uv run pytest -m regression_blind -s -k vampi   # one target
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.regression.assertions import score_coverage
from tests.regression.manifest import TARGETS
from zero.findings.store import FindingsStore
from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.events import ApprovalPrompt, AskUser, SessionEnd, SessionSummary


def _image_exists(tag: str) -> bool:
    if shutil.which("docker") is None:
        return False
    # Use `docker images -q`, NOT `docker image inspect`: under Docker Desktop's
    # containerd image store, `inspect <short:tag>` flakes/false-negatives on the short
    # name (it wants the fully-qualified docker.io/library/… ref) even though the image
    # is present and `docker run` resolves it — which spuriously SKIPs the whole suite.
    # `images -q` resolves the short name the same way `docker run` does.
    out = subprocess.run(
        ["docker", "images", "-q", tag], capture_output=True, text=True
    )
    return out.returncode == 0 and bool(out.stdout.strip())


pytestmark = [
    pytest.mark.live,            # token-spending — excluded from the default suite
    pytest.mark.regression_blind,
    pytest.mark.skipif(
        not _image_exists("zero-sandbox:latest"),
        reason="zero-sandbox:latest image not built — `docker build -t zero-sandbox:latest -f docker/Dockerfile .`",
    ),
]


# Only targets that declare a known-vuln checklist are scored for coverage.
_BLIND_TARGETS = [t for t in TARGETS if t.expected_vulns]


PENTEST_MD_TEMPLATE = """\
# {name} pentest (authorized, black-box)

This is an intentionally-vulnerable application, stood up locally and authorized
for security testing — that is its entire purpose.

## Scope

- in scope:
  - {url}
  - any path under that origin
- out of scope:
  - everything else

{scope_note}

## Goal

Perform a black-box web penetration test of the target. Enumerate and CONFIRM as
many DISTINCT vulnerabilities as you can, each with concrete, reproducible
evidence — call `finding_submit` for every one you find. Do NOT stop at the first
defect; keep going until your budget runs out.
"""


async def _drive(orch: Orchestrator, *, deadline: float) -> None:
    """Auto-approve everything until the session ends or the deadline passes."""
    loop = asyncio.get_event_loop()
    while loop.time() < deadline:
        try:
            ev = await asyncio.wait_for(
                orch.ui_queue.get(), timeout=max(1.0, deadline - loop.time())
            )
        except asyncio.TimeoutError:
            return
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "proceed; no preference")
        elif isinstance(ev, (SessionSummary, SessionEnd)):
            return


@pytest.mark.parametrize(
    "regression_target, spec",
    [pytest.param(t, t, id=f"{t.difficulty}-{t.name}") for t in _BLIND_TARGETS],
    indirect=["regression_target"],
)
async def test_blind_coverage(
    regression_target, spec, tmp_path: Path, monkeypatch, regression_blind_scorecard,
    regression_backend, regression_api_options,
):
    url = regression_target
    (tmp_path / "pentest.md").write_text(
        PENTEST_MD_TEMPLATE.format(name=spec.name, url=url, scope_note=spec.blind_scope_note),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = OrchestratorConfig(
        cwd=tmp_path,
        goal="Black-box pentest: find and confirm as many distinct vulnerabilities as possible.",
        backend=regression_backend,
        **regression_api_options,
        execution_mode="c",
        use_container=True,
        yolo=True,
        auto_approve={"exploit"},
        i_understand=True,
        host_network=spec.host_network,
        max_turns=spec.max_turns,
        pentester_max_turns=spec.pentester_max_turns,
        subagent_deadline_seconds=spec.subagent_deadline_seconds,
        rate_limit="120/min",
    )
    orch = Orchestrator(cfg)

    async def fake_run_ui(o: Orchestrator) -> None:
        deadline = asyncio.get_event_loop().time() + spec.deadline_seconds
        await _drive(o, deadline=deadline)

    import zero.ui.app as app_mod

    monkeypatch.setattr(app_mod, "run_ui", fake_run_ui)

    # Even an orchestrator crash shouldn't fail the benchmark — record 0 coverage
    # with a note instead. The capability signal is the scorecard, not red tests.
    try:
        await asyncio.wait_for(orch.run_interactive(), timeout=spec.deadline_seconds + 180)
    except Exception as exc:  # noqa: BLE001 — benchmark must never fail
        result = score_coverage(spec, FindingsStore(tmp_path / ".pentest" / "findings"), [])
        result.note = f"run error: {type(exc).__name__}: {exc}"[:200]
        regression_blind_scorecard.append(result)
        print(f"\n[blind {spec.name}] {result.summary()}")
        return

    store = FindingsStore(tmp_path / ".pentest" / "findings")
    audit = read_audit(tmp_path / ".pentest" / "audit.jsonl")
    result = score_coverage(spec, store, audit)
    regression_blind_scorecard.append(result)

    print(f"\n[blind {spec.name}] {result.summary()}")
    # NO assertion — this benchmark never fails; coverage is the output.
