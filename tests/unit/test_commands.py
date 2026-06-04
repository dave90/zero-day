from pathlib import Path

from zero.ui.commands import CommandRegistry, build_default_registry


def _reg(findings_dir: Path = Path("/tmp"), **overrides):
    return build_default_registry(
        findings_dir=findings_dir,
        clear_scrollback=overrides.get("clear_scrollback", lambda: None),
        request_exit=overrides.get("request_exit", lambda: None),
    )


def test_dispatch_non_command_returns_false():
    assert _reg().dispatch("hello") == (False, None)


def test_unknown_slash_reports_error():
    was_cmd, out = _reg().dispatch("/bogus")
    assert was_cmd is True
    assert isinstance(out, str) and "unknown" in out


def test_help_lists_commands():
    _, out = _reg().dispatch("/help")
    assert isinstance(out, list)
    text = "\n".join(out)
    for name in ("/help", "/exit", "/clear", "/scope", "/findings"):
        assert name in text


def test_clear_runs_callback():
    cleared = {"n": 0}
    reg = _reg(clear_scrollback=lambda: cleared.update(n=cleared["n"] + 1))
    reg.dispatch("/clear")
    assert cleared["n"] == 1


def test_exit_calls_request_exit():
    exited = {"n": 0}
    reg = _reg(request_exit=lambda: exited.update(n=exited["n"] + 1))
    was_cmd, out = reg.dispatch("/exit")
    assert was_cmd is True
    assert out is None
    assert exited["n"] == 1


def test_quit_alias_also_exits():
    exited = {"n": 0}
    reg = _reg(request_exit=lambda: exited.update(n=exited["n"] + 1))
    reg.dispatch("/quit")
    assert exited["n"] == 1


def test_findings_handles_missing_dir(tmp_path: Path):
    _, out = _reg(findings_dir=tmp_path / "missing").dispatch("/findings")
    assert isinstance(out, list)


def test_findings_lists_files(tmp_path: Path):
    confirmed = tmp_path / "confirmed"
    confirmed.mkdir()
    (confirmed / "f1.md").write_text("x", encoding="utf-8")
    _, out = _reg(findings_dir=tmp_path).dispatch("/findings")
    assert any("f1.md" in line for line in out)
