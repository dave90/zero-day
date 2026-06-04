"""RPC client tests using a mocked httpx transport — no socket, no Docker."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from zero.container.rpc import (
    ContainerRPC,
    RPCError,
    decode_response,
    encode_request,
)


def test_encode_request_shape():
    assert encode_request({"x": 1}) == {"args": {"x": 1}}


def test_decode_response_full():
    r = decode_response(
        {"ok": True, "output": "hi", "truncated": True, "meta": {"k": "v"}}
    )
    assert r.ok and r.output == "hi" and r.truncated is True
    assert r.meta == {"k": "v"}


def test_decode_response_minimal():
    r = decode_response({"ok": False, "output": "boom"})
    assert r.ok is False and r.output == "boom"
    assert r.truncated is False and r.meta is None


def test_decode_response_missing_fields():
    with pytest.raises(RPCError):
        decode_response({"ok": True})


def test_decode_response_wrong_type():
    with pytest.raises(RPCError):
        decode_response("not a dict")  # type: ignore[arg-type]


async def test_call_round_trip(tmp_path: Path):
    """End-to-end call() with a MockTransport — no real socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/tools/python_exec"
        body = json.loads(request.content.decode())
        assert body == {"args": {"code": "print(1)"}}
        return httpx.Response(
            200,
            json={"ok": True, "output": "1\n", "truncated": False, "meta": None},
        )

    rpc = ContainerRPC(base_url="http://_/")
    # Bypass connect() — inject our own MockTransport client.
    rpc._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://_/"
    )
    try:
        r = await rpc.call("python_exec", {"code": "print(1)"})
    finally:
        await rpc.aclose()
    assert r.ok and "1" in r.output


async def test_call_handles_http_error(tmp_path: Path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    rpc = ContainerRPC(base_url="http://_/")
    rpc._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://_/"
    )
    try:
        with pytest.raises(RPCError):
            await rpc.call("x", {})
    finally:
        await rpc.aclose()


async def test_call_handles_unknown_tool_404(tmp_path: Path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown tool: bogus"})

    rpc = ContainerRPC(base_url="http://_/")
    rpc._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://_/"
    )
    try:
        with pytest.raises(RPCError):
            await rpc.call("bogus", {})
    finally:
        await rpc.aclose()
