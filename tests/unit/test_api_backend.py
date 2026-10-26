"""Provider adapter tests: no external requests, real keys, or tokenizers."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from zero.agents.base import AgentLoop
from zero.cli import main
from zero.llm.api import ApiBackend, ApiConfig, ApiRuntime, resolve_context_window
from zero.llm.backend import BackendError, Message
from zero.llm.envelope import parse_envelope, FinalEnvelope
from zero.llm.llm_trace import set_llm_log_root
from zero.orchestrator.audit import AuditLog, read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig


def response(text='{"type":"final","answer":"done"}', **message):
    return {"id": "test-request", "choices": [{"finish_reason": "stop", "message": {
        "content": text, **message,
    }}], "usage": {"prompt_tokens": 100, "completion_tokens": 9,
                  "prompt_tokens_details": {"cached_tokens": 40}}}


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv("ZERO_TEST_KEY", "fake-unit-key-never-a-real-secret")
    sdk = SimpleNamespace(model_cost={}, acompletion=AsyncMock(return_value=response()))
    monkeypatch.setattr("zero.llm.api._sdk", lambda: sdk)
    return sdk


@pytest.fixture
async def runtime(sdk):
    runtime = ApiRuntime(ApiConfig(key_env="ZERO_TEST_KEY", max_output_tokens=256),
                         "openai/local-test", 16000, trace=False)
    yield runtime
    await runtime.aclose()


async def test_roles_catalog_current_system_and_full_history(runtime, sdk):
    loop = AgentLoop(ApiBackend(runtime), "current instructions", [{
        "name": "probe", "description": "a test probe", "risk_level": "safe",
        "schema": {"type": "object"},
    }])
    loop.add_user_message("remember marker-alpha")
    await loop.step()
    loop.add_observation("probe", True, "observed marker-beta")
    loop.system_prompt = "changed instructions"
    await loop.step()
    calls = sdk.acompletion.call_args_list
    assert calls[0].kwargs["api_key"] == "fake-unit-key-never-a-real-secret"
    assert calls[0].kwargs["num_retries"] == calls[0].kwargs["max_retries"] == 0
    sent = calls[1].kwargs["messages"]
    assert "changed instructions" in sent[0]["content"]
    assert "probe" in sent[0]["content"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert "marker-alpha" in sent[1]["content"]
    assert "[tool observation]" in sent[-1]["content"]
    assert "tools" not in calls[1].kwargs  # no provider-native tools
    assert loop.context_tokens == 100


@pytest.mark.parametrize("envelope", [
    {"type": "action", "tool": "probe", "args": {"payload": "test"}},
    {"type": "actions", "calls": [{"tool": "probe", "args": {}}]},
    {"type": "ask_user", "question": "Continue?"},
    {"type": "final", "answer": "done"},
])
async def test_every_envelope_is_preserved(runtime, sdk, envelope):
    sdk.acompletion.return_value = response(json.dumps(envelope))
    result = await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    assert json.loads(result.text) == envelope
    parse_envelope(result.text)


async def test_forks_isolate_history_and_reasoning(runtime, sdk):
    prototype = ApiBackend(runtime)
    a, b = AgentLoop(prototype, "sys", []), AgentLoop(prototype, "sys", [])
    assert a.backend is not b.backend and a.backend.runtime is b.backend.runtime
    sdk.acompletion.return_value = response(reasoning_content="private continuation")
    a.add_user_message("actor A secret")
    await a.step()
    sdk.acompletion.return_value = response()
    b.add_user_message("actor B only")
    await b.step()
    assert "actor A secret" not in str(sdk.acompletion.call_args.kwargs["messages"])
    assert "private continuation" not in str(sdk.acompletion.call_args.kwargs["messages"])
    a.add_user_message("continue A")
    await a.step()
    assert sdk.acompletion.call_args.kwargs["messages"][2]["reasoning_content"] == "private continuation"


async def test_summary_is_plain_text_even_with_explicit_json(runtime, sdk):
    runtime.config = replace(runtime.config, response_format="json")
    sdk.acompletion.return_value = response("One consolidated answer.")
    result = await ApiBackend(runtime, purpose="summary").run("Summarize", [Message("user", "data")], [])
    assert result.text == "One consolidated answer."
    assert "response_format" not in sdk.acompletion.call_args.kwargs
    assert "Tool catalog" not in sdk.acompletion.call_args.kwargs["messages"][0]["content"]


async def test_multiblock_text_and_cache_usage(runtime, sdk):
    sdk.acompletion.return_value = response([
        {"type": "thinking", "text": "not envelope text"},
        {"type": "text", "text": '{"type":"final",'},
        {"type": "text", "text": '"answer":"done"}'},
    ])
    result = await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    assert isinstance(parse_envelope(result.text), FinalEnvelope)
    assert result.usage == {"input_tokens": 60, "cache_read_input_tokens": 40,
                            "cache_creation_input_tokens": 0, "output_tokens": 9}
    assert result.context_tokens == 100


@pytest.mark.parametrize("patch,match", [
    ({"choices": []}, "no choices"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, "truncated"),
    ({"choices": [{"message": {"content": None, "reasoning_content": "thinking"}}]}, "empty text"),
    ({"choices": [{"message": {"refusal": "refused"}}]}, "refused"),
    ({"choices": [{"message": {"tool_calls": [{"id": "t1"}]}}]}, "native tool"),
])
async def test_bad_outputs_are_not_actions(runtime, sdk, patch, match):
    sdk.acompletion.return_value = {**response(), **patch}
    with pytest.raises(BackendError, match=match):
        await ApiBackend(runtime).run("sys", [Message("user", "go")], [])


class ProviderError(Exception):
    def __init__(self, status, message="request failed"):
        super().__init__(message)
        self.status_code = status
        self.response = SimpleNamespace(headers={"retry-after": "0"})


async def test_retry_budget_and_permanent_auth_failure(runtime, sdk):
    sdk.acompletion.side_effect = [ProviderError(429), ProviderError(503), response()]
    await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    assert sdk.acompletion.await_count == 3
    sdk.acompletion.reset_mock(side_effect=True)
    sdk.acompletion.side_effect = ProviderError(401, "fake-unit-key-never-a-real-secret")
    for _ in range(2):
        with pytest.raises(BackendError) as error:
            await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
        assert "fake-unit-key" not in str(error.value)
    assert sdk.acompletion.await_count == 1  # shared bad credentials never hit provider twice


async def test_json_auto_fallback_is_specific_and_bounded(runtime, sdk):
    runtime.json_supported = True
    sdk.acompletion.side_effect = [ProviderError(400, "response_format json_object not supported"), response()]
    await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    assert sdk.acompletion.call_args_list[0].kwargs["response_format"] == {"type": "json_object"}
    assert "response_format" not in sdk.acompletion.call_args_list[1].kwargs
    assert runtime.json_supported is False
    sdk.acompletion.reset_mock(side_effect=True)
    sdk.acompletion.side_effect = ProviderError(400, "bad model name")
    with pytest.raises(BackendError):
        await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    assert sdk.acompletion.await_count == 1


async def test_all_actor_forks_share_concurrency_limit(runtime, sdk):
    runtime.semaphore = asyncio.Semaphore(2)
    active = peak = 0

    async def slow(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            return response()
        finally:
            active -= 1

    sdk.acompletion.side_effect = slow
    await asyncio.gather(*(ApiBackend(runtime).fork().run("sys", [Message("user", "go")], [])
                           for _ in range(6)))
    assert peak == 2 and active == 0


async def test_cancellation_releases_permit_and_timeout_includes_queue(runtime, sdk):
    entered = asyncio.Event()

    async def hanging(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    sdk.acompletion.side_effect = hanging
    task = asyncio.create_task(ApiBackend(runtime).run("sys", [Message("user", "go")], []))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.semaphore._value == runtime.config.max_concurrency
    runtime.timeout = 0.01
    await runtime.semaphore.acquire()
    runtime.semaphore = asyncio.Semaphore(0)
    with pytest.raises(BackendError, match="timed out"):
        await ApiBackend(runtime).run("sys", [Message("user", "go")], [])


async def test_compression_preserves_original_history_and_is_actor_local(runtime, sdk):
    runtime.context_window = 9000
    runtime.semaphore = asyncio.Semaphore(1)  # compression must not acquire recursively
    history = [Message("user", "exact engagement constraint /evidence/abc")]
    for i in range(8):
        history += [Message("user", f"old-{i}:" + "x" * 900), Message("assistant", "noted")]
    history += [Message("user", "latest exact payload")]
    original = list(history)

    async def answer(**kwargs):
        if kwargs["messages"][0]["content"].startswith("Summarize earlier"):
            return response("Earlier evidence: /evidence/abc; work remains pending.")
        return response()

    sdk.acompletion.side_effect = answer
    backend = ApiBackend(runtime)
    await backend.run("system", history, [])
    request = sdk.acompletion.call_args.kwargs["messages"]
    assert history == original
    assert "/evidence/abc" in request[1]["content"]
    assert "latest exact payload" in request[-1]["content"]
    assert "Earlier conversation summary" in request[2]["content"]
    assert backend.fork()._summary == ""
    old_calls = sdk.acompletion.await_count
    await backend.run("changed system", history, [])
    assert sdk.acompletion.await_count == old_calls + 1  # covered prefix not compressed again


async def test_context_overflow_has_only_one_repair_and_oversized_input_fails(runtime, sdk):
    class ContextWindowExceededError(Exception):
        pass

    sdk.acompletion.side_effect = ContextWindowExceededError("context too long")
    history = [Message("user", "first marker"), Message("assistant", "ack"),
               Message("user", "middle"), Message("assistant", "ack"),
               Message("user", "recent"), Message("assistant", "ack"),
               Message("user", "latest")]
    # Compression itself cannot complete when the provider is consistently broken;
    # its exception must be classified rather than escaping into outer turn retries.
    with pytest.raises(BackendError, match="context"):
        await ApiBackend(runtime).run("sys", history, [])
    assert sdk.acompletion.await_count <= 3
    sdk.acompletion.reset_mock(side_effect=True)
    with pytest.raises(BackendError, match="context"):
        await ApiBackend(runtime).run("x" * 20000, [Message("user", "go")], [])
    assert sdk.acompletion.await_count == 0


async def test_context_overflow_can_recover_with_one_summary(runtime, sdk):
    class ContextWindowExceededError(Exception):
        pass

    sdk.acompletion.side_effect = [ContextWindowExceededError(), response("earlier marker"), response()]
    history = [Message("user", "first"), Message("assistant", "ack"),
               Message("user", "middle"), Message("assistant", "ack"),
               Message("user", "recent"), Message("assistant", "ack"), Message("user", "latest")]
    result = await ApiBackend(runtime).run("sys", history, [])
    assert isinstance(parse_envelope(result.text), FinalEnvelope)
    assert sdk.acompletion.await_count == 3
    assert "Summarize earlier" in sdk.acompletion.call_args_list[1].kwargs["messages"][0]["content"]


async def test_cancellation_during_backoff_does_not_hold_permit(runtime, sdk, monkeypatch):
    backing_off = asyncio.Event()

    async def backoff(delay):
        backing_off.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("zero.llm.api.asyncio.sleep", backoff)
    sdk.acompletion.side_effect = ProviderError(429)
    task = asyncio.create_task(ApiBackend(runtime).run("sys", [Message("user", "go")], []))
    await backing_off.wait()
    assert runtime.semaphore._value == runtime.config.max_concurrency
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sdk.acompletion.await_count == 1


async def test_permanent_backend_error_stops_ralph_driver_after_one_attempt(sdk, tmp_path):
    orch = Orchestrator(OrchestratorConfig(
        cwd=tmp_path, backend="api", model="openai/local-test", context_window=16000,
        api=ApiConfig(key_env="ZERO_TEST_KEY", max_output_tokens=256), use_container=False,
    ))
    sdk.acompletion.side_effect = ProviderError(401)
    loop = AgentLoop(orch.backend, "sys", [], max_turns=20)
    loop.add_user_message("go")
    try:
        result = await orch._drive_agent_to_final(loop, agent_label="analyzer", session_id="analyzer")
        assert "backend errors" in result
        assert sdk.acompletion.await_count == 1 and loop.turn == 1
    finally:
        await orch._api_runtime.aclose()
        orch.audit.close()
        orch.thinking.close()


async def test_traces_redact_credentials_even_with_quotes(runtime, sdk, monkeypatch, tmp_path):
    key = 'fake-unit-key-"with-quotes"'
    monkeypatch.setenv("ZERO_TEST_KEY", key)
    configured = ApiRuntime(runtime.config, runtime.model, runtime.context_window, trace=True)
    configured.audit = AuditLog(tmp_path / "audit.jsonl")
    set_llm_log_root(tmp_path / "llm")
    sdk.acompletion.return_value = response(json.dumps({"type": "final", "answer": key}))
    try:
        result = await ApiBackend(configured).run("sys " + key, [Message("user", "go")], [])
        assert key not in result.text
        assert configured._api_key == key
    finally:
        await configured.aclose()
        configured.audit.close()
        set_llm_log_root(None)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_text()
            assert key not in content and "fake-unit-key-" not in content
    assert read_audit(tmp_path / "audit.jsonl")[0]["kind"] == "api_call"


def test_unknown_models_need_explicit_context_and_glm_has_offline_fallback(sdk):
    with pytest.raises(ValueError, match="context"):
        resolve_context_window("openai/new-model", None)
    assert resolve_context_window("openai/new-model", 32000) == 32000
    assert resolve_context_window("openrouter/z-ai/glm-5.3", None) == 1_000_000


def test_missing_explicit_key_never_falls_back(sdk, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "other-key")
    with pytest.raises(ValueError, match="MISSING_TEST_KEY"):
        ApiRuntime(ApiConfig(key_env="MISSING_TEST_KEY"), "openai/new-model", 32000)


@pytest.mark.parametrize("args,match", [
    (["--backend", "api"], "requires an explicit"),
    (["--backend", "api", "--model", ""], "requires an explicit"),
    (["--api-retries", "0"], "require --backend api"),
    (["--backend", "api", "--model", "openai/local-test", "--execution-mode", "b"], "mode b"),
])
def test_cli_invalid_configs_fail_before_runtime(args, match, sdk, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["run", *args])
    assert result.exit_code == 2 and match in result.output
    assert not (tmp_path / ".pentest").exists()


def test_workflow_factories_share_only_runtime(sdk, tmp_path):
    cfg = OrchestratorConfig(cwd=tmp_path, backend="api", model="openai/local-test",
                             context_window=16000, api=ApiConfig(key_env="ZERO_TEST_KEY",
                             max_output_tokens=256), use_container=False)
    orch = Orchestrator(cfg)
    try:
        a, b = AgentLoop(orch.backend, "A", []), AgentLoop(orch.subagent_backend, "B", [])
        assert a.backend is not b.backend
        assert a.backend.runtime is b.backend.runtime is orch._api_runtime
        assert orch._make_session("ralph-analyzer") is None
        assert orch._build_backend(purpose="summary").purpose == "summary"
    finally:
        orch.audit.close()
        orch.thinking.close()
