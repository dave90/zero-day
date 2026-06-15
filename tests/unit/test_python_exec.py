"""python_exec runs in a PERSISTENT, per-session interpreter (no per-call
timeout). State survives across calls within a session and is isolated between
sessions — that isolation is what makes concurrent subagents safe."""
from zero.tools.python_exec import _CRASH_RECOVERY_HINT, PythonExecArgs, PythonExecTool


async def test_runs_code():
    t = PythonExecTool()
    try:
        r = await t.run(PythonExecArgs(code="print(1 + 2)"))
        assert r.ok
        assert "3" in r.output
    finally:
        await t.aclose()


async def test_captures_traceback_on_failure():
    t = PythonExecTool()
    try:
        r = await t.run(PythonExecArgs(code="raise ValueError('boom')"))
        assert r.ok is False
        assert "boom" in r.output and "ValueError" in r.output
    finally:
        await t.aclose()


async def test_state_persists_within_a_session():
    t = PythonExecTool()
    try:
        r1 = await t.run(PythonExecArgs(code="x = 41", session_id="s1"))
        assert r1.ok
        r2 = await t.run(PythonExecArgs(code="print(x + 1)", session_id="s1"))
        assert r2.ok and "42" in r2.output
    finally:
        await t.aclose()


async def test_imports_and_helpers_persist():
    t = PythonExecTool()
    try:
        await t.run(PythonExecArgs(code="import math\ndef sq(n):\n    return n * n", session_id="s"))
        r = await t.run(PythonExecArgs(code="print(sq(int(math.sqrt(81))))", session_id="s"))
        assert r.ok and "81" in r.output
    finally:
        await t.aclose()


async def test_sessions_are_isolated():
    """A variable defined in one session must NOT be visible in another — this is
    what keeps concurrent pentester/validator subagents from colliding."""
    t = PythonExecTool()
    try:
        await t.run(PythonExecArgs(code="secret = 'leaked'", session_id="actor_a"))
        r = await t.run(PythonExecArgs(code="print(secret)", session_id="actor_b"))
        assert r.ok is False
        assert "NameError" in r.output
    finally:
        await t.aclose()


async def test_session_survives_user_sys_exit():
    """User code calling sys.exit() must not kill the persistent worker — state
    from before it survives."""
    t = PythonExecTool()
    try:
        await t.run(PythonExecArgs(code="keep = 7", session_id="s"))
        await t.run(PythonExecArgs(code="import sys; sys.exit(1)", session_id="s"))
        r = await t.run(PythonExecArgs(code="print(keep)", session_id="s"))
        assert r.ok and "7" in r.output
    finally:
        await t.aclose()


async def test_native_crash_recovers_then_reports():
    """A NATIVE crash (segfault/abort) that Python can't catch kills the worker.
    A one-off crash recovers (worker restarts, state reset note); the SAME crashing
    code dying again on the fresh worker gets a legible diagnostic, NOT the opaque
    '0 bytes read...' framing error (the Blueprint_Heist pdfminer-segfault bug)."""
    t = PythonExecTool()
    try:
        # os.abort() raises SIGABRT → the process dies hard, like a C-extension
        # segfault. It's deterministic, so it kills the restart too.
        r = await t.run(PythonExecArgs(code="import os; os.abort()", session_id="s"))
        assert r.ok is False
        assert r.output == _CRASH_RECOVERY_HINT
        assert "0 bytes" not in r.output  # the raw framing symptom is gone
        # The session is usable again afterwards (fresh worker).
        r2 = await t.run(PythonExecArgs(code="print('alive')", session_id="s"))
        assert r2.ok and "alive" in r2.output
    finally:
        await t.aclose()


async def test_transient_crash_recovers_with_reset_note(tmp_path):
    """A worker that dies MID-EXCHANGE but whose code succeeds on the restart gets
    the result plus a 'session was reset' note. Modelled with a crash-once-then-pass
    snippet: it aborts the first time (worker dies mid-call), removing the sentinel
    so the retry on the fresh worker prints cleanly."""
    sentinel = tmp_path / "crash_once"
    sentinel.write_text("x")
    code = (
        "import os\n"
        f"if os.path.exists({str(sentinel)!r}):\n"
        f"    os.remove({str(sentinel)!r}); os.abort()\n"
        "print('recovered')\n"
    )
    t = PythonExecTool()
    try:
        r = await t.run(PythonExecArgs(code=code, session_id="s"))
        assert r.ok and "recovered" in r.output
        assert "reset" in r.output  # told the agent its state is gone
    finally:
        await t.aclose()


async def test_long_call_yields_then_polls_to_done():
    """A call slower than wait_seconds YIELDS (status running + partial output) and
    keeps executing; polling it returns the final result. No hard timeout/kill."""
    t = PythonExecTool()
    try:
        code = (
            "import time, sys\n"
            "print('starting', flush=True)\n"
            "time.sleep(2)\n"
            "print('finished')\n"
        )
        r = await t.run(PythonExecArgs(code=code, wait_seconds=1, session_id="s"))
        assert r.ok  # 'running' is reported as ok=True
        assert "STILL RUNNING" in r.output
        assert "starting" in r.output          # partial output is surfaced
        assert "finished" not in r.output       # not done yet
        # While it's running, a new run is refused with a busy hint.
        busy = await t.run(PythonExecArgs(code="print('x')", wait_seconds=1, session_id="s"))
        assert busy.ok is False and "STILL RUNNING" in busy.output
        # Poll until it completes.
        done = await t.run(PythonExecArgs(action="poll", wait_seconds=5, session_id="s"))
        assert done.ok and "finished" in done.output
        # Session is usable normally afterwards.
        r2 = await t.run(PythonExecArgs(code="print(2+2)", session_id="s"))
        assert r2.ok and "4" in r2.output
    finally:
        await t.aclose()


async def test_cancel_stops_a_running_call_and_resets_session():
    t = PythonExecTool()
    try:
        await t.run(PythonExecArgs(code="keep=1", session_id="s"))
        r = await t.run(PythonExecArgs(code="import time; time.sleep(30)", wait_seconds=1, session_id="s"))
        assert "STILL RUNNING" in r.output
        c = await t.run(PythonExecArgs(action="cancel", session_id="s"))
        assert c.ok and "cancelled" in c.output
        # Session was reset; a fresh call works (globals gone, which is expected).
        r2 = await t.run(PythonExecArgs(code="print('alive')", session_id="s"))
        assert r2.ok and "alive" in r2.output
    finally:
        await t.aclose()


async def test_poll_with_nothing_running_is_a_clean_error():
    t = PythonExecTool()
    try:
        r = await t.run(PythonExecArgs(action="poll", session_id="s"))
        assert r.ok is False and "nothing to poll" in r.output
    finally:
        await t.aclose()


async def test_subprocess_stdout_does_not_corrupt_framing():
    """A subprocess that inherits stdout (no capture_output) must NOT corrupt the
    RPC frame stream — the protocol now lives on a private fd, and fd 1 is /dev/null
    inside the worker. Before the fix this desynced the protocol and looked like a
    crash."""
    t = PythonExecTool()
    try:
        code = (
            "import subprocess, sys\n"
            # Child floods its (inherited) stdout with 200 KB — would clobber the
            # frame stream if framing still used fd 1.
            "subprocess.run([sys.executable, '-c', "
            "'import sys; sys.stdout.write(\"X\"*200000); sys.stdout.flush()'])\n"
            "print('PARENT_OK')\n"
        )
        r = await t.run(PythonExecArgs(code=code, session_id="s"))
        assert r.ok, r.output
        assert "PARENT_OK" in r.output
        assert "X" * 50 not in r.output  # child's flood went to /dev/null, not us
        # Protocol still in sync: the next call returns its own correct result.
        r2 = await t.run(PythonExecArgs(code="print(6 * 7)", session_id="s"))
        assert r2.ok and "42" in r2.output
    finally:
        await t.aclose()
