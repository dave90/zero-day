from zero.ui.events import ActivityLine, ToolObservation
from zero.ui.render import _compact_observation, render_event


def test_activity_line_renders_text_verbatim():
    assert list(render_event(ActivityLine(text='  ▸ pentester #3 → "XSS" @ /x'))) == [
        '  ▸ pentester #3 → "XSS" @ /x'
    ]


def test_short_output_passes_through():
    out, lines, chars = _compact_observation("hi\nthere", max_lines=6, max_chars=400)
    assert out == "hi\nthere"
    assert lines == 0
    assert chars == 0


def test_truncates_by_line_count():
    text = "\n".join(f"line{i}" for i in range(50))
    out, hidden_lines, hidden_chars = _compact_observation(text, max_lines=6, max_chars=10_000)
    assert hidden_lines == 44
    assert hidden_chars == 0
    assert out.count("\n") == 5  # 6 lines = 5 separators


def test_truncates_by_char_count_on_single_long_line():
    text = "x" * 1000
    out, hidden_lines, hidden_chars = _compact_observation(text, max_lines=6, max_chars=400)
    assert hidden_lines == 0
    assert hidden_chars == 600
    assert len(out) == 400


def test_render_observation_emits_compact_block_with_hint():
    long_output = "\n".join(f"row {i}" for i in range(40))
    ev = ToolObservation(
        tool="sqlmap", call_id="abcd1234", ok=True, output=long_output
    )
    lines = list(render_event(ev))
    # Header carries the call_id so the user can grep audit.jsonl.
    assert "abcd1234" in lines[0]
    # Compact block: 6 lines max + a "more lines" hint.
    body_lines = [l for l in lines if l.startswith("      ") and "more" not in l]
    assert len(body_lines) <= 6
    assert any("more lines" in l and "audit.jsonl" in l for l in lines)


def test_render_short_output_has_no_truncation_hint():
    # `notes` observations are intentionally suppressed (the note text is shown at
    # proposal time), so use a regular tool here to exercise the compaction path.
    ev = ToolObservation(tool="httpx_tool", call_id="c1", ok=True, output="ok\n")
    lines = list(render_event(ev))
    assert lines  # the observation actually renders
    assert not any("more lines" in l for l in lines)
    assert not any("more chars" in l for l in lines)
