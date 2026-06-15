"""Real Docker e2e for the M4 sandbox. Requires:

  * Docker daemon running on the host
  * Image `zero-sandbox:latest` built from this repo:
        docker build -t zero-sandbox:latest -f docker/Dockerfile .

Tests are gated by the `docker` marker so the default run skips them on
machines without Docker (or where the image hasn't been built yet).

Run with:
    uv run pytest -q -m docker tests/e2e/test_container_real.py
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from zero.container.manager import ContainerManager, ContainerSpec
from zero.container.rpc import ContainerRPC, RPCError


def _image_exists(tag: str) -> bool:
    if shutil.which("docker") is None:
        return False
    rc = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    )
    return rc.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not _image_exists("zero-sandbox:latest"),
        reason="zero-sandbox:latest image not built — run `docker build -t zero-sandbox:latest -f docker/Dockerfile .`",
    ),
]


async def test_python_exec_runs_in_kali(tmp_path: Path):
    """The smoking gun: /etc/os-release on Kali ⇒ proves the call ran
    inside the sandbox, not on the host."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            r = await rpc.call(
                "python_exec",
                {
                    "code": "print(open('/etc/os-release').read())",
                    "timeout_seconds": 10,
                },
            )
            assert r.ok, r.output
            assert "kali" in r.output.lower(), r.output
    finally:
        await mgr.stop()


async def test_wordlists_present_in_sandbox(tmp_path: Path):
    """The image must ship the wordlists ffuf/sqlmap/hydra need — real runs failed
    with '/usr/share/wordlists/... not found' (ffuf dumped usage and stalled).
    Checks dirb common.txt, rockyou.txt (unpacked), and seclists."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            r = await rpc.call(
                "python_exec",
                {
                    "code": (
                        "import os\n"
                        "for p in ['/usr/share/wordlists/dirb/common.txt',\n"
                        "          '/usr/share/wordlists/rockyou.txt',\n"
                        "          '/usr/share/seclists']:\n"
                        "    print(p, os.path.exists(p))\n"
                    )
                },
            )
            assert r.ok, r.output
            assert "/usr/share/wordlists/dirb/common.txt True" in r.output, r.output
            assert "/usr/share/wordlists/rockyou.txt True" in r.output, r.output
            assert "/usr/share/seclists True" in r.output, r.output
    finally:
        await mgr.stop()


async def test_python_exec_yields_then_polls_in_sandbox(tmp_path: Path):
    """In-container: a call slower than wait_seconds yields 'still running' and a
    later poll returns the result (the no-hard-timeout / look-and-decide model)."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            r = await rpc.call(
                "python_exec",
                {"code": "import time; print('go', flush=True); time.sleep(3)",
                 "wait_seconds": 1, "session_id": "s"},
            )
            assert "STILL RUNNING" in r.output and "go" in r.output, r.output
            done = await rpc.call(
                "python_exec", {"action": "poll", "wait_seconds": 6, "session_id": "s"},
            )
            assert done.ok, done.output
    finally:
        await mgr.stop()


async def test_unknown_tool_returns_rpc_error(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            with pytest.raises(RPCError):
                await rpc.call("does_not_exist", {})
    finally:
        await mgr.stop()


async def test_bind_mount_round_trip(tmp_path: Path):
    """A tool that writes under cwd writes through the bind mount."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            # python_exec runs inside the sandbox; writing under cwd must land on
            # the host through the /work bind mount.
            r = await rpc.call(
                "python_exec",
                {"code": "open('round_trip.txt', 'w').write('from-container')"},
            )
            assert r.ok, r.output
        host_file = tmp_path / "round_trip.txt"
        assert host_file.exists()
        assert "from-container" in host_file.read_text()
    finally:
        await mgr.stop()


async def test_container_removed_on_stop(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    name = mgr.name
    assert name is not None
    await mgr.stop()
    rc = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True
    )
    assert rc.returncode != 0, (
        f"container {name} still present after stop:\n{rc.stdout}"
    )


async def test_kali_security_tools_on_path(tmp_path: Path):
    """M5 sanity check: the binary tool surface is reachable from python_exec."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            code = (
                "import shutil\n"
                "for b in ['sqlmap','nmap','ffuf','nuclei','httpx','jwt_tool','curl','jq']:\n"
                "    print(b, '=', shutil.which(b))\n"
            )
            r = await rpc.call("python_exec", {"code": code})
            assert r.ok, r.output
            for required in ("sqlmap", "nmap", "ffuf", "curl", "httpx", "jwt_tool"):
                assert f"{required} = /" in r.output, r.output
    finally:
        await mgr.stop()


async def test_jwt_tool_decodes_on_first_call(tmp_path: Path):
    """Regression (Blueprint_Heist): jwt_tool wasn't installed at all (not an apt
    package), so every call returned 'not on PATH'. After installing ticarpi
    jwt_tool from source AND seeding its config in the image, the FIRST call must
    actually DECODE the token — not just run one-time config setup."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        # A throwaway, well-formed HS256 token ({"role":"user","iat":...}); the
        # signature must be valid base64url or jwt_tool refuses to decode it.
        tok = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJyb2xlIjoidXNlciIsImlhdCI6MTc4MDcyODk4Nn0."
            "M-p9n8ba6W-wEEDj3ozMdfeaCivlp252Es6LJyTcToQ"
        )
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            code = (
                "import subprocess\n"
                f"p = subprocess.run(['jwt_tool', {tok!r}], capture_output=True, text=True)\n"
                "print(p.stdout)\n"
            )
            r = await rpc.call("python_exec", {"code": code})
            assert r.ok, r.output
            # Decoded the payload on the FIRST call (config already seeded),
            # rather than only printing the "Running config setup" notice.
            assert "role" in r.output and "user" in r.output, r.output
            assert "config setup" not in r.output, r.output
    finally:
        await mgr.stop()


async def test_httpx_is_projectdiscovery_not_python_cli(tmp_path: Path):
    """Regression: the pip `httpx` library's console script used to shadow the
    ProjectDiscovery `httpx` probe on PATH, so `httpx_tool` invoked the wrong
    program ("pip install 'httpx[cli]'") and every probe failed. `httpx` must
    resolve to the PD binary inside the sandbox."""
    spec = ContainerSpec(cwd=tmp_path, image="zero-sandbox:latest")
    mgr = ContainerManager(spec)
    await mgr.start()
    try:
        async with ContainerRPC(mgr.rpc_base_url()) as rpc:
            # The real tool path: run httpx_tool against a throwaway target and
            # confirm it did NOT hit the Python-httpx-CLI error.
            r = await rpc.call("httpx_tool", {"target": "http://127.0.0.1:1"})
            assert "httpx[cli]" not in r.output, (
                f"httpx resolved to the Python httpx CLI, not the PD probe:\n{r.output}"
            )
    finally:
        await mgr.stop()
