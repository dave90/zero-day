import httpx

from zero.tools.http_replay import HttpReplayArgs, HttpReplayTool


async def test_get_round_trip(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"hello": "world"})

    # Patch httpx.AsyncClient inside the tool to use a MockTransport.
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("zero.tools.http_replay.httpx.AsyncClient", patched)

    r = await HttpReplayTool().run(HttpReplayArgs(url="http://x/"))
    assert r.ok
    assert "200 OK" in r.output
    assert "hello" in r.output


async def test_http_error(monkeypatch):
    def handler(_request):
        raise httpx.ConnectError("nope")

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("zero.tools.http_replay.httpx.AsyncClient", patched)

    r = await HttpReplayTool().run(HttpReplayArgs(url="http://x/"))
    assert r.ok is False
    assert "http error" in r.output


async def test_post_with_json_body(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["content"] = request.content
        return httpx.Response(201, text="created")

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("zero.tools.http_replay.httpx.AsyncClient", patched)

    r = await HttpReplayTool().run(
        HttpReplayArgs(method="POST", url="http://x/", json_body={"k": 1})
    )
    assert r.ok
    assert seen["method"] == "POST"
    assert b'"k"' in seen["content"]
