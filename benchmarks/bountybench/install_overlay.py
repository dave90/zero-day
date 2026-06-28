#!/usr/bin/env python3
"""Install (or remove) the zero→BountyBench overlay into a BountyBench checkout.

What it does (idempotent):
  1. Copies `bb_overlay/` into `<BOUNTYBENCH>/zero_adapter/` (a top-level package,
     importable when BB runs as `python -m workflows.runner` from its repo root).
  2. Appends a small registration block to `<BOUNTYBENCH>/workflows/runner.py` that
     adds `zero_detect_workflow` / `zero_exploit_workflow` to `WorkflowRunner._workflow_factory`
     (so `--workflow-type zero_detect_workflow` is accepted). Guarded by a marker so it's
     applied at most once.

Pure stdlib so it runs from either the zero repo's env or BB's venv.

Usage:
    python benchmarks/bountybench/install_overlay.py --bountybench /path/to/bountybench
    python benchmarks/bountybench/install_overlay.py --uninstall   # uses $BOUNTYBENCH_HOME

After installing, the zero package must be importable inside BB's venv:
    source <BOUNTYBENCH>/venv/bin/activate
    pip install -e /path/to/zero-day
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

_MARKER_BEGIN = "# >>> zero_adapter registration (auto-added by zero install_overlay.py)"
_MARKER_END = "# <<< zero_adapter registration"

_REGISTRATION_BLOCK = f"""

{_MARKER_BEGIN}
# Adds zero-driven workflows to the runner factory at import time (before the
# WorkflowRunner is constructed in main()), so --workflow-type accepts them.
try:
    from zero_adapter.zero_detect_workflow import ZeroDetectWorkflow as _ZeroDetectWorkflow
    from zero_adapter.zero_exploit_workflow import ZeroExploitWorkflow as _ZeroExploitWorkflow

    WorkflowRunner._workflow_factory["zero_detect_workflow"] = _ZeroDetectWorkflow
    WorkflowRunner._workflow_factory["zero_exploit_workflow"] = _ZeroExploitWorkflow
except Exception as _zero_exc:  # noqa: BLE001 — never break the stock runner
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "zero_adapter workflows not registered (%s). Is the `zero` package installed "
        "in this venv? `pip install -e /path/to/zero-day`.",
        _zero_exc,
    )
{_MARKER_END}
"""


def _overlay_src() -> Path:
    return Path(__file__).resolve().parent / "bb_overlay"


def _resolve_bb(arg: str | None) -> Path:
    root = arg or os.environ.get("BOUNTYBENCH_HOME")
    if not root:
        sys.exit("error: pass --bountybench <path> or set $BOUNTYBENCH_HOME")
    p = Path(root).expanduser().resolve()
    if not (p / "workflows" / "runner.py").is_file():
        sys.exit(f"error: {p} does not look like a BountyBench checkout (no workflows/runner.py)")
    return p


def _copy_overlay(bb: Path) -> Path:
    dest = bb / "zero_adapter"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _overlay_src(), dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return dest


def _patch_runner(bb: Path) -> bool:
    runner = bb / "workflows" / "runner.py"
    text = runner.read_text(encoding="utf-8")
    if _MARKER_BEGIN in text:
        return False  # already registered
    # The block MUST run before the runner is constructed, so insert it ABOVE the
    # `if __name__ == "__main__":` guard (appending at EOF would run after main()
    # when invoked as `python -m workflows.runner`).
    guard = "\nif __name__ == "
    idx = text.find(guard)
    if idx != -1:
        new = text[:idx] + "\n" + _REGISTRATION_BLOCK.strip() + "\n" + text[idx:]
    else:
        new = text.rstrip() + "\n" + _REGISTRATION_BLOCK
    runner.write_text(new, encoding="utf-8")
    return True


def _unpatch_runner(bb: Path) -> bool:
    runner = bb / "workflows" / "runner.py"
    text = runner.read_text(encoding="utf-8")
    if _MARKER_BEGIN not in text:
        return False
    start = text.index(_MARKER_BEGIN)
    end = text.index(_MARKER_END) + len(_MARKER_END)
    # also strip a leading run of blank lines we added
    new = (text[:start].rstrip() + "\n" + text[end:].lstrip("\n"))
    runner.write_text(new, encoding="utf-8")
    return True


def install(bb: Path) -> None:
    dest = _copy_overlay(bb)
    patched = _patch_runner(bb)
    print(f"✓ copied overlay → {dest}")
    print(f"✓ runner factory {'patched' if patched else 'already had registration'}")
    print()
    print("Next:")
    print(f"  source {bb / 'venv' / 'bin' / 'activate'}")
    print("  pip install -e /path/to/zero-day      # make `zero` importable in BB's venv")
    print("  python -m workflows.runner --workflow-type zero_exploit_workflow \\")
    print("      --task_dir bountytasks/<system> --bounty_number <n> \\")
    print("      --use_mock_model --phase_iterations 2")


def uninstall(bb: Path) -> None:
    dest = bb / "zero_adapter"
    if dest.exists():
        shutil.rmtree(dest)
        print(f"✓ removed {dest}")
    if _unpatch_runner(bb):
        print("✓ removed runner registration block")
    else:
        print("· no runner registration block found")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bountybench", help="path to BountyBench checkout (or $BOUNTYBENCH_HOME)")
    ap.add_argument("--uninstall", action="store_true", help="remove the overlay")
    args = ap.parse_args()
    bb = _resolve_bb(args.bountybench)
    (uninstall if args.uninstall else install)(bb)


if __name__ == "__main__":
    main()
