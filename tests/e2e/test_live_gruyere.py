"""Live full-stack e2e against Google Gruyere.

This is the most realistic test we have: it spins up the real Docker
sandbox, drives the real `claude` CLI, runs the full Pentest → Validator
pipeline against an actually-vulnerable target, and asserts that zero
produces at least one finding (confirmed, rejected, or pending — any
file on disk proves the whole pipeline plumbed end-to-end).

Gated by markers + binary presence; skips cleanly if anything's missing
so the default suite stays green on machines without the prerequisites:

  * marker `gruyere`           — opt-in (slow + costs claude tokens)
  * `docker image inspect zero-sandbox:latest` succeeds
  * `claude` on PATH

Run it:

    docker build -t zero-sandbox:latest -f docker/Dockerfile .
    uv run pytest -q -m gruyere -s tests/e2e/test_live_gruyere.py

Expect 3–5 minutes and a few dollars of claude usage.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.events import (
    ApprovalPrompt,
    AskUser,
    Finding,
    SessionEnd,
    SessionSummary,
)


# Public Gruyere instance used for this test. It is Google's intentionally-
# vulnerable training app — authorized for security testing is its entire
# purpose. The path segment is a per-user instance ID; substitute your own
# if you have one, but this one works fine for the e2e (each Gruyere
# instance is isolated and the bugs are identical across instances).
GRUYERE_URL = (
    "https://google-gruyere.appspot.com/"
    "554906294150075200827896525189694841833/"
)


def _image_exists(tag: str) -> bool:
    if shutil.which("docker") is None:
        return False
    rc = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    return rc.returncode == 0


pytestmark = [
    pytest.mark.gruyere,
    pytest.mark.skipif(
        shutil.which("claude") is None,
        reason="claude CLI not on PATH",
    ),
    pytest.mark.skipif(
        not _image_exists("zero-sandbox:latest"),
        reason="zero-sandbox:latest image not built — `docker build -t zero-sandbox:latest -f docker/Dockerfile .`",
    ),
]


PENTEST_MD_TEMPLATE = """\
# Google Gruyere pentest (unauthenticated, e2e)

Target is Google's intentionally-vulnerable training app. It is
authorized for security testing — that is its entire purpose.

## Scope

- in scope:
  - {gruyere_url}
  - any path under that prefix
- out of scope:
  - everything else on appspot.com / google.com
  - any other Gruyere instance ID

## Goal

Find ONE unauthenticated web-security defect. Be quick — don't enumerate
exhaustively. The /snippets.gtl endpoint with a `uid` parameter is a
known-fast win (reflected XSS) but any defect class is acceptable. Once
you have one with concrete evidence, call `finding_submit`.
"""


async def _drive_until_summary(
    orch: Orchestrator, *, deadline: float
) -> tuple[list[Finding], SessionSummary | None]:
    """Auto-approve everything, collect finding events + summary."""
    findings: list[Finding] = []
    summary: SessionSummary | None = None
    loop = asyncio.get_event_loop()
    while loop.time() < deadline:
        try:
            ev = await asyncio.wait_for(
                orch.ui_queue.get(),
                timeout=max(1.0, deadline - loop.time()),
            )
        except asyncio.TimeoutError:
            return findings, summary
        if isinstance(ev, ApprovalPrompt):
            orch.submit_approval(ev.call_id, Decision.YES)
        elif isinstance(ev, AskUser):
            orch.submit_ask_user_answer(ev.ask_id, "proceed; no preference")
        elif isinstance(ev, Finding):
            # Validation now runs in the BACKGROUND, so the verdict card
            # arrives AFTER the pentest agent's `[final]`. One verdict proves
            # the full submit→validate→persist pipeline ran end to end — keep
            # auto-approving (the validator makes its own tool calls) until we
            # see it, then stop.
            findings.append(ev)
            return findings, summary
        elif isinstance(ev, SessionSummary):
            summary = ev
        elif isinstance(ev, SessionEnd):
            return findings, summary
        # Note: we deliberately do NOT return on the pentest agent's
        # `[final]` — after it, the agent idles (no tokens burned) while the
        # background validator finishes; we wait for the Finding verdict above.
    return findings, summary


async def test_zero_finds_at_least_one_finding_on_gruyere(tmp_path: Path, monkeypatch):
    """Real run: zero should produce at least one finding on disk
    (confirmed/rejected/pending — any proves the full submit→validate→
    persist pipeline ran end to end against a real target)."""
    (tmp_path / "pentest.md").write_text(
        PENTEST_MD_TEMPLATE.format(gruyere_url=GRUYERE_URL), encoding="utf-8"
    )

    monkeypatch.chdir(tmp_path)
    cfg = OrchestratorConfig(
        cwd=tmp_path,
        goal="Find ONE unauthenticated web bug; submit the first concrete one you confirm.",
        backend="claude",
        execution_mode="a",   # this test exercises the single-agent path
        use_container=True,
        yolo=True,            # auto-approve safe + active
        auto_approve={"exploit"},
        i_understand=True,    # required for exploit auto-approve
        max_turns=30,
        rate_limit="120/min",
    )
    orch = Orchestrator(cfg)

    # Replace the prompt_toolkit UI with our fake auto-approver. The
    # orchestrator's run_interactive() awaits run_ui(orch), so patching
    # the symbol it imports inside that method swaps in our handler.
    findings: list[Finding] = []
    summary_holder: dict[str, SessionSummary | None] = {"s": None}

    async def fake_run_ui(o: Orchestrator) -> None:
        deadline = asyncio.get_event_loop().time() + 600  # 10 min hard cap
        fs, summary = await _drive_until_summary(o, deadline=deadline)
        findings.extend(fs)
        summary_holder["s"] = summary

    import zero.ui.app as app_mod

    monkeypatch.setattr(app_mod, "run_ui", fake_run_ui)

    # The orchestrator handles its own cleanup (container teardown,
    # audit close) in `run_interactive`'s finally block.
    await asyncio.wait_for(orch.run_interactive(), timeout=900)

    # --- Assertions ---------------------------------------------------------

    findings_root = tmp_path / ".pentest" / "findings"
    all_files = (
        list((findings_root / "confirmed").glob("*.md"))
        + list((findings_root / "rejected").glob("*.md"))
        + list((findings_root / "pending").glob("*.md"))
    )
    audit = read_audit(tmp_path / ".pentest" / "audit.jsonl")
    kinds = [r["kind"] for r in audit]

    # Diagnostics so failures are debuggable.
    print(f"\n[gruyere e2e] findings produced: {len(all_files)}")
    for f in all_files:
        print(f"  - {f.relative_to(tmp_path)}")
    print(f"[gruyere e2e] audit event histogram:")
    from collections import Counter
    for k, n in sorted(Counter(kinds).items(), key=lambda x: -x[1]):
        print(f"  {n:>4}  {k}")
    if summary_holder["s"] is not None:
        s = summary_holder["s"]
        print(
            f"[gruyere e2e] summary: confirmed={len(s.confirmed)} "
            f"rejected={len(s.rejected)} pending={len(s.pending)}"
        )

    # Level 1: the run completed cleanly.
    assert "session_start" in kinds
    assert "session_end" in kinds

    # Level 2: the container actually started.
    assert "container_started" in kinds, (
        "sandbox never came up; check Docker + image"
    )

    # Level 3: at least one tool call left the orchestrator (the agent
    # actually tried something against gruyere). Without this, nothing
    # else can succeed.
    assert kinds.count("tool_proposal") >= 1, (
        f"agent made no tool calls; kinds={kinds!r}"
    )

    # Level 4: the agent submitted at least one finding (proves the
    # explore→submit flow works end-to-end on a real target).
    assert kinds.count("finding_submit") >= 1, (
        "agent never reached a submission — try increasing --max-turns"
    )

    # Level 5: at least one finding landed on disk.
    assert all_files, "no finding files written; check pending/ for the reason"

    # Level 6 (informational only — don't fail on this): a confirmed
    # finding means the validator also succeeded.
    confirmed_files = list((findings_root / "confirmed").glob("*.md"))
    if confirmed_files:
        body = confirmed_files[0].read_text(encoding="utf-8")
        assert "---" in body  # frontmatter present
        # CVSS 4.0 vector should appear for any confirmed finding.
        assert "CVSS:4.0/" in body
