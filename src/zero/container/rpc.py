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

import asyncio
from typing import Any, Awaitable, Callable, Optional

import httpx

from zero.tools.base import ToolResult

# A recovery hook: given the base_url that just failed, ensure a healthy sandbox exists
# and return its (possibly NEW) RPC base_url — or None if it can't be recovered. The
# orchestrator wires this to re-create the container (new name/port) after a crash/OOM
# or an operator `docker rm`, so a mid-run sandbox loss doesn't kill the engagement.
RecoverHook = Callable[[str], Awaitable[Optional[str]]]


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
    """Thin async client. The container is expected to be running already.

    On a transport failure it retries up to `max_attempts`, reconnecting each time
    (handling a transient drop) and — if a `recover` hook is set — asking it for a
    healthy endpoint between attempts (handling a container that was removed/crashed:
    the hook re-creates it and returns the new base_url). A bounded loop, so a truly
    dead sandbox fails fast with a clear error instead of spinning forever."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 600.0,
        recover: RecoverHook | None = None,
        max_attempts: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._recover = recover
        self.max_attempts = max(1, max_attempts)
        self.retry_backoff = retry_backoff

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
        last_err: Exception | None = None
        for attempt in range(self.max_attempts):
            if self._client is None:
                await self.connect()
            assert self._client is not None
            try:
                resp = await self._client.post(
                    f"/tools/{tool_name}", json=encode_request(args)
                )
            except httpx.HTTPError as e:
                # Transport failure: the sandbox is unreachable (dropped / removed /
                # crashed). Drop the dead client, try to recover a healthy endpoint,
                # then retry. Bounded by max_attempts.
                last_err = e
                await self.aclose()
                if attempt + 1 < self.max_attempts:
                    await self._recover_between_attempts()
                    continue
                raise RPCError(f"rpc transport error: {e}") from e
            # A response came back — NOT a transport problem, so don't retry these.
            if resp.status_code != 200:
                raise RPCError(
                    f"rpc {tool_name} returned HTTP {resp.status_code}: {resp.text[:200]}"
                )
            try:
                body = resp.json()
            except ValueError as e:
                raise RPCError(f"rpc {tool_name} returned non-JSON: {e}") from e
            return decode_response(body)
        raise RPCError(f"rpc transport error after {self.max_attempts} attempts: {last_err}")

    async def _recover_between_attempts(self) -> None:
        """Ask the recover hook for a healthy endpoint (it may re-create the container
        and hand back a new base_url), then back off briefly. Recovery failure must not
        mask the original transport error — we just proceed to the next attempt against
        the same url (which will fail and, on the last attempt, surface the error)."""
        if self._recover is not None:
            try:
                new_url = await self._recover(self.base_url)
            except Exception:  # noqa: BLE001 — recovery is best-effort
                new_url = None
            if new_url:
                self.base_url = new_url.rstrip("/")
        if self.retry_backoff > 0:
            await asyncio.sleep(self.retry_backoff)
