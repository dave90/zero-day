"""Read ./pentest.md raw and surface it for prepending to the system prompt.

Phase 1 deliberately does not parse the file — Claude Code's CLAUDE.md model.
"""
from __future__ import annotations

from pathlib import Path

def load_pentest_md(cwd: Path) -> str | None:
    """Return the raw contents of ./pentest.md, or None if absent/unreadable."""
    p = cwd / "pentest.md"
    if not p.exists() or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def wrap_for_prompt(raw: str) -> str:
    """Return the brief as plain markdown for the system prompt.

    No XML-ish `<project_context>` wrapper: the caller's `load_system_prompt` already
    prepends a `## Project context` markdown heading, which is the delimiter — wrapping
    markdown content in an XML tag on top of that was redundant and unclear. The contents
    ARE the full brief (scope/target URL/creds/goal); there's nothing extra to open."""
    return raw.strip()


def project_context_for(cwd: Path) -> str | None:
    """Convenience: load and wrap if present, else None."""
    raw = load_pentest_md(cwd)
    if raw is None:
        return None
    return wrap_for_prompt(raw)
