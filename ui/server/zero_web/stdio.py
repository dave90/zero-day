"""Read the parent's NDJSON pipe without platform-specific asyncio pipe handles."""

from __future__ import annotations

import asyncio
import os
import threading

MAX_COMMAND_BYTES = 16 * 1024 * 1024


class CommandReader:
    def __init__(self, fd: int):
        self._queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def deliver(item):
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, item)
            except RuntimeError:
                return False  # The runtime has already closed its event loop.
            return True

        def read():
            # Windows subprocess stdin is a synchronous pipe, which Proactor
            # connect_read_pipe cannot attach to. A daemon avoids hanging executor
            # shutdown if the parent keeps stdin open. os.read avoids holding a
            # buffered sys.stdin lock during interpreter shutdown.
            buffer = b""
            try:
                while chunk := os.read(fd, 64 * 1024):
                    buffer += chunk
                    while b"\n" in buffer:
                        line, _, buffer = buffer.partition(b"\n")
                        if len(line) + 1 > MAX_COMMAND_BYTES:
                            raise ValueError("Worker command exceeds the IPC size limit")
                        if not deliver(line + b"\n"):
                            return
                    if len(buffer) > MAX_COMMAND_BYTES:
                        raise ValueError("Worker command exceeds the IPC size limit")
                if buffer and not deliver(buffer):
                    return
                deliver(b"")
            except Exception as exc:
                deliver(exc)

        threading.Thread(target=read, name="studio-stdin", daemon=True).start()

    async def readline(self) -> bytes:
        item = await self._queue.get()
        if isinstance(item, Exception):
            raise item
        return item
