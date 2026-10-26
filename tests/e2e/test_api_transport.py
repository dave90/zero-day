"""Real SDK, local provider: no credits or API account required."""
import pytest

from zero.agents.base import AgentLoop
from zero.llm.api import ApiBackend, ApiConfig, ApiRuntime
from zero.llm.backend import BackendError, Message


def runtime_for(server, **options):
    return ApiRuntime(ApiConfig(base_url=server.base_url, key_env="ZERO_TEST_API_KEY",
                                max_output_tokens=256, **options),
                      "openai/zero-loopback", 64000, trace=False, timeout=10)


async def test_real_sdk_sends_correct_http_and_full_history(api_server):
    runtime = runtime_for(api_server)
    loop = AgentLoop(ApiBackend(runtime), "Return JSON", [])
    try:
        loop.add_user_message("remember marker-123")
        await loop.step()
        loop.add_observation("probe", True, "marker-456")
        await loop.step()
    finally:
        await runtime.aclose()
    first, second = api_server.requests
    assert first["path"] == "/v1/chat/completions"
    assert first["headers"]["authorization"] == "Bearer fake-loopback-provider-key"
    assert first["body"]["model"] == "zero-loopback"
    assert first["body"]["max_tokens"] == 256
    assert [m["role"] for m in second["body"]["messages"]] == ["system", "user", "assistant", "user"]
    assert "marker-123" in second["body"]["messages"][1]["content"]
    assert "marker-456" in second["body"]["messages"][-1]["content"]
    assert runtime._session.closed


async def test_native_anthropic_sdk_conversion_uses_messages_endpoint(api_server):
    runtime = ApiRuntime(ApiConfig(base_url=api_server.base_url.removesuffix("/v1"),
                                   key_env="ZERO_TEST_API_KEY",
                                   max_output_tokens=256),
                         "anthropic/claude-test-model", 64000, trace=False, timeout=10)
    try:
        result = await ApiBackend(runtime).run("system instructions", [Message("user", "hello")], [])
    finally:
        await runtime.aclose()
    request = api_server.requests[0]
    assert request["path"] == "/v1/messages"
    assert request["headers"].get("x-api-key") == "fake-loopback-provider-key"
    assert request["body"]["messages"][0]["role"] == "user"
    assert "system instructions" in str(request["body"]["system"])
    assert result.usage["input_tokens"] == 100


async def test_real_openrouter_adapter_passes_glm_model_id_and_custom_endpoint(api_server):
    runtime = ApiRuntime(ApiConfig(base_url=api_server.base_url, key_env="ZERO_TEST_API_KEY",
                                   max_output_tokens=256),
                         "openrouter/z-ai/glm-5.3", 64000, trace=False, timeout=10)
    try:
        await ApiBackend(runtime).run("Return JSON", [Message("user", "hello")], [])
    finally:
        await runtime.aclose()
    request = api_server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["body"]["model"] == "z-ai/glm-5.3"
    assert request["headers"]["authorization"] == "Bearer fake-loopback-provider-key"


async def test_http_retries_are_not_multiplied_by_sdk(api_server):
    def respond(body):
        if len(api_server.requests) < 3:
            return 429, {"error": {"message": "test rate limit", "type": "rate_limit_error"}}
        return '{"type":"final","answer":"recovered"}'

    api_server.respond = respond
    runtime = runtime_for(api_server)
    try:
        result = await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
    finally:
        await runtime.aclose()
    assert "recovered" in result.text
    assert len(api_server.requests) == 3


async def test_http_auth_error_hits_provider_once_and_redacts_error_body(api_server):
    api_server.respond = lambda body: (401, {"error": {
        "message": "invalid key fake-loopback-provider-key", "type": "authentication_error",
    }})
    runtime = runtime_for(api_server)
    try:
        for _ in range(2):
            with pytest.raises(BackendError) as error:
                await ApiBackend(runtime).run("sys", [Message("user", "go")], [])
            assert "fake-loopback-provider-key" not in str(error.value)
    finally:
        await runtime.aclose()
    assert len(api_server.requests) == 1
