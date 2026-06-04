"""The status line surfaces turn/mode, approvals waiting, and the
confirmed/rejected finding counts. The 'validating' counter, the live
'subagents running' counter, and the leading agent ('pentest') word were
removed to keep the footer terse."""
from zero.ui.events import Status
from zero.ui.render import status_line


def test_status_line_shows_approvals_and_findings():
    st = Status(
        turn=3,
        tokens=1200,
        pending_approvals=1,
        validations_pending=2,
        validations_confirmed=4,
        validations_rejected=0,
        running_pentesters=3,
        running_validators=1,
    )
    line = status_line(st)
    assert "turn 3" in line
    assert "approvals waiting: 1" in line
    assert "4 confirmed" in line


def test_status_line_omits_removed_fields():
    line = status_line(
        Status(running_pentesters=3, running_validators=1, validations_pending=2)
    )
    # No leading agent label, no validating counter, no live-subagent counter.
    assert "pentest" not in line
    assert "validating" not in line
    assert "running" not in line


def test_status_line_defaults_to_zero_counts():
    line = status_line(Status())
    assert "approvals waiting: 0" in line
    assert "0 confirmed / 0 rejected" in line
