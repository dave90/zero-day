"""Fixed-size live peer swarm over one engagement service owner."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any

from zero.findings.report import write_report
from zero.swarm.models import WorkerIdentity
from zero.swarm.store import SwarmStore
from zero.swarm.summarizer import SwarmSummarizer, deterministic_summary
from zero.swarm.worker import SwarmWorker
from zero.ui.events import (
    AgentTextDelta,
    AgentTextEnd,
    GoalBanner,
    SessionSummary,
    Status,
    SwarmPhase,
)

if TYPE_CHECKING:
    from zero.orchestrator.orchestrator import Orchestrator


_LANES = (
    "surface mapping and exposure discovery",
    "authentication and access control",
    "server-side injection and request handling",
    "client-side and business-logic abuse",
)


class SwarmRuntime:
    """Deterministic lifecycle coordinator; offensive decisions stay in peer loops."""

    def __init__(self, services: "Orchestrator", project_context: str | None) -> None:
        self.services = services
        self.project_context = project_context
        # Existing focused-child runners read this engagement context from their
        # shared service owner. There is still only one container/router/store.
        self.services._project_ctx = project_context
        self.size = services.cfg.swarm_size
        self.store = SwarmStore(services.cfg.cwd / ".pentest" / "swarm" / "state.sqlite3")
        self.identities: list[WorkerIdentity] = []
        self.lanes: dict[str, str] = {}
        self.workers: list[SwarmWorker] = []
        self._tasks: set[asyncio.Task] = set()
        self._event = asyncio.Event()
        self._stopping = False
        self._cycle_count = 0
        self._current_cycle_id = ""
        self._phase = "idle"
        for index in range(1, self.size + 1):
            worker_id = f"worker-{index}"
            lane = _LANES[(index - 1) % len(_LANES)]
            identity = WorkerIdentity(
                engagement_id=self.store.engagement_id,
                swarm_id=self.store.swarm_id,
                worker_id=worker_id,
                name=f"zero-{index}",
            )
            self.identities.append(identity)
            self.lanes[worker_id] = lane
            self.store.register_worker(
                worker_id, identity.actor_id, identity.name,
                generation=identity.generation, lane=lane,
            )
        self.store.retire_workers_except(identity.worker_id for identity in self.identities)

    def _backend_for(self, identity: WorkerIdentity):
        factory = getattr(self.services, "_swarm_backend_factory", None)
        return factory(identity) if factory is not None else self.services._build_backend()

    def signal_event(self) -> None:
        self._event.set()

    async def await_event(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return
        finally:
            self._event.clear()

    async def run(self) -> None:
        """Process the CLI goal and subsequent user messages one FIFO cycle at a time."""
        goal = self.services.cfg.goal
        while not self._stopping:
            if not goal:
                goal = await self.services.user_queue.get()
            await self.run_cycle(goal)
            goal = None

    async def run_cycle(self, goal: str) -> str:
        initial_cycle = self._cycle_count == 0
        findings_before = self._findings_fingerprint()
        cycle_id = self.store.start_cycle(goal, [i.worker_id for i in self.identities])
        self._current_cycle_id = cycle_id
        self._phase = "bootstrap" if initial_cycle else "follow-up"
        await self.services._emit(GoalBanner(text=goal))
        self.workers = [
            SwarmWorker(
                self,
                ident,
                self._backend_for(ident),
                bootstrap_required=initial_cycle,
            )
            for ident in self.identities
        ]
        await self.services._emit(Status(turn=0, mode="swarm"))
        await self.services._emit(SwarmPhase(
            phase=self._phase,
            size=self.size,
            text=(
                "Peers are negotiating ownership before target testing.\n"
                "Live coordination: /agents · /work · /messages · /groups"
                if initial_cycle else
                "All peers are reviewing the new operator message against shared state."
            ),
        ))
        tasks = {
            asyncio.create_task(worker.run(goal, cycle_id), name=f"swarm-{worker.identity.worker_id}"):
            worker for worker in self.workers
        }
        self._tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.difference_update(tasks)
        for worker, result in zip(self.workers, results):
            if isinstance(result, BaseException):
                self.services.audit.write(
                    "swarm_worker_error", worker_id=worker.identity.worker_id,
                    error=f"{type(result).__name__}: {result}",
                )
                cycle = self.store.cycle(cycle_id)
                if not any(c["worker_id"] == worker.identity.worker_id for c in cycle["contributions"]):
                    self.store.contribute(
                        cycle_id, worker.identity.worker_id,
                        f"worker failed: {type(result).__name__}: {result}", status="unavailable",
                    )

        self._phase = "consolidating"
        cycle = self.store.cycle(cycle_id)
        expected = set(cycle["expected_workers"])
        contributed = {c["worker_id"] for c in cycle["contributions"]}
        await self.services._emit(SwarmPhase(
            phase="consolidating",
            size=self.size,
            text=f"Collected {len(contributed)}/{len(expected)} peer contributions; producing one response.",
        ))
        snapshot = {
            "cycle_id": cycle_id,
            "user_message": goal,
            "contributions": cycle["contributions"],
            "missing_workers": sorted(expected - contributed),
            "findings": {
                verdict: [
                    record.model_dump(mode="json")
                    for record in self.services.findings_store.all_records(verdict)
                ]
                for verdict in ("confirmed", "rejected", "pending")
            },
            "unresolved_work": [
                w for w in self.store.list_work()
                if w["state"] not in {"completed", "failed", "cancelled"}
            ],
            "candidates": self.store.candidates(),
        }
        summary = await self._summarize(snapshot)
        self.store.complete_cycle(cycle_id, summary)
        findings_changed = self._findings_fingerprint() != findings_before
        if initial_cycle or findings_changed:
            await self._write_report()
        else:
            self.services.audit.write(
                "swarm_followup_response", cycle_id=cycle_id, report_unchanged=True
            )
        await self.services._emit(AgentTextDelta(text=f"\n{summary}\n"))
        await self.services._emit(AgentTextEnd())
        self._phase = "ready"
        await self.services._emit(Status(turn=0, mode="swarm"))
        self._cycle_count += 1
        return summary

    def ui_snapshot(self) -> dict[str, int | str]:
        """Small live projection for the toolbar; authoritative data stays in SQLite."""
        workers = self.store.workers()
        work = self.store.list_work()
        contributions = []
        if self._current_cycle_id:
            contributions = self.store.cycle(self._current_cycle_id)["contributions"]
        bootstrapped = sum(worker._bootstrap_complete for worker in self.workers)
        phase = self._phase
        if phase == "bootstrap" and self.workers and bootstrapped == self.size:
            phase = "working"
        return {
            "phase": phase,
            "size": self.size,
            "active": sum(worker["state"] == "working" for worker in workers),
            "bootstrapped": bootstrapped,
            "contributions": len(contributions),
            "messages": len(self.store.messages(limit=10_000)),
            "work_total": len(work),
            "work_done": sum(
                item["state"] in {"completed", "failed", "cancelled"} for item in work
            ),
        }

    def _findings_fingerprint(self) -> str:
        return json.dumps(self.services.findings_store.index(), sort_keys=True)

    async def _summarize(self, snapshot: dict[str, Any]) -> str:
        factory = getattr(self.services, "_swarm_summarizer_backend_factory", None)
        try:
            backend = factory() if factory is not None else self.services._build_backend(
                purpose="summary"
            )
            backend.set_actor(f"swarm-{self.store.swarm_id}__summarizer-{uuid.uuid4().hex[:6]}")
            summarizer = SwarmSummarizer(
                backend,
                session=self.services._make_session(
                    f"swarm-{self.store.swarm_id}__summarizer-{uuid.uuid4().hex[:6]}"
                ),
            )
            summary = await summarizer.summarize(snapshot)
            if summary:
                return summary
        except Exception as exc:  # noqa: BLE001 - contributions remain authoritative
            self.services.audit.write(
                "swarm_summarizer_error", error=f"{type(exc).__name__}: {exc}"
            )
        return deterministic_summary(snapshot)

    async def _write_report(self) -> str:
        attempts = [attempt for worker in self.workers for attempt in worker.subagents.attempts]
        path = write_report(
            self.services.findings_store,
            self.services.cfg.cwd / ".pentest" / "report.md",
            attempts=attempts,
        )
        def summary_rows(verdict: str) -> list[dict[str, Any]]:
            rows = []
            for record in self.services.findings_store.all_records(verdict):
                validation = record.validation
                rows.append({
                    "id": record.id,
                    "title": record.finding.title,
                    "cvss_score": validation.cvss40_score if validation else 0.0,
                    "file_path": self.services._find_record_path(record.id, verdict),
                })
            return rows

        # SessionSummary is a compact UI projection, unlike the nested record
        # documents passed to the narrative summarizer above. Keeping that contract
        # aligned with legacy mode avoids '(untitled)' / CVSS 0.0 report cards.
        confirmed = summary_rows("confirmed")
        rejected = summary_rows("rejected")
        pending = summary_rows("pending")
        await self.services._emit(SessionSummary(
            confirmed=confirmed, rejected=rejected, pending=pending,
            findings_dir=str(self.services.findings_store.root),
            audit_path=str(self.services.audit.path), report_path=str(path),
            turns=sum(getattr(w.loop, "turn", 0) for w in self.workers),
        ))
        self.services.audit.write("swarm_report", report_path=str(path), workers=self.size)
        return str(path)

    def cancel_turns(self) -> bool:
        active = False
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
                active = True
        return active

    def respond_now(self) -> bool:
        """Freeze available contributions by making active peers explicitly unavailable."""
        return self.cancel_turns()

    def inspection_lines(self, view: str) -> list[str]:
        if view == "agents":
            return [
                f"{w['name']} ({w['worker_id']})  {w['state']}  lane: {w['lane']}"
                for w in self.store.workers()
            ] or ["(no swarm workers)"]
        if view == "messages":
            return [
                f"#{m['sequence']} {m['sender_name']} → {m['addressing']}: {m['body']}"
                for m in reversed(self.store.messages())
            ] or ["(no swarm messages)"]
        if view == "groups":
            return [
                f"{g['group_id']} {g['name']} [{g['state']}] members={','.join(g['members'])}"
                for g in self.store.groups()
            ] or ["(no swarm groups)"]
        if view == "work":
            return [
                f"{w['work_id']} [{w['state']}] {w['title']} @ {w['target']}"
                for w in self.store.list_work()
            ] or ["(no shared work)"]
        if view == "history":
            return [
                f"#{e['sequence']} {e['kind']} {e['actor_id'] or ''}".rstrip()
                for e in self.store.events()
            ] or ["(no swarm history)"]
        return [f"unknown swarm view {view!r}"]

    async def close(self) -> None:
        self._stopping = True
        self.cancel_turns()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        self.store.close()
