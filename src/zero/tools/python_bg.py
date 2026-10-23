"""Detachable background python jobs.

Start python that runs in the BACKGROUND (a server loop, a listener, a long
poller), keep it alive, read its accumulated logs, stop it, or let it auto-close
after a TTL. This is the general primitive for out-of-band work: the agent writes
whatever listener it needs — an HTTP server that 302-redirects to `file://`, an XXE
DTD host, a socket catcher — runs it detached, and polls the logs for callbacks.

Distinct from `python_exec` (the persistent request/response interpreter): a
`serve_forever()` there would never return and, with no per-call timeout, would
hang the agent's turn forever. Background jobs are fire-and-forget + poll.

Runs wherever the tool runs: the sandbox container by default (so jobs spawn inside
the sandbox), or the host under `--no-container`. For a REMOTE target to reach a
listener a job binds, run with `--host-network` so the bound port sits on the
host's interfaces.
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import Literal

from pydantic import BaseModel, Field

from zero.llm.claude_code import neutralize_nul
from zero.tools.base import RiskLevel, Tool, ToolResult

_DEFAULT_TTL = 300
_MAX_TTL = 3600
_MAX_JOBS = 16
_BUF_LINES = 1000          # ring buffer: keep the last N lines
_BUF_CHARS = 64_000        # ...and at most this many chars


class _BgJob:
    """One detached `python -u -c <code>` process with captured output + TTL."""

    def __init__(self, job_id: str, code: str, ttl: int, owner_id: str) -> None:
        self.id = job_id
        self.owner_id = owner_id
        self.ttl = ttl
        self.started = time.monotonic()
        self.status = "running"
        self.exit_code: int | None = None
        self._lines: list[str] = []
        self._chars = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._timer: asyncio.Task | None = None
        self._code = code

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", self._code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # merge so the log is one stream
        )
        self._reader = asyncio.create_task(self._drain())
        self._timer = asyncio.create_task(self._ttl_kill())

    async def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = neutralize_nul(raw.decode("utf-8", "replace").rstrip("\n"))
                self._lines.append(line)
                self._chars += len(line) + 1
                # Trim the ring buffer from the front.
                while len(self._lines) > _BUF_LINES or self._chars > _BUF_CHARS:
                    dropped = self._lines.pop(0)
                    self._chars -= len(dropped) + 1
        except Exception:  # noqa: BLE001 — reader must never crash the orchestrator
            pass
        # stdout closed → the process is exiting; record final status.
        rc = await self._proc.wait()
        self.exit_code = rc
        if self.status == "running":
            self.status = f"exited({rc})"

    async def _ttl_kill(self) -> None:
        try:
            await asyncio.sleep(self.ttl)
        except asyncio.CancelledError:
            return
        if self.status == "running":
            self.status = "auto-closed (ttl)"
            await self._kill()

    async def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        if self.status == "running":
            self.status = "stopped"
        await self._kill()
        if self._timer is not None:
            self._timer.cancel()

    def logs(self, tail_lines: int | None = None) -> str:
        lines = self._lines if not tail_lines else self._lines[-tail_lines:]
        return "\n".join(lines)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class PythonBgArgs(BaseModel):
    action: Literal["start", "logs", "stop", "list"] = Field(
        description="start a background job | read its logs | stop it | list jobs"
    )
    code: str | None = Field(default=None, description="python source (action=start)")
    job_id: str | None = Field(default=None, description="job id (action=logs/stop)")
    ttl_seconds: int = Field(default=_DEFAULT_TTL, ge=1, le=_MAX_TTL,
                             description="auto-close the job after this many seconds (action=start)")
    tail_lines: int | None = Field(default=None, description="only the last N log lines (action=logs)")
    # Managed automatically by the orchestrator; any model-supplied value is overwritten.
    session_id: str = Field(default="_default")


class PythonBgTool(Tool):
    name = "python_bg"
    description = (
        "Run python in the BACKGROUND and poll it — for listeners / servers / long"
        " pollers (e.g. an HTTP server that redirects to file:// for an SSRF, an XXE"
        " DTD host, a socket catcher). Actions: start(code, ttl_seconds) -> job_id;"
        " logs(job_id) -> captured stdout/stderr so far (your callbacks); stop(job_id);"
        " list. Jobs auto-close after ttl_seconds. Use this — NOT python_exec — for"
        " anything that blocks/serves, since python_exec would hang. (A bound port is"
        " only reachable by a remote target when zero runs with --host-network.)"
    )
    risk_level = RiskLevel.ACTIVE
    runs_on_host = True
    Args = PythonBgArgs

    def __init__(self) -> None:
        self._jobs: dict[str, _BgJob] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PythonBgArgs)
        if args.action == "start":
            return await self._start(args)
        if args.action == "logs":
            return self._logs(args)
        if args.action == "stop":
            return await self._stop(args)
        return self._list(args.session_id)

    async def _start(self, args: PythonBgArgs) -> ToolResult:
        if not args.code or not args.code.strip():
            return ToolResult(ok=False, output="code required for action=start")
        async with self._lock:
            await self._reap_finished_locked()
            live = sum(1 for j in self._jobs.values() if j.status == "running")
            if live >= _MAX_JOBS:
                return ToolResult(ok=False, output=f"too many background jobs (max {_MAX_JOBS}); stop some first")
            self._counter += 1
            job_id = f"bg{self._counter}"
            job = _BgJob(job_id, args.code, args.ttl_seconds, args.session_id)
            try:
                await job.start()
            except OSError as e:
                return ToolResult(ok=False, output=f"failed to start background job: {e}")
            self._jobs[job_id] = job
        return ToolResult(
            ok=True,
            output=(
                f"started background job {job_id} (auto-closes in {args.ttl_seconds}s). "
                f"Read its output with python_bg(action='logs', job_id='{job_id}'); "
                f"stop it with python_bg(action='stop', job_id='{job_id}')."
            ),
            meta={"job_id": job_id},
        )

    def _get(self, job_id: str | None, owner_id: str) -> _BgJob | None:
        job = self._jobs.get(job_id) if job_id else None
        return job if job is not None and job.owner_id == owner_id else None

    def _logs(self, args: PythonBgArgs) -> ToolResult:
        job = self._get(args.job_id, args.session_id)
        if job is None:
            return ToolResult(ok=False, output=f"no such job: {args.job_id!r}")
        body = job.logs(args.tail_lines)
        header = f"[job {job.id} · {job.status} · {job.elapsed:.0f}s]"
        return ToolResult(ok=True, output=f"{header}\n{body}" if body else f"{header}\n(no output yet)")

    async def _stop(self, args: PythonBgArgs) -> ToolResult:
        job = self._get(args.job_id, args.session_id)
        if job is None:
            return ToolResult(ok=False, output=f"no such job: {args.job_id!r}")
        await job.stop()
        return ToolResult(ok=True, output=f"job {job.id} stopped ({job.status}).")

    def _list(self, owner_id: str) -> ToolResult:
        jobs = [job for job in self._jobs.values() if job.owner_id == owner_id]
        if not jobs:
            return ToolResult(ok=True, output="(no background jobs)")
        lines = [
            f"{j.id}  {j.status:18}  {j.elapsed:.0f}s  {j._chars}B captured"
            for j in jobs
        ]
        return ToolResult(ok=True, output="\n".join(lines))

    async def _reap_finished_locked(self) -> None:
        # Drop long-finished jobs to keep the registry bounded (caller holds lock).
        done = [jid for jid, j in self._jobs.items()
                if j.status != "running" and j.elapsed > _MAX_TTL]
        for jid in done:
            self._jobs.pop(jid, None)

    async def aclose(self) -> None:
        """Kill every background job (session teardown; in the container they die
        with it)."""
        async with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for j in jobs:
            await j.stop()
