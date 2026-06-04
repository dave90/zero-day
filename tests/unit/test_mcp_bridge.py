"""End-to-end of the bridge — start it, hit each endpoint with httpx, assert
shape. No `claude` and no MCP server needed."""
from __future__ import annotations

import httpx
import pytest

from zero.llm.mcp_bridge import BridgeHandlers, BridgeServer


@pytest.fixture
async def bridge():
    calls: list[tuple[str, dict]] = []
    asks: list[tuple[str, list[str], bool, bool]] = []

    async def call_tool(name: str, args: dict) -> dict:
        calls.append((name, args))
        if name == "broken":
            return {"ok": False, "output": "nope"}
        return {"ok": True, "output": f"ran {name}", "truncated": False, "meta": None}

    async def ask_user(q: str, opts: list[str], free_form: bool, blocking: bool) -> str:
        asks.append((q, opts, free_form, blocking))
        return "the-answer"

    handlers = BridgeHandlers(
        catalog=[{"name": "x", "description": "X", "risk_level": "safe", "schema": {}}],
        call_tool=call_tool,
        ask_user=ask_user,
    )
    server = BridgeServer(handlers)
    await server.start()
    yield server, calls, asks
    await server.stop()


async def test_healthz(bridge):
    server, _, _ = bridge
    async with httpx.AsyncClient() as client:
        r = await client.get(server.base_url + "/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_catalog(bridge):
    server, _, _ = bridge
    async with httpx.AsyncClient() as client:
        r = await client.get(server.base_url + "/catalog")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["name"] == "x"


async def test_call_tool_round_trip(bridge):
    server, calls, _ = bridge
    async with httpx.AsyncClient() as client:
        r = await client.post(
            server.base_url + "/tools/foo", json={"args": {"a": 1}}
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["output"] == "ran foo"
    assert calls == [("foo", {"a": 1})]


async def test_ask_user_round_trip(bridge):
    server, _, asks = bridge
    async with httpx.AsyncClient() as client:
        r = await client.post(
            server.base_url + "/ask_user",
            json={"question": "q", "options": ["o1"], "free_form": False, "blocking": False},
        )
    assert r.status_code == 200
    assert r.json()["answer"] == "the-answer"
    assert asks == [("q", ["o1"], False, False)]


async def test_call_tool_handler_failure_path(bridge):
    server, _, _ = bridge
    async with httpx.AsyncClient() as client:
        r = await client.post(server.base_url + "/tools/broken", json={"args": {}})
    assert r.status_code == 200
    assert r.json()["ok"] is False
