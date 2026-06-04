import pytest
from pydantic import BaseModel

from zero.tools.base import RiskLevel, Tool, ToolResult
from zero.tools.registry import ToolRegistry, default_registry


class _Args(BaseModel):
    x: int


class _T(Tool):
    name = "t"
    description = "test"
    risk_level = RiskLevel.SAFE
    Args = _Args

    async def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(ok=True, output="ok")


def test_register_and_get():
    reg = ToolRegistry()
    t = _T()
    reg.register(t)
    assert reg.get("t") is t
    assert "t" in reg


def test_duplicate_name_rejected():
    reg = ToolRegistry()
    reg.register(_T())
    with pytest.raises(ValueError):
        reg.register(_T())


def test_missing_name_rejected():
    class _U(_T):
        name = ""

    with pytest.raises(ValueError):
        ToolRegistry().register(_U())


def test_get_missing():
    with pytest.raises(KeyError):
        ToolRegistry().get("nope")


def test_catalog_shape():
    reg = ToolRegistry()
    reg.register(_T())
    cat = reg.catalog()
    assert len(cat) == 1
    assert cat[0]["name"] == "t"
    assert cat[0]["risk_level"] == "safe"
    assert "schema" in cat[0]
    assert cat[0]["schema"]["type"] == "object"


def test_default_registry_has_expected_tools():
    reg = default_registry()
    names = reg.names()
    for expected in ("notes", "web_search", "web_fetch", "python_exec"):
        assert expected in names
