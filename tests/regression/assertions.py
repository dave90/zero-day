"""Matching + scoring for the capability regression suite — pure, no I/O.

Kept separate from the pytest plumbing so the logic is unit-testable offline
(see `tests/unit/test_regression_assertions.py`, which runs in the free default
suite). The live test in `test_regression_targets.py` calls `score_target`.

The matcher is deliberately *tolerant* of LLM nondeterminism: a target passes
when a CONFIRMED finding matches the ground truth by CWE (any-of), OR by an
endpoint hint together with a vuln-class keyword. The OR arm catches the agent
labelling the right bug with a sibling CWE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing heavy modules in the offline unit test path
    from zero.findings.schema import FindingRecord

    from tests.regression.manifest import TargetSpec


_CWE_RE = re.compile(r"cwe[-_\s]*0*(\d+)", re.IGNORECASE)


def normalize_cwe(s: str | None) -> str:
    """'cwe 79' / 'CWE_079' / 'CWE-79' -> 'CWE-79'. '' for anything CWE-less."""
    if not s:
        return ""
    m = _CWE_RE.search(s)
    return f"CWE-{int(m.group(1))}" if m else ""


def _record_cwes(rec: "FindingRecord") -> set[str]:
    cwes = {normalize_cwe(rec.finding.cwe)}
    if rec.validation is not None and rec.validation.cwe:
        cwes.add(normalize_cwe(rec.validation.cwe))
    cwes.discard("")
    return cwes


def record_matches(spec: "TargetSpec", rec: "FindingRecord") -> bool:
    """Does this finding record match the target's ground-truth vuln?

    Two arms, both deliberately precise so a PASS is a true-positive on the
    *intended* vulnerability, not just any confirmed bug:

      * Arm 1 (authoritative) — the agent's or validator's CWE intersects the
        target's expected CWE set.
      * Arm 2 (refinement) — for when the agent labels the right bug with a
        sibling CWE: an endpoint hint AND a vuln-class keyword must BOTH match.
        Both signal sources must be *defined* and *matched*; requiring a positive
        endpoint signal (not merely "no hints were given") stops a generic
        keyword like "injection" from matching an unrelated bug on an unrelated
        endpoint. A target with no `endpoint_hints` therefore relies solely on
        Arm 1 — keywords never match on their own.
    """
    # Arm 1: CWE intersection (authoritative).
    if spec.expected_cwes & _record_cwes(rec):
        return True

    # Arm 2: endpoint hint AND keyword — both must be defined and both must match.
    if spec.endpoint_hints and spec.keywords:
        endpoint = (rec.finding.endpoint or "").lower()
        ep_ok = any(h.lower() in endpoint for h in spec.endpoint_hints)
        haystack = " ".join(
            [rec.finding.title or "", rec.finding.summary or "", rec.finding.evidence or ""]
        ).lower()
        kw_ok = any(k.lower() in haystack for k in spec.keywords)
        if ep_ok and kw_ok:
            return True
    return False


@dataclass
class TargetResult:
    name: str
    difficulty: str
    container_started: bool = False
    tool_calls: int = 0
    submitted: int = 0           # finding_submit events seen
    matched_confirmed: bool = False   # right vuln AND validator-confirmed -> PASS
    matched_any: bool = False         # right vuln in any bucket (diagnostic only)
    matched_title: str | None = None
    matched_cwe: str | None = None
    skipped: bool = False
    skip_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.matched_confirmed

    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"

    def explain(self) -> str:
        if self.matched_confirmed:
            return (
                f"{self.name}: confirmed {self.matched_cwe or '?'} "
                f"({self.matched_title!r})"
            )
        if self.matched_any:
            return (
                f"{self.name}: matched the expected vuln but only in a "
                f"non-confirmed bucket (validator did not confirm); "
                f"submitted={self.submitted}, tool_calls={self.tool_calls}"
            )
        return (
            f"{self.name}: no confirmed finding matched the expected vuln "
            f"(submitted={self.submitted}, tool_calls={self.tool_calls})"
        )


def score_target(spec: "TargetSpec", store, audit: list[dict]) -> TargetResult:
    """Build a TargetResult from the on-disk findings + audit log of a run."""
    kinds = [r.get("kind") for r in audit]
    res = TargetResult(name=spec.name, difficulty=spec.difficulty)
    res.container_started = "container_started" in kinds
    res.tool_calls = kinds.count("tool_proposal")
    res.submitted = kinds.count("finding_submit")

    confirmed = store.all_records("confirmed")
    others = store.all_records("pending") + store.all_records("rejected")

    for rec in confirmed:
        if record_matches(spec, rec):
            res.matched_confirmed = True
            res.matched_any = True
            res.matched_title = rec.finding.title
            cwes = _record_cwes(rec) & spec.expected_cwes
            res.matched_cwe = next(iter(cwes), next(iter(_record_cwes(rec)), None))
            return res

    res.matched_any = any(record_matches(spec, rec) for rec in others)
    return res


# ============================ blind coverage mode ============================
# Used by tests/regression/test_regression_blind.py: instead of one pass/fail,
# score HOW MANY of a target's known vulns (`spec.expected_vulns`) the agent
# confirmed. Never raises — purely a report.


@dataclass
class CoverageResult:
    name: str
    difficulty: str
    found: list[str] = field(default_factory=list)    # VulnSpec.name values confirmed
    missed: list[str] = field(default_factory=list)   # VulnSpec.name values not found
    container_started: bool = False
    tool_calls: int = 0
    note: str = ""                                     # e.g. a plumbing problem

    @property
    def total(self) -> int:
        return len(self.found) + len(self.missed)

    @property
    def pct(self) -> int:
        return (100 * len(self.found) // self.total) if self.total else 0

    def summary(self) -> str:
        s = f"{self.name}: {len(self.found)}/{self.total} ({self.pct}%)"
        if self.found:
            s += f"  found[{','.join(self.found)}]"
        if self.missed:
            s += f"  missed[{','.join(self.missed)}]"
        if self.note:
            s += f"  — {self.note}"
        return s


def score_coverage(spec: "TargetSpec", store, audit: list[dict]) -> CoverageResult:
    """How many of `spec.expected_vulns` were confirmed? A vuln counts as found
    when ANY validator-confirmed finding matches it (same matcher as the strict
    suite). Never raises."""
    kinds = [r.get("kind") for r in audit]
    res = CoverageResult(name=spec.name, difficulty=spec.difficulty)
    res.container_started = "container_started" in kinds
    res.tool_calls = kinds.count("tool_proposal")

    confirmed = store.all_records("confirmed")
    for vuln in spec.expected_vulns:
        if any(record_matches(vuln, rec) for rec in confirmed):
            res.found.append(vuln.name)
        else:
            res.missed.append(vuln.name)

    if not res.container_started:
        res.note = "container did not start"
    elif res.tool_calls == 0:
        res.note = "agent made no tool calls"
    return res
