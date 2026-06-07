"""Static checks on the docker/ assets — no daemon required."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).parents[2] / "docker"


def test_entrypoint_exists_and_executable():
    p = DOCKER_DIR / "entrypoint.sh"
    assert p.exists()
    # On a fresh clone the +x bit may not survive packaging; accept either,
    # but require the shebang.
    head = p.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("#!") and "bash" in head


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_entrypoint_bash_syntax():
    p = DOCKER_DIR / "entrypoint.sh"
    rc = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert rc.returncode == 0, rc.stderr


def test_dockerfile_present_and_uses_kali():
    df = DOCKER_DIR / "Dockerfile"
    assert df.exists()
    text = df.read_text(encoding="utf-8")
    assert "kalilinux/kali-rolling" in text
    assert "ENTRYPOINT" in text
    # Names that should be baked in per plan §M4. (jwt_tool is installed from
    # source — ticarpi — since it isn't an apt package; httpx-toolkit provides
    # the ProjectDiscovery probe.)
    for tool in ("sqlmap", "nmap", "ffuf", "nuclei", "jwt_tool", "httpx-toolkit"):
        assert tool in text


def test_tool_server_module_present():
    sv = DOCKER_DIR / "tool_server" / "server.py"
    assert sv.exists()
    txt = sv.read_text(encoding="utf-8")
    assert "FastAPI" in txt
    assert "/tools/{name}" in txt
    assert "/healthz" in txt
