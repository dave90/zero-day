"""Cross-process runtime exclusion, including the native Windows locking path."""

import builtins
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from zero.orchestrator import ownership
from zero.orchestrator.ownership import WorkspaceOwnership

ATTEMPT = """
import sys
from pathlib import Path
from zero.orchestrator.ownership import WorkspaceOwnership
try:
    with WorkspaceOwnership(Path(sys.argv[1])):
        pass
except RuntimeError as exc:
    assert "already in use" in str(exc)
    sys.exit(23)
"""


def attempt(path):
    return subprocess.run(
        [sys.executable, "-c", ATTEMPT, str(path)], capture_output=True, timeout=10, check=False
    )


def test_lock_excludes_other_processes_and_can_be_reacquired(tmp_path):
    lock = tmp_path / ".zero-runtime.lock"
    lock.write_text("stale-pid-that-must-be-replaced")
    with WorkspaceOwnership(tmp_path):
        result = attempt(tmp_path)
        assert result.returncode == 23, result.stderr.decode()
    assert lock.read_text() == str(os.getpid())
    result = attempt(tmp_path)
    assert result.returncode == 0, result.stderr.decode()


def test_process_loss_releases_lock_without_removing_file(tmp_path):
    code = """
import sys
from pathlib import Path
from zero.orchestrator.ownership import WorkspaceOwnership
with WorkspaceOwnership(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout.readline().strip() == b"locked"
        assert attempt(tmp_path).returncode == 23
    finally:
        process.kill()
        process.communicate(timeout=10)
    assert (tmp_path / ".zero-runtime.lock").exists()
    with WorkspaceOwnership(tmp_path):
        pass


def test_unexpected_lock_failure_closes_handle_and_preserves_error(tmp_path, monkeypatch):
    def fail(_):
        raise OSError(errno.EIO, "synthetic disk error")

    monkeypatch.setattr(ownership, "_lock", fail)
    lock = WorkspaceOwnership(tmp_path)
    with pytest.raises(OSError, match="synthetic disk error"):
        lock.__enter__()
    assert lock.handle is None


def test_windows_import_and_locking_do_not_require_fcntl(tmp_path):
    state = {"error": 0, "open": 0}

    def create(_, initial_owner, name):
        assert initial_owner and name.startswith("Global\\ZeroRuntime-")
        state["error"] = 183 if state["open"] else 0
        state["open"] += 1
        return 100

    def close(_):
        state["open"] -= 1
        return True

    def release(_):
        return True

    kernel32 = SimpleNamespace(CreateMutexW=create, CloseHandle=close, ReleaseMutex=release)
    ctypes = SimpleNamespace(
        WinDLL=lambda *a, **kw: kernel32,
        set_last_error=lambda value: state.update(error=value),
        get_last_error=lambda: state["error"],
    )
    windows_os = SimpleNamespace(**{**vars(os), "name": "nt"})

    def windows_import(name, globals=None, locals=None, fromlist=(), level=0):
        assert name != "fcntl", "Windows cannot import the Unix-only fcntl module"
        if name == "os":
            return windows_os
        if name == "ctypes" and not fromlist:
            return ctypes
        return builtins.__import__(name, globals, locals, fromlist, level)

    namespace = {"__builtins__": {**vars(builtins), "__import__": windows_import}}
    # Load our own module with Windows imports while retaining the host Path implementation.
    exec(  # noqa: S102
        compile(Path(ownership.__file__).read_text(), ownership.__file__, "exec"), namespace
    )
    with namespace["WorkspaceOwnership"](tmp_path):
        with pytest.raises(RuntimeError, match="already in use"), namespace["WorkspaceOwnership"](
            tmp_path
        ):
            pass
        assert state["open"] == 1
    assert state["open"] == 0
    assert (tmp_path / ".zero-runtime.lock").read_text() == str(os.getpid())


def test_owned_workspace_can_be_removed_while_locked(tmp_path):
    work = tmp_path / "work"
    with WorkspaceOwnership(work):
        (work / "pentest.md").write_text("Synthetic assessment")
        shutil.rmtree(work)
    assert not work.exists()
