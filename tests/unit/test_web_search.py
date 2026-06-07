"""WebSearchTool behavior — the DuckDuckGo call is mocked (no network)."""
from __future__ import annotations

import sys
import types

import pytest

from zero.tools.web_search import WebSearchArgs, WebSearchTool


def _install_fake_ddgs(monkeypatch, results=None, *, raise_exc=None):
    """Install a fake `ddgs` module so WebSearchTool._search runs offline.
    WebSearchTool does `from ddgs import DDGS` inside the method."""
    mod = types.ModuleType("ddgs")

    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query, max_results):
            if raise_exc is not None:
                raise raise_exc
            return list(results or [])

    mod.DDGS = _DDGS
    monkeypatch.setitem(sys.modules, "ddgs", mod)


async def test_web_search_formats_results(monkeypatch):
    _install_fake_ddgs(
        monkeypatch,
        results=[
            {"title": "Log4Shell", "href": "https://x/1", "body": "CVE-2021-44228"},
            {"title": "Advisory", "url": "https://x/2", "body": "patch now"},
        ],
    )
    r = await WebSearchTool().run(WebSearchArgs(query="log4shell", max_results=2))
    assert r.ok
    assert "Log4Shell" in r.output and "https://x/1" in r.output
    assert "Advisory" in r.output and "https://x/2" in r.output  # `url` key also handled


async def test_web_search_no_results(monkeypatch):
    _install_fake_ddgs(monkeypatch, results=[])
    r = await WebSearchTool().run(WebSearchArgs(query="nothing"))
    assert r.ok
    assert r.output == "(no results)"


async def test_web_search_handles_provider_error(monkeypatch):
    _install_fake_ddgs(monkeypatch, raise_exc=RuntimeError("rate limited"))
    r = await WebSearchTool().run(WebSearchArgs(query="x"))
    assert r.ok is False
    assert "search failed" in r.output and "rate limited" in r.output


def test_web_search_args_bounds():
    # max_results is clamped to [1, 20] by the field constraints.
    with pytest.raises(Exception):
        WebSearchArgs(query="x", max_results=0)
    with pytest.raises(Exception):
        WebSearchArgs(query="x", max_results=21)
