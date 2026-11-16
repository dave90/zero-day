"""Exercise synchronous OS pipes used by Windows worker stdin, without models."""

import asyncio
import os
import subprocess
import sys

import pytest

from zero_web import stdio
from zero_web.stdio import CommandReader


async def test_command_pipe_preserves_lines_utf8_and_eof():
    read_fd, write_fd = os.pipe()
    try:
        reader = CommandReader(read_fd)
        # Split both a JSON line and a multibyte character across OS writes.
        data = '{"text":"café"}\n{"action":"close"}\n'.encode()
        os.write(write_fd, data[:13])
        await asyncio.sleep(0.01)
        os.write(write_fd, data[13:])
        os.close(write_fd)
        write_fd = None
        assert await asyncio.wait_for(reader.readline(), 3) == data.splitlines(True)[0]
        assert await asyncio.wait_for(reader.readline(), 3) == data.splitlines(True)[1]
        assert await asyncio.wait_for(reader.readline(), 3) == b""
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("data", [b"x" * 33, b"x" * 32 + b"\n"])
async def test_oversized_commands_fail_instead_of_hanging(data, monkeypatch):
    monkeypatch.setattr(stdio, "MAX_COMMAND_BYTES", 32)
    read_fd, write_fd = os.pipe()
    try:
        reader = CommandReader(read_fd)
        os.write(write_fd, data)
        os.close(write_fd)
        with pytest.raises(ValueError, match="IPC size limit"):
            await asyncio.wait_for(reader.readline(), 3)
    finally:
        os.close(read_fd)


def test_runtime_can_exit_while_parent_still_has_stdin_open():
    code = """
import asyncio
import sys
from zero_web.stdio import CommandReader
async def main():
    CommandReader(sys.stdin.fileno())
    await asyncio.sleep(0.05)
asyncio.run(main())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=5) == 0, process.stderr.read().decode()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
