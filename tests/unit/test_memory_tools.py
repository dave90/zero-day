"""memory_read / memory_write tools resolve purely from the injected session_id."""
import pytest

from zero.tools._memroot import path_for
from zero.tools.memory import (
    MemoryReadArgs,
    MemoryReadTool,
    MemoryWriteArgs,
    MemoryWriteTool,
)


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_MEMORY_ROOT", str(tmp_path))
    return tmp_path


async def test_write_then_read_via_tools(rooted):
    w = await MemoryWriteTool().run(MemoryWriteArgs(content="notes here", session_id="gino_2"))
    assert w.ok and "memory saved" in w.output
    r = await MemoryReadTool().run(MemoryReadArgs(session_id="gino_2"))
    assert r.ok and r.output == "notes here"


async def test_read_empty_is_ok(rooted):
    r = await MemoryReadTool().run(MemoryReadArgs(session_id="fresh"))
    assert r.ok and "empty" in r.output


async def test_no_path_field_only_session_id(rooted):
    # The agent cannot name another agent's file — Args has only content/session_id.
    assert set(MemoryWriteArgs.model_fields) == {"content", "session_id"}
    assert set(MemoryReadArgs.model_fields) == {"session_id"}


async def test_session_id_is_a_declared_arg_field(rooted):
    # Guards the load-bearing fact that _invoke_tool's `tool.Args(**args)` keeps the
    # injected session_id (pydantic drops unknown keys). If this regresses, every
    # agent would clobber one shared default file.
    args = MemoryWriteArgs(**{"content": "x", "session_id": "gino_2"})
    assert args.session_id == "gino_2"
    await MemoryWriteTool().run(args)
    assert path_for("gino_2").exists()
    assert not path_for("_default").exists()


async def test_isolation_across_actors(rooted):
    await MemoryWriteTool().run(MemoryWriteArgs(content="A", session_id="gino_2"))
    await MemoryWriteTool().run(MemoryWriteArgs(content="B", session_id="orchestrator"))
    a = await MemoryReadTool().run(MemoryReadArgs(session_id="gino_2"))
    o = await MemoryReadTool().run(MemoryReadArgs(session_id="orchestrator"))
    assert a.output == "A" and o.output == "B"
