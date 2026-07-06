"""custom_tool_call / custom_tool_list run a stored script host-side (the same
path the sandbox-remoted tool takes): args arrive as JSON on stdin, stdout is
captured, exit code maps to ok."""
import pytest

from zero.tools._customtools import write_tool
from zero.tools.custom_tools import (
    CustomToolCallArgs,
    CustomToolCallTool,
    CustomToolListArgs,
    CustomToolListTool,
)


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    return tmp_path


_ECHO = """
import sys, json
args = json.load(sys.stdin)
print("got:", args.get("who", "nobody"))
"""


async def test_call_passes_args_via_stdin(rooted):
    write_tool("echo", "Echo the 'who' arg.", _ECHO)
    r = await CustomToolCallTool().run(CustomToolCallArgs(name="echo", args={"who": "gino"}))
    assert r.ok
    assert "got: gino" in r.output


async def test_call_missing_tool(rooted):
    r = await CustomToolCallTool().run(CustomToolCallArgs(name="nope", args={}))
    assert not r.ok and "no such custom tool" in r.output


async def test_call_nonzero_exit_is_not_ok(rooted):
    write_tool("boom", "Always fails.", "import sys; sys.exit(3)")
    r = await CustomToolCallTool().run(CustomToolCallArgs(name="boom", args={}))
    assert not r.ok
    assert "exited 3" in r.output


async def test_call_times_out(rooted):
    write_tool("slow", "Sleeps.", "import time; time.sleep(5)")
    r = await CustomToolCallTool().run(
        CustomToolCallArgs(name="slow", args={}, timeout_seconds=1)
    )
    assert not r.ok and "timed out" in r.output


async def test_list_shows_descriptions(rooted):
    write_tool("a", "Tool A usage.", "pass")
    write_tool("b", "Tool B usage.", "pass")
    r = await CustomToolListTool().run(CustomToolListArgs())
    assert r.ok
    assert "### a" in r.output and "Tool A usage." in r.output
    assert "### b" in r.output


async def test_list_empty(rooted):
    r = await CustomToolListTool().run(CustomToolListArgs())
    assert r.ok and "no custom tools" in r.output
