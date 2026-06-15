"""list_dir: a plain listing of dirs + files, jailed to the files root."""
import pytest

from zero.tools.list_dir import ListDirArgs, ListDirTool


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("x")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / ".env").write_text("SECRET=1")  # hidden, must still show
    return tmp_path


async def test_lists_dirs_first_then_files_including_hidden(rooted):
    r = await ListDirTool().run(ListDirArgs(path="."))
    assert r.ok
    lines = r.output.splitlines()
    assert lines == ["sub/", ".env", "a.txt"]  # dir first (trailing /), then files sorted


async def test_recursive(rooted):
    r = await ListDirTool().run(ListDirArgs(path=".", recursive=True))
    assert r.ok
    assert "sub/" in r.output and "sub/deep.txt" in r.output


async def test_max_entries_truncates(rooted):
    for i in range(10):
        (rooted / f"f{i}.txt").write_text("x")
    r = await ListDirTool().run(ListDirArgs(path=".", max_entries=3))
    assert r.ok and r.truncated and "truncated" in r.output
    assert len([ln for ln in r.output.splitlines() if not ln.startswith("…")]) == 3


async def test_missing_path(rooted):
    r = await ListDirTool().run(ListDirArgs(path="nope"))
    assert r.ok is False and "not found" in r.output


async def test_not_a_directory(rooted):
    r = await ListDirTool().run(ListDirArgs(path="a.txt"))
    assert r.ok is False and "not a directory" in r.output


# --- jail ---

async def test_jail_rejects_parent_traversal(rooted):
    r = await ListDirTool().run(ListDirArgs(path="../../etc"))
    assert r.ok is False and "outside the files root" in r.output


async def test_jail_rejects_absolute_path(rooted):
    r = await ListDirTool().run(ListDirArgs(path="/etc"))
    assert r.ok is False and "outside the files root" in r.output


async def test_jail_rejects_symlink_escape(rooted, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("nope")
    (rooted / "link").symlink_to(outside)
    r = await ListDirTool().run(ListDirArgs(path="link"))
    assert r.ok is False and "outside the files root" in r.output
