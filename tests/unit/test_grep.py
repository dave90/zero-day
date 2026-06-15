"""grep: regex search over the files root, jailed, with binary/large skipping."""
import pytest

from zero.tools.grep import GrepTool, GrepArgs


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    (tmp_path / "app.py").write_text("import os\nSECRET = 'abc'\nprint(SECRET)\n")
    (tmp_path / "notes.txt").write_text("nothing here\nTODO: fix\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "more.py").write_text("password = 'hunter2'\n")
    return tmp_path


async def test_literal_match_recursive(rooted):
    r = await GrepTool().run(GrepArgs(pattern="SECRET"))
    assert r.ok
    assert "app.py:2: SECRET = 'abc'" in r.output
    assert "app.py:3: print(SECRET)" in r.output


async def test_regex_match(rooted):
    r = await GrepTool().run(GrepArgs(pattern=r"password\s*=\s*'\w+'"))
    assert r.ok and "sub/more.py:1: password = 'hunter2'" in r.output


async def test_ignore_case(rooted):
    r = await GrepTool().run(GrepArgs(pattern="todo", ignore_case=True))
    assert r.ok and "notes.txt:2: TODO: fix" in r.output


async def test_include_glob(rooted):
    r = await GrepTool().run(GrepArgs(pattern=".", include="*.py"))
    assert r.ok
    assert "app.py" in r.output and "more.py" in r.output
    assert "notes.txt" not in r.output


async def test_no_matches(rooted):
    r = await GrepTool().run(GrepArgs(pattern="zzz-not-present"))
    assert r.ok and "(no matches)" in r.output


async def test_invalid_regex(rooted):
    r = await GrepTool().run(GrepArgs(pattern="("))
    assert r.ok is False and "invalid regex" in r.output


async def test_binary_file_skipped(rooted):
    (rooted / "blob.bin").write_bytes(b"PASSWORD\x00\x00secret")
    r = await GrepTool().run(GrepArgs(pattern="PASSWORD"))
    # The literal also doesn't appear in text files → no matches, and the binary
    # file is noted as skipped rather than matched.
    assert r.ok and "blob.bin" not in r.output
    assert "skipped" in r.output and "binary" in r.output


async def test_single_file_path(rooted):
    r = await GrepTool().run(GrepArgs(pattern="import", path="app.py"))
    assert r.ok and "app.py:1: import os" in r.output


async def test_context_lines(rooted):
    r = await GrepTool().run(GrepArgs(pattern="SECRET = ", path="app.py", context=1))
    # context shows the surrounding lines with '-' separators
    assert "app.py:1- import os" in r.output
    assert "app.py:2: SECRET = 'abc'" in r.output


# --- jail ---

async def test_jail_rejects_parent_traversal(rooted):
    r = await GrepTool().run(GrepArgs(pattern="root", path="../../etc"))
    assert r.ok is False and "outside the files root" in r.output


async def test_jail_rejects_absolute_path(rooted):
    r = await GrepTool().run(GrepArgs(pattern="root", path="/etc"))
    assert r.ok is False and "outside the files root" in r.output
