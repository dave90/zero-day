"""todo_write / todo_read: per-actor checklist store + tools + render."""
import pytest

from zero.tools.todo import (
    TodoReadArgs,
    TodoReadTool,
    TodoWriteArgs,
    TodoWriteTool,
    format_todo,
    path_for,
    read_todo_items,
)
from zero.ui.events import TodoUpdate
from zero.ui.render import render_event


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    # todo_root() derives from memory_root().parent → keep both under tmp_path.
    monkeypatch.setenv("ZERO_MEMORY_ROOT", str(tmp_path / "memory"))
    return tmp_path


async def test_write_then_read_roundtrip(rooted):
    items = [{"step": "recon", "status": "done"}, {"step": "sqli", "status": "in_progress"}]
    w = await TodoWriteTool().run(TodoWriteArgs(items=items, session_id="gino_2"))
    assert w.ok and "recon" in w.output
    r = await TodoReadTool().run(TodoReadArgs(session_id="gino_2"))
    assert "recon" in r.output and "sqli" in r.output


async def test_write_replaces_not_appends(rooted):
    await TodoWriteTool().run(TodoWriteArgs(items=[{"step": "a"}], session_id="g"))
    await TodoWriteTool().run(TodoWriteArgs(items=[{"step": "b"}], session_id="g"))
    items = read_todo_items("g")
    assert [i["step"] for i in items] == ["b"]  # replaced


async def test_isolation_between_sessions(rooted):
    await TodoWriteTool().run(TodoWriteArgs(items=[{"step": "ga"}], session_id="g"))
    await TodoWriteTool().run(TodoWriteArgs(items=[{"step": "pa"}], session_id="p"))
    assert read_todo_items("g")[0]["step"] == "ga"
    assert read_todo_items("p")[0]["step"] == "pa"


def test_path_stays_inside_root(rooted):
    p = path_for("../../escape")
    assert p.parent == (rooted / "todo").resolve() or p.parent.name == "todo"
    assert p.suffix == ".json"


async def test_no_path_field_only_session_id(rooted):
    assert set(TodoWriteArgs.model_fields) == {"items", "session_id"}
    assert set(TodoReadArgs.model_fields) == {"session_id"}


def test_read_missing_is_empty(rooted):
    assert read_todo_items("never") == []
    assert format_todo([]) == "(no plan yet)"


def test_todo_update_renders_checklist():
    ev = TodoUpdate(items=[
        {"step": "recon", "status": "done"},
        {"step": "sqli", "status": "in_progress"},
        {"step": "report", "status": "pending"},
    ])
    out = "\n".join(render_event(ev))
    assert "plan" in out
    assert "☑ recon" in out and "◐ sqli" in out and "☐ report" in out
