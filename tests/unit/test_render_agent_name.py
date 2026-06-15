"""The orchestrator is presented to the user as `zero` in the UI; subagents keep
their own labels."""
from zero.ui.events import TurnStart
from zero.ui.render import render_event


def test_orchestrator_shows_as_zero():
    text = "\n".join(render_event(TurnStart(agent="orchestrator", turn=3)))
    assert "zero · turn 3" in text
    assert "orchestrator" not in text


def test_other_agents_keep_their_name():
    assert "pentest · turn 1" in "\n".join(render_event(TurnStart(agent="pentest", turn=1)))
    assert "validator · turn 2" in "\n".join(render_event(TurnStart(agent="validator", turn=2)))
