"""Offline test for the Ralph per-iteration backup + selective reset."""
from __future__ import annotations

from zero.ralph import state


def _seed_pentest(cwd):
    """Build a realistic .pentest/ tree + a custom tool in zero_files/."""
    pentest = cwd / ".pentest"
    (pentest / "findings" / "confirmed").mkdir(parents=True)
    (pentest / "findings" / "confirmed" / "abc123.md").write_text("# a finding", encoding="utf-8")
    (pentest / "findings" / "index.json").write_text("{}", encoding="utf-8")
    (pentest / "memory").mkdir()
    (pentest / "memory" / "orchestrator.md").write_text("scratch", encoding="utf-8")
    (pentest / "llm").mkdir()
    (pentest / "llm" / "orchestrator.log").write_text("io", encoding="utf-8")
    (pentest / "audit.jsonl").write_text('{"kind":"x"}\n', encoding="utf-8")
    (pentest / "thinking.log").write_text("thoughts", encoding="utf-8")
    # Custom tools live OUTSIDE .pentest/ (in the files-root jail).
    ct = cwd / "zero_files" / "custom_tools"
    ct.mkdir(parents=True)
    (ct / "my_tool.py").write_text('"""a tool."""\n', encoding="utf-8")


def test_backup_and_reset_carries_findings_and_preserves_custom_tools(tmp_path):
    _seed_pentest(tmp_path)

    dest = state.backup_and_reset(tmp_path, 3)

    # 1. A full snapshot was taken under .ralph_logs/.pentest_3.
    assert dest == tmp_path / ".ralph_logs" / ".pentest_3"
    assert (dest / "memory" / "orchestrator.md").read_text(encoding="utf-8") == "scratch"
    assert (dest / "findings" / "confirmed" / "abc123.md").exists()

    pentest = tmp_path / ".pentest"
    # 2. findings/ + the master logs are CARRIED FORWARD (not wiped).
    assert (pentest / "findings" / "confirmed" / "abc123.md").exists()
    assert (pentest / "findings" / "index.json").exists()
    assert (pentest / "audit.jsonl").exists()
    assert (pentest / "thinking.log").exists()
    # 3. Volatile working state is wiped fresh, but memory/ AND llm/ are recreated
    #    empty so writers that assume their dir exists (memory tools, the per-actor
    #    LLM trace) don't crash on the next iteration. (Regression: a wiped llm/ made
    #    the trace FileHandler FileNotFoundError and abort iterations 2+.)
    for sub in ("memory", "llm"):
        assert (pentest / sub).is_dir(), f"{sub}/ should be recreated"
        assert list((pentest / sub).iterdir()) == [], f"{sub}/ should be empty"
    # The old llm log content was snapshotted, not carried forward.
    assert (dest / "llm" / "orchestrator.log").exists()
    # 4. Custom tools (outside .pentest/) are untouched.
    assert (tmp_path / "zero_files" / "custom_tools" / "my_tool.py").exists()


def test_backup_and_reset_missing_pentest_is_noop(tmp_path):
    # No .pentest/ yet — must not raise and should leave an empty .pentest/ behind.
    dest = state.backup_and_reset(tmp_path, 1)
    assert dest == tmp_path / ".ralph_logs" / ".pentest_1"
    assert (tmp_path / ".pentest").is_dir()
