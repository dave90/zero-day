"""In-process HTTP bridge used by the Mode B MCP server.

`claude` (Mode B) spawns our MCP server (`zero.llm.mcp_server`). The MCP
server is a separate process — it can't reach the orchestrator's
ApprovalGate / audit log / UI in-process. The bridge fills that gap:
during a Mode B session the orchestrator starts this Starlette app on a
local ephemeral port (`http://127.0.0.1:<port>/`), passes the URL to the
MCP server via env, and the server POSTs each tool call here.

Endpoints:
  POST /tools/{name}    body {"args": {...}}  → ToolResult JSON
  POST /ask_user        body {"question": ..., "options": ..., ...} → {"answer": ...}
  GET  /catalog         → [{name, description, risk_level, schema}, ...]
  GET  /healthz         → {"status": "ok"}
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import uvicorn
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class BridgeHandlers:
    """Plug-in points the orchestrator wires when starting the bridge."""

    def __init__(
        self,
        catalog: list[dict[str, Any]],
        call_tool: Callable[[str, dict], Awaitable[dict]],
        ask_user: Callable[[str, list[str], bool, bool], Awaitable[str]],
    ) -> None:
        self.catalog = catalog
        self.call_tool = call_tool
        self.ask_user = ask_user


def make_app(handlers: BridgeHandlers) -> Starlette:
    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def catalog(_request: Request) -> JSONResponse:
        return JSONResponse(handlers.catalog)

    async def call_tool(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        args = body.get("args", {})
        result = await handlers.call_tool(name, args)
        return JSONResponse(result)

    async def ask_user(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        question = body.get("question", "")
        options = body.get("options", []) or []
        free_form = bool(body.get("free_form", True))
        blocking = bool(body.get("blocking", True))
        try:
            answer = await handlers.ask_user(question, options, free_form, blocking)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"answer": answer})

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/catalog", catalog, methods=["GET"]),
            Route("/tools/{name}", call_tool, methods=["POST"]),
            Route("/ask_user", ask_user, methods=["POST"]),
        ]
    )


class BridgeServer:
    """Run an MCP-bridge HTTP server in the same process as the orchestrator."""

    def __init__(self, handlers: BridgeHandlers, *, host: str = "127.0.0.1") -> None:
        self.handlers = handlers
        self.host = host
        self.port: int = 0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        app = make_app(self.handlers)
        config = uvicorn.Config(
            app=app, host=self.host, port=0, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        # uvicorn writes the chosen port into config.port after binding.
        self._task = asyncio.create_task(self._server.serve(), name="mcp_bridge")
        # Wait for socket bind so config.port is populated.
        while not getattr(self._server, "started", False):
            await asyncio.sleep(0.02)
        # uvicorn 0.27+ exposes the bound port via self._server.servers[0].sockets[0]
        socks = self._server.servers
        if socks:
            for sock in socks[0].sockets:
                bound = sock.getsockname()
                if bound[1]:
                    self.port = bound[1]
                    break

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._server = None
        self._task = None
