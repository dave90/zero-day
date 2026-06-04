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
            r = await rpc.call(
                "notes",
                {"action": "append", "text": "from-container"},
            )
            assert r.ok, r.output
        host_notes = tmp_path / ".pentest" / "notes.md"
        assert host_notes.exists()
        assert "from-container" in host_notes.read_text()
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
                "for b in ['sqlmap','nmap','ffuf','nuclei','curl','jq']:\n"
                "    print(b, '=', shutil.which(b))\n"
            )
            r = await rpc.call("python_exec", {"code": code})
            assert r.ok, r.output
            for required in ("sqlmap", "nmap", "ffuf", "curl"):
                assert f"{required} = /" in r.output, r.output
    finally:
        await mgr.stop()
