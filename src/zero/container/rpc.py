"""Host-side RPC client for the in-container tool server.

Wire format (per endpoint POST /tools/{name}):
  request body : {"args": {...}}
  response body: {"ok": bool, "output": str, "truncated": bool, "meta": {...} | null}

Transport is plain TCP. (The earlier UDS-in-shared-volume design ran into
a Docker-Desktop-on-macOS limitation: uvicorn chmods the UDS after binding
and the chmod fails over virtiofs. TCP avoids that and works the same on
both Linux and macOS.)
"""
from __future__ import annotations

from typing import Any

import httpx

from zero.tools.base import ToolResult


class RPCError(RuntimeError):
    pass


def encode_request(args: dict) -> dict[str, Any]:
    return {"args": args}


def decode_response(body: dict[str, Any]) -> ToolResult:
    if not isinstance(body, dict):
        raise RPCError(f"expected JSON object, got {type(body).__name__}")
    if "ok" not in body or "output" not in body:
        raise RPCError(f"missing required fields in response: {body!r}")
    return ToolResult(
        ok=bool(body["ok"]),
        output=str(body["output"]),
        truncated=bool(body.get("truncated", False)),
        meta=body.get("meta"),
    )


class ContainerRPC:
    """Thin async client. The container is expected to be running already."""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ContainerRPC":
        await self.connect()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, tool_name: str, args: dict) -> ToolResult:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        try:
            resp = await self._client.post(
                f"/tools/{tool_name}", json=encode_request(args)
            )
        except httpx.HTTPError as e:
            raise RPCError(f"rpc transport error: {e}") from e
        if resp.status_code != 200:
            raise RPCError(
                f"rpc {tool_name} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = resp.json()
        except ValueError as e:
            raise RPCError(f"rpc {tool_name} returned non-JSON: {e}") from e
        return decode_response(body)
