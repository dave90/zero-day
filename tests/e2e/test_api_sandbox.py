"""Real SDK plus real Docker tools; no external provider or real API key."""
import shutil
import subprocess
from dataclasses import replace

import pytest

from tests.e2e.test_api_workflows import WorkflowProvider, cfg_for, run_public
from zero.orchestrator.orchestrator import Orchestrator


def have_image():
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "image", "inspect", "zero-sandbox:latest"], capture_output=True,
    ).returncode == 0


pytestmark = [pytest.mark.docker, pytest.mark.skipif(
    not have_image(), reason="Build sandbox first: docker build -t zero-sandbox:latest -f docker/Dockerfile .",
)]


async def test_api_orchestrates_real_sandbox_children_and_validator_without_key_forwarding(
    api_server, tmp_path, monkeypatch,
):
    api_server.respond = WorkflowProvider()
    observed = {}
    original = Orchestrator._start_container_and_swap_tools

    async def start(orch):
        await original(orch)
        assert orch._container is not None and orch._rpc is not None
        observed["name"] = orch._container.name
        result = await orch._rpc.call("python_exec", {"code": (
            "import os\nprint(os.environ.get('ZERO_TEST_API_KEY', '<unset>'))\n"
            "print(open('/etc/os-release').read())"
        ), "timeout_seconds": 10, "session_id": "api-sandbox-check"})
        observed["output"] = result.output
        assert result.ok

    monkeypatch.setattr(Orchestrator, "_start_container_and_swap_tools", start)
    cfg = replace(cfg_for(api_server, tmp_path), use_container=True)
    orch = await run_public(cfg, monkeypatch)
    assert observed.get("name"), "Sandbox did not start; host fallback does not pass this test"
    assert "kali" in observed["output"].lower()
    assert "<unset>" in observed["output"]
    assert "fake-loopback-provider-key" not in observed["output"]
    assert len(orch.findings_store.all_records("confirmed")) == 1
