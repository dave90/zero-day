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
    for expected in ("web_search", "web_fetch", "python_exec"):
        assert expected in names
    assert "notes" not in names  # the notes tool was removed


# Drive a completeness smoke test off the registry itself, so EVERY tool we
# ship (current and future) is covered — a newly added tool can't slip in
# without basic metadata/schema validation.
@pytest.mark.parametrize("name", sorted(default_registry().names()))
def test_every_registered_tool_is_well_formed(name):
    tool = default_registry().get(name)
    assert tool.name == name
    assert tool.description and isinstance(tool.description, str)
    assert isinstance(tool.risk_level, RiskLevel)
    assert issubclass(tool.Args, BaseModel)
    # json_schema() must produce a valid object schema (this is what the LLM
    # sees in the tool catalog).
    schema = tool.json_schema()
    assert schema.get("type") == "object"


def test_binary_tools_build_argv_without_raising():
    """Every BinaryTool wrapper turns a minimal valid Args into a list[str] argv
    (the step that, if broken, fails the tool only at runtime in the sandbox)."""
    from zero.tools.binary import BinaryTool

    # Minimal valid args per binary tool (required fields only).
    minimal = {
        "sqlmap": {"url": "http://h/x?id=1"},
        "nmap": {"target": "h"},
        "ffuf": {"url": "http://h/FUZZ", "wordlist": "/w"},
        "nuclei": {"target": "http://h"},
        "httpx_tool": {"target": "http://h"},
        "jwt_tool": {"token": "t"},
        "curl_raw": {"url": "http://h"},
    }
    reg = default_registry()
    for name in reg.names():
        tool = reg.get(name)
        if not isinstance(tool, BinaryTool):
            continue
        assert name in minimal, f"add minimal args for new binary tool {name!r}"
        argv = tool.args_to_argv(tool.Args(**minimal[name]))
        assert isinstance(argv, list) and all(isinstance(s, str) for s in argv)
