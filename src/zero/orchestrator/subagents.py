"""SubagentManager — concurrent pentester/validator subagents (M8).

The Orchestrator dispatches focused subagents; this manager runs them as
independent asyncio tasks, capped by a semaphore (default 10), tracks live
state for the status panel, records every attack attempt for the final
report, and surfaces completed results through a queue that the orchestrator
drains via `await_subagents`.

It is deliberately decoupled from the orchestrator: it takes an injected
`runner` coroutine `(SubagentRecord) -> SubagentResult` that actually builds
and drives the subagent's AgentLoop. That keeps it unit-testable without the
full orchestrator (and lets tests inject a gated fake runner).
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from zero.ui.events import SubagentFinished, SubagentStarted, UIEvent

# Funny Italian first names (men + women) used to label subagents — a random one
# is prepended to the serial counter, so an id reads like `gino_2` instead of `#2`.
# Purely cosmetic; the trailing number is still the unique serial.
_FUNNY_NAMES: tuple[str, ...] = (
    "gino", "pino", "nino", "dino", "lino", "beppe", "gigi", "ciccio", "tonino",
    "peppino", "rocco", "nello", "cosimo", "vito", "saverio", "gennaro", "ottavio",
    "ugo", "aldo", "gualtiero", "ermenegildo", "osvaldo", "fiorello", "battista",
    "primo", "secondo", "amedeo", "bartolomeo", "pasquale", "ovidio",
    "gina", "pina", "nina", "carmela", "assunta", "concetta", "immacolata", "rosa",
    "filomena", "antonietta", "wanda", "loredana", "ornella", "gelsomina", "palmira",
    "agata", "santina", "romina", "addolorata", "cleofe",
)

# How many characters of a subagent's `summary` reach the orchestrator via
# `format_results`. Deliberately LARGE: a pentester that didn't fully solve it
# should be able to hand back the exact promising payloads — even a full
# exploit script — verbatim, so the orchestrator can refine a near-miss instead
# of re-deriving it from a one-liner. The pentester brief quotes this same number,
# so keep the two in sync by importing this constant rather than hard-coding it.
# (~50k chars ≈ 12k tokens per returned result — generous, and tunable here.)
SUMMARY_MAX_CHARS = 50_000


@dataclass
class SubagentResult:
    subagent_id: str
    kind: str                       # "pentester" | "validator"
    target: str
    idea: str
    status: str                     # EXPLOITED|NO_FINDING|ERROR | confirmed|rejected|pending
    summary: str = ""
    candidate_findings: list[dict] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    finding_ids: list[str] = field(default_factory=list)
    turns: int = 0
    duration: float = 0.0


@dataclass
class AttackAttempt:
    subagent_id: str
    kind: str
    target: str
    idea: str
    status: str = "running"
    outcome: str = ""


@dataclass
class SubagentRecord:
    id: str
    kind: str
    target: str
    idea: str
    context: str = ""
    max_turns: int | None = None
    finding: Any = None             # parsed Finding for validators
    status: str = "queued"          # queued | running | done
    start_ts: float = 0.0
    loop: Any = None                # live AgentLoop (for snapshot turn)
    result: SubagentResult | None = None


Runner = Callable[[SubagentRecord], Awaitable[SubagentResult]]
Emit = Callable[[UIEvent], Awaitable[None]]


class SubagentManager:
    def __init__(
        self,
        *,
        max_concurrent: int,
        runner: Runner,
        emit: Emit,
        max_total: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_concurrent = max_concurrent
        # Hard ceiling on the TOTAL subagents ever spawned this run (0 = unlimited).
        # Distinct from `max_concurrent` (how many run AT ONCE). `_counter` is the
        # running total, so `at_total_capacity()` gates further dispatches.
        self.max_total = max_total
        self._sem = asyncio.Semaphore(max_concurrent)
        self._runner = runner
        self._emit = emit
        self._clock = clock
        self._running: dict[str, SubagentRecord] = {}
        self._completed: list[SubagentResult] = []
        self.attempts: list[AttackAttempt] = []
        self._tasks: set[asyncio.Task] = set()
        self._done: asyncio.Queue[SubagentResult] = asyncio.Queue()
        self._counter = 0

    # --------------------------------------------------------------- dispatch

    def at_total_capacity(self) -> bool:
        """True once `max_total` subagents have been spawned this run (the orchestrator
        checks this before dispatching). Always False when max_total is 0 (unlimited)."""
        return bool(self.max_total) and self._counter >= self.max_total

    def spawn(
        self,
        kind: Literal["pentester", "validator"],
        *,
        target: str,
        idea: str,
        context: str = "",
        max_turns: int | None = None,
        finding: Any = None,
    ) -> str:
        """Register + launch a subagent. Returns its id immediately. Execution
        is gated by the semaphore (over-cap dispatches queue)."""
        self._counter += 1
        # Serial counter (1,2,3…) keeps ids unique; prepend a random funny name
        # purely for flavour, e.g. `gino_2`.
        sid = f"{random.choice(_FUNNY_NAMES)}_{self._counter}"
        rec = SubagentRecord(
            id=sid, kind=kind, target=target, idea=idea,
            context=context, max_turns=max_turns, finding=finding,
        )
        self._running[sid] = rec
        self.attempts.append(AttackAttempt(subagent_id=sid, kind=kind, target=target, idea=idea))
        task = asyncio.create_task(self._run(rec), name=f"subagent-{sid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return sid

    async def _run(self, rec: SubagentRecord) -> None:
        async with self._sem:                      # cap CONCURRENT execution
            rec.status = "running"
            rec.start_ts = self._clock()
            await self._emit(
                SubagentStarted(subagent_id=rec.id, subkind=rec.kind, target=rec.target, idea=rec.idea)
            )
            try:
                result = await self._runner(rec)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a subagent crash must not kill the session
                result = SubagentResult(
                    subagent_id=rec.id, kind=rec.kind, target=rec.target, idea=rec.idea,
                    status="ERROR", summary=f"{type(e).__name__}: {e}",
                    turns=getattr(rec.loop, "turn", 0),
                    duration=self._clock() - rec.start_ts,
                )
        # Completion bookkeeping — pop + enqueue are adjacent (no await between)
        # so await_("all") never observes a torn state.
        rec.result = result
        rec.status = "done"
        self._completed.append(result)
        self._update_attempt(rec, result)
        self._running.pop(rec.id, None)
        self._done.put_nowait(result)
        await self._emit(
            SubagentFinished(
                subagent_id=rec.id, subkind=rec.kind, target=rec.target,
                status=result.status, finding_ids=result.finding_ids, duration=result.duration,
            )
        )

    def _update_attempt(self, rec: SubagentRecord, result: SubagentResult) -> None:
        for a in self.attempts:
            if a.subagent_id == rec.id:
                a.status = result.status
                a.outcome = result.summary[:200]
                return

    # ----------------------------------------------------------------- await

    async def await_(
        self, which: Literal["any", "all"] = "any", timeout: float | None = None
    ) -> list[SubagentResult]:
        """Block until results are available; return the completed ones.

        which="any": return as soon as ≥1 completes (plus any already done).
        which="all": return once every running subagent has completed (bounded
                     by timeout; partial on timeout).
        """
        results: list[SubagentResult] = []
        if which == "all":
            deadline = None if timeout is None else self._clock() + timeout
            while self._running or not self._done.empty():
                remaining = None if deadline is None else max(0.0, deadline - self._clock())
                try:
                    results.append(await asyncio.wait_for(self._done.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
            while not self._done.empty():
                results.append(self._done.get_nowait())
            return results
        # which == "any"
        try:
            results.append(await asyncio.wait_for(self._done.get(), timeout=timeout))
        except asyncio.TimeoutError:
            return []
        while not self._done.empty():
            results.append(self._done.get_nowait())
        return results

    def collect_done(self) -> list[SubagentResult]:
        """Non-blocking: drain and return any results that have already
        completed. Used when a blocking await is interrupted (e.g. the operator
        interjects) so we still hand back whatever finished in the meantime."""
        out: list[SubagentResult] = []
        while not self._done.empty():
            out.append(self._done.get_nowait())
        return out

    async def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -------------------------------------------------------------- snapshots

    def running_counts(self) -> tuple[int, int]:
        """(pentesters, validators) currently in flight (queued or running)."""
        p = sum(1 for r in self._running.values() if r.kind == "pentester")
        v = sum(1 for r in self._running.values() if r.kind == "validator")
        return p, v

    def running_summary(self) -> str:
        p, v = self.running_counts()
        return f"{p} pentester{'s' if p != 1 else ''}, {v} validator{'s' if v != 1 else ''} (cap {self.max_concurrent})"

    def snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self._running.values():
            out.append(
                {
                    "id": r.id,
                    "kind": r.kind,
                    "target": r.target,
                    "idea": r.idea,
                    "turn": getattr(r.loop, "turn", 0),
                    "elapsed": (self._clock() - r.start_ts) if r.status == "running" else 0.0,
                    "status": r.status,
                    # Context-window tokens used by this subagent's last LLM call
                    # (the live AgentLoop tracks it). The UI shows used/window.
                    "context_tokens": getattr(r.loop, "context_tokens", 0),
                }
            )
        return out

    # -------------------------------------------------------- format for LLM

    def format_results(self, results: list[SubagentResult]) -> str:
        if not results:
            return f"(no completions yet) still running: {self.running_summary()}"
        blocks: list[str] = [f"=== results: {len(results)} subagent(s) completed ==="]
        for r in results:
            lines = [
                f"\n{r.kind} {r.subagent_id}  target {r.target!r}  status={r.status}"
                f"  (turns {r.turns}, {r.duration:.0f}s)",
                f"summary: {r.summary.strip()[:SUMMARY_MAX_CHARS]}",
            ]
            if r.candidate_findings:
                lines.append(f"candidate findings: {len(r.candidate_findings)}")
                for cf in r.candidate_findings[:6]:
                    lines.append(f"  - {cf.get('title', '(untitled)')}  {cf.get('cwe', '')}")
            if r.discovered:
                lines.append("discovered creds/tokens/notes (use to chain):")
                for d in r.discovered[:10]:
                    lines.append(f"  - {d}")
            if r.finding_ids:
                lines.append(f"persisted finding ids: {', '.join(r.finding_ids)}")
            blocks.append("\n".join(lines))
        blocks.append(f"\nstill running: {self.running_summary()}")
        return "\n".join(blocks)

    def format_snapshot(self) -> str:
        snap = self.snapshot()
        if not snap:
            return f"no subagents running. completed this session: {len(self._completed)}."
        lines = [f"running subagents ({len(snap)}):"]
        for s in snap:
            lines.append(
                f"  {s['id']} {s['kind']:9} {s['target']:24} idea={s['idea']!r}"
                f"  turn {s['turn']}  {s['elapsed']:.0f}s  [{s['status']}]"
            )
        lines.append(f"completed this session: {len(self._completed)}")
        return "\n".join(lines)
