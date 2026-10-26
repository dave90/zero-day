"""One collaborating top-level orchestrator peer."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from zero.agents.swarm_orchestrator import build_swarm_orchestrator_loop
from zero.findings.dedup import dedup_hash
from zero.llm.backend import terminal_backend_error
from zero.llm.envelope import ActionEnvelope, ActionsEnvelope, AskUserEnvelope, FinalEnvelope
from zero.orchestrator.subagents import SubagentManager
from zero.orchestrator.rate_limit import RateLimiter
from zero.tools.custom_tools import is_custom_mgmt_tool
from zero.tools.dispatch import (
    AwaitSubagentsArgs,
    DispatchPentesterArgs,
    DispatchValidatorArgs,
    FinalizeArgs,
    is_dispatch_tool,
    is_inspect_tool,
)
from zero.tools.memory import read_memory
from zero.ui.events import ActivityLine
from zero.swarm.tools import (
    AwaitSwarmEventArgs,
    SwarmClaimWorkArgs,
    SwarmCreateGroupArgs,
    SwarmCreateWorkArgs,
    SwarmFinishBootstrapArgs,
    SwarmFinishCycleArgs,
    SwarmInboxArgs,
    SwarmListWorkArgs,
    SwarmPublishEvidenceArgs,
    SwarmQueryEvidenceArgs,
    SwarmSendArgs,
    SwarmUpdateWorkArgs,
    SwarmUpdateGroupArgs,
    is_swarm_tool,
    swarm_catalog,
)

if TYPE_CHECKING:
    from zero.swarm.models import WorkerIdentity
    from zero.swarm.runtime import SwarmRuntime


_MAX_CONSECUTIVE_TURN_ERRORS = 3
_TURN_RETRY_DELAYS = (1.0, 2.0)
_BOOTSTRAP_LOCAL_TOOLS = {"memory_read", "memory_write", "todo_read", "todo_write"}
_DEFAULT_LEASE_SECONDS = 300.0


class SwarmWorker:
    def __init__(
        self,
        runtime: "SwarmRuntime",
        identity: "WorkerIdentity",
        backend: Any,
        *,
        bootstrap_required: bool = True,
    ) -> None:
        self.runtime = runtime
        self.services = runtime.services
        self.identity = identity
        self.backend = backend
        self.loop = None
        self.cycle_id = ""
        self._finished = False
        self._candidate_count = 0
        self._validator_count = 0
        self._validator_candidates: dict[str, str] = {}
        self._bootstrap_required = bootstrap_required
        self._bootstrap_complete = not bootstrap_required
        self._bootstrap_inspected = False
        self._bootstrap_announced = False
        self._bootstrap_waited = False
        self._bootstrap_peer_input = False
        self._bootstrap_claims: set[str] = set()
        self._lease_claims: dict[str, tuple[str, float]] = {}
        self._lease_changed = asyncio.Event()
        self._lease_task: asyncio.Task | None = None
        self.rate_limiter = RateLimiter(default_rate=self.services.cfg.rate_limit)
        self.services._actor_rate_limiters[self.identity.actor_id] = self.rate_limiter
        self.subagents = SubagentManager(
            max_concurrent=self.services.cfg.max_concurrent_subagents,
            max_total=self.services.cfg.max_total_subagents,
            runner=self._run_child,
            emit=self.services._emit,
            id_prefix=self.identity.actor_id,
        )

    async def run(self, goal: str, cycle_id: str) -> str:
        self.cycle_id = cycle_id
        ident = self.identity
        self.runtime.store.set_worker_state(ident.worker_id, "working")
        self.loop = build_swarm_orchestrator_loop(
            self.backend,
            self.services._orchestrator_catalog() + swarm_catalog(),
            project_context=self.runtime.project_context,
            max_turns=self.services.cfg.max_turns,
            session=self.services._make_session(ident.actor_id),
        )
        self.loop.backend.set_actor(ident.actor_id)
        directory = ", ".join(
            f"{w['worker_id']}={w['name']} ({w['lane']})" for w in self.runtime.store.workers()
        )
        self.loop.add_user_message(
            f"RESPONSE CYCLE: {cycle_id}\nYOUR ID: {ident.worker_id}\n"
            f"YOUR COVERAGE LANE: {self.runtime.lanes[ident.worker_id]}\n"
            f"PEERS: {directory}\n"
            f"CYCLE MODE: {'INITIAL BOOTSTRAP REQUIRED' if self._bootstrap_required else 'FOLLOW-UP; REUSE EXISTING STATE'}\n\n"
            f"OPERATOR GOAL: {goal}"
        )
        prior_memory = read_memory(ident.actor_id)
        if prior_memory:
            self.loop.add_user_message(
                "## Your private memory from the prior cycle\n\n" + prior_memory
            )
        consecutive_turn_errors = 0
        self._lease_task = asyncio.create_task(
            self._renew_work_leases(), name=f"lease-renewal-{ident.worker_id}"
        )
        try:
            while not self._finished:
                await self._inject_inbox()
                self.runtime.store.set_worker_state(ident.worker_id, "working")
                try:
                    step = await self.loop.step()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one peer must not die on a glitch
                    consecutive_turn_errors += 1
                    self.services.audit.write(
                        "swarm_turn_error",
                        worker_id=ident.worker_id,
                        actor_id=ident.actor_id,
                        cycle_id=cycle_id,
                        error=f"{type(exc).__name__}: {exc}",
                        consecutive=consecutive_turn_errors,
                    )
                    if terminal_backend_error(exc) or consecutive_turn_errors >= _MAX_CONSECUTIVE_TURN_ERRORS:
                        raise
                    self.loop.add_observation(
                        "(system)", False,
                        f"your previous turn failed: {exc}. Reply with EXACTLY ONE valid "
                        "JSON envelope (action / actions / ask_user / final) as text; do not "
                        "make a native tool call.",
                    )
                    await self.services._emit(ActivityLine(
                        text=(
                            f"  ! {ident.name} turn failed "
                            f"({consecutive_turn_errors}/{_MAX_CONSECUTIVE_TURN_ERRORS}); retrying"
                        )
                    ))
                    await asyncio.sleep(_TURN_RETRY_DELAYS[consecutive_turn_errors - 1])
                    continue
                consecutive_turn_errors = 0
                env = step.envelope
                self.services._log_thinking(ident.actor_id, self.loop.turn, env)
                self.services.audit.write(
                    "swarm_agent_step", worker_id=ident.worker_id, actor_id=ident.actor_id,
                    cycle_id=cycle_id, turn=self.loop.turn,
                    envelope_type=env.__class__.__name__, raw=step.raw_text, usage=step.usage,
                )
                if isinstance(env, FinalEnvelope):
                    if not self._bootstrap_complete:
                        if self.loop.turn >= self.loop.max_turns:
                            finished = await self._finish(
                                "bootstrap incomplete when the turn budget ended; " + env.answer,
                                unavailable=True,
                            )
                        else:
                            self.loop.add_observation(
                                "finalize", False,
                                "initial swarm bootstrap must complete before contributing",
                            )
                            continue
                    else:
                        finished = await self._finish(
                            env.answer, allow_unvalidated=self.loop.turn >= self.loop.max_turns
                        )
                    if not finished:
                        self.loop.add_observation(
                            "finalize", False,
                            "candidate findings still need one independent Validator each",
                        )
                elif isinstance(env, AskUserEnvelope):
                    answer = await self.services._do_ask_user(env, agent=ident.actor_id)
                    self.loop.add_ask_user_answer(answer)
                elif isinstance(env, ActionsEnvelope):
                    # Coordination and dispatch mutate shared state; preserve model order.
                    for call in env.calls:
                        await self._handle_action(call)
                        if self._finished:
                            break
                elif isinstance(env, ActionEnvelope):
                    await self._handle_action(env)
            cycle = self.runtime.store.cycle(cycle_id)
            mine = next(c for c in cycle["contributions"] if c["worker_id"] == ident.worker_id)
            return mine["body"]
        except asyncio.CancelledError:
            self.runtime.store.contribute(cycle_id, ident.worker_id, "cancelled", status="unavailable")
            raise
        finally:
            await self.subagents.cancel_all()
            self._abandon_inflight_validations()
            if self._lease_task is not None:
                self._lease_task.cancel()
                await asyncio.gather(self._lease_task, return_exceptions=True)
            self.services._actor_rate_limiters.pop(ident.actor_id, None)
            self.runtime.store.set_worker_state(ident.worker_id, "idle")

    async def _run_child(self, record):
        self.services._actor_rate_limiters[record.id] = self.rate_limiter
        try:
            return await self.services._run_subagent(record)
        finally:
            self.services._actor_rate_limiters.pop(record.id, None)

    def _abandon_inflight_validations(self) -> None:
        for subagent_id, candidate_id in list(self._validator_candidates.items()):
            self.runtime.store.abandon_validation(
                candidate_id, subagent_id, reason="worker stopped before verdict"
            )
        self._validator_candidates.clear()

    def _remember_claim(self, claim, lease_seconds: float) -> None:
        self._lease_claims[claim.work_id] = (claim.lease_token, lease_seconds)
        self._lease_changed.set()

    def _forget_claim(self, work_id: str) -> None:
        self._lease_claims.pop(work_id, None)
        self._lease_changed.set()

    async def _renew_work_leases(self) -> None:
        """Keep this peer's claims alive while LLM calls or children are running."""
        while True:
            if not self._lease_claims:
                self._lease_changed.clear()
                await self._lease_changed.wait()
                continue
            interval = max(
                0.25,
                min(60.0, min(seconds for _token, seconds in self._lease_claims.values()) / 3),
            )
            self._lease_changed.clear()
            try:
                await asyncio.wait_for(self._lease_changed.wait(), timeout=interval)
                continue
            except asyncio.TimeoutError:
                pass
            for work_id, (token, lease_seconds) in list(self._lease_claims.items()):
                renewed = self.runtime.store.update_work(
                    self.identity.worker_id,
                    work_id,
                    token,
                    action="renew",
                    lease_seconds=lease_seconds,
                )
                if not renewed:
                    self._lease_claims.pop(work_id, None)
                    self.services.audit.write(
                        "swarm_lease_lost",
                        worker_id=self.identity.worker_id,
                        work_id=work_id,
                    )

    async def _inject_inbox(self) -> None:
        messages = self.runtime.store.inbox(self.identity.worker_id)
        if not messages:
            return
        if any(m["sender_id"] != self.identity.worker_id for m in messages):
            self._bootstrap_peer_input = True
        blocks = []
        for m in messages:
            refs = ", ".join(m["evidence_ids"]) or "none"
            blocks.append(
                f"[{m['message_id']}] from {m['sender_name']} ({m['sender_id']}); "
                f"kind={m['kind']}; evidence={refs}\n{m['body']}"
            )
        self.loop.add_observation("swarm_inbox", True, "\n\n".join(blocks))
        self.runtime.store.acknowledge(self.identity.worker_id, [m["message_id"] for m in messages])

    async def _handle_action(self, call: ActionEnvelope) -> None:
        if is_swarm_tool(call.tool):
            output = await self._handle_swarm(call.tool, call.args)
            self.loop.add_observation(call.tool, True, output)
            return
        if not self._bootstrap_complete and call.tool not in _BOOTSTRAP_LOCAL_TOOLS:
            self.loop.add_observation(
                call.tool,
                False,
                "initial swarm bootstrap incomplete: inspect status, claim work, announce "
                "ownership, wait for peer coordination, then call swarm_finish_bootstrap",
            )
            return
        if is_dispatch_tool(call.tool):
            output = await self._handle_dispatch(call.tool, call.args)
            if output is not None:
                self.loop.add_observation(call.tool, True, output)
            return
        if is_inspect_tool(call.tool):
            output = await self.services._handle_inspect(call)
            self.loop.add_observation(call.tool, True, output)
            return
        if is_custom_mgmt_tool(call.tool):
            output = await self.services._handle_custom_mgmt(call)
            self.loop.add_observation(call.tool, True, output)
            return
        result = await self.services._run_one_tool(
            call.tool, call.args, label=self.identity.name, reason=call.thought,
            session_id=self.identity.actor_id,
        )
        self.loop.add_observation(call.tool, result.ok, result.output)

    async def _handle_swarm(self, tool: str, args: dict) -> str:
        store, wid = self.runtime.store, self.identity.worker_id
        if tool == "swarm_status":
            self._bootstrap_inspected = True
            return json.dumps({"workers": store.workers(), "work": store.list_work()}, default=str)
        if tool == "swarm_send":
            a = SwarmSendArgs(**args)
            mid = store.send_message(wid, **a.model_dump())
            if a.kind == "coordination":
                self._bootstrap_announced = True
            self.runtime.signal_event()
            return f"message accepted as {mid}"
        if tool == "swarm_inbox":
            a = SwarmInboxArgs(**args)
            store.acknowledge(wid, a.acknowledge_ids)
            messages = store.inbox(wid)
            if any(m["sender_id"] != wid for m in messages):
                self._bootstrap_peer_input = True
            return json.dumps(messages, default=str)
        if tool == "await_swarm_event":
            a = AwaitSwarmEventArgs(**args)
            await self.runtime.await_event(a.timeout)
            self._bootstrap_waited = True
            return "swarm state changed; continue to the next turn to receive new inbox data"
        if tool == "swarm_create_group":
            a = SwarmCreateGroupArgs(**args)
            gid = store.create_group(wid, a.name, a.topic, a.member_ids)
            return f"created group {gid}"
        if tool == "swarm_update_group":
            a = SwarmUpdateGroupArgs(**args)
            store.update_group(wid, **a.model_dump())
            self.runtime.signal_event()
            return "group updated"
        if tool == "swarm_create_work":
            a = SwarmCreateWorkArgs(**args)
            create_args = a.model_dump(exclude={"claim"})
            work_id, created = store.create_work(wid, **create_args)
            claim = store.claim_work(wid, work_id) if a.claim else None
            if claim is not None:
                self._bootstrap_claims.add(work_id)
                self._remember_claim(claim, _DEFAULT_LEASE_SECONDS)
            self.runtime.signal_event()
            status = f"{'created' if created else 'reused'} work {work_id}"
            if a.claim:
                status += (
                    f" and claimed it with lease {claim.lease_token}"
                    if claim is not None else " but claim failed because another peer owns it"
                )
            return status
        if tool == "swarm_list_work":
            a = SwarmListWorkArgs(**args)
            self._bootstrap_inspected = True
            return json.dumps(store.list_work(state=a.state, owner=wid if a.mine else None))
        if tool == "swarm_claim_work":
            a = SwarmClaimWorkArgs(**args)
            claim = store.claim_work(wid, a.work_id, lease_seconds=a.lease_seconds)
            if claim is not None:
                self._bootstrap_claims.add(a.work_id)
                self._remember_claim(claim, a.lease_seconds)
            return json.dumps(claim.__dict__) if claim else "claim failed: work is not ready"
        if tool == "swarm_update_work":
            a = SwarmUpdateWorkArgs(**args)
            ok = store.update_work(wid, **a.model_dump())
            if ok and a.action in {"release", "complete", "fail", "cancel"}:
                self._forget_claim(a.work_id)
            elif ok and a.action == "renew":
                self._lease_claims[a.work_id] = (a.lease_token, _DEFAULT_LEASE_SECONDS)
            self.runtime.signal_event()
            return "work updated" if ok else "work update rejected: owner or lease token mismatch"
        if tool == "swarm_publish_evidence":
            a = SwarmPublishEvidenceArgs(**args)
            evidence = store.publish_evidence(wid, **a.model_dump())
            self.runtime.signal_event()
            return f"published immutable evidence {evidence.evidence_id} ({evidence.content_hash})"
        if tool == "swarm_query_evidence":
            a = SwarmQueryEvidenceArgs(**args)
            return json.dumps(store.query_evidence(wid, **a.model_dump()), default=str)
        if tool == "swarm_finish_bootstrap":
            a = SwarmFinishBootstrapArgs(**args)
            if self._bootstrap_complete:
                return "bootstrap already complete"
            owned = {
                item["work_id"] for item in store.list_work(owner=wid)
                if item["state"] in {"claimed", "running"}
            }
            checks = {
                "inspect shared status/work": self._bootstrap_inspected,
                "claim at least one work item": bool(owned & self._bootstrap_claims),
                "announce ownership with a coordination message": self._bootstrap_announced,
                "wait for a coordination event": self._bootstrap_waited,
                "observe peer input or reach the quiet-consensus threshold": (
                    self._bootstrap_peer_input or self.loop.turn >= 5
                ),
            }
            missing = [label for label, passed in checks.items() if not passed]
            if missing:
                return "bootstrap REFUSED: " + "; ".join(missing)
            self._bootstrap_complete = True
            self.services.audit.write(
                "swarm_bootstrap_completed",
                worker_id=wid,
                actor_id=self.identity.actor_id,
                cycle_id=self.cycle_id,
                work_ids=sorted(owned & self._bootstrap_claims),
                summary=a.summary,
            )
            await self.services._emit(ActivityLine(
                text=(
                    f"  ◈ {self.identity.name} bootstrap complete · "
                    f"owns {len(owned & self._bootstrap_claims)} work item(s)"
                )
            ))
            return "bootstrap complete; target tools and child dispatches are now enabled"
        if tool == "swarm_finish_cycle":
            a = SwarmFinishCycleArgs(**args)
            if not self._bootstrap_complete and not a.unavailable:
                return "finish REFUSED: initial swarm bootstrap is incomplete"
            finished = await self._finish(a.contribution, unavailable=a.unavailable)
            return (
                "contribution recorded" if finished else
                "finish REFUSED: candidate findings still need independent Validators"
            )
        return f"unknown swarm tool {tool!r}"

    async def _handle_dispatch(self, tool: str, args: dict) -> str | None:
        if tool == "dispatch_pentester":
            a = DispatchPentesterArgs(**args)
            if self.subagents.at_total_capacity():
                return "peer subagent budget reached; await current children and finish"
            sid = self.subagents.spawn(
                "pentester", target=a.target, idea=a.attack_idea,
                context=a.context, max_turns=a.max_turns,
            )
            self.services.audit.write(
                "dispatch_pentester", worker_id=self.identity.worker_id,
                subagent_id=sid, target=a.target, idea=a.attack_idea,
            )
            return f"dispatched {sid}"
        if tool == "dispatch_validator":
            a = DispatchValidatorArgs(**args)
            finding = a.to_finding()
            digest = dedup_hash(finding)
            candidate_id, acquired = self.runtime.store.reserve_candidate(
                digest, finding.model_dump(), self.identity.worker_id
            )
            # This peer handed the candidate to the central reservation path even
            # when another peer already owns the single Validator attempt.
            self._validator_count += 1
            prior = self.services.findings_store.lookup_by_hash(digest)
            if prior is not None:
                self.runtime.store.commit_candidate_verdict(
                    candidate_id, prior["verdict"], self.identity.actor_id
                )
                return f"candidate {candidate_id} already has verdict {prior['verdict']}"
            if not acquired:
                return f"candidate {candidate_id} already reserved; source provenance attached"
            self.services._inflight_hashes.add(digest)
            self.services._validations_pending += 1
            self.services._validators_dispatched += 1
            sid = self.subagents.spawn(
                "validator", target=finding.endpoint, idea=finding.title, finding=finding,
            )
            self._validator_candidates[sid] = candidate_id
            self.runtime.store.start_validation(candidate_id, sid)
            return f"dispatched independent validator {sid} for candidate {candidate_id}"
        if tool == "await_subagents":
            a = AwaitSubagentsArgs(**args)
            p, v = self.subagents.running_counts()
            if p == 0 and v == 0 and not self.subagents.has_pending_results():
                return "no subagents are running and no results are waiting"
            results = await self.subagents.await_(a.which, a.timeout)
            self._commit_validator_results(results)
            self._candidate_count += sum(
                len(r.candidate_findings) for r in results if r.kind == "pentester"
            )
            return self.subagents.format_results(results)
        if tool == "list_subagents":
            return self.subagents.format_snapshot()
        if tool == "finalize":
            a = FinalizeArgs(**args)
            if self._candidate_count > self._validator_count:
                return "finalize REFUSED: candidate findings require an independent Validator"
            finished = await self._finish(a.summary)
            return (
                "contribution recorded; the coordinator owns the final report" if finished else
                "finalize REFUSED: drained children returned candidates that still need Validators"
            )
        return f"unknown dispatch tool {tool!r}"

    async def _finish(
        self, contribution: str, *, unavailable: bool = False,
        allow_unvalidated: bool = False,
    ) -> bool:
        if self._finished:
            return True
        results = await self.subagents.await_("all", timeout=None)
        self._commit_validator_results(results)
        self._candidate_count += sum(
            len(r.candidate_findings) for r in results if r.kind == "pentester"
        )
        if (
            not unavailable
            and not allow_unvalidated
            and self._candidate_count > self._validator_count
        ):
            return False
        if allow_unvalidated and self._candidate_count > self._validator_count:
            contribution += (
                f"\n\nPending reason: turn budget ended with "
                f"{self._candidate_count - self._validator_count} unvalidated candidate(s)."
            )
        self.runtime.store.contribute(
            self.cycle_id, self.identity.worker_id, contribution,
            status="unavailable" if unavailable else "complete",
        )
        self.services.audit.write(
            "swarm_contribution", worker_id=self.identity.worker_id,
            cycle_id=self.cycle_id, unavailable=unavailable,
        )
        await self.services._emit(ActivityLine(
            text=f"  ▸ {self.identity.name} submitted its swarm contribution"
        ))
        self._finished = True
        return True

    def _commit_validator_results(self, results) -> None:
        for result in results:
            candidate_id = self._validator_candidates.pop(result.subagent_id, None)
            if candidate_id is not None:
                self.runtime.store.commit_candidate_verdict(
                    candidate_id, result.status, result.subagent_id
                )
