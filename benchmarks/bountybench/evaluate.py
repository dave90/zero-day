"""Aggregate BountyBench pilot results into a scorecard (Markdown + JSON).

`run_pilot.py` writes one JSON record per bounty run (the authoritative verdict from
BountyBench's saved run log + the bounty's dollar reward + token usage). This module
turns a list of those records into a scorecard grouped by task type, mirroring the style
of `tests/regression/assertions.py` and the regression terminal summary.

The aggregation (`aggregate`) is a pure function so it's unit-tested offline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunRecord:
    """One bounty run. Mirrors what run_pilot writes per line."""

    system: str
    bounty_number: int
    task_type: str
    success: bool
    dollar_value: float = 0.0    # bounty reward, credited only when success
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    error: str = ""

    @property
    def label(self) -> str:
        return f"{self.system}#{self.bounty_number}/{self.task_type}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRecord":
        return cls(
            system=d["system"],
            bounty_number=int(d["bounty_number"]),
            task_type=d["task_type"],
            success=bool(d.get("success", False)),
            dollar_value=float(d.get("dollar_value", 0) or 0),
            input_tokens=int(d.get("input_tokens", 0) or 0),
            output_tokens=int(d.get("output_tokens", 0) or 0),
            duration_s=float(d.get("duration_s", 0) or 0),
            error=d.get("error", "") or "",
        )


@dataclass
class Scorecard:
    records: list[RunRecord] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def solved(self) -> int:
        return sum(1 for r in self.records if r.success)

    @property
    def dollars_earned(self) -> float:
        return sum(r.dollar_value for r in self.records if r.success)

    @property
    def dollars_available(self) -> float:
        return sum(r.dollar_value for r in self.records)

    @property
    def input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.records)

    @property
    def output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.records)

    def by_task_type(self) -> dict[str, list[RunRecord]]:
        out: dict[str, list[RunRecord]] = {}
        for r in self.records:
            out.setdefault(r.task_type, []).append(r)
        return out

    def to_dict(self) -> dict[str, Any]:
        groups = {
            t: {
                "solved": sum(1 for r in rs if r.success),
                "total": len(rs),
                "dollars_earned": sum(r.dollar_value for r in rs if r.success),
            }
            for t, rs in self.by_task_type().items()
        }
        return {
            "total": self.total,
            "solved": self.solved,
            "dollars_earned": self.dollars_earned,
            "dollars_available": self.dollars_available,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "by_task_type": groups,
            "runs": [
                {
                    "label": r.label,
                    "success": r.success,
                    "dollar_value": r.dollar_value,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "duration_s": r.duration_s,
                    "error": r.error,
                }
                for r in self.records
            ],
        }

    def to_markdown(self) -> str:
        lines = ["# BountyBench pilot scorecard", ""]
        pct = (100 * self.solved // self.total) if self.total else 0
        lines.append(
            f"**Solved {self.solved}/{self.total} ({pct}%)** · "
            f"**${self.dollars_earned:,.0f} / ${self.dollars_available:,.0f}** earned · "
            f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens"
        )
        for task_type, rs in sorted(self.by_task_type().items()):
            s = sum(1 for r in rs if r.success)
            d = sum(r.dollar_value for r in rs if r.success)
            lines += ["", f"## {task_type}  ({s}/{len(rs)}, ${d:,.0f})", ""]
            lines.append("| bounty | result | $ | tokens (in/out) | note |")
            lines.append("|---|---|---|---|---|")
            for r in rs:
                mark = "✅ PASS" if r.success else "❌ fail"
                note = r.error or ""
                lines.append(
                    f"| {r.system}#{r.bounty_number} | {mark} | "
                    f"{r.dollar_value:,.0f} | {r.input_tokens:,}/{r.output_tokens:,} | {note} |"
                )
        return "\n".join(lines) + "\n"


def aggregate(records: list[RunRecord]) -> Scorecard:
    """Pure aggregation — the unit-tested core."""
    return Scorecard(records=list(records))


def load_records(results_path: Path) -> list[RunRecord]:
    """Read a JSONL results file (one run per line) into RunRecords."""
    out: list[RunRecord] = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(RunRecord.from_dict(json.loads(line)))
    return out


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Aggregate a BountyBench pilot results file.")
    ap.add_argument("results", type=Path, help="path to the pilot results .jsonl")
    ap.add_argument("--json-out", type=Path, help="also write the scorecard as JSON here")
    args = ap.parse_args()

    card = aggregate(load_records(args.results))
    print(card.to_markdown())
    if args.json_out:
        args.json_out.write_text(json.dumps(card.to_dict(), indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
