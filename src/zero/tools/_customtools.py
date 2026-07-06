"""Store layout for orchestrator-authored custom tools.

A custom tool is a single self-describing Python script the orchestrator writes
once and any agent can re-run — so a useful bit of automation (a JWT forger, a
bespoke parser, an exploit harness) is built once instead of re-derived in
`python_exec` every turn. Scripts live UNDER the files-root jail at
`<files-root>/custom_tools/<name>.py`, so they persist across runs and — because
`zero_files` is bind-mounted into the sandbox — are visible both host-side and in
the container with no extra mount.

The script's MODULE DOCSTRING is its description/usage: it documents what the
tool does and what JSON args it expects on stdin. Listing parses that docstring
with `ast` (never executes the script). Every path goes through
`_fsroot.resolve_in_root`, so a tool name can never escape the jail.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from zero.tools._fsroot import PathOutsideRoot, resolve_in_root

STORE_SUBDIR = "custom_tools"

# Tool names become filenames and are echoed back into prompts, so keep them to a
# flat, predictable shape. Anything else is collapsed to `_` (defensive — the
# orchestrator is told to use snake_case names).
_UNSAFE = re.compile(r"[^a-z0-9_]+")


def safe_tool_name(name: str) -> str:
    """Sanitize a custom-tool name into a flat `[a-z0-9_]` stem. Returns "" for a
    name with no usable characters (callers reject that)."""
    return _UNSAFE.sub("_", (name or "").strip().lower()).strip("_")


def path_for(name: str) -> Path:
    """The script path for `name`, always inside `<files-root>/custom_tools`.

    Raises ValueError for an empty/unusable name and PathOutsideRoot if the
    sanitized path would somehow escape the jail (belt-and-suspenders — the
    sanitized stem has no separators)."""
    stem = safe_tool_name(name)
    if not stem:
        raise ValueError(f"invalid tool name: {name!r} (use snake_case: a-z, 0-9, _)")
    target, _root = resolve_in_root(f"{STORE_SUBDIR}/{stem}.py")
    return target


def compose_script(description: str, body: str) -> str:
    """Build the script text with `description` as its module docstring, then
    validate it parses. Raises SyntaxError if the composed script won't compile
    (the orchestrator must fix it before it is written)."""
    desc = (description or "").strip()
    # A triple-quoted docstring can't contain `"""`; fold any to single quotes so
    # the description text can never break out of the docstring.
    desc = desc.replace('"""', "'''")
    text = f'"""{desc}"""\n\n{body.strip()}\n'
    compile(text, "<custom_tool>", "exec")  # raises SyntaxError on bad code
    return text


def write_tool(name: str, description: str, body: str) -> Path:
    """Create or overwrite a custom tool. Raises ValueError (bad name) or
    SyntaxError (won't compile) before touching disk."""
    text = compose_script(description, body)  # validate first
    p = path_for(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def read_tool(name: str) -> str | None:
    """The full script text, or None if the tool doesn't exist."""
    try:
        p = path_for(name)
    except (ValueError, PathOutsideRoot):
        return None
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def delete_tool(name: str) -> bool:
    """Remove a custom tool. Returns True if a file was deleted, False if none
    existed."""
    try:
        p = path_for(name)
    except (ValueError, PathOutsideRoot):
        return False
    if not p.exists():
        return False
    p.unlink()
    return True


def _docstring_of(path: Path) -> str:
    """The module docstring of a script (its description), without executing it.
    Returns a placeholder for an unparseable/empty file rather than raising."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return "(unparseable script — no description)"
    return ast.get_docstring(tree) or "(no description)"


def list_tools() -> list[dict[str, str]]:
    """All custom tools as `{name, description}`, sorted by name. Empty when the
    store doesn't exist yet."""
    try:
        root, _r = resolve_in_root(STORE_SUBDIR)
    except PathOutsideRoot:
        return []
    if not root.exists():
        return []
    out: list[dict[str, str]] = []
    for p in sorted(root.glob("*.py")):
        out.append({"name": p.stem, "description": _docstring_of(p)})
    return out


def store_dir() -> Path:
    """The custom-tools directory (may not exist yet). Used for diagnostics."""
    root, _r = resolve_in_root(STORE_SUBDIR)
    return root
