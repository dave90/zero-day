"""Mode B MCP server.

Spawned by `claude` (via `--mcp-config`) as a subprocess that speaks the
Model Context Protocol over stdio. Tool calls received here are forwarded
to the orchestrator's bridge (HTTP, localhost) so the orchestrator's
ApprovalGate, audit log, and UI participate exactly as in Mode A.

The MCP server learns its bridge URL + the tool catalog from environment
variables set by the orchestrator before spawning `claude`:

  ZERO_MCP_BRIDGE_URL   — base URL of the in-process bridge
  ZERO_MCP_AUTH_TOKEN   — opaque token, sent as `X-Zero-Token` on calls
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import httpx

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - import-time guard
    FastMCP = None  # type: ignore[assignment]


def _bridge_url() -> str:
    url = os.environ.get("ZERO_MCP_BRIDGE_URL")
    if not url:
        raise SystemExit("ZERO_MCP_BRIDGE_URL not set")
    return url.rstrip("/")


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get("ZERO_MCP_AUTH_TOKEN", "")
    return {"X-Zero-Token": tok} if tok else {}


async def fetch_catalog() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(_bridge_url() + "/catalog", headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()


async def call_bridge_tool(name: str, args: dict[str, Any]) -> str:
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            _bridge_url() + f"/tools/{name}",
            json={"args": args},
            headers=_auth_headers(),
        )
        if resp.status_code != 200:
            return f"bridge error {resp.status_code}: {resp.text[:300]}"
        body = resp.json()
        prefix = "" if body.get("ok") else "[FAIL] "
        return prefix + str(body.get("output", ""))


async def call_ask_user(
    question: str, options: list[str], free_form: bool, blocking: bool
) -> str:
    async with httpx.AsyncClient(timeout=3600.0) as client:
        resp = await client.post(
            _bridge_url() + "/ask_user",
            json={
                "question": question,
                "options": options,
                "free_form": free_form,
                "blocking": blocking,
            },
            headers=_auth_headers(),
        )
        if resp.status_code != 200:
            return f"ask_user bridge error {resp.status_code}"
        return resp.json().get("answer", "")


def build_server(catalog: list[dict[str, Any]]):
    """Construct a FastMCP server that exposes every catalog tool + ask_user."""
    if FastMCP is None:
        raise SystemExit(
            "the `mcp` package is not installed; run `uv sync` to install"
            " Mode B dependencies"
        )
    app = FastMCP(name="zero")

    for entry in catalog:
        name = entry["name"]
        description = entry.get("description", "")

        # Pythonic closure capture.
        def _make_handler(tool_name: str):
            async def handler(args: dict[str, Any] | None = None) -> str:
                return await call_bridge_tool(tool_name, args or {})

            handler.__name__ = f"tool_{tool_name}"
            handler.__doc__ = description
            return handler

        app.tool(name=name, description=description)(_make_handler(name))

    @app.tool(
        name="ask_user",
        description=(
            "Ask the human a question. Use sparingly — prefer trying a tool."
        ),
    )
    async def ask_user_tool(  # type: ignore[no-untyped-def]
        question: str,
        options: list[str] | None = None,
        free_form: bool = True,
        blocking: bool = True,
    ) -> str:
        return await call_ask_user(question, options or [], free_form, blocking)

    return app


def main() -> int:
    """Standalone entry point: discover catalog from the bridge, run on stdio."""
    catalog = asyncio.run(fetch_catalog())
    app = build_server(catalog)
    app.run()  # stdio transport by default
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
