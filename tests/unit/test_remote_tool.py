"""RemoteTool forwards through the RPC client and keeps the local schema."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from zero.container.rpc import ContainerRPC
from zero.tools.python_exec import PythonExecArgs, PythonExecTool
from zero.tools.remote import wrap_with_remote


async def test_wrap_with_remote_preserves_metadata(tmp_path: Path):
    local = PythonExecTool()
    rpc = ContainerRPC(base_url="http://_/")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={"ok": True, "output": "from-container", "truncated": False, "meta": None},
        )

    rpc._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://_/"
    )

    remote = wrap_with_remote(local, rpc)
    # Same name/description/risk_level/Args schema as the local tool.
    assert remote.name == local.name
    assert remote.description == local.description
    assert remote.risk_level == local.risk_level
    assert remote.Args is local.Args
    assert remote.json_schema() == local.json_schema()
    # Sandboxed: a remoted tool is no longer host-exec.
    assert remote.runs_on_host is False

    try:
        result = await remote.run(PythonExecArgs(code="print(1)"))
    finally:
        await rpc.aclose()
    assert result.ok
    assert result.output == "from-container"


def test_wrap_with_remote_preserves_requires_approval():
    """A no-approval local tool (burp_traffic) must keep bypassing the gate after
    it is remoted into the sandbox — wrap_with_remote must carry the flag, not
    reset it to the default True."""
    from zero.tools.burp_traffic import BurpTrafficTool

    rpc = ContainerRPC(base_url="http://_/")
    assert wrap_with_remote(BurpTrafficTool(), rpc).requires_approval is False
    # A normal tool stays gated.
    assert wrap_with_remote(PythonExecTool(), rpc).requires_approval is True


async def test_remote_tool_endpoint_uses_tool_name(tmp_path: Path):
    local = PythonExecTool()
    rpc = ContainerRPC(base_url="http://_/")
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200, json={"ok": True, "output": "ok"}
        )

    rpc._client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler), base_url="http://_/"
    )
    remote = wrap_with_remote(local, rpc)
    try:
        await remote.run(PythonExecArgs(code="x"))
    finally:
        await rpc.aclose()
    assert seen_paths == ["/tools/python_exec"]
