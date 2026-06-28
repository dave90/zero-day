"""Offline, free tests for the BountyBench pilot scorecard aggregation."""
from __future__ import annotations

import json

from benchmarks.bountybench.evaluate import (
    RunRecord,
    Scorecard,
    aggregate,
    load_records,
)


def _records() -> list[RunRecord]:
    return [
        RunRecord("lunary", 0, "exploit", success=True, dollar_value=1080,
                  input_tokens=100, output_tokens=20),
        RunRecord("mlflow", 0, "exploit", success=False, dollar_value=500,
                  input_tokens=50, output_tokens=10, error="runner exit 1"),
        RunRecord("lunary", 0, "detect", success=True, dollar_value=1080,
                  input_tokens=200, output_tokens=40),
    ]


def test_aggregate_counts_and_dollars():
    card = aggregate(_records())
    assert card.total == 3
    assert card.solved == 2
    # Only successful runs are credited.
    assert card.dollars_earned == 1080 + 1080
    assert card.dollars_available == 1080 + 500 + 1080
    assert card.input_tokens == 350
    assert card.output_tokens == 70


def test_by_task_type_grouping():
    card = aggregate(_records())
    groups = card.by_task_type()
    assert set(groups) == {"exploit", "detect"}
    assert len(groups["exploit"]) == 2
    assert len(groups["detect"]) == 1


def test_to_dict_shape():
    d = aggregate(_records()).to_dict()
    assert d["solved"] == 2 and d["total"] == 3
    assert d["by_task_type"]["exploit"]["solved"] == 1
    assert d["by_task_type"]["exploit"]["total"] == 2
    assert d["by_task_type"]["detect"]["dollars_earned"] == 1080
    assert len(d["runs"]) == 3


def test_markdown_renders_summary_and_marks():
    md = aggregate(_records()).to_markdown()
    assert "Solved 2/3" in md
    assert "✅ PASS" in md and "❌ fail" in md
    assert "runner exit 1" in md  # error surfaced in the row


def test_empty_scorecard_is_safe():
    card = Scorecard()
    assert card.total == 0 and card.solved == 0
    assert card.dollars_earned == 0
    assert "Solved 0/0 (0%)" in card.to_markdown()


def test_load_records_roundtrip(tmp_path):
    recs = _records()
    p = tmp_path / "results.jsonl"
    p.write_text(
        "\n".join(json.dumps(aggregate([r]).to_dict()["runs"][0] | {
            "system": r.system, "bounty_number": r.bounty_number, "task_type": r.task_type,
        }) for r in recs) + "\n",
        encoding="utf-8",
    )
    loaded = load_records(p)
    assert len(loaded) == 3
    assert aggregate(loaded).solved == 2
