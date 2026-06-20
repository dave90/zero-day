import json

from zero.agents.validator import load_system_prompt, parse_validation
from zero.findings.schema import Validation
from zero.llm.envelope import FinalEnvelope


def test_system_prompt_forbids_native_tools_and_mandates_action_envelopes():
    """Regression: the validator was reaching for Claude Code's native tools
    (Bash/curl/WebFetch/Monitor) — which `claude -p` denies non-interactively —
    instead of our `action` envelope, then parking findings pending with
    'tools unavailable'. The prompt must explicitly forbid native tools and
    tell it to route through `action` envelopes (the rule the Pentest prompt
    already has)."""
    prompt = load_system_prompt()
    low = prompt.lower()
    assert "built-in" in low or "native" in low, prompt
    assert "action" in low and "envelope" in low
    # Must name the real escape hatch so it actually tries instead of giving up.
    assert "http_replay" in low
    # Must steer it away from concluding pending merely because "no tools".
    assert "unavailable" in low


def test_parse_validation_plain_json():
    obj = {
        "verdict": "confirmed",
        "rationale": "x",
        "reproducer": "y",
        "cvss40_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
        "cvss40_score": 9.8,
        "cwe": "CWE-89",
    }
    v = parse_validation(FinalEnvelope(answer=json.dumps(obj)))
    assert isinstance(v, Validation)
    assert v.verdict == "confirmed"
    assert v.cvss40_score == 9.8


def test_parse_validation_fenced():
    obj = {"verdict": "rejected", "rationale": "no", "reproducer": "n/a"}
    text = f"```json\n{json.dumps(obj)}\n```"
    v = parse_validation(FinalEnvelope(answer=text))
    assert v is not None and v.verdict == "rejected"


def test_parse_validation_bad_returns_none():
    assert parse_validation(FinalEnvelope(answer="not json at all")) is None
    assert parse_validation(FinalEnvelope(answer="{}")) is None


def test_parse_validation_salvages_prose_verdict():
    """Regression: on a `--resume` session the validator often drops the JSON-answer
    format and emits a prose verdict like 'VALIDATION: CONFIRMED — …'. A clearly-stated
    verdict must be salvaged (verdict + prose rationale), not discarded as `pending`."""
    # Real prose finals observed in a session-resume run.
    for prose in (
        "VALIDATION: CONFIRMED — Reflected XSS in the error page (CWE-79, medium).",
        "VALIDATION — VERDICT: CONFIRMED",
        "VALIDATION: CONFIRMED (high confidence)",
    ):
        v = parse_validation(FinalEnvelope(answer=prose))
        assert v is not None and v.verdict == "confirmed", prose
        assert v.rationale == prose                       # prose kept as rationale

    # An explicit 'rejected' marker wins over an earlier mention of another word.
    v = parse_validation(
        FinalEnvelope(answer="Could not reproduce. VERDICT: rejected — no reflection.")
    )
    assert v is not None and v.verdict == "rejected"

    # No verdict word at all → still None (don't invent a verdict).
    assert parse_validation(FinalEnvelope(answer="the weather is nice today")) is None


def test_validator_recovers_when_model_uses_sibling_key():
    """End-to-end: the real-world bug was claude emitting
        {"type":"final","validation":{...}}
    The envelope parser now absorbs that sibling into `answer`. This
    test proves the resulting FinalEnvelope is parseable as a real
    Validation — i.e. the validator can still produce a verdict in this
    case instead of parking the finding as pending."""
    from zero.llm.envelope import parse_envelope

    raw = json.dumps(
        {
            "type": "final",
            "validation": {
                "verdict": "confirmed",
                "rationale": "reflected XSS confirmed",
                "reproducer": "GET /x?p=<script>alert(1)</script>",
                "cvss40_vector": (
                    "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:P/VC:L/VI:L"
                    "/VA:N/SC:N/SI:N/SA:N"
                ),
                "cvss40_score": 6.1,
                "cwe": "CWE-79",
            },
        }
    )
    env = parse_envelope(raw)
    assert isinstance(env, FinalEnvelope)
    v = parse_validation(env)
    assert v is not None
    assert v.verdict == "confirmed"
    assert v.cvss40_score == 6.1
