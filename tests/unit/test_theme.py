"""Theme colorizer: it adds style WITHOUT ever mutating the text.

The whole design rests on one invariant — `stylize()` only attaches style
classes, never edits characters — so the byte-for-byte render tests stay valid.
These tests pin that invariant plus the marker→class mapping, the CVSS severity
thresholds, and the ctx usage bar.
"""
from __future__ import annotations

from prompt_toolkit.formatted_text import fragment_list_to_text

from zero.ui import theme


def _classes(ft) -> list[str]:
    return [style for style, _text in ft]


# ----------------------------------------------------- the core invariant

SAMPLE_LINES = [
    "  → nmap  [active]  target=10.0.0.5",
    "  → sqlmap  [exploit]  url=http://x/?id=1",
    "  ← nmap ok  [call a1b2]",
    "  ← curl FAIL (truncated)  [call deadbeef]",
    "  ↳ Mapping the login flow",
    "    Will probe /login next",                       # reasoning continuation
    "   CVSS 4.0:  7.5  CVSS:4.0/AV:N/AC:L",
    "★ CONFIRMED — IDOR on /api/orders/{id}",
    "✗ REJECTED  — not reproducible",
    "⤴ DUPLICATE — same as a1b2c3d",
    "… PENDING   — needs a second account",
    "      … (12 more lines — full output in audit.jsonl)",
    "  ✓ [pentester #3] EXPLOITED — /x (12s)",
    "  ⟳ [validator #4] started — /login  «auth bypass»",
    "  ⨯ turn cancelled: interrupted_by_user",
    "  ? Which account should I use? — options: 1) alice, 2) bob",
    "╭─ goal ─────",
    "│  find all the vulns",
    "═" * 60,
    "  ENGAGEMENT REPORT",
    "zero · mode c · backend claude · model x · tools default · ctx-window 200,000",
    "an utterly unmarked line passes straight through",
]


def test_stylize_never_mutates_text():
    for line in SAMPLE_LINES:
        assert fragment_list_to_text(theme.stylize(line)) == line


# ----------------------------------------------------- marker → class map

def test_tool_proposal_colors_tool_risk_and_args():
    ft = theme.stylize("  → sqlmap  [exploit]  url=http://x/?id=1")
    classes = _classes(ft)
    assert "class:accent" in classes          # tool name
    assert "class:risk.exploit" in classes    # risk badge by level
    assert "class:dim" in classes             # args


def test_observation_ok_vs_fail():
    assert "class:ok" in _classes(theme.stylize("  ← nmap ok  [call a1b2]"))
    assert "class:fail" in _classes(theme.stylize("  ← curl FAIL  [call deadbeef]"))


def test_finding_labels_map_to_verdict_classes():
    assert _classes(theme.stylize("★ CONFIRMED — x")) == ["class:finding.confirmed"]
    assert _classes(theme.stylize("✗ REJECTED — x")) == ["class:finding.rejected"]
    assert _classes(theme.stylize("… PENDING — x")) == ["class:finding.pending"]
    assert _classes(theme.stylize("⤴ DUPLICATE — x")) == ["class:finding.duplicate"]


def test_truncation_hint_is_dim_not_pending():
    # "… (N more …)" must NOT be mistaken for a pending "…" item.
    assert _classes(theme.stylize("      … (5 more lines)")) == ["class:dim"]


def test_unmarked_line_passes_through_unstyled():
    ft = theme.stylize("just some text")
    assert _classes(ft) == [""]


# ----------------------------------------------------- CVSS severity

def test_cvss_severity_thresholds():
    assert theme._sev_class(9.8) == "sev.crit"
    assert theme._sev_class(7.5) == "sev.high"
    assert theme._sev_class(5.0) == "sev.med"
    assert theme._sev_class(2.1) == "sev.low"


def test_cvss_line_colors_score_by_severity():
    ft = theme.stylize("   CVSS 4.0:  9.1  CVSS:4.0/AV:N")
    assert "class:sev.crit" in _classes(ft)


def test_risk_class_helper():
    assert theme.risk_class("destructive") == "risk.destructive"
    assert theme.risk_class("nonsense") == "dim"


# ----------------------------------------------------- ctx bar + spinner

def test_ctx_bar_empty_without_window():
    assert list(theme.ctx_bar(5_000, 0)) == []


def test_ctx_bar_color_by_pressure():
    assert "class:bar.ok" in _classes(theme.ctx_bar(10_000, 100_000))    # 10%
    assert "class:bar.warn" in _classes(theme.ctx_bar(80_000, 100_000))  # 80%
    assert "class:bar.hot" in _classes(theme.ctx_bar(95_000, 100_000))   # 95%
    # the percentage label is preserved verbatim from fmt_context
    assert "100k" in fragment_list_to_text(theme.ctx_bar(10_000, 100_000))


def test_spinner_frame_is_one_braille_glyph():
    assert theme.spinner_frame() in theme.SPINNER_FRAMES


# ----------------------------------------------------- markdown rendering

def test_has_markdown_detects_constructs_and_ignores_plain():
    assert theme.has_markdown("plain **bold** here")
    assert theme.has_markdown("uses `code`")
    assert theme.has_markdown("## a header")
    assert theme.has_markdown("- a bullet")
    assert theme.has_markdown("1. numbered")
    assert not theme.has_markdown("zero · mode c · backend claude")
    assert not theme.has_markdown("ready — type a goal")


def test_render_markdown_strips_markers_keeps_text():
    md = "## Title\nA line with **bold** and `code` and *it*.\n1. **One** with `x`\n- two"
    flat = fragment_list_to_text(theme.render_markdown(md))
    # The syntax markers are gone…
    assert "##" not in flat
    assert "**" not in flat
    assert "`" not in flat
    # …but every word survives, and list glyphs render.
    for word in ["Title", "bold", "code", "One", "two"]:
        assert word in flat
    assert "•" in flat       # bullet rendered
    assert "1. " in flat     # numbered kept


def test_render_markdown_styles_header_bold_and_code():
    ft = theme.render_markdown("## Heading\nuse `snippet` now")
    classes = {style for style, _ in ft}
    assert "class:md.h2" in classes
    assert "class:md.code" in classes


def test_render_markdown_peels_leading_tag():
    ft = theme.render_markdown("\n[final] ## Done\nbody")
    assert ("class:dim", "[final] ") in ft
    assert fragment_list_to_text(ft).lstrip("\n").startswith("[final] Done")


def test_has_markdown_detects_tables_and_fences():
    assert theme.has_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert theme.has_markdown("Proof\n```\ncode\n```")


def test_render_markdown_table_aligns_and_drops_pipes():
    md = "| | |\n|---|---|\n| Severity | High |\n| CWE | CWE-79 |"
    ft = theme.render_markdown(md)
    flat = fragment_list_to_text(ft)
    assert "|---|" not in flat and "| Severity |" not in flat   # raw pipes gone
    assert "Severity" in flat and "High" in flat and "CWE-79" in flat
    # header-less table → first column rendered as bold labels
    assert ("class:md.bold", "Severity") in ft


def test_render_markdown_table_with_header():
    md = "| Name | Val |\n|---|---|\n| a | 1 |"
    ft = theme.render_markdown(md)
    assert ("class:md.bold", "Name") in ft        # header row bold
    assert "1" in fragment_list_to_text(ft)


def test_render_markdown_code_block_hides_fences():
    ft = theme.render_markdown("PoC\n```\ncurl http://x\n```\nend")
    flat = fragment_list_to_text(ft)
    assert "```" not in flat                       # fence markers hidden
    assert "curl http://x" in flat
    assert ("class:md.code", "curl http://x") in ft
