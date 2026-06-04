import pytest

from zero.tools.web_fetch import WebFetchArgs, WebFetchTool


async def test_bad_url_scheme():
    tool = WebFetchTool()
    r = await tool.run(WebFetchArgs(url="ftp://example.com"))
    assert r.ok is False


async def test_blocked_host_refused():
    tool = WebFetchTool(blocked_hosts={"localhost"})
    r = await tool.run(WebFetchArgs(url="http://localhost:3000/api"))
    assert r.ok is False
    assert "refused" in r.output
