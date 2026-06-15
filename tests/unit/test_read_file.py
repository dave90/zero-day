"""read_file: read content, page with offset/max_bytes, jailed to the files root."""
import pytest

from zero.tools.read_file import ReadFileArgs, ReadFileTool


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    (tmp_path / "a.txt").write_text("hello world")
    (tmp_path / "sub").mkdir()
    return tmp_path


async def test_reads_content(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="a.txt"))
    assert r.ok and "hello world" in r.output and not r.truncated


async def test_max_bytes_truncates(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="a.txt", max_bytes=5))
    assert r.ok and r.truncated
    assert r.output.startswith("hello")
    assert "truncated" in r.output


async def test_offset_paging(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="a.txt", offset=6))
    assert r.ok and r.output.startswith("world") and not r.truncated


async def test_missing_file(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="nope.txt"))
    assert r.ok is False and "not found" in r.output


async def test_directory_is_not_a_file(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="sub"))
    assert r.ok is False and "is a directory" in r.output


# --- jail ---

async def test_jail_rejects_parent_traversal(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="../../etc/passwd"))
    assert r.ok is False and "outside the files root" in r.output


async def test_jail_rejects_absolute_path(rooted):
    r = await ReadFileTool().run(ReadFileArgs(path="/etc/passwd"))
    assert r.ok is False and "outside the files root" in r.output


async def test_jail_rejects_symlink_escape(rooted, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("nope")
    (rooted / "link.txt").symlink_to(outside / "secret.txt")
    r = await ReadFileTool().run(ReadFileArgs(path="link.txt"))
    assert r.ok is False and "outside the files root" in r.output
