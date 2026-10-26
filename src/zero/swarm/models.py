"""Versioned domain contracts for the durable swarm control plane."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


SCHEMA_VERSION = 1


class WorkState(StrEnum):
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResponseState(StrEnum):
    QUEUED = "queued"
    COLLECTING = "collecting"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkerIdentity:
    engagement_id: str
    swarm_id: str
    worker_id: str
    name: str
    generation: int = 1

    @property
    def actor_id(self) -> str:
        return (
            f"swarm-{self.swarm_id}__{self.worker_id}__"
            f"generation-{self.generation}__orchestrator"
        )


@dataclass(frozen=True)
class WorkClaim:
    work_id: str
    lease_token: str
    lease_expires_at: float


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    producer_id: str
    kind: str
    target: str
    summary: str
    content: str
    content_hash: str
    visibility: str
    work_id: str | None = None
