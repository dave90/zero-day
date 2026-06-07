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
}
# Tools covered by a non-execution check instead of a full run:
PRESENCE_ONLY = {"nuclei"}        # binary on PATH; a real run downloads templates (slow/network)
DEP_ONLY = {"web_search"}         # needs the external internet; assert its library imports instead
INTERCEPTED = {"finding_submit"}  # orchestrator-intercepted — never RPC'd to the sandbox
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
    covered = set(BINARY_TOOLS) | set(EXEC_ARGS) | PRESENCE_ONLY | DEP_ONLY | INTERCEPTED
    missing = set(ALL_TOOLS) - covered
    assert not missing, (
        f"tools with NO sandbox coverage: {sorted(missing)} — "
        "add them to tests/e2e/test_tools_in_sandbox.py (EXEC_ARGS or a check)"
    )


@pytest.fixture(scope="module")
def sandbox_url(tmp_path_factory):
    """One Kali sandbox container for the whole module (fast). The container is an
    external process reached over HTTP RPC, so per-test event loops are fine."""
    mgr = ContainerManager(
        ContainerSpec(cwd=tmp_path_factory.mktemp("sandbox"), image="zero-sandbox:latest")
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
async def test_web_search_dependency_present(sandbox_url):
    """web_search needs the external internet, so don't make a live query in the
    default suite — instead assert its provider library imports in the sandbox."""
    async with ContainerRPC(sandbox_url) as rpc:
        r = await rpc.call("python_exec", {"code": "import ddgs; print('ddgs-ok')"})
    assert r.ok and "ddgs-ok" in r.output, r.output
