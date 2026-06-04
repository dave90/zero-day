"""Read ./pentest.md raw and surface it for prepending to the system prompt.

Phase 1 deliberately does not parse the file — Claude Code's CLAUDE.md model.
"""
from __future__ import annotations

from pathlib import Path

_DELIM_OPEN = "<project_context source=\"pentest.md\">"
_DELIM_CLOSE = "</project_context>"


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
    """Wrap pentest.md contents in a delimited block for the system prompt."""
    return f"{_DELIM_OPEN}\n{raw.rstrip()}\n{_DELIM_CLOSE}"


def project_context_for(cwd: Path) -> str | None:
    """Convenience: load and wrap if present, else None."""
    raw = load_pentest_md(cwd)
    if raw is None:
        return None
    return wrap_for_prompt(raw)
