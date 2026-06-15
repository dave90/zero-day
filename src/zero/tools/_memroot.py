"""Per-agent memory store layout (the `memory_read` / `memory_write` tools).

Each actor in an engagement — the orchestrator, and every pentester / validator
subagent — gets ONE markdown file holding a bounded, self-maintained summary of
what it is doing (the agent overwrites it; it never grows like the transcript).
Files live on the HOST under `$ZERO_MEMORY_ROOT` (set at orchestrator startup),
defaulting to `./.pentest/memory` under the cwd. Unlike the files-root jail
(`_fsroot`), memory always resolves on the host and is NEVER mounted into the
sandbox — so the orchestrator can read any subagent's memory after it finishes,
crashes, or while it is still running.

The file is keyed purely by the per-actor `session_id` the orchestrator injects
(the LLM never sets it — see `_invoke_tool`), so an agent can only ever touch its
own file. `path_for` sanitizes that id into a safe filename defensively, so even a
label-shaped id can't escape the directory.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Anything outside this set is collapsed to `_`, so `..`, `/`, NUL, spaces, and
# the `validator[dino_5 /x]` label shape all reduce to a flat, contained name.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def memory_root() -> Path:
    """The memory directory, read lazily from `$ZERO_MEMORY_ROOT` (set on the
    host at orchestrator startup) or `./.pentest/memory` under the cwd."""
    env = os.environ.get("ZERO_MEMORY_ROOT")
    return Path(env).resolve() if env else (Path.cwd() / ".pentest" / "memory").resolve()


def safe_name(session_id: str) -> str:
    """Sanitize an (infra-set) actor id into a flat, contained filename stem."""
    name = _UNSAFE.sub("_", session_id.strip()).strip("._-")
    return name or "_default"


# Back-compat alias (was named `_safe_name` before `todo.py` reused it).
_safe_name = safe_name


def path_for(session_id: str) -> Path:
    """The memory file for `session_id`, always inside `memory_root()`.

    The id is sanitized to `[A-Za-z0-9_.-]`, so the result can never escape the
    root (defensive: real ids are already clean — `orchestrator`, `gino_2`)."""
    root = memory_root()
    p = (root / f"{_safe_name(session_id)}.md").resolve()
    # Belt-and-suspenders: the sanitized name has no separators, but assert
    # containment anyway so a future change can't silently open a hole.
    assert p.parent == root, f"memory path {p} escaped root {root}"
    return p
