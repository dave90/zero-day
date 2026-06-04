"""Dispatch tools (M8): arg parsing, interception detection, finding round-trip."""
import pytest
from pydantic import ValidationError

from zero.findings.schema import Finding
from zero.tools.dispatch import (
    DISPATCH_TOOL_NAMES,
    AwaitSubagentsArgs,
    DispatchPentesterArgs,
    DispatchPentesterTool,
    DispatchValidatorArgs,
    dispatch_tools,
    is_dispatch_tool,
    validator_args_to_finding,
)


def test_is_dispatch_tool():
    assert is_dispatch_tool("dispatch_pentester")
    assert is_dispatch_tool("await_subagents")
    assert is_dispatch_tool("finalize")
    assert not is_dispatch_tool("finding_submit")
    assert not is_dispatch_tool("http_replay")


def test_dispatch_tools_catalog_complete():
    names = {t.name for t in dispatch_tools()}
    assert names == set(DISPATCH_TOOL_NAMES)
    assert len(names) == 5


def test_pentester_args_parse_and_reject():
    a = DispatchPentesterArgs(attack_idea="SQLi in sort", target="/api/orders")
    assert a.context == "" and a.max_turns is None
    with pytest.raises(ValidationError):
        DispatchPentesterArgs(target="/x")  # missing attack_idea


def test_await_args_defaults_and_choice():
    # Default is "any": the orchestrator acts as soon as one subagent finishes.
    assert AwaitSubagentsArgs().which == "any"
    with pytest.raises(ValidationError):
        AwaitSubagentsArgs(which="some")  # not in Literal


async def test_dispatch_tool_run_is_sentinel():
    res = await DispatchPentesterTool().run(DispatchPentesterArgs(attack_idea="x", target="/y"))
    assert res.ok is False
    assert "orchestrator" in res.output


def test_validator_args_to_finding_drops_source_subagent():
    raw = {
        "title": "IDOR",
        "summary": "s",
        "endpoint": "GET /api/orders/{id}",
        "cwe": "CWE-639",
        "evidence": "e",
        "source_subagent": "#3",
    }
    f = validator_args_to_finding(raw)
    assert isinstance(f, Finding)
    assert f.title == "IDOR"
    assert not hasattr(f, "source_subagent")
    # round-trips through the model
    assert DispatchValidatorArgs(**raw).to_finding().cwe == "CWE-639"
