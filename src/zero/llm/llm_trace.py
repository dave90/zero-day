"""Per-actor LLM I/O trace logs.

When enabled, every `claude -p` / `codex` call writes its FULL input (argv, system
prompt, stdin body) and FULL output (stdout, stderr, return code) to a file named
after the actor that made the call — one file per agent, since a run has several
concurrent actors (the orchestrator plus each pentester/validator subagent):

    .pentest/llm/orchestrator.log
    .pentest/llm/pentester-3e1d5bb2.log
    .pentest/llm/validator-....log

This is a debugging/tracing aid, OFF by default (full prompts are large). Enable it
either from the CLI (`zero run --llm-log`) or by setting `ZERO_LLM_LOG_DIR` in the
environment (so non-CLI entry points — tests, the regression suite — can turn it on
without plumbing a flag through).

It uses the stdlib `logging` module: one `logging.Logger` + `FileHandler` per actor,
created lazily and cached. Nothing here touches the root logger or other handlers.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

# Where per-actor logs are written. None ⇒ tracing disabled. Initialised lazily from
# the ZERO_LLM_LOG_DIR env var on first use, or set explicitly via set_llm_log_root().
_log_root: Path | None = None
_root_resolved = False
_loggers: dict[str, logging.Logger] = {}
_unsafe = re.compile(r"[^A-Za-z0-9._-]+")


def set_llm_log_root(path: Path | str | None) -> None:
    """Enable (path) or disable (None) LLM I/O tracing. Idempotent; creates the dir.

    Changing the root DROPS the cached per-actor loggers (closing their file handlers)
    so subsequent calls open fresh files under the NEW root. Without this, a process
    that runs several engagements to different dirs (e.g. the regression suite) keeps
    writing every actor's I/O to the FIRST engagement's files — one run's
    `orchestrator.log` swallows them all, and later runs' dirs come up empty."""
    global _log_root, _root_resolved
    _root_resolved = True
    new_root = Path(path) if path is not None else None
    if new_root != _log_root:
        for lg in _loggers.values():
            for h in list(lg.handlers):
                lg.removeHandler(h)
                h.close()
        _loggers.clear()
    _log_root = new_root
    if _log_root is not None:
        _log_root.mkdir(parents=True, exist_ok=True)


def _resolve_root() -> Path | None:
    """Lazily honour ZERO_LLM_LOG_DIR if set_llm_log_root() was never called."""
    global _root_resolved
    if not _root_resolved:
        env = os.environ.get("ZERO_LLM_LOG_DIR")
        set_llm_log_root(env or None)
    return _log_root


def reset_llm_loggers() -> None:
    """Close and drop every cached per-actor logger. The next trace_call re-opens
    fresh file handlers (against the current root). Call this when the log directory
    has been recreated under the process's feet — e.g. Ralph mode snapshots+wipes
    `.pentest/` between iterations — so stale handlers (which hold open fds to the
    now-unlinked old files) don't leak and new actors open clean files."""
    for lg in _loggers.values():
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
    _loggers.clear()


def _logger_for(actor: str) -> logging.Logger | None:
    root = _resolve_root()
    if root is None:
        return None
    safe = _unsafe.sub("_", actor) or "actor"
    cached = _loggers.get(safe)
    if cached is not None:
        return cached
    try:
        # Self-heal the log dir: it may have been removed since startup (Ralph wipes
        # `.pentest/` between iterations), and FileHandler does NOT create parents —
        # without this, opening the handler raises FileNotFoundError. A logging
        # failure must never break a turn, so swallow any error and disable tracing
        # for this actor instead.
        root.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"zero.llm.trace.{safe}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # don't leak full prompts to the app's root logger/UI
        handler = logging.FileHandler(root / f"{safe}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        _loggers[safe] = logger
        return logger
    except Exception:  # noqa: BLE001 — tracing must never break the engagement
        return None


def trace_call(
    actor: str,
    *,
    argv: list[str],
    system_prompt: str,
    stdin_body: str,
    stdout: str,
    stderr: str,
    returncode: int | None,
    resume: bool,
    attempt: int = 0,
) -> None:
    """Append one full request/response record to the actor's trace file. No-op when
    tracing is disabled. Never raises — a logging failure must not break a turn."""
    logger = _logger_for(actor)
    if logger is None:
        return
    try:
        sp = system_prompt if system_prompt else "(omitted — carried in resumed session)"
        # argv[0] is the absolute claude path; keep it, it's useful for "which CLI".
        cmd = " ".join(argv)
        logger.info(
            "\n========== %s  actor=%s  resume=%s  attempt=%d  rc=%s ==========\n"
            "----- ARGV -----\n%s\n"
            "----- SYSTEM PROMPT (%d chars) -----\n%s\n"
            "----- INPUT / stdin (%d chars) -----\n%s\n"
            "----- OUTPUT / stdout (%d chars) -----\n%s\n"
            "----- STDERR (%d chars) -----\n%s\n",
            "CALL", actor, resume, attempt, returncode,
            cmd,
            len(system_prompt), sp,
            len(stdin_body), stdin_body,
            len(stdout), stdout,
            len(stderr), stderr,
        )
    except Exception:  # noqa: BLE001 — tracing must never break the engagement
        pass
