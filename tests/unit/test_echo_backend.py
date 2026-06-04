from zero.llm.echo import EchoBackend
from zero.llm.envelope import parse_envelope


async def test_default_script_emits_valid_envelopes():
    be = EchoBackend()
    seen: list[str] = []
    for _ in range(10):
        resp = await be.run("sys", [], [])
        env = parse_envelope(resp.text)
        seen.append(type(env).__name__)
        if seen[-1] == "FinalEnvelope":
            break
    assert seen[-1] == "FinalEnvelope"
    assert "ActionEnvelope" in seen
    assert "AskUserEnvelope" in seen


async def test_custom_script():
    be = EchoBackend(script=[{"type": "final", "answer": "x"}])
    resp = await be.run("sys", [], [])
    env = parse_envelope(resp.text)
    assert env.__class__.__name__ == "FinalEnvelope"
