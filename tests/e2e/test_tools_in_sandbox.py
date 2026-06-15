"""Every registered tool is exercised inside the real Docker sandbox.

This is the layer that catches "the tool's command is correct but the binary is
missing / the wrong program / a python dep isn't installed in the image" — bugs
that unit tests structurally cannot see (they never run the sandbox). Examples
this would have caught: `httpx` resolving to the pip httpx CLI instead of the
ProjectDiscovery probe, and `jwt_tool` not being installed at all.

The coverage is REGISTRY-DRIVEN: `test_every_registered_tool_has_sandbox_coverage`
fails (offline, fast) if a newly added tool isn't accounted for here, so no tool
can ship without a sandbox check.

Requires Docker + the built image:
    docker build -t zero-sandbox:latest -f docker/Dockerfile .
The sandbox tests skip (loudly, with a reason) when the image isn't present;
they otherwise run as part of the default `pytest` invocation.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from zero.container.manager import ContainerManager, ContainerSpec
from zero.container.rpc import ContainerRPC
from zero.tools.binary import BinaryTool
from zero.tools.registry import default_registry

_REG = default_registry()
ALL_TOOLS = sorted(_REG.names())
BINARY_TOOLS = sorted(n for n in ALL_TOOLS if isinstance(_REG.get(n), BinaryTool))

# Hermetic invocations. 127.0.0.1:1 is a closed port: the tool RUNS and simply
# can't connect (web_fetch/http_replay return a clean "http error:", the binary
# tools exit non-zero) — never an external network call.
EXEC_ARGS: dict[str, dict] = {
    "python_exec": {"code": "print('sandbox-ok')"},
    "burp_traffic": {"action": "list_hosts"},
    "web_fetch": {"url": "http://127.0.0.1:1/"},
    "http_replay": {"url": "http://127.0.0.1:1/", "method": "GET"},
    "httpx_tool": {"target": "http://127.0.0.1:1"},
    "curl_raw": {"url": "http://127.0.0.1:1/"},
    "nmap": {"target": "127.0.0.1", "ports": "1"},
    "ffuf": {"url": "http://127.0.0.1:1/FUZZ", "wordlist": "/etc/hostname", "match_status": "200"},
    "sqlmap": {"url": "http://127.0.0.1:1/?id=1", "extra_flags": ["--batch"], "timeout_seconds": 90},
    "python_bg": {"action": "list"},   # cheap "runs in sandbox" check; lifecycle in its own docker test
    # Read-only fs tools, jailed to /zero_files (mounted by the fixture below). The
    # root starts empty, so these run cleanly (list → empty, grep → no matches,
    # read → not-found) — none is a "could not run".
    "list_dir": {"path": "."},
    "grep": {"pattern": "x", "path": "."},
    "read_file": {"path": "anything.txt"},
}
# Tools covered by a non-execution check instead of a full run:
PRESENCE_ONLY = {"nuclei"}        # binary on PATH; a real run downloads templates (slow/network)
DEP_ONLY = {"web_search"}         # needs the external internet; assert its library imports instead
INTERCEPTED = {"finding_submit"}  # orchestrator-intercepted — never RPC'd to the sandbox
INSTALLER = {"pip_install"}       # has its own docker test + unit tests (network-aware)
HOST_ONLY = {"memory_read", "memory_write", "todo_read", "todo_write"}  # run on the host (excluded from remoting); covered by unit tests
# jwt_tool is on PATH here (BINARY_TOOLS) and functionally decoded in
# test_container_real.py::test_jwt_tool_decodes_on_first_call.

# Output substrings that mean the tool ITSELF could not run in the sandbox (as
# opposed to running fine but the target refusing the connection).
_COULD_NOT_RUN = (
    "not on PATH",
    "binary not configured",
    "failed to spawn",
    "could not run because the required dependencies",  # the pip httpx CLI
    "httpx[cli]",
    "No module named",
    "ModuleNotFoundError",
    "ImportError",
    "embedded null byte",
)


def _image_exists(tag: str = "zero-sandbox:latest") -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode == 0


_NEEDS_IMAGE = pytest.mark.skipif(
    not _image_exists(),
    reason="zero-sandbox:latest not built — run `docker build -t zero-sandbox:latest -f docker/Dockerfile .`",
)


def test_every_registered_tool_has_sandbox_coverage():
    """Registry-driven guard (offline): every tool MUST be exercised by a sandbox
    test below — executed, present-on-PATH, dependency-checked, or explicitly
    intercepted. A new tool can't be added without sandbox coverage."""
    covered = (
        set(BINARY_TOOLS) | set(EXEC_ARGS) | PRESENCE_ONLY | DEP_ONLY
        | INTERCEPTED | INSTALLER | HOST_ONLY
    )
    missing = set(ALL_TOOLS) - covered
    assert not missing, (
        f"tools with NO sandbox coverage: {sorted(missing)} — "
        "add them to tests/e2e/test_tools_in_sandbox.py (EXEC_ARGS or a check)"
    )


@pytest.fixture(scope="module")
def sandbox_url(tmp_path_factory):
    """One Kali sandbox container for the whole module (fast). The container is an
    external process reached over HTTP RPC, so per-test event loops are fine."""
    files_root = tmp_path_factory.mktemp("zero_files")  # absolute, exists → bind-mounted
    mgr = ContainerManager(
        ContainerSpec(
            cwd=tmp_path_factory.mktemp("sandbox"),
            image="zero-sandbox:latest",
            files_root=files_root,
        )
    )
    asyncio.run(mgr.start())
    try:
        yield mgr.rpc_base_url()
    finally:
        asyncio.run(mgr.stop())


@pytest.mark.docker
@_NEEDS_IMAGE
@pytest.mark.parametrize("name", BINARY_TOOLS)
async def test_binary_tool_resolves_in_sandbox(sandbox_url, name):
    """Every BinaryTool's `binary` resolves on PATH inside the sandbox (catches a
    missing binary like jwt_tool, and a misnamed/shadowed one)."""
    tool = _REG.get(name)
    code = f"import shutil; print(shutil.which({tool.binary!r}) or 'MISSING')"
    async with ContainerRPC(sandbox_url) as rpc:
        r = await rpc.call("python_exec", {"code": code})
    assert r.ok, r.output
    assert "MISSING" not in r.output and "/" in r.output, (
        f"{name}: binary {tool.binary!r} not on PATH in sandbox: {r.output!r}"
    )


@pytest.mark.docker
@_NEEDS_IMAGE
@pytest.mark.parametrize("name", sorted(EXEC_ARGS))
async def test_tool_executes_in_sandbox(sandbox_url, name):
    """Every runnable tool actually executes via the RPC path against a closed
    local port — proving it runs in the sandbox (no missing/wrong binary, no
    missing python dep). A refused connection is fine; a 'could not run' is not."""
    async with ContainerRPC(sandbox_url) as rpc:
        r = await rpc.call(name, EXEC_ARGS[name])
    hit = [s for s in _COULD_NOT_RUN if s in (r.output or "")]
    assert not hit, f"{name} could not run in sandbox {hit}: {(r.output or '')[:400]}"


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_python_exec_session_persists_in_sandbox(sandbox_url):
    """python_exec keeps a PERSISTENT per-session interpreter inside the sandbox:
    state survives across RPC calls in the same session and is isolated between
    sessions (what makes concurrent subagents safe)."""
    async with ContainerRPC(sandbox_url) as rpc:
        r1 = await rpc.call("python_exec", {"code": "carry = 123", "session_id": "t"})
        assert r1.ok, r1.output
        r2 = await rpc.call("python_exec", {"code": "print(carry)", "session_id": "t"})
        assert r2.ok and "123" in r2.output, r2.output           # persisted across calls
        r3 = await rpc.call("python_exec", {"code": "print(carry)", "session_id": "other"})
        assert r3.ok is False and "NameError" in r3.output, r3.output  # isolated session


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_python_exec_survives_native_crash_in_sandbox(sandbox_url):
    """Regression (Blueprint_Heist): a native crash (segfault/abort, as pdfminer/
    pypdf did on the rendered PDF) used to surface the opaque '0 bytes read...'
    framing error and wedge the run. Now it returns a legible diagnostic and the
    session is reusable; and a subprocess flooding its inherited stdout can no
    longer corrupt the RPC frame stream (framing lives on a private fd)."""
    async with ContainerRPC(sandbox_url) as rpc:
        crash = await rpc.call("python_exec", {"code": "import os; os.abort()", "session_id": "c"})
        assert crash.ok is False, crash.output
        assert "native crash" in crash.output and "0 bytes" not in crash.output, crash.output
        alive = await rpc.call("python_exec", {"code": "print('alive')", "session_id": "c"})
        assert alive.ok and "alive" in alive.output, alive.output

        flood = (
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', "
            "'import sys; sys.stdout.write(chr(88)*200000); sys.stdout.flush()'])\n"
            "print('PARENT_OK')\n"
        )
        r = await rpc.call("python_exec", {"code": flood, "session_id": "d"})
        assert r.ok and "PARENT_OK" in r.output and ("X" * 50) not in r.output, r.output
        r2 = await rpc.call("python_exec", {"code": "print(6 * 7)", "session_id": "d"})
        assert r2.ok and "42" in r2.output, r2.output  # framing still in sync


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_pdfminer_preinstalled_in_sandbox(sandbox_url):
    """Regression (Blueprint_Heist): the agent burned ~6 turns trying to install
    pdfminer at runtime (it hung). It's now pre-baked, so the import just works —
    no install needed."""
    async with ContainerRPC(sandbox_url) as rpc:
        r = await rpc.call(
            "python_exec",
            {"code": "import pdfminer; print('pdfminer', pdfminer.__version__)"},
        )
    assert r.ok and "pdfminer" in r.output, r.output


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_pip_install_runs_in_sandbox(sandbox_url):
    """pip_install runs end-to-end in the sandbox without hanging (uses an
    already-satisfied package so it's fast and offline-friendly), and rejects
    option-injection specs."""
    async with ContainerRPC(sandbox_url) as rpc:
        ok = await rpc.call("pip_install", {"packages": ["pdfminer.six"]})
        assert ok.ok, ok.output  # already satisfied → fast success, no hang
        bad = await rpc.call("pip_install", {"packages": ["--index-url=http://evil/"]})
        assert bad.ok is False and "unsafe" in bad.output, bad.output


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_python_bg_lifecycle_in_sandbox(sandbox_url):
    """A detached background job runs IN the sandbox: start a server loop, read its
    captured logs, then stop it. (This is the OOB-listener primitive.)"""
    import asyncio

    code = (
        "import sys, time\n"
        "print('listener up', flush=True)\n"
        "for i in range(100):\n"
        "    print('tick', i, flush=True); time.sleep(0.2)\n"
    )
    async with ContainerRPC(sandbox_url) as rpc:
        start = await rpc.call("python_bg", {"action": "start", "code": code, "ttl_seconds": 30})
        assert start.ok, start.output
        job_id = (start.meta or {}).get("job_id")
        assert job_id, start.output
        # Poll the logs until the background output shows up.
        seen = ""
        for _ in range(40):
            r = await rpc.call("python_bg", {"action": "logs", "job_id": job_id})
            if "listener up" in r.output:
                seen = r.output
                break
            await asyncio.sleep(0.1)
        assert "listener up" in seen, seen
        stop = await rpc.call("python_bg", {"action": "stop", "job_id": job_id})
        assert stop.ok and "stopped" in stop.output, stop.output


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_fs_tools_jailed_to_files_root_in_sandbox(sandbox_url):
    """The read-only fs tools see files under the mounted /zero_files root and are
    HARD-jailed: a path that escapes the root (e.g. /etc/passwd) is rejected, proving
    the agent can't read the wider container/host filesystem with them."""
    async with ContainerRPC(sandbox_url) as rpc:
        # Drop a file into the jail root (python_exec can write anywhere it can reach).
        w = await rpc.call("python_exec", {"code": "open('/zero_files/loot.txt','w').write('TOPSECRET flag{x}')"})
        assert w.ok, w.output

        ls = await rpc.call("list_dir", {"path": "."})
        assert ls.ok and "loot.txt" in ls.output, ls.output
        g = await rpc.call("grep", {"pattern": "flag\\{", "path": "."})
        assert g.ok and "loot.txt:1:" in g.output, g.output
        rd = await rpc.call("read_file", {"path": "loot.txt"})
        assert rd.ok and "TOPSECRET" in rd.output, rd.output

        # Jail: must NOT be able to read outside the root.
        esc = await rpc.call("read_file", {"path": "/etc/passwd"})
        assert esc.ok is False and "outside the files root" in esc.output, esc.output


@pytest.mark.docker
@_NEEDS_IMAGE
async def test_web_search_dependency_present(sandbox_url):
    """web_search needs the external internet, so don't make a live query in the
    default suite — instead assert its provider library imports in the sandbox."""
    async with ContainerRPC(sandbox_url) as rpc:
        r = await rpc.call("python_exec", {"code": "import ddgs; print('ddgs-ok')"})
    assert r.ok and "ddgs-ok" in r.output, r.output
