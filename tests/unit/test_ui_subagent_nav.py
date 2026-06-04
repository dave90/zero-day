"""Live subagent list: ↑/↓ selection + Enter expand/collapse (M8 UX)."""
from __future__ import annotations

import asyncio
from pathlib import Path

from zero.ui.app import ZeroUI


class _FakeSubs:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return self._snap


class _StubOrch:
    def __init__(self, tmp_path: Path, subs) -> None:
        self.ui_queue: asyncio.Queue = asyncio.Queue()
        self.cfg = type("Cfg", (), {"cwd": tmp_path})()
        self.subagents = subs

    def request_cancel_turn(self) -> bool:
        return False


def _snap():
    return [
        {"id": "#1", "kind": "pentester", "target": "/a", "idea": "SQLi in sort param",
         "turn": 3, "elapsed": 12.0, "status": "running"},
        {"id": "#2", "kind": "pentester", "target": "/b", "idea": "auth bypass",
         "turn": 1, "elapsed": 4.0, "status": "running"},
    ]


def test_toolbar_lists_running_with_default_selection(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path, _FakeSubs(_snap())))
    bar = ui._toolbar()
    assert "#1 pentester /a" in bar
    assert "#2 pentester /b" in bar
    assert "›⏳ #1" in bar          # first row selected by default
    assert "[↑/↓ select" in bar


def test_toolbar_selection_and_expand(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path, _FakeSubs(_snap())))
    ui._sel_idx = 1                  # ↓ moved selection to the 2nd subagent
    bar = ui._toolbar()
    assert "›⏳ #2" in bar
    assert "goal:" not in bar        # collapsed

    ui._sel_expanded = True          # Enter expands
    bar = ui._toolbar()
    assert "↳ goal: auth bypass" in bar
    assert "turn 1" in bar


def test_expanded_description_wraps_not_truncated(tmp_path: Path, monkeypatch):
    long_idea = (
        "Reflected XSS / HTML injection via the uid parameter that reflects "
        "unescaped into multiple sink contexts including an inline script and "
        "a quoted attribute value"
    )
    snap = [{"id": "#1", "kind": "pentester", "target": "/x", "idea": long_idea,
             "turn": 1, "elapsed": 1.0, "status": "running"}]
    ui = ZeroUI(_StubOrch(tmp_path, _FakeSubs(snap)))
    monkeypatch.setattr(ui, "_term_width", lambda: 40)
    ui._sel_expanded = True
    bar = ui._toolbar()
    # The full goal is present (the tail words would be cut if truncated).
    assert "inline script" in bar
    assert "attribute value" in bar
    # And the wrapped goal lines respect the width (nothing cut off-screen).
    goal_lines = [ln for ln in bar.splitlines() if ln.strip().startswith("↳ goal:")]
    assert goal_lines and all(len(ln) <= 40 for ln in goal_lines)


def test_toolbar_resets_when_no_subagents(tmp_path: Path):
    ui = ZeroUI(_StubOrch(tmp_path, _FakeSubs([])))
    ui._sel_idx = 5
    ui._sel_expanded = True
    bar = ui._toolbar()
    assert ui._sel_idx == 0 and ui._sel_expanded is False
    # only the status line remains (terse footer: turn/mode/approvals/findings)
    assert "approvals waiting:" in bar
