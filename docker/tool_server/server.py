"""In-container tool-RPC server.

POST /tools/{name}
    body : {"args": {...}}
    resp : {"ok": bool, "output": str, "truncated": bool, "meta": {...}|null}

Reuses the same `zero.tools.*` registry the host uses, so the same Tool
subclasses run in both places — the wire format is the only boundary.

The container's working directory is the user's repo at /work, mounted in
by ContainerManager. PYTHONPATH=/work/src (set in the Dockerfile) makes
`import zero` resolve to the host checkout — no duplicate install needed.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError

from zero.tools.base import ToolResult
from zero.tools.registry import default_registry

app = FastAPI(title="zero tool-rpc", version="0.1")

# In-container we want python_exec to run here (the whole point of the
# sandbox), so the default registry's `PythonExecTool` is exactly the right
# implementation — it execs a subprocess on whatever box it's running on,
# which is the container.
_REGISTRY = default_registry()


class RPCRequest(BaseModel):
    args: dict[str, Any]


class RPCResponse(BaseModel):
    ok: bool
    output: str
    truncated: bool = False
    meta: dict[str, Any] | None = None


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "uname": os.uname().sysname}


@app.get("/tools")
async def list_tools() -> dict[str, list[dict[str, Any]]]:
    return {"tools": _REGISTRY.catalog()}


@app.post("/tools/{name}", response_model=RPCResponse)
async def call_tool(name: str, req: RPCRequest) -> RPCResponse:
    if name not in _REGISTRY:
        raise HTTPException(status_code=404, detail=f"unknown tool: {name}")
    tool = _REGISTRY.get(name)
    try:
        parsed = tool.Args(**req.args)
    except ValidationError as e:
        return RPCResponse(ok=False, output=f"args validation error: {e}")
    result: ToolResult = await tool.run(parsed)
    return RPCResponse(
        ok=result.ok,
        output=result.output,
        truncated=result.truncated,
        meta=result.meta,
    )
