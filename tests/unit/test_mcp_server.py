"""Smoke test: mcp_server.build_server registers the right tool names."""
from __future__ import annotations


def test_build_server_exposes_catalog_tools_and_ask_user():
    from zero.llm.mcp_server import build_server

    catalog = [
        {"name": "notes", "description": "notes tool", "risk_level": "safe", "schema": {}},
        {"name": "sqlmap", "description": "sqli", "risk_level": "exploit", "schema": {}},
    ]
    app = build_server(catalog)
    # FastMCP keeps registered tools accessible via `_tool_manager`.
    # We don't depend on the private layout — just confirm `app.list_tools`
    # returns the expected names.
    import asyncio

    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert "notes" in names
    assert "sqlmap" in names
    assert "ask_user" in names
