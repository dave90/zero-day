"""Capability regression suite: run the REAL `zero` agent against a stable of
known-vulnerable docker targets and assert it CONFIRMS the expected vulnerability.

This is the capability counterpart to `tests/e2e/test_live_gruyere.py`. Gruyere
proves the pipeline plumbs end-to-end against one remote app; this suite guards
the agent's *finding ability* across a graded set of targets (easy -> hard), so a
change that quietly makes `zero` worse shows up as a drop in the scorecard.

Each parametrized case (one per `manifest.TARGETS`):
  1. stands the target up in docker (the `regression_target` fixture),
  2. writes a `pentest.md` scoping the run to that target,
  3. drives a real orchestrated run (`execution_mode="c"`, real LLM backend + real
     sandbox) auto-approving everything, exactly like the gruyere driver,
  4. asserts a *validator-confirmed* finding matches the target's ground-truth
     vuln (see `assertions.record_matches`).

Gated + opt-in (slow, costs LLM tokens). Run it:

    docker build -t zero-sandbox:latest -f docker/Dockerfile .
    uv run pytest -q -m regression -s tests/regression/
    # one target:
    uv run pytest -q -m regression -s tests/regression/ -k juice-shop
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.regression.assertions import score_target
from tests.regression.manifest import TARGETS
from zero.findings.store import FindingsStore
from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.events import (
    ApprovalPrompt,
    AskUser,
    SessionEnd,
    SessionSummary,
)


def _image_exists(tag: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(
        ["docker", "image", "inspect", tag], capture_output=True
    ).returncode == 0


pytestmark = [
    pytest.mark.live,          # token-spending — excluded from the default suite
    pytest.mark.regression,
    pytest.mark.skipif(
        not _image_exists("zero-sandbox:latest"),
        reason="zero-sandbox:latest image not built — `docker build -t zero-sandbox:latest -f docker/Dockerfile .`",
    ),
]


PENTEST_MD_TEMPLATE = """\
# {name} pentest (authorized regression target)

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

{goal}

Be efficient. Once you have ONE defect with concrete, reproducible evidence,
call `finding_submit` — don't enumerate exhaustively.
"""


async def _drive(orch: Orchestrator, *, deadline: float) -> None:
    """Auto-approve everything until the session ends or the deadline passes.

    Unlike the gruyere driver, we do NOT early-return on the first Finding — for
    a regression check the agent needs room to reach a *confirmed* finding of the
    right class, so we keep approving (the background validator makes its own tool
    calls) until SessionEnd/SessionSummary or the wall-clock deadline.
    """
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
    [pytest.param(t, t, id=f"{t.difficulty}-{t.name}") for t in TARGETS],
    indirect=["regression_target"],
)
async def test_regression_target(
    regression_target, spec, tmp_path: Path, monkeypatch, regression_scorecard,
    regression_backend,
):
    url = regression_target
    (tmp_path / "pentest.md").write_text(
        PENTEST_MD_TEMPLATE.format(
            name=spec.name, url=url, scope_note=spec.scope_note, goal=spec.goal
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = OrchestratorConfig(
        cwd=tmp_path,
        goal=spec.goal,
        backend=regression_backend,
        execution_mode="c",       # orchestrated — the production default
        use_container=True,
        yolo=True,                # auto-approve safe + active
        auto_approve={"exploit"},
        i_understand=True,        # required for exploit auto-approve
        host_network=spec.host_network,
        max_turns=spec.max_turns,
        pentester_max_turns=spec.pentester_max_turns,
        subagent_deadline_seconds=spec.subagent_deadline_seconds,
        rate_limit="120/min",
    )
    orch = Orchestrator(cfg)

    # Swap the prompt_toolkit UI for our auto-approver, exactly like the gruyere
    # e2e: run_interactive() awaits run_ui(orch); patching the symbol the method
    # imports swaps in our driver.
    async def fake_run_ui(o: Orchestrator) -> None:
        deadline = asyncio.get_event_loop().time() + spec.deadline_seconds
        await _drive(o, deadline=deadline)

    import zero.ui.app as app_mod

    monkeypatch.setattr(app_mod, "run_ui", fake_run_ui)

    # The orchestrator owns its cleanup (container teardown, audit close) in
    # run_interactive()'s finally block.
    await asyncio.wait_for(orch.run_interactive(), timeout=spec.deadline_seconds + 180)

    # --- Score + record -----------------------------------------------------
    store = FindingsStore(tmp_path / ".pentest" / "findings")
    audit = read_audit(tmp_path / ".pentest" / "audit.jsonl")
    result = score_target(spec, store, audit)
    regression_scorecard.append(result)

    print(f"\n[regression {spec.name} backend={regression_backend}] {result.explain()}")

    # Plumbing sanity (deterministic — a failure here means the harness broke,
    # not the agent).
    assert result.container_started, "sandbox container never came up"
    assert result.tool_calls >= 1, "agent made no tool calls"

    # The capability assertion: a validator-confirmed finding of the right class.
    assert result.matched_confirmed, result.explain()
