"""The Ralph progress journal (`.ralph/progress.txt`).

Append-only, human-readable, and — crucially — lives OUTSIDE `.pentest/` so it
survives the per-iteration reset. It is the loop's durable memory: the next
iteration's orchestrator reads it first to decide what to work on and to avoid
redoing what prior iterations already covered (the Ralph analog of git history).
"""
from __future__ import annotations

from pathlib import Path


def read_progress(progress_path: Path) -> str:
    if not progress_path.exists():
        return ""
    try:
        return progress_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def append_iteration(
    progress_path: Path,
    *,
    iteration: int,
    summary: str,
    confirmed_titles: list[str],
    task_notes: list[str] | None = None,
) -> None:
    """Append one iteration's record: what the orchestrator did, the findings it
    confirmed, and the per-task completion notes from the analyzer_validator."""
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        f"## Iteration {iteration}",
        "",
        "### Orchestrator summary",
        summary.strip() or "(no summary)",
        "",
        "### Confirmed findings this run (cumulative on disk)",
    ]
    if confirmed_titles:
        lines += [f"- {t}" for t in confirmed_titles]
    else:
        lines.append("- (none confirmed yet)")
    if task_notes:
        lines += ["", "### Task completion notes"]
        lines += [f"- {n}" for n in task_notes]
    lines.append("")
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
