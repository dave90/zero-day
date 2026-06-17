"""Mode B argv + MCP config assembly. No claude required."""
from pathlib import Path

from zero.llm.claude_code import build_mcp_config, build_mode_b_argv


def test_mcp_config_shape():
    cfg = build_mcp_config(bridge_url="http://127.0.0.1:9999", auth_token="xyz")
    assert "mcpServers" in cfg
    assert "zero" in cfg["mcpServers"]
    s = cfg["mcpServers"]["zero"]
    assert s["args"] == ["-m", "zero.llm.mcp_server"]
    assert s["env"]["ZERO_MCP_BRIDGE_URL"] == "http://127.0.0.1:9999"
    assert s["env"]["ZERO_MCP_AUTH_TOKEN"] == "xyz"


def test_mode_b_argv_includes_mcp_config_and_disallow(tmp_path: Path):
    cfg_path = tmp_path / "mcp.json"
    argv = build_mode_b_argv(
        binary="/usr/bin/claude",
        mcp_config_path=cfg_path,
        disallowed_builtin_tools=("WebSearch", "WebFetch"),
    )
    assert argv[0] == "/usr/bin/claude"
    i = argv.index("--mcp-config")
    assert argv[i + 1] == str(cfg_path)
    j = argv.index("--disallowedTools")
    assert argv[j + 1] == "WebSearch,WebFetch"
    # The prompt is piped on STDIN, not argv (Windows command-line limit) — so there is
    # no `-p <prompt>` positional; `--print` selects non-interactive mode.
    assert "--print" in argv
    assert "-p" not in argv


def test_mode_b_argv_no_disallow():
    argv = build_mode_b_argv(
        binary="claude",
        mcp_config_path=Path("/tmp/c.json"),
        disallowed_builtin_tools=(),
    )
    assert "--disallowedTools" not in argv
