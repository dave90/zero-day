from zero.llm.envelope import AskUserEnvelope
from zero.orchestrator.ask_user_governor import AskUserGovernor


def _q(text: str, blocking: bool = True) -> AskUserEnvelope:
    return AskUserEnvelope(question=text, options=[], free_form=True, blocking=blocking)


def test_default_proceeds():
    g = AskUserGovernor()
    d = g.evaluate("pentest", _q("Which scope first?"))
    assert d.proceed is True


def test_per_session_cap():
    g = AskUserGovernor(limits={"pentest": 2})
    for i in range(2):
        d = g.evaluate("pentest", _q(f"Q{i}"))
        assert d.proceed
        g.record_answer("pentest", _q(f"Q{i}"), "ans")
    d = g.evaluate("pentest", _q("Q3"))
    assert d.proceed is False
    assert d.refusal is not None


def test_duplicate_question_short_circuits():
    g = AskUserGovernor()
    q = _q("Should I focus on auth or orders?")
    g.record_answer("pentest", q, "auth")
    d = g.evaluate("pentest", _q("should i focus on auth or orders??"))
    assert d.proceed is False
    assert d.answer == "auth"


def test_yolo_auto_answers_non_blocking():
    g = AskUserGovernor(yolo=True)
    d = g.evaluate("pentest", _q("optional?", blocking=False))
    assert d.proceed is False
    assert d.answer == ""


def test_yolo_still_blocks_on_blocking():
    g = AskUserGovernor(yolo=True)
    d = g.evaluate("pentest", _q("really blocking", blocking=True))
    assert d.proceed is True


def test_validator_has_smaller_cap_by_default():
    g = AskUserGovernor()
    for i in range(3):
        assert g.evaluate("validator", _q(f"Q{i}")).proceed
        g.record_answer("validator", _q(f"Q{i}"), "a")
    assert g.evaluate("validator", _q("Q3")).refusal is not None
