"""Explicitly opt-in, token-spending API smoke/workflow tests.

ZERO_TEST_API_MODEL=openrouter/z-ai/glm-5.3 uv run pytest -m live_api tests/e2e/test_live_api.py
Keys use the normal provider environment; no coding CLI or gateway is required.
"""
import asyncio
import os

import pytest

from zero.agents.base import AgentLoop
from zero.llm.api import ApiBackend, ApiConfig, ApiRuntime, resolve_context_window
from zero.llm.envelope import FinalEnvelope
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ui.events import ApprovalPrompt, AskUser, SessionSummary, AgentTextDelta
from zero.orchestrator.approval import Decision
from zero.orchestrator.audit import read_audit
from zero.tools.read_file import ReadFileTool, ReadFileArgs

pytestmark = [pytest.mark.live, pytest.mark.live_api]


@pytest.fixture
def provider_config():
    model = os.environ.get("ZERO_TEST_API_MODEL")
    if not model:
        pytest.skip("set ZERO_TEST_API_MODEL and provider credentials to spend API credits")
    config = ApiConfig(base_url=os.environ.get("ZERO_TEST_API_BASE_URL"),
                       key_env=os.environ.get("ZERO_TEST_API_KEY_ENV"))
    override = os.environ.get("ZERO_TEST_API_CONTEXT_WINDOW")
    window = resolve_context_window(model, int(override) if override else None)
    try:
        ApiRuntime(config, model, window, trace=False)
    except ValueError as exc:
        if "credential" in str(exc).lower():
            pytest.skip(str(exc))
        raise
    return model, config, window


async def test_live_api_envelope_recall_and_real_file_observation(provider_config, tmp_path, monkeypatch):
    model, config, window = provider_config
    runtime = ApiRuntime(config, model, window, trace=False, timeout=180)
    monkeypatch.setenv("ZERO_FILES_ROOT", str(tmp_path))
    (tmp_path / "marker.txt").write_text("live-fixture-beta")
    loop = AgentLoop(ApiBackend(runtime), "Follow the operator's requested JSON envelope format.", [])
    try:
        loop.add_user_message('Remember marker live-fixture-alpha. Reply {"type":"final","answer":"ack"}.')
        assert isinstance((await loop.step()).envelope, FinalEnvelope)
        tool = await ReadFileTool().run(ReadFileArgs(path="marker.txt"))
        assert tool.ok
        loop.add_observation("read_file", tool.ok, tool.output)
        loop.add_user_message("Return a final envelope whose answer contains both exact markers.")
        result = await loop.step()
        assert isinstance(result.envelope, FinalEnvelope)
        assert "live-fixture-alpha" in result.envelope.answer
        assert "live-fixture-beta" in result.envelope.answer
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("workflow", ["orchestrated", "ralph", "swarm"])
async def test_live_api_workflow(provider_config, tmp_path, monkeypatch, workflow):
    import zero.ui.app

    model, api, window = provider_config
    goal = (
        "Run a bounded workflow smoke test on supplied local files only. "
        "Read fixture.txt with read_file. Dispatch one focused Pentester to inspect it, "
        "await its result, then dispatch one independent Validator to assess the synthetic "
        "claim that fixture.txt contains smoke-marker. A rejected/pending result is acceptable: "
        "this is a fixture, not a vulnerability claim. Finish with a report. "
        "Do not contact any external target. In swarm mode negotiate distinct ownership "
        "before testing and share the marker as evidence. In Ralph mode plan a single task "
        "with this workflow as its acceptance criteria and complete one bounded pass."
    )
    cfg = OrchestratorConfig(cwd=tmp_path, backend="api", model=model, api=api,
                             context_window=window, goal=goal, use_container=False,
                             yolo=True, max_turns=14, pentester_max_turns=4, validator_max_turns=4,
                             max_total_subagents=4, llm_timeout_seconds=180,
                             ralph=workflow == "ralph", ralph_iterations=1,
                             ralph_approve_prd=False, ralph_epics=False, ralph_discussion_rounds=1,
                             swarm_size=2 if workflow == "swarm" else 1)
    orch = Orchestrator(cfg)
    (orch._files_root_abs / "fixture.txt").write_text("smoke-marker")

    async def ui(o):
        while True:
            event = await o.ui_queue.get()
            if isinstance(event, ApprovalPrompt):
                o.submit_approval(event.call_id, Decision.NO)  # read_file never needs approval
            elif isinstance(event, AskUser):
                o.submit_ask_user_answer(event.ask_id, "Use only the supplied file; proceed.")
            elif isinstance(event, SessionSummary):
                if not cfg.ralph or event.report_path.endswith("ralph_report.md"):
                    return
            elif isinstance(event, AgentTextDelta) and "Giving up this line" in event.text:
                pytest.fail("live API workflow stopped on backend errors")

    monkeypatch.setattr(zero.ui.app, "run_ui", ui)
    await asyncio.wait_for(orch.run_interactive(), timeout=900)
    events = read_audit(orch.audit.path)
    assert any(e["kind"] == "api_call" for e in events)
    assert any(e["kind"] == "dispatch_pentester" for e in events)
    assert any(e["kind"] in {"dispatch_validator", "validation_started"} for e in events)
    if workflow == "ralph":
        assert any(e["kind"] == "ralph_done" for e in events)
    if workflow == "swarm":
        assert not any(e["kind"] == "swarm_summarizer_error" for e in events)
    assert orch._api_runtime._closed
