"""Detachable background python jobs: start / logs / stop / list, TTL auto-close,
log capture, and the end-to-end out-of-band callback-capture pattern (offline:
a background HTTP server records a request we make to it)."""
import asyncio

from zero.tools.python_bg import PythonBgArgs, PythonBgTool


async def _wait_for(tool: PythonBgTool, job_id: str, needle: str, timeout: float = 5.0) -> str:
    deadline = asyncio.get_event_loop().time() + timeout
    last = ""
    while asyncio.get_event_loop().time() < deadline:
        r = await tool.run(PythonBgArgs(action="logs", job_id=job_id))
        last = r.output
        if needle in last:
            return last
        await asyncio.sleep(0.05)
    return last


async def test_start_logs_stop():
    t = PythonBgTool()
    try:
        start = await t.run(PythonBgArgs(
            action="start",
            code="import time\nfor i in range(100):\n    print('tick', i, flush=True); time.sleep(0.1)",
            ttl_seconds=30,
        ))
        assert start.ok and start.meta and start.meta["job_id"]
        job_id = start.meta["job_id"]

        out = await _wait_for(t, job_id, "tick 0")
        assert "tick 0" in out
        assert "running" in out  # status header

        stop = await t.run(PythonBgArgs(action="stop", job_id=job_id))
        assert stop.ok and "stopped" in stop.output

        # list shows it as not-running afterwards
        lst = await t.run(PythonBgArgs(action="list"))
        assert job_id in lst.output
    finally:
        await t.aclose()


async def test_ttl_auto_close():
    t = PythonBgTool()
    try:
        start = await t.run(PythonBgArgs(
            action="start",
            code="import time\nwhile True:\n    time.sleep(0.1)",
            ttl_seconds=1,
        ))
        job_id = start.meta["job_id"]
        # After the 1s TTL the job must no longer be running.
        out = await _wait_for(t, job_id, "auto-closed", timeout=4.0)
        assert "auto-closed" in out, out
    finally:
        await t.aclose()


async def test_oob_callback_capture_end_to_end():
    """The whole OOB pattern, offline: a background HTTP server records an inbound
    request. (With a real engagement + --host-network, the 'request' comes from the
    target via SSRF/XXE; here we make it ourselves.)"""
    server = (
        "import http.server, socketserver\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        print('HIT', self.path, flush=True)\n"
        "        self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "    def log_message(self, *a): pass\n"
        "srv = socketserver.TCPServer(('127.0.0.1', 0), H)\n"
        "print('PORT', srv.server_address[1], flush=True)\n"
        "srv.serve_forever()\n"
    )
    t = PythonBgTool()
    try:
        start = await t.run(PythonBgArgs(action="start", code=server, ttl_seconds=30))
        job_id = start.meta["job_id"]

        out = await _wait_for(t, job_id, "PORT ")
        port = int(out.split("PORT ")[1].split()[0])

        # Make the "callback".
        import urllib.request
        await asyncio.to_thread(
            lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/exfil?secret=abc", timeout=3).read()
        )

        hit = await _wait_for(t, job_id, "HIT /exfil")
        assert "HIT /exfil?secret=abc" in hit, hit
    finally:
        await t.aclose()


async def test_max_jobs_cap(monkeypatch):
    import zero.tools.python_bg as mod
    monkeypatch.setattr(mod, "_MAX_JOBS", 2)
    t = PythonBgTool()
    try:
        ids = []
        for _ in range(2):
            r = await t.run(PythonBgArgs(action="start", code="import time; time.sleep(30)", ttl_seconds=30))
            assert r.ok, r.output
            ids.append(r.meta["job_id"])
        over = await t.run(PythonBgArgs(action="start", code="import time; time.sleep(30)", ttl_seconds=30))
        assert over.ok is False and "too many" in over.output
    finally:
        await t.aclose()


async def test_aclose_kills_jobs():
    t = PythonBgTool()
    start = await t.run(PythonBgArgs(action="start", code="import time\nwhile True: time.sleep(0.1)", ttl_seconds=30))
    job_id = start.meta["job_id"]
    await _wait_for(t, job_id, "", timeout=0.5)  # let it spin up
    await t.aclose()
    # The job's process is dead and the registry is cleared.
    assert not t._jobs


async def test_logs_unknown_job():
    t = PythonBgTool()
    r = await t.run(PythonBgArgs(action="logs", job_id="nope"))
    assert r.ok is False and "no such job" in r.output


async def test_jobs_are_private_to_the_injected_actor():
    t = PythonBgTool()
    try:
        start = await t.run(PythonBgArgs(
            action="start", code="import time; time.sleep(30)", ttl_seconds=30,
            session_id="actor-a",
        ))
        job_id = start.meta["job_id"]
        own = await t.run(PythonBgArgs(action="list", session_id="actor-a"))
        other = await t.run(PythonBgArgs(action="list", session_id="actor-b"))
        denied = await t.run(PythonBgArgs(
            action="logs", job_id=job_id, session_id="actor-b"
        ))
        assert job_id in own.output
        assert "no background jobs" in other.output
        assert not denied.ok and "no such job" in denied.output
    finally:
        await t.aclose()
