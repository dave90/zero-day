"""Path jail for the read-only filesystem tools (`list_dir`, `grep`, `read_file`).

These tools must only ever see files under ONE root folder — never the wider host
filesystem. The root is `$ZERO_FILES_ROOT` (set to `/zero_files` inside the sandbox,
or the resolved `--files-root` on the host); absent that, it defaults to `zero_files`
under the current working directory.

`resolve_in_root` is the single chokepoint: it resolves a user-supplied path against
the root and refuses anything that escapes it — via `..`, an absolute path, or a
symlink pointing outside (`Path.resolve()` collapses `..` and follows symlinks, so the
containment check after resolving catches all three). Paths handed to these tools are
therefore interpreted as RELATIVE TO THE ROOT.
"""
from __future__ import annotations

import os
from pathlib import Path


class PathOutsideRoot(Exception):
    """Raised when a requested path resolves outside the files root."""


def files_root() -> Path:
    """The jail root. Read lazily so the same code works in the sandbox (env set to
    /zero_files) and on the host (env set to the resolved --files-root, or unset →
    ./zero_files)."""
    env = os.environ.get("ZERO_FILES_ROOT")
    return Path(env).resolve() if env else (Path.cwd() / "zero_files").resolve()


def resolve_in_root(user_path: str) -> tuple[Path, Path]:
    """Resolve `user_path` under the files root, rejecting any escape.

    Returns (resolved_path, root). Raises PathOutsideRoot if the path would leave the
    root. An absolute `user_path` collapses to itself under `root / user_path`, so the
    containment check rejects absolute paths outside the root just like `..`/symlinks.
    """
    root = files_root()
    p = (root / user_path).resolve()
    if p != root and not p.is_relative_to(root):
        raise PathOutsideRoot(
            f"path {user_path!r} is outside the files root {root} — these tools are "
            "jailed to it; use a path relative to the root"
        )
    return p, root
