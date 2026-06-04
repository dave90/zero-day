import pytest
from pydantic import BaseModel

from zero.tools.base import RiskLevel, Tool, ToolResult
from zero.tools.registry import ToolRegistry


class _Args(BaseModel):
    x: int = 0


class _T(Tool):
    name = "t"
    description = "test"
    risk_level = RiskLevel.SAFE
    Args = _Args

    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def run(self, args: BaseModel) -> ToolResult:
        return ToolResult(ok=True, output=self.tag)


def test_replace_swaps_existing():
    reg = ToolRegistry()
    reg.register(_T("local"))
    reg.replace(_T("remote"))
    assert reg.get("t").tag == "remote"  # type: ignore[attr-defined]


def test_replace_missing_raises():
    reg = ToolRegistry()
    with pytest.raises(KeyError):
        reg.replace(_T("x"))
