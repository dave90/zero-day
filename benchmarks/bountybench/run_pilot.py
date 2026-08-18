#!/usr/bin/env python3
"""Drive the BountyBench pilot: run each manifest bounty through BountyBench's runner
with `zero` as the executor, capture the authoritative verdict, and write a results file
that `evaluate.py` aggregates into a scorecard.

This runs on the HOST and shells out to BountyBench's `python -m workflows.runner` (using
BB's own venv) once per bounty. The model under test and run budget are passed to `zero`
via `ZERO_BB_*` environment variables that the overlay agent reads.

Prereqs (see README): BountyBench cloned with `bountytasks` fetched, the overlay installed
(`install_overlay.py`), `zero` pip-installed into BB's venv, Docker running, and the
`zero-sandbox` image built. Token-spending.

Usage:
    python benchmarks/bountybench/run_pilot.py \
        --bountybench /path/to/bountybench \
        --deadline 1200 --max-turns 30
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Importable both as a module (python -m benchmarks.bountybench.run_pilot) and as a
# script (python benchmarks/bountybench/run_pilot.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.bountybench.pilot import PILOT, Bounty, bounty_metadata_path, list_available  # noqa: E402

_SAVED_LOG_RE = re.compile(r"Saved log to:\s*(\S+)")
# BountyBench colorizes its logger output, so the "Saved log to:" line ends in an ANSI
# reset (\x1b[0m). `(\S+)` would otherwise swallow that escape into the captured path
# ("…json\x1b[0m"), making the log unreadable and scoring a real PASS as a fail. Strip
# all ANSI SGR sequences from the captured streams before matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _bb_python(bb: Path, override: str | None) -> str:
    if override:
        return override
    venv_py = bb / "venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def _dollar_value(bb: Path, b: Bounty) -> float:
    """Detect/Exploit credit the disclosure bounty."""
    meta_path = bounty_metadata_path(bb, b)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    return float(meta.get("disclosure_bounty", 0) or 0)


def _zero_usage(bb: Path, b: Bounty) -> dict:
    """Real token usage from zero's OWN audit (BB's workflow_usage is 0 under
    --use_mock_model, since zero — not BB — calls the model). Reads the newest
    persisted engagement dir for this bounty/task. Returns {input_tokens, output_tokens}."""
    runs_root = (
        bb / "bountytasks" / b.system / "bounties"
        / f"bounty_{b.bounty_number}" / "zero_runs"
    )
    audits = sorted(
        runs_root.glob(f"{b.task_type}_*/.pentest/audit.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    if not audits:
        return {}
    inp = out = 0
    for line in audits[-1].read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") != "agent_step" or not isinstance(e.get("usage"), dict):
            continue
        u = e["usage"]
        inp += sum(int(u.get(k, 0) or 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        out += int(u.get("output_tokens", 0) or 0)
    return {"input_tokens": inp, "output_tokens": out}


def _parse_bb_log(stdout: str, bb: Path) -> tuple[bool, dict]:
    """Find the saved BB run-log JSON in stdout and read the authoritative verdict.

    BB prints the path relative to ITS OWN cwd (the BB repo), so resolve it against
    `bb`, not our process cwd. Returns (success, usage_dict); (False, {}) if unreadable.
    """
    stdout = _ANSI_RE.sub("", stdout)  # drop BB's color codes so paths parse cleanly
    m = None
    for m in _SAVED_LOG_RE.finditer(stdout):
        pass  # take the last "Saved log to:" line
    if not m:
        print("  [verdict] no BB run-log found in stdout → fail")
        return False, {}
    log_path = Path(m.group(1))
    if not log_path.is_absolute():
        log_path = bb / log_path
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [verdict] could not read BB log {log_path}: {e} → fail")
        return False, {}
    summary = data.get("workflow_metadata", {}).get("workflow_summary", {})
    success = bool(summary.get("success", False))
    print(f"  [verdict] BB says success={success}  (log: {log_path.name})")
    return success, data.get("workflow_usage", {})


def _runner_cmd(
    b: Bounty, *, bb_python: str, phase_iterations: int, use_cwe: bool = False
) -> list[str]:
    cmd = [
        bb_python, "-m", "workflows.runner",
        "--workflow-type", b.workflow_type,
        "--task_dir", b.task_dir,
        "--bounty_number", str(b.bounty_number),
        "--use_mock_model",                    # zero owns the real model, not BB
        "--phase_iterations", str(phase_iterations),
    ]
    if use_cwe and b.task_type == "detect":
        cmd.append("--use_cwe")
    return cmd


def run_one(
    bb: Path,
    b: Bounty,
    *,
    bb_python: str,
    env: dict,
    phase_iterations: int,
    use_cwe: bool = False,
) -> dict:
    cmd = _runner_cmd(
        b, bb_python=bb_python, phase_iterations=phase_iterations, use_cwe=use_cwe
    )
    print(f"\n=== {b.label} ===\n$ {' '.join(cmd)}", flush=True)
    start = time.time()
    proc = subprocess.run(
        cmd, cwd=str(bb), env=env, capture_output=True, text=True
    )
    duration = time.time() - start
    # BountyBench logs (incl. "Saved log to:" and the verify output) go to STDERR via
    # its logger; the verdict JSON pointer is there, not on stdout. Parse BOTH streams.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Stream a tail so a watching operator sees progress.
    sys.stdout.write(combined[-2000:])

    success, _bb_usage = _parse_bb_log(combined, bb)
    usage = _zero_usage(bb, b)  # real tokens from zero's audit (BB's is 0 under mock)
    return {
        "system": b.system,
        "bounty_number": b.bounty_number,
        "task_type": b.task_type,
        "success": success,
        "dollar_value": _dollar_value(bb, b),
        "input_tokens": int(usage.get("input_tokens", 0) or 0),
        "output_tokens": int(usage.get("output_tokens", 0) or 0),
        "duration_s": round(duration, 1),
        "error": "" if proc.returncode == 0 else f"runner exit {proc.returncode}",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bountybench", help="BountyBench checkout (or $BOUNTYBENCH_HOME)")
    ap.add_argument("--bb-python", help="python for BB's venv (default <bb>/venv/bin/python)")
    ap.add_argument("--backend", default="codex", help="zero backend: codex | claude")
    ap.add_argument("--model", default=None, help="zero model id under test (None = zero default)")
    ap.add_argument("--deadline", type=int, default=1200, help="per-engagement wall-clock seconds")
    ap.add_argument("--max-turns", type=int, default=30, help="zero orchestrator turn budget")
    ap.add_argument("--network", default="shared_net", help="docker network of the BB target")
    ap.add_argument("--no-container", action="store_true", help="run zero on the host (no sandbox)")
    ap.add_argument("--phase-iterations", type=int, default=2,
                    help="BB phase iterations (>=2: executor then scorer)")
    ap.add_argument("--use-cwe", action="store_true",
                    help="pass BountyBench's CWE hint to Detect tasks (higher-information variant)")
    ap.add_argument("--results", type=Path, help="output JSONL (default results/pilot_<ts>.jsonl)")
    ap.add_argument("--only", help="comma-separated systems to include (e.g. lunary,mlflow)")
    ap.add_argument("--task", choices=["exploit", "detect"],
                    help="only run this task type (default: both, as in the manifest)")
    # Ad-hoc single bounty (bypasses the PILOT manifest) — run ANY fetched bounty.
    ap.add_argument("--system", help="run one ad-hoc bounty: bountytasks/<system>")
    ap.add_argument("--bounty-number", type=int, default=0, help="ad-hoc bounty number")
    args = ap.parse_args()

    root = args.bountybench or os.environ.get("BOUNTYBENCH_HOME")
    if not root:
        sys.exit("error: pass --bountybench or set $BOUNTYBENCH_HOME")
    bb = Path(root).expanduser().resolve()

    if args.system:
        # Ad-hoc single bounty, not from the manifest.
        bounties = list_available(
            bb, [Bounty(args.system, args.bounty_number, args.task or "exploit")]
        )
        if not bounties:
            sys.exit(
                f"bounty not found on disk: bountytasks/{args.system}/bounties/"
                f"bounty_{args.bounty_number} (is the codebase submodule fetched?)"
            )
    else:
        bounties = list_available(bb)
        if args.only:
            wanted = {s.strip() for s in args.only.split(",")}
            bounties = [b for b in bounties if b.system in wanted]
        if args.task:
            bounties = [b for b in bounties if b.task_type == args.task]
        if not bounties:
            sys.exit(
                "no pilot bounties available on disk — is `bountytasks` fetched? "
                f"(checked {len(PILOT)} manifest entries under {bb / 'bountytasks'})"
            )

    # The knobs the overlay agent reads (per-engagement).
    env = dict(os.environ)
    env.update({
        "ZERO_BB_BACKEND": args.backend,
        "ZERO_BB_DEADLINE": str(args.deadline),
        "ZERO_BB_MAX_TURNS": str(args.max_turns),
        "ZERO_BB_NETWORK": args.network,
    })
    if args.model:
        env["ZERO_BB_MODEL"] = args.model
    if args.no_container:
        env["ZERO_BB_NO_CONTAINER"] = "1"

    results_dir = Path(__file__).resolve().parent / "results"
    results_dir.mkdir(exist_ok=True)
    results_path = args.results or (results_dir / f"pilot_{int(time.time())}.jsonl")
    bb_python = _bb_python(bb, args.bb_python)

    print(f"BountyBench: {bb}\nbackend={args.backend} model={args.model} "
          f"bounties={[b.label for b in bounties]}\nresults → {results_path}")

    with open(results_path, "w", encoding="utf-8") as fh:
        for b in bounties:
            rec = run_one(bb, b, bb_python=bb_python, env=env,
                          phase_iterations=args.phase_iterations,
                          use_cwe=args.use_cwe)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"  → {b.label}: {'PASS' if rec['success'] else 'fail'} "
                  f"(${rec['dollar_value']:,.0f}, {rec['duration_s']}s)")

    print(f"\nDone. Aggregate: python benchmarks/bountybench/evaluate.py {results_path}")


if __name__ == "__main__":
    main()
