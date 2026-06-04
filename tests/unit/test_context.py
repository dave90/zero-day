from pathlib import Path

from zero.orchestrator.context import (
    load_pentest_md,
    project_context_for,
    wrap_for_prompt,
)


def test_absent_returns_none(tmp_path: Path):
    assert load_pentest_md(tmp_path) is None
    assert project_context_for(tmp_path) is None


def test_present_returns_contents(tmp_path: Path):
    (tmp_path / "pentest.md").write_text("scope: localhost\n", encoding="utf-8")
    assert load_pentest_md(tmp_path) == "scope: localhost\n"


def test_wraps_with_delimiter():
    out = wrap_for_prompt("hello")
    assert out.startswith("<project_context")
    assert out.endswith("</project_context>")
    assert "hello" in out


def test_project_context_for_wraps(tmp_path: Path):
    (tmp_path / "pentest.md").write_text("x\n", encoding="utf-8")
    ctx = project_context_for(tmp_path)
    assert ctx is not None
    assert "x" in ctx and "<project_context" in ctx
