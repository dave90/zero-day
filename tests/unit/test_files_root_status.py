"""Startup announcement: tell the operator whether the files-root folder has data
the agent can read (so it's obvious if the intended folder was actually loaded)."""
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig


def _drain_text(orch) -> str:
    chunks = []
    while not orch.ui_queue.empty():
        ev = orch.ui_queue.get_nowait()
        chunks.append(getattr(ev, "text", ""))
    return "".join(chunks)


async def test_announces_empty_files_root(tmp_path):
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False)
    orch = Orchestrator(cfg)
    await orch._announce_files_root()
    text = _drain_text(orch)
    assert "files root" in text and "EMPTY" in text
    assert str(orch._files_root_abs) in text


async def test_announces_loaded_files_root(tmp_path):
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False)
    orch = Orchestrator(cfg)
    (orch._files_root_abs / "loot.txt").write_text("secret")
    (orch._files_root_abs / "notes.md").write_text("x")
    await orch._announce_files_root()
    text = _drain_text(orch)
    assert "LOADED" in text and "2 file(s)" in text
    assert "loot.txt" in text and "notes.md" in text


async def test_loaded_status_is_audited(tmp_path):
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False)
    orch = Orchestrator(cfg)
    (orch._files_root_abs / "a").write_text("1")
    await orch._announce_files_root()
    orch.audit.close()
    import json
    rows = [json.loads(ln) for ln in (tmp_path / ".pentest" / "audit.jsonl").read_text().splitlines()]
    status = [r for r in rows if r.get("kind") == "files_root_status"]
    assert status and status[-1]["file_count"] == 1
