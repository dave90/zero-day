"""Ralph on-disk layout + the per-iteration backup/reset.

Two roots beside the engagement `.pentest/`:
- `.ralph/`      — durable loop state (`prd.json`, `progress.txt`, `dossiers/`). NEVER reset.
- `.ralph_logs/`  — per-iteration snapshots `.pentest_<n>/` + the final report.

The reset between iterations gives each pass a fresh `.pentest/` (the Ralph
"fresh context") with two deliberate exceptions:
- **`findings/` carries forward** (user decision): confirmed vulns + the dedup
  index persist so a later iteration never re-reports a confirmed finding and the
  final report is cumulative.
- **`audit.jsonl` / `thinking.log` stay in place**: they are the master run log
  (already snapshotted into each backup) AND the Orchestrator holds them open —
  unlinking them would write into a dead inode. Keeping them is both correct and
  safe.

Custom tools need NO special handling: they live under `zero_files/custom_tools/`
(the files-root jail), entirely outside `.pentest/`, so the reset preserves them
automatically.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

# Task ids come from an LLM-authored PRD, so sanitize before using one as a filename.
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


# Names directly under `.pentest/` that the reset PRESERVES (see module docstring).
_KEEP = {"findings", "audit.jsonl", "thinking.log"}
# Subdirs recreated empty after a reset so writers that assume their dir exists don't
# crash mid-run: `memory/` (the memory tools) and `llm/` (the per-actor I/O trace —
# FileHandler does not create parents, so a wiped llm/ would FileNotFoundError on the
# next iteration's first call). `todo/`, `traffic/`, `report.md` self-heal via their
# own writers and need not be listed.
_RECREATE_DIRS = ("memory", "llm")


def pentest_dir(cwd: Path) -> Path:
    return cwd / ".pentest"


def ralph_dir(cwd: Path) -> Path:
    return cwd / ".ralph"


def logs_dir(cwd: Path) -> Path:
    return cwd / ".ralph_logs"


def prd_path(cwd: Path) -> Path:
    return ralph_dir(cwd) / "prd.json"


def progress_path(cwd: Path) -> Path:
    return ralph_dir(cwd) / "progress.txt"


def dossiers_dir(cwd: Path) -> Path:
    return ralph_dir(cwd) / "dossiers"


def dossier_path(cwd: Path, task_id: str) -> Path:
    """The durable per-task evidence dossier. Lives under `.ralph/` (never reset), so
    the accumulated testing evidence for a task survives the per-iteration wipe of
    `.pentest/` — the fix for Ralph re-picking the same task forever because each
    fresh orchestrator session inherited only a bare claim, not the actual work."""
    name = _UNSAFE_ID.sub("_", task_id.strip()).strip("._-") or "task"
    return dossiers_dir(cwd) / f"{name}.md"


def final_report_path(cwd: Path) -> Path:
    return logs_dir(cwd) / "ralph_report.md"


def ensure_dirs(cwd: Path) -> None:
    ralph_dir(cwd).mkdir(parents=True, exist_ok=True)
    dossiers_dir(cwd).mkdir(parents=True, exist_ok=True)
    logs_dir(cwd).mkdir(parents=True, exist_ok=True)


_SNAP_PREFIX = ".pentest_"


def last_iteration_index(cwd: Path) -> int:
    """Highest iteration N already snapshotted under `.ralph_logs/.pentest_N`, or 0
    if none. Used to continue numbering on `--ralph-resume` so backups don't collide
    and the progress journal reads as one continuous log."""
    logs = logs_dir(cwd)
    if not logs.exists():
        return 0
    best = 0
    for p in logs.iterdir():
        if p.is_dir() and p.name.startswith(_SNAP_PREFIX):
            try:
                best = max(best, int(p.name[len(_SNAP_PREFIX):]))
            except ValueError:
                continue
    return best


def backup_and_reset(cwd: Path, iteration: int) -> Path:
    """Snapshot `.pentest/` to `.ralph_logs/.pentest_<iteration>/`, then reset it for
    the next pass — preserving `findings/` (carry forward) and the open master logs.
    Returns the snapshot path. A missing `.pentest/` is a no-op (returns the dest)."""
    src = pentest_dir(cwd)
    dest = logs_dir(cwd) / f".pentest_{iteration}"
    ensure_dirs(cwd)
    if src.exists():
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # copytree over an actively-open audit.jsonl/thinking.log is fine — we only
        # read their bytes for the snapshot.
        shutil.copytree(src, dest, dirs_exist_ok=True)
        for child in src.iterdir():
            if child.name in _KEEP:
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    # Recreate the empty roots the host-side tools expect to write into.
    src.mkdir(parents=True, exist_ok=True)
    for sub in _RECREATE_DIRS:
        (src / sub).mkdir(parents=True, exist_ok=True)
    return dest
