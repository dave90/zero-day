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


def test_wrap_is_plain_markdown_no_xml_tag():
    # The brief is surfaced as plain markdown (under the caller's `## Project context`
    # heading) — NOT wrapped in an XML-ish <project_context> tag.
    out = wrap_for_prompt("  hello\n")
    assert out == "hello"
    assert "<project_context" not in out


def test_project_context_for_is_markdown(tmp_path: Path):
    (tmp_path / "pentest.md").write_text("x\n", encoding="utf-8")
    ctx = project_context_for(tmp_path)
    assert ctx is not None
    assert "x" in ctx and "<project_context" not in ctx
