"""Finding / Validation pydantic models. Frontmatter on disk."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field


Verdict = Literal["confirmed", "rejected", "pending"]


class Finding(BaseModel):
    """What the Pentest agent submits."""

    title: str
    summary: str
    endpoint: str = Field(description="path + method, e.g. 'GET /api/orders/{id}'")
    method: str = Field(default="GET", description="HTTP method")
    cwe: str = Field(default="CWE-?", description="CWE identifier, e.g. 'CWE-639'")
    severity_hint: str = Field(default="medium", description="agent's hint; Validator scores")
    evidence: str = Field(description="reproducer steps, raw HTTP, payloads, observations")
    affected_credentials: list[str] = Field(default_factory=list)
    notes: str = ""

    # Dedup hint: the agent SHOULD fill this; we'll compute one in dedup.py
    # too, but the agent's perspective is useful for grouping.
    payload_shape: str = Field(default="", description="e.g. 'sqlmap/boolean-or-union'")


class Validation(BaseModel):
    """What the Validator agent produces."""

    verdict: Verdict
    rationale: str
    reproducer: str = Field(description="exact steps that reproduce the finding (or refute it)")
    cvss40_vector: str = Field(
        default="",
        description="CVSS 4.0 vector, e.g. 'CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N'",
    )
    cvss40_score: float = Field(default=0.0, ge=0.0, le=10.0)
    cwe: str = ""  # may refine the agent's CWE
    additional_evidence: str = ""


class FindingRecord(BaseModel):
    """What ends up on disk: a Finding + its Validation + housekeeping."""

    id: str
    finding: Finding
    validation: Validation | None = None
    dedup_hash: str
    duplicate_of: str | None = None
    created_at: float = Field(default_factory=time.time)
    submitted_by: str = "pentest"
