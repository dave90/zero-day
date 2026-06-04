import pytest

from zero.orchestrator.approval import ApprovalGate, ApprovalRequest, Decision
from zero.tools.base import RiskLevel


def make_req(rl: RiskLevel, tool: str = "t", host_exec: bool = False) -> ApprovalRequest:
    return ApprovalRequest(tool=tool, args={}, risk_level=rl, call_id="c1", host_exec=host_exec)


async def deny(_req):
    return Decision.NO


async def accept(_req):
    return Decision.YES


async def always(_req):
    return Decision.ALWAYS


# Auto-approve matrix --------------------------------------------------------


@pytest.mark.parametrize(
    "rl,auto,yolo,iu,expected",
    [
        (RiskLevel.SAFE, set(), False, False, False),
        (RiskLevel.SAFE, {"safe"}, False, False, True),
        (RiskLevel.SAFE, set(), True, False, True),
        (RiskLevel.ACTIVE, set(), False, False, False),
        (RiskLevel.ACTIVE, {"active"}, False, False, True),
        (RiskLevel.ACTIVE, set(), True, False, True),
        (RiskLevel.EXPLOIT, {"exploit"}, False, False, False),  # needs i_understand
        (RiskLevel.EXPLOIT, {"exploit"}, False, True, True),
        (RiskLevel.EXPLOIT, set(), True, True, False),  # yolo does NOT cover exploit
        (RiskLevel.DESTRUCTIVE, {"safe", "active", "exploit"}, True, True, False),
    ],
)
async def test_auto_approve_matrix(rl, auto, yolo, iu, expected):
    gate = ApprovalGate(prompter=deny, auto_approve=auto, yolo=yolo, i_understand=iu)
    allowed = await gate.check(make_req(rl))
    assert allowed is expected


# Interactive decisions ------------------------------------------------------


async def test_prompt_yes():
    gate = ApprovalGate(prompter=accept)
    assert await gate.check(make_req(RiskLevel.ACTIVE)) is True


async def test_prompt_no():
    gate = ApprovalGate(prompter=deny)
    assert await gate.check(make_req(RiskLevel.ACTIVE)) is False


async def test_always_makes_subsequent_calls_skip_prompt():
    calls = {"n": 0}

    async def prompter(_req):
        calls["n"] += 1
        return Decision.ALWAYS

    gate = ApprovalGate(prompter=prompter)
    assert await gate.check(make_req(RiskLevel.ACTIVE, tool="sqlmap")) is True
    assert await gate.check(make_req(RiskLevel.ACTIVE, tool="sqlmap")) is True
    # Second call did not re-prompt.
    assert calls["n"] == 1


async def test_destructive_always_prompts():
    gate = ApprovalGate(prompter=deny, yolo=True, auto_approve={"exploit"}, i_understand=True)
    assert await gate.check(make_req(RiskLevel.DESTRUCTIVE)) is False


# Host-execution boundary ----------------------------------------------------


async def test_host_exec_not_auto_approved_even_with_yolo():
    """The core hardening: a tool that runs on the host is NEVER auto-approved
    by --yolo or mode-c's safe+active default — it must prompt."""
    gate = ApprovalGate(prompter=deny, yolo=True, auto_approve={"safe", "active"})
    assert await gate.check(make_req(RiskLevel.ACTIVE, host_exec=True)) is False
    # the non-host version of the same risk IS auto-approved
    assert await gate.check(make_req(RiskLevel.ACTIVE, host_exec=False)) is True


async def test_host_exec_auto_approved_only_with_flag():
    gate = ApprovalGate(prompter=deny, auto_approve={"active"}, allow_host_exec=True)
    assert await gate.check(make_req(RiskLevel.ACTIVE, host_exec=True)) is True


async def test_host_exec_still_runs_if_user_approves():
    gate = ApprovalGate(prompter=accept)
    assert await gate.check(make_req(RiskLevel.ACTIVE, host_exec=True)) is True


async def test_host_exec_always_is_remembered():
    calls = {"n": 0}

    async def prompter(_req):
        calls["n"] += 1
        return Decision.ALWAYS

    gate = ApprovalGate(prompter=prompter)
    r1 = make_req(RiskLevel.ACTIVE, tool="python_exec", host_exec=True)
    r2 = make_req(RiskLevel.ACTIVE, tool="python_exec", host_exec=True)
    assert await gate.check(r1) is True
    assert await gate.check(r2) is True
    assert calls["n"] == 1  # explicit ALWAYS bypasses the host-exec guard thereafter
