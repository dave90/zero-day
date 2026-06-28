r"""Offline, free unit tests for the host-side BountyBench pilot driver
(`benchmarks/bountybench/run_pilot.py`) — no BountyBench, Docker, or tokens.

Regression guard: BountyBench colorizes its logger, so the "Saved log to:" line ends
in an ANSI reset escape (\x1b[0m). The verdict parser's `(\S+)` used to swallow that
escape into the captured path ("…json\x1b[0m"), so a real PASS — the engagement printed
`success=True` — was read back as a fail because the (corrupted) log path didn't exist.
"""
from __future__ import annotations

import json

from benchmarks.bountybench.pilot import Bounty
from benchmarks.bountybench.run_pilot import _parse_bb_log, _runner_cmd


def _seed_log(bb, *, success: bool) -> str:
    """Write a minimal BB run-log JSON under `bb` and return its BB-relative path."""
    rel = "logs/2026-06-24/ZeroExploitWorkflow/lunary_0/_run_2026-06-24_18-40-29.json"
    log_path = bb / rel
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps({"workflow_metadata": {"workflow_summary": {"success": success}}}),
        encoding="utf-8",
    )
    return rel


def test_parse_bb_log_strips_ansi_from_saved_path(tmp_path):
    # BB's colorized STATUS line: the path is followed by the ANSI reset escape.
    rel = _seed_log(tmp_path, success=True)
    stdout = (
        "\x1b[1;33m2026-06-24 18:46:25 STATUS\x1b[0m\n"
        f"Saved log to: {rel}\x1b[0m\n"
    )
    success, _ = _parse_bb_log(stdout, tmp_path)
    assert success is True


def test_parse_bb_log_plain_path_still_works(tmp_path):
    rel = _seed_log(tmp_path, success=False)
    success, _ = _parse_bb_log(f"Saved log to: {rel}\n", tmp_path)
    assert success is False


def test_parse_bb_log_takes_last_saved_line(tmp_path):
    # BB prints "Saved log to:" repeatedly; the final one is the authoritative verdict.
    rel = _seed_log(tmp_path, success=True)
    stale = tmp_path / "logs" / "stale.json"
    stale.write_text(json.dumps({"workflow_metadata": {"workflow_summary": {"success": False}}}))
    stdout = f"Saved log to: logs/stale.json\nSaved log to: {rel}\x1b[0m\n"
    success, _ = _parse_bb_log(stdout, tmp_path)
    assert success is True


def test_parse_bb_log_missing_pointer_is_fail(tmp_path):
    success, usage = _parse_bb_log("no pointer here\n", tmp_path)
    assert success is False
    assert usage == {}


def test_runner_cmd_passes_cwe_only_to_detect():
    detect_cmd = _runner_cmd(
        Bounty("lunary", 0, "detect"),
        bb_python="python",
        phase_iterations=2,
        use_cwe=True,
    )
    exploit_cmd = _runner_cmd(
        Bounty("lunary", 0, "exploit"),
        bb_python="python",
        phase_iterations=2,
        use_cwe=True,
    )
    assert "--use_cwe" in detect_cmd
    assert "--use_cwe" not in exploit_cmd
