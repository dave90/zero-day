"""The custom-tools store (`_customtools`): create/list/modify/delete, the
docstring-as-description convention, name sanitization, syntax rejection, and the
files-root jail."""
import pytest

from zero.tools._customtools import (
    compose_script,
    delete_tool,
    list_tools,
    path_for,
    read_tool,
    safe_tool_name,
    write_tool,
)


@pytest.fixture
def rooted(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    return tmp_path


def test_safe_tool_name_normalizes():
    assert safe_tool_name("JWT Forge!") == "jwt_forge"
    assert safe_tool_name("  weird--name  ") == "weird_name"
    assert safe_tool_name("...") == ""


def test_write_then_read_and_list(rooted):
    p = write_tool("jwt_forge", "Forge a JWT.\nargs: {token}", "print('hi')")
    assert p.exists() and p.name == "jwt_forge.py"
    text = read_tool("jwt_forge")
    # The description becomes the module docstring (the "script self-describes").
    assert text.startswith('"""Forge a JWT.')
    assert "print('hi')" in text
    listed = list_tools()
    assert listed == [{"name": "jwt_forge", "description": "Forge a JWT.\nargs: {token}"}]


def test_modify_overwrites(rooted):
    write_tool("t", "v1", "print(1)")
    write_tool("t", "v2", "print(2)")
    assert list_tools()[0]["description"] == "v2"
    assert "print(2)" in read_tool("t")
    assert len(list_tools()) == 1


def test_delete(rooted):
    write_tool("gone", "d", "pass")
    assert delete_tool("gone") is True
    assert read_tool("gone") is None
    assert delete_tool("gone") is False  # already gone


def test_bad_syntax_rejected_before_write(rooted):
    with pytest.raises(SyntaxError):
        write_tool("broken", "desc", "def (:")
    assert read_tool("broken") is None  # nothing written


def test_empty_name_rejected(rooted):
    with pytest.raises(ValueError):
        path_for("...")


def test_description_cannot_break_out_of_docstring(rooted):
    # A description containing triple-quotes must not produce invalid/escaping code.
    text = compose_script('evil """ + __import__("os")', "pass")
    compile(text, "<t>", "exec")  # still valid
    assert '"""' not in text.split("\n\n")[0].strip('"')


def test_unparseable_file_listed_gracefully(rooted):
    # A hand-dropped non-compiling .py shouldn't crash listing.
    store = rooted / "custom_tools"
    store.mkdir()
    (store / "junk.py").write_text("def (:\n", encoding="utf-8")
    out = list_tools()
    assert out == [{"name": "junk", "description": "(unparseable script — no description)"}]


def test_list_empty_when_no_store(rooted):
    assert list_tools() == []
