"""Run Python in a PERSISTENT, per-session interpreter.

Each `session_id` gets its own long-lived `python` worker subprocess whose
globals survive across calls — so an agent can define a helper / fetch a token
once and reuse it on later turns instead of rebuilding scaffolding every call
(a fresh subprocess per call was ~30-40% of wasted effort in real runs).

Concurrency: the orchestrator gives each actor (orchestrator, each pentester /
validator subagent) a distinct `session_id`, so concurrently-running subagents
get fully isolated interpreters — no shared globals, no races. A single actor
runs its turns sequentially, so a session's worker is never called twice at
once; a per-session lock guards that anyway.

There is no HARD timeout that kills work. Instead a call YIELDS after a soft
`wait_seconds` (default 120s): the code keeps running in a background thread inside
the worker, and the tool returns "still running" + the output so far, so the agent
can LOOK and decide — call again with `action="poll"` to keep waiting, or
`action="cancel"` to stop it (which resets the session). This is the Claude-Code
background-shell pattern; it keeps one slow call (e.g. a recursive `glob` over
/usr/share) from freezing the whole turn forever. Sessions run wherever the tool
runs: in the sandbox container (the in-container tool server holds the singleton)
or on the host under `--no-container`.
"""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import time

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult

# Worker: a tiny server reading length-prefixed JSON COMMAND frames on stdin and
# writing length-prefixed JSON replies. Commands: run / poll. User code runs in a
# background THREAD into a PERSISTENT globals dict so the control loop stays
# responsive: `run` starts the thread and waits up to `wait` seconds; if the code is
# still going it replies status=running with the output so far, and the agent can
# `poll` again (or the host can kill the worker to cancel). stdlib-only.
_WORKER_SRC = r'''
import sys, os, io, json, struct, traceback, contextlib, importlib, threading
# Move the length-prefixed RPC channel onto a PRIVATE fd: keep a dup of the real
# stdout for the protocol, then point fd 1 AND fd 2 at /dev/null. Now nothing the
# user code runs can corrupt the frame stream — not a subprocess that inherits
# stdout, not a C library that writes straight to fd 1/2. Python-level stdout/
# stderr are still captured per-exec via redirect_stdout/stderr below. (Trade-off:
# an inherited subprocess's stdout is discarded — use capture_output=True to read
# it; that's also what keeps a crashing lib's child from taking down this worker.)
_OUT = os.fdopen(os.dup(1), "wb")
_devnull = os.open(os.devnull, os.O_WRONLY)
os.dup2(_devnull, 1)
os.dup2(_devnull, 2)
_IN = sys.stdin.buffer
_G = {"__name__": "__main__"}
_CAP = 200000

_buf = io.StringIO()
_done = threading.Event()
_thread = None
_ok = True

def _read(n):
    b = b""
    while len(b) < n:
        c = _IN.read(n - len(b))
        if not c:
            return None
        b += c
    return b

def _send(obj):
    data = json.dumps(obj).encode("utf-8", "replace")
    _OUT.write(struct.pack(">I", len(data))); _OUT.write(data); _OUT.flush()

def _snapshot():
    try:
        out = _buf.getvalue()
    except Exception:
        return "[partial output unavailable]"
    if len(out) > _CAP:
        out = out[:_CAP] + "\n... (output truncated)"
    return out

def _runner(code):
    global _ok
    try:
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            exec(compile(code, "<python_exec>", "exec"), _G)
        _ok = True
    except BaseException:          # noqa — SystemExit etc. must not kill the worker
        _ok = False
        try:
            _buf.write(traceback.format_exc())
        except Exception:
            pass
    finally:
        _done.set()

while True:
    hdr = _read(4)
    if hdr is None:
        break
    (n,) = struct.unpack(">I", hdr)
    body = _read(n)
    if body is None:
        break
    try:
        msg = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        msg = {"cmd": "run", "code": body.decode("utf-8", "replace")}  # legacy: raw code
    cmd = msg.get("cmd", "run")
    wait = msg.get("wait", 120)
    try:
        wait = max(0.0, float(wait))
    except Exception:
        wait = 120.0
    if cmd == "run":
        if _thread is not None and _thread.is_alive():
            _send({"status": "busy"}); continue
        # Pick up packages installed via pip_install since this worker started.
        importlib.invalidate_caches()
        _buf = io.StringIO(); _done = threading.Event(); _ok = True
        _thread = threading.Thread(target=_runner, args=(msg.get("code", ""),), daemon=True)
        _thread.start()
        _done.wait(timeout=wait)
        if _done.is_set():
            _send({"status": "done", "ok": _ok, "output": _snapshot()}); _thread = None
        else:
            _send({"status": "running", "output": _snapshot()})
    elif cmd == "poll":
        if _thread is None:
            _send({"status": "idle"}); continue
        _done.wait(timeout=wait)
        if _done.is_set():
            _send({"status": "done", "ok": _ok, "output": _snapshot()}); _thread = None
        else:
            _send({"status": "running", "output": _snapshot()})
    else:
        _send({"status": "unknown_cmd"})
'''

# Cap on simultaneously-live workers (across all sessions) to bound memory over a
# long run; the least-recently-used idle session is evicted past this.
_MAX_SESSIONS = 32

# Shown when a fresh worker dies AGAIN on the same code: the call deterministically
# crashes the interpreter (a native segfault / OOM in a C extension — pdfminer,
# pypdf, lxml, Pillow, numpy — which Python can't catch). The raw symptom is an
# opaque "0 bytes read..." framing error; this turns it into something the agent
# can act on, without hardcoding any vuln/library playbook.
_CRASH_RECOVERY_HINT = (
    "python_exec interpreter died running this code and died AGAIN on retry — this "
    "is a native crash (segfault) or out-of-memory kill in a C extension that "
    "Python cannot catch (pdfminer, pypdf, lxml, Pillow, numpy and similar can do "
    "this on malformed or complex input). Your session state (variables, imports) "
    "was lost. Isolate the crashing call in a CHILD process so it can only kill the "
    "child, not your session:\n"
    "    import subprocess, sys\n"
    "    script = r'''<the code that crashes>'''\n"
    "    r = subprocess.run([sys.executable, '-c', script], capture_output=True,"
    " text=True, timeout=120)\n"
    "    print(r.returncode, r.stdout[-2000:], r.stderr[-500:])\n"
    "capture_output=True is required — it keeps the child's output off the session "
    "channel."
)


_DEAD = (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError)


def _running_banner(wait: float) -> str:
    return (
        f"[python_exec STILL RUNNING after {wait:g}s — the code keeps executing in the "
        "background; output so far is below. Call python_exec again with "
        '{"action":"poll"} to keep waiting, or {"action":"cancel"} to stop it '
        "(cancel resets this session — variables/imports are lost).]\n"
    )


_BUSY_HINT = (
    "a previous python_exec call in this session is STILL RUNNING — use "
    '{"action":"poll"} to check on it or {"action":"cancel"} to abort it before '
    "running new code."
)

_RESET_NOTE = (
    "[note: your python session was reset (the worker had died); previously defined "
    "variables are gone — re-establish them]\n"
)


class _Session:
    """One persistent worker subprocess for a session_id."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self.last_used = time.monotonic()
        # True while a run is executing in the worker but hasn't completed (it
        # yielded). New `run`s are refused until it's polled to completion or cancelled.
        self._pending = False

    async def _ensure(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        # stderr → DEVNULL: the worker repoints its own fd 2 to /dev/null anyway,
        # so this pipe would only ever sit unread (a fill-up deadlock risk).
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", _WORKER_SRC,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def run(self, code: str, wait: float) -> ToolResult:
        async with self._lock:
            self.last_used = time.monotonic()
            if self._pending:
                return ToolResult(ok=False, output=_BUSY_HINT)
            await self._ensure()
            msg = {"cmd": "run", "code": code, "wait": wait}
            try:
                return self._format(await self._exchange(msg), wait=wait)
            except _DEAD:
                # Worker died mid-exchange (hard crash / os._exit / OOM-kill).
                # Restart it — globals are lost — and retry ONCE; a transient crash
                # recovers here.
                await self.close()
                await self._ensure()
            try:
                rep = await self._exchange(msg)
            except _DEAD:
                # Died AGAIN on a fresh worker → this code deterministically crashes
                # the interpreter (native segfault / OOM). Don't keep looping on it;
                # hand the agent a legible, actionable diagnostic.
                await self.close()
                return ToolResult(ok=False, output=_CRASH_RECOVERY_HINT)
            return self._format(rep, wait=wait, reset=True)

    async def poll(self, wait: float) -> ToolResult:
        async with self._lock:
            self.last_used = time.monotonic()
            if not self._pending:
                return ToolResult(
                    ok=False,
                    output="no python_exec call is running in this session — nothing to poll.",
                )
            try:
                return self._format(await self._exchange({"cmd": "poll", "wait": wait}), wait=wait)
            except _DEAD:
                # The background code crashed the interpreter while we waited.
                await self.close()
                self._pending = False
                return ToolResult(ok=False, output=_CRASH_RECOVERY_HINT)

    async def cancel(self) -> ToolResult:
        async with self._lock:
            self.last_used = time.monotonic()
            had = self._pending or (self._proc is not None and self._proc.returncode is None)
            await self.close()  # killing the worker is the only sure way to stop the code
            self._pending = False
            if not had:
                return ToolResult(ok=True, output="nothing was running; session is clean.")
            return ToolResult(
                ok=True,
                output="python_exec call cancelled; the session was reset "
                "(variables/imports are gone).",
            )

    def _format(self, rep: dict, *, wait: float, reset: bool = False) -> ToolResult:
        status = rep.get("status")
        if status == "running":
            self._pending = True
            return ToolResult(ok=True, output=_running_banner(wait) + str(rep.get("output", "")))
        if status == "busy":
            return ToolResult(ok=False, output=_BUSY_HINT)
        # done (or unknown)
        self._pending = False
        out = str(rep.get("output", ""))
        if reset:
            out = _RESET_NOTE + out
        return ToolResult(ok=bool(rep.get("ok", False)), output=out)

    async def _exchange(self, msg: dict) -> dict:
        assert self._proc is not None and self._proc.stdin and self._proc.stdout
        data = json.dumps(msg).encode("utf-8", "replace")
        self._proc.stdin.write(struct.pack(">I", len(data)) + data)
        await self._proc.stdin.drain()
        hdr = await self._proc.stdout.readexactly(4)
        (n,) = struct.unpack(">I", hdr)
        payload = await self._proc.stdout.readexactly(n)
        return json.loads(payload.decode("utf-8", "replace"))

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


class PythonExecArgs(BaseModel):
    code: str = Field(
        default="",
        description="Python source; runs in your persistent session (variables persist"
        " across calls). Required for action='run'; ignored for poll/cancel.",
    )
    action: str = Field(
        default="run",
        description="'run' (default) executes code; 'poll' checks a call that is still"
        " running and waits more; 'cancel' stops a still-running call (resets the session).",
    )
    wait_seconds: float = Field(
        default=120.0, ge=1.0, le=3600.0,
        description="how long to wait before yielding a 'still running' result you can"
        " poll — NOT a kill timeout; the code keeps running in the background.",
    )
    # Managed automatically by the orchestrator (one isolated session per agent).
    # You do not need to set this.
    session_id: str = Field(default="_default")


class PythonExecTool(Tool):
    name = "python_exec"
    description = (
        "Run a Python snippet and return its stdout + stderr. Your interpreter is"
        " PERSISTENT: variables, imports, and functions you define survive across"
        " calls, so set up helpers/tokens once and reuse them. There is NO kill"
        " timeout: if a call runs longer than wait_seconds it KEEPS RUNNING in the"
        " background and you get the output so far — call again with action='poll' to"
        " wait more, or action='cancel' to stop it (cancel resets the session). Avoid"
        " unbounded work like recursive glob over /usr/share; target known paths."
    )
    risk_level = RiskLevel.ACTIVE  # arbitrary code; treat as ACTIVE on the host
    runs_on_host = True
    Args = PythonExecArgs

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        self._sessions_lock = asyncio.Lock()

    async def _session(self, session_id: str) -> _Session:
        async with self._sessions_lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                await self._evict_if_full()
                sess = _Session()
                self._sessions[session_id] = sess
            return sess

    async def _evict_if_full(self) -> None:
        # Caller holds _sessions_lock. Evict the least-recently-used session
        # when over the cap so workers don't accumulate across a long run.
        while len(self._sessions) >= _MAX_SESSIONS:
            sid = min(self._sessions, key=lambda k: self._sessions[k].last_used)
            await self._sessions.pop(sid).close()

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, PythonExecArgs)
        sess = await self._session(args.session_id or "_default")
        if args.action == "poll":
            return await sess.poll(args.wait_seconds)
        if args.action == "cancel":
            return await sess.cancel()
        return await sess.run(args.code, args.wait_seconds)

    async def aclose(self) -> None:
        """Kill every session worker (host-mode teardown; in the container they
        die with it)."""
        async with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            await s.close()
