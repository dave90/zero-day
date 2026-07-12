"""The per-task evidence dossier (`.ralph/dossiers/<task_id>.md`).

Append-only, human-readable, and — like `progress.txt` — lives OUTSIDE `.pentest/`
so it survives the per-iteration reset. It is the durable record of the *actual
testing evidence* accumulated for one task across iterations.

Why it exists: every Ralph iteration runs a FRESH orchestrator session with a wiped
memory (`.pentest/memory/` is recreated empty each pass). Without this file the only
thing carrying forward was the orchestrator's own bare-claim summary in
`progress.txt` — a *claim* that a task was covered, never the evidence. The
completion judge, handed only that claim, rightly refused to trust it and rejected;
the next fresh session had nothing to build on and re-asserted; the task looped
forever (observed: 32 iterations on one task, orchestrator effort decaying to a
single grep + read per pass). The dossier persists the real work so BOTH the
validator (to verify) and the next iteration (to extend) see evidence, not a claim.
"""
from __future__ import annotations

from pathlib import Path

# The dossier grows unbounded across iterations; cap what we feed back into a prompt
# so token cost stays sane. Newest sections are the most relevant (they carry the
# validator's latest "what's missing"), so we keep the TAIL.
DOSSIER_MAX_CHARS = 12_000


def append_evidence(
    dossier_path: Path,
    *,
    iteration: int,
    summary: str,
    memory_notes: str = "",
) -> None:
    """Append one iteration's evidence for a task: the orchestrator's finalize
    summary plus the working notes it wrote to its (about-to-be-wiped) memory
    scratchpad. Called by the driver right before `backup_and_reset`."""
    dossier_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        f"## Iteration {iteration}",
        "",
        "### Orchestrator summary",
        summary.strip() or "(no summary)",
    ]
    notes = memory_notes.strip()
    if notes:
        lines += ["", "### Working notes (from the orchestrator's memory scratchpad)", notes]
    lines.append("")
    with dossier_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_dossier(dossier_path: Path, *, max_chars: int = DOSSIER_MAX_CHARS) -> str:
    """The accumulated evidence for a task, tail-truncated to `max_chars` (the newest
    sections — including the latest validator-flagged gaps — matter most). Empty
    string if the task has no dossier yet."""
    if not dossier_path.exists():
        return ""
    try:
        text = dossier_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return "…(earlier evidence truncated)…\n" + text[-max_chars:]
