"""Forced shutdown must include a Windows worker's model/tool descendants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from zero_web import sessions


async def test_windows_cleanup_kills_tree_without_unix_signals(monkeypatch):
    process = SimpleNamespace(pid=123, returncode=None, kill=Mock(), wait=AsyncMock())
    killer = SimpleNamespace(wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=killer)
    monkeypatch.setattr(sessions, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sessions.asyncio, "create_subprocess_exec", spawn)
    await sessions.terminate_tree(process)
    assert spawn.call_args.args == ("taskkill", "/PID", "123", "/T", "/F")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_windows_cleanup_falls_back_if_taskkill_is_unavailable(monkeypatch):
    process = SimpleNamespace(pid=123, returncode=None, kill=Mock(), wait=AsyncMock())
    monkeypatch.setattr(sessions, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        sessions.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError)
    )
    await sessions.terminate_tree(process)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_cleanup_never_signals_an_already_exited_worker(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(sessions.asyncio, "create_subprocess_exec", spawn)
    await sessions.terminate_tree(SimpleNamespace(returncode=0))
    spawn.assert_not_awaited()


async def test_cleanup_stops_real_descendant(tmp_path):
    import asyncio
    import sys

    from zero.orchestrator.ownership import WorkspaceOwnership

    # The descendant owns a lock so reacquiring it proves the tool child exited.
    child = """
import sys
from pathlib import Path
from zero.orchestrator.ownership import WorkspaceOwnership
with WorkspaceOwnership(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    parent = """
import subprocess, sys
p = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
assert p.stdout.readline().strip() == b'locked'
print('ready', flush=True)
sys.stdin.readline()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", parent, child, str(tmp_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        start_new_session=sessions.os.name != "nt",
    )
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 10)).strip() == b"ready"
        await sessions.terminate_tree(process)
        with WorkspaceOwnership(tmp_path):
            pass
    finally:
        if process.returncode is None:
            await sessions.terminate_tree(process)
