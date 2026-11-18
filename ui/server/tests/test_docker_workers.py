"""Free integration check of actual web-owned sandboxes; no target or model calls."""

import asyncio
import json
import shutil
import subprocess

import pytest

from zero_web.config import RunConfig


def sandbox_available():
    if not shutil.which("docker"):
        return False
    try:
        return (
            subprocess.run(
                ["docker", "image", "inspect", "zero-sandbox:latest"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not sandbox_available(),
        reason="Build/start Docker: docker build -t zero-sandbox:latest -f docker/Dockerfile .",
    ),
]


async def test_two_web_workers_own_separate_real_sandboxes_and_clean_up(client):
    root = "/api/v1"
    sessions = []
    names = []
    try:
        for label in ("alpha", "beta"):
            attack = (
                await client.post(
                    root + "/engagements",
                    json={
                        "name": label,
                        "target": "https://fixture.example",
                        "context": "# Offline Docker check",
                    },
                )
            ).json()
            cfg = RunConfig().model_dump()
            cfg.update(
                files_root="context", container=True, execution_mode="a", goal="Offline fixture"
            )
            await client.patch(root + f"/engagements/{attack['id']}", json={"config": cfg})
            await client.post(
                root + f"/engagements/{attack['id']}/files",
                json={"name": "marker.txt", "content": label},
            )
            response = await client.post(
                root + f"/engagements/{attack['id']}/sessions",
                json={"config": cfg, "idempotency_key": label},
            )
            assert response.status_code == 201, response.text
            sid = response.json()["session_id"]
            sessions.append(sid)
            session = (await client.get(root + f"/sessions/{sid}/snapshot")).json()
            assert session["snapshot"]["effective"]["execution_location"] == "sandbox"
            work = client._transport.app.state.store.work(attack["id"])
            audit = [
                json.loads(line)
                for line in (work / ".pentest/audit.jsonl").read_text().splitlines()
            ]
            name = next(e["name"] for e in audit if e["kind"] == "container_started")
            names.append(name)
            mounted = await asyncio.to_thread(
                subprocess.run,
                ["docker", "exec", name, "cat", "/zero_files/marker.txt"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert mounted.returncode == 0 and mounted.stdout == label
        assert len(set(names)) == 2
    finally:
        for sid in sessions:
            await client.post(root + f"/sessions/{sid}/close", json={"idempotency_key": "close"})
    for name in names:
        assert (
            subprocess.run(["docker", "inspect", name], capture_output=True, timeout=5).returncode
            != 0
        )
