"""Write a single Markdown report: found & approved vs tested & rejected/failed."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from zero.findings.store import FindingsStore


def _record_block(rec: Any, *, with_reproducer: bool) -> list[str]:
    lines = [f"## {rec.finding.title}", f"- ID: `{rec.id}`", f"- Endpoint: `{rec.finding.endpoint}`", f"- CWE: {rec.finding.cwe}"]
    if rec.validation:
        if rec.validation.cvss40_score or rec.validation.cvss40_vector:
            lines.append(f"- CVSS 4.0: {rec.validation.cvss40_score} (`{rec.validation.cvss40_vector}`)")
        lines.append(f"- Verdict: **{rec.validation.verdict}**")
    lines.append("")
    lines.append(rec.finding.summary)
    lines.append("")
    if rec.validation and rec.validation.rationale.strip():
        lines.append("**Why:** " + rec.validation.rationale.strip())
        lines.append("")
    if with_reproducer and rec.validation and rec.validation.reproducer.strip():
        lines.append("### Reproducer")
        lines.append("```")
        lines.append(rec.validation.reproducer.strip())
        lines.append("```")
        lines.append("")
    return lines


def write_report(
    store: FindingsStore, out_path: Path, *, attempts: Iterable[Any] | None = None
) -> Path:
    """Aggregate the engagement into one report.

    Sections: Found & approved (confirmed), Tested & rejected, Pending, and
    Tested but didn't work (attack ideas that produced no confirmed finding).
    `attempts` is the SubagentManager's AttackAttempt list (M8); None in modes a/b.
    """
    confirmed = store.all_records("confirmed")
    rejected = store.all_records("rejected")
    pending = store.all_records("pending")

    lines: list[str] = ["# Pentest findings report", ""]
    lines.append(
        f"**{len(confirmed)} confirmed** · {len(rejected)} rejected · {len(pending)} pending"
    )
    lines.append("")

    lines.append("## Found & approved")
    lines.append("")
    if not confirmed:
        lines.append("_(no confirmed findings)_")
        lines.append("")
    else:
        for rec in confirmed:
            lines.extend(_record_block(rec, with_reproducer=True))

    lines.append("## Tested & rejected")
    lines.append("")
    if not rejected:
        lines.append("_(none)_")
        lines.append("")
    else:
        for rec in rejected:
            lines.append(f"- **{rec.finding.title}** (`{rec.id}`, {rec.finding.endpoint})")
            if rec.validation and rec.validation.rationale.strip():
                lines.append(f"  - why not: {rec.validation.rationale.strip()}")
        lines.append("")

    lines.append("## Pending (could not be confirmed this session)")
    lines.append("")
    if not pending:
        lines.append("_(none)_")
        lines.append("")
    else:
        for rec in pending:
            note = rec.finding.notes.strip().splitlines()[-1] if rec.finding.notes.strip() else ""
            lines.append(f"- **{rec.finding.title}** (`{rec.id}`) — {note}")
        lines.append("")

    # Tested-but-didn't-work attack ideas (M8). A pentester that found nothing
    # or errored — distinct from a rejected *finding*.
    failed = [
        a for a in (attempts or [])
        if getattr(a, "kind", "") == "pentester" and getattr(a, "status", "") in ("NO_FINDING", "ERROR")
    ]
    lines.append("## Tested but didn't work")
    lines.append("")
    if not failed:
        lines.append("_(none recorded)_")
        lines.append("")
    else:
        for a in failed:
            lines.append(f"- [{a.subagent_id}] {a.idea} @ `{a.target}` — {a.status}")
            if getattr(a, "outcome", ""):
                lines.append(f"  - {a.outcome}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
