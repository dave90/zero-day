import json

import pytest

from zero.llm.envelope import (
    ActionEnvelope,
    AskUserEnvelope,
    EnvelopeError,
    FinalEnvelope,
    parse_envelope,
)


def test_action_envelope_minimal():
    e = parse_envelope(json.dumps({"type": "action", "tool": "x", "args": {}}))
    assert isinstance(e, ActionEnvelope)
    assert e.tool == "x"
    assert e.args == {}
    assert e.thought == ""


def test_action_envelope_with_thought():
    e = parse_envelope(
        json.dumps(
            {"type": "action", "tool": "notes", "args": {"action": "read"}, "thought": "hi"}
        )
    )
    assert isinstance(e, ActionEnvelope)
    assert e.thought == "hi"


def test_ask_user_envelope_defaults():
    e = parse_envelope(json.dumps({"type": "ask_user", "question": "which?"}))
    assert isinstance(e, AskUserEnvelope)
    assert e.options == []
    assert e.free_form is True
    assert e.blocking is True


def test_final_envelope():
    e = parse_envelope(json.dumps({"type": "final", "answer": "done"}))
    assert isinstance(e, FinalEnvelope)
    assert e.answer == "done"


def test_tolerates_trailing_data_after_first_object():
    # Regression: a subagent emitted a valid envelope followed by extra content
    # (a second object), and json.loads failed with "Extra data", killing the
    # subagent. We now decode the first object and ignore the trailing data.
    e = parse_envelope('{"type": "final", "answer": "done"}{"stray": 1}')
    assert isinstance(e, FinalEnvelope)
    assert e.answer == "done"

    e2 = parse_envelope(
        '{"type": "action", "tool": "http_replay", "args": {"x": 1}}\nleftover prose'
    )
    assert isinstance(e2, ActionEnvelope)
    assert e2.tool == "http_replay"


def test_rejects_extra_fields():
    # action and ask_user still strict; final is intentionally lenient.
    with pytest.raises(EnvelopeError):
        parse_envelope(
            json.dumps({"type": "action", "tool": "x", "args": {}, "extra": 1})
        )


def test_final_tolerates_extra_fields():
    # Real-world: claude sometimes emits commentary or a sibling key.
    # The final envelope must absorb these — the alternative is parking
    # legitimate findings as pending for cosmetic reasons.
    e = parse_envelope(
        json.dumps({"type": "final", "answer": "x", "extra": 1, "note": "ok"})
    )
    assert isinstance(e, FinalEnvelope)
    assert e.answer == "x"


def test_final_absorbs_validation_sibling_key():
    # The exact bug a user hit: claude emitted
    #   {"type":"final","validation":{...}}
    # instead of putting the validation JSON inside "answer". We treat
    # `validation`/`result`/`payload`/`data` siblings as the payload.
    validation = {"verdict": "confirmed", "rationale": "ok", "reproducer": "x"}
    e = parse_envelope(
        json.dumps({"type": "final", "validation": validation})
    )
    assert isinstance(e, FinalEnvelope)
    parsed = json.loads(e.answer)
    assert parsed["verdict"] == "confirmed"


def test_final_stringifies_object_answer():
    # Some models emit `answer` as an object directly. We stringify
    # rather than fail.
    e = parse_envelope(json.dumps({"type": "final", "answer": {"k": 1}}))
    assert isinstance(e, FinalEnvelope)
    assert json.loads(e.answer) == {"k": 1}


def test_rejects_missing_required():
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps({"type": "action", "tool": "x"}))


def test_rejects_unknown_type():
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps({"type": "wat", "x": 1}))


def test_rejects_non_json():
    with pytest.raises(EnvelopeError):
        parse_envelope("hello world")


def test_repairs_trailing_comma():
    # The exact slip that made real runs fail every turn: a trailing comma
    # ("Expecting property name enclosed in double quotes"). The repair pass
    # recovers it instead of killing the turn.
    e = parse_envelope('{"type": "action", "tool": "curl_raw", "args": {"url": "x"},}')
    assert isinstance(e, ActionEnvelope)
    assert e.tool == "curl_raw"
    e2 = parse_envelope('{"type": "final", "answer": "done",}')
    assert isinstance(e2, FinalEnvelope)


def test_repairs_python_literals():
    e = parse_envelope(
        '{"type": "action", "tool": "x", "args": {"follow": True, "n": None}}'
    )
    assert isinstance(e, ActionEnvelope)
    assert e.args == {"follow": True, "n": None}


def test_repair_leaves_string_contents_untouched():
    # A trailing-comma-looking sequence or Python word INSIDE a string value must
    # survive verbatim — the repair only touches structure outside strings.
    e = parse_envelope(json.dumps({"type": "final", "answer": "True, None,]"}))
    assert isinstance(e, FinalEnvelope)
    assert e.answer == "True, None,]"


def test_unrepairable_json_includes_raw_in_error():
    # Balanced braces (so _extract_json accepts it) but invalid JSON the repair
    # pass can't fix (missing comma) — the error must quote the raw text.
    with pytest.raises(EnvelopeError, match="raw:"):
        parse_envelope('{"type": "action" "tool": "x", "args": {}}')


def test_strips_code_fence():
    body = json.dumps({"type": "final", "answer": "x"})
    e = parse_envelope(f"```json\n{body}\n```")
    assert isinstance(e, FinalEnvelope)


def test_extracts_object_from_prose():
    body = json.dumps({"type": "final", "answer": "x"})
    e = parse_envelope(f"sure, here it is: {body} -- ok?")
    assert isinstance(e, FinalEnvelope)


def test_empty_response_rejected():
    with pytest.raises(EnvelopeError):
        parse_envelope("   ")


def test_action_tool_must_be_string():
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps({"type": "action", "tool": "", "args": {}}))


def test_action_args_must_be_object():
    with pytest.raises(EnvelopeError):
        parse_envelope(json.dumps({"type": "action", "tool": "x", "args": []}))
