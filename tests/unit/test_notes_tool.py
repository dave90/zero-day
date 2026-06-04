from pathlib import Path

from zero.tools.notes import NotesArgs, NotesTool


async def test_append_then_read(tmp_path: Path):
    tool = NotesTool(path=tmp_path / "n.md")
    r = await tool.run(NotesArgs(action="append", text="first"))
    assert r.ok
    r = await tool.run(NotesArgs(action="append", text="second"))
    assert r.ok
    r = await tool.run(NotesArgs(action="read"))
    assert r.ok
    assert r.output == "first\nsecond\n"


async def test_read_when_empty(tmp_path: Path):
    tool = NotesTool(path=tmp_path / "n.md")
    r = await tool.run(NotesArgs(action="read"))
    assert r.ok and r.output == ""


async def test_append_requires_text(tmp_path: Path):
    tool = NotesTool(path=tmp_path / "n.md")
    r = await tool.run(NotesArgs(action="append", text=None))
    assert r.ok is False
