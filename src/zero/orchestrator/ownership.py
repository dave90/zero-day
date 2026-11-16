"""Advisory cross-process ownership shared by terminal and headless runtimes."""
from __future__ import annotations

import errno
import os
from pathlib import Path

if os.name == "nt":
    def _lock(path):
        import ctypes
        import hashlib
        from ctypes import wintypes

        # A named kernel mutex keeps ownership outside the directory being
        # deleted. Windows cannot remove a directory containing an open lock file.
        key = os.path.normcase(str(path.resolve())).encode("utf-8")
        name = "Global\\ZeroRuntime-" + hashlib.sha256(key).hexdigest()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        release = kernel32.ReleaseMutex
        release.argtypes = [wintypes.HANDLE]
        release.restype = wintypes.BOOL
        ctypes.set_last_error(0)
        handle = create(None, True, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            # Reject even same-thread re-entry: native mutex waits are recursive.
            # Only the owner keeps a handle, so process loss removes the mutex.
            close(handle)
            raise BlockingIOError(errno.EAGAIN, "Runtime mutex already exists")
        return kernel32, handle

    def _unlock(token):
        kernel32, handle = token
        try:
            if not kernel32.ReleaseMutex(handle):
                import ctypes

                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(handle)
else:
    import fcntl

    def _lock(path):
        # Never truncate before ownership is acquired; another runtime may own it.
        handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        return handle

    def _unlock(handle):
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class WorkspaceOwnership:
    def __init__(self, cwd: Path) -> None:
        self.path = cwd / ".zero-runtime.lock"
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = _lock(self.path)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise RuntimeError(
                    "Engagement directory is already in use by another runtime"
                ) from None
            raise
        try:
            # Diagnostic only: a stale PID never owns a workspace. On Windows
            # close this file immediately so deleting an owned attack is possible.
            self.path.write_text(str(os.getpid()), encoding="ascii")
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            try:
                _unlock(self.handle)
            finally:
                self.handle = None
