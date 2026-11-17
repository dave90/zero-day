"""The startup acknowledgement must follow the worker's actual runtime metadata."""

import json
import os
import subprocess
import sys

import pytest

from zero_web.config import RunConfig


@pytest.mark.parametrize("mode", ["a", "c"])
def test_initial_ack_follows_effective_runtime_state(tmp_path, mode):
    config = RunConfig().model_dump()
    config.update(container=False, execution_mode=mode, goal="Offline startup fixture")
    commands = [
        {
            "version": 1,
            "action": "init",
            "command_id": "initialize",
            "run_id": "fixture-run",
            "scripted": True,
            "config": config,
        },
        {"version": 1, "action": "close", "command_id": "close"},
    ]
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"ZERO_FILES_ROOT", "ZERO_MEMORY_ROOT", "ZERO_TODO_ROOT", "ZERO_LLM_LOG_DIR"}
    }
    result = subprocess.run(
        [sys.executable, "-m", "zero_web.worker"],
        input="".join(json.dumps(command) + "\n" for command in commands),
        text=True, capture_output=True, cwd=tmp_path, env=env, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    acknowledged = next(
        i for i, message in enumerate(messages)
        if message.get("command_id") == "initialize"
    )
    assert messages[acknowledged]["result"] == {"ready": True}
    initial_states = [
        message["payload"] for message in messages[:acknowledged]
        if message.get("type") == "event" and message["payload"]["kind"] == "runtime_state"
    ]
    assert any(
        state["execution_location"] == "host" and state["backend"] == "echo"
        for state in initial_states
    ), messages
