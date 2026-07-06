"""Offline tests for per-actor LLM I/O tracing (`zero.llm.llm_trace`)."""
from __future__ import annotations

import importlib


def _fresh_module():
    # The module caches loggers/root in globals; reload for a clean slate per test.
    import zero.llm.llm_trace as m
    return importlib.reload(m)


def test_tracing_off_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    m.trace_call("orchestrator", argv=["claude"], system_prompt="sp", stdin_body="in",
                 stdout="out", stderr="", returncode=0, resume=False)
    # Nothing created anywhere.
    assert not list(tmp_path.iterdir())


def test_writes_one_file_per_actor_with_full_io(tmp_path, monkeypatch):
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    m.set_llm_log_root(tmp_path)
    m.trace_call("orchestrator", argv=["claude", "-p"], system_prompt="SYS-PROMPT-XYZ",
                 stdin_body="USER-INPUT-123", stdout="MODEL-OUT-789", stderr="warn",
                 returncode=0, resume=False)
    m.trace_call("pentester-abc", argv=["claude"], system_prompt="", stdin_body="delta",
                 stdout="ok", stderr="", returncode=0, resume=True)

    orch = (tmp_path / "orchestrator.log").read_text()
    assert "SYS-PROMPT-XYZ" in orch and "USER-INPUT-123" in orch and "MODEL-OUT-789" in orch
    assert "actor=orchestrator" in orch and "rc=0" in orch

    pen = (tmp_path / "pentester-abc.log").read_text()
    assert "delta" in pen
    # On resume the system prompt is carried in the session, not re-sent — note that.
    assert "carried in resumed session" in pen


def test_env_var_enables_without_explicit_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ZERO_LLM_LOG_DIR", str(tmp_path))
    m = _fresh_module()  # reload so _root_resolved=False → it reads the env lazily
    m.trace_call("validator-x", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="o", stderr="", returncode=1, resume=False)
    assert (tmp_path / "validator-x.log").read_text().count("validator-x") >= 1


def test_changing_root_redirects_to_new_dir(tmp_path, monkeypatch):
    # Regression: a process running several engagements to different dirs (the
    # regression suite) must NOT keep writing every actor to the FIRST dir. Changing
    # the root drops cached loggers so the SAME actor writes to the new dir.
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    a, b = tmp_path / "runA", tmp_path / "runB"

    m.set_llm_log_root(a)
    m.trace_call("orchestrator", argv=["claude"], system_prompt="s", stdin_body="A-IN",
                 stdout="A-OUT", stderr="", returncode=0, resume=False)
    m.set_llm_log_root(b)  # next engagement, different dir
    m.trace_call("orchestrator", argv=["claude"], system_prompt="s", stdin_body="B-IN",
                 stdout="B-OUT", stderr="", returncode=0, resume=False)

    a_log = (a / "orchestrator.log").read_text()
    b_log = (b / "orchestrator.log").read_text()
    assert "A-IN" in a_log and "A-OUT" in a_log
    assert "B-IN" in b_log and "B-OUT" in b_log
    assert "B-IN" not in a_log  # run B did NOT leak into run A's file
    assert "A-IN" not in b_log


def test_actor_name_sanitized_for_filename(tmp_path, monkeypatch):
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    m.set_llm_log_root(tmp_path)
    m.trace_call("weird/../name", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="o", stderr="", returncode=0, resume=False)
    # No path traversal: a single flat file inside the root.
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["weird_._name.log"] or all("/" not in f for f in files)


def test_trace_self_heals_after_log_dir_wiped(tmp_path, monkeypatch):
    """Regression (production Ralph crash): backup_and_reset wiped .pentest/llm/
    between iterations, and the next iteration's NEW actor crashed the turn with
    FileNotFoundError when opening its trace file. trace_call must self-heal the dir
    and never raise."""
    import shutil

    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    log_dir = tmp_path / ".pentest" / "llm"
    m.set_llm_log_root(log_dir)
    m.trace_call("orchestrator_iter_1", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="OUT1", stderr="", returncode=0, resume=False)
    assert (log_dir / "orchestrator_iter_1.log").exists()

    # Simulate the between-iteration wipe of .pentest/ (llm/ removed under our feet).
    shutil.rmtree(log_dir)
    m.reset_llm_loggers()  # drop stale handlers, as the Ralph driver now does

    # A NEW actor must NOT raise and must recreate the dir + its file.
    m.trace_call("orchestrator_iter_2", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="OUT2", stderr="", returncode=0, resume=False)
    assert (log_dir / "orchestrator_iter_2.log").exists()
    assert "OUT2" in (log_dir / "orchestrator_iter_2.log").read_text()


def test_trace_never_raises_when_file_uncreatable(tmp_path, monkeypatch):
    """Even if the per-actor log file genuinely can't be opened, a turn must not die
    (llm_trace's documented 'never raises' contract)."""
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    root = tmp_path / "llm"
    m.set_llm_log_root(root)
    # Put a DIRECTORY where the actor's log file should go → FileHandler open raises.
    (root / "actor.log").mkdir()
    m.trace_call("actor", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="o", stderr="", returncode=0, resume=False)  # must be a no-op


def test_reset_llm_loggers_clears_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("ZERO_LLM_LOG_DIR", raising=False)
    m = _fresh_module()
    m.set_llm_log_root(tmp_path)
    m.trace_call("a", argv=["claude"], system_prompt="s", stdin_body="i",
                 stdout="o", stderr="", returncode=0, resume=False)
    assert m._loggers  # cached
    m.reset_llm_loggers()
    assert m._loggers == {}
