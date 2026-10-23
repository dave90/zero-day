import asyncio
import hashlib
import json

from click.testing import CliRunner

from zero.cli import main
from zero.llm.backend import LLMBackend, LLMResponse
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.orchestrator.router import AgentRouter
from zero.agents.swarm_orchestrator import build_swarm_orchestrator_loop
from zero.swarm.runtime import SwarmRuntime
from zero.swarm.worker import SwarmWorker
from zero.ui.events import AgentTextDelta, SessionSummary, SwarmPhase


def action(tool, args):
    return json.dumps({"type": "action", "tool": tool, "args": args, "thought": "test"})


def final(answer):
    return json.dumps({"type": "final", "answer": answer})


class ScriptedBackend(LLMBackend):
    def __init__(self, script):
        self.script = list(script)
        self.histories = []

    async def run(self, system_prompt, messages, tools):
        self.histories.append([m.content for m in messages])
        if not self.script:
            raise AssertionError("script exhausted")
        return LLMResponse(text=self.script.pop(0))


class BlockingBackend(LLMBackend):
    def __init__(self):
        self.started = asyncio.Event()

    async def run(self, system_prompt, messages, tools):
        self.started.set()
        await asyncio.Event().wait()


class TransientFailureBackend(ScriptedBackend):
    def __init__(self, script, *, failures=1):
        super().__init__(script)
        self.failures = failures

    async def run(self, system_prompt, messages, tools):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary model capacity error")
        return await super().run(system_prompt, messages, tools)


FINDING = {
    "title": "Admin access with leaked token",
    "summary": "peer evidence token grants admin access",
    "endpoint": "GET /admin",
    "method": "GET",
    "cwe": "CWE-287",
    "severity_hint": "high",
    "evidence": "Authorization: Bearer chain-token returned 200 admin",
    "payload_shape": "bearer-token",
}


def pentester_final():
    return final(json.dumps({
        "status": "EXPLOITED", "summary": "exact handed-off token returned admin data",
        "candidate_findings": [FINDING], "discovered": [],
    }))


def validator_final():
    return final(json.dumps({
        "verdict": "confirmed", "rationale": "independently reproduced with the token",
        "reproducer": "GET /admin with Authorization: Bearer chain-token",
        "cvss40_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N",
        "cvss40_score": 7.7, "cwe": "CWE-287",
    }))


async def test_cross_peer_evidence_handoff_is_used_and_independently_validated(tmp_path):
    content = "Authorization: Bearer chain-token"
    evidence_id = "ev_" + hashlib.sha256(content.encode()).hexdigest()[:16]
    peer1 = ScriptedBackend([
        action("swarm_status", {}),
        action("swarm_create_work", {
            "kind": "recon", "title": "Recover authentication material",
            "target": "/login", "hypothesis": "login responses expose a reusable token",
            "priority": 10, "claim": True,
        }),
        action("swarm_send", {
            "to": "worker-2", "kind": "coordination",
            "body": "I own token recovery from /login; you own validation on /admin.",
        }),
        action("await_swarm_event", {"timeout": 1}),
        action("swarm_finish_bootstrap", {"summary": "Own /login token recovery"}),
        action("swarm_publish_evidence", {
            "kind": "credential", "target": "/login", "summary": "recovered token",
            "content": content, "visibility": "swarm",
        }),
        action("swarm_send", {
            "to": "worker-2", "kind": "handoff", "body": "Use the recovered token on /admin",
            "evidence_ids": [evidence_id],
        }),
        action("swarm_finish_cycle", {"contribution": "published a token for peer 2"}),
    ])
    peer2 = ScriptedBackend([
        action("swarm_status", {}),
        action("swarm_create_work", {
            "kind": "auth", "title": "Validate peer token on admin",
            "target": "/admin", "hypothesis": "peer token grants admin access",
            "priority": 10, "claim": True,
        }),
        action("swarm_send", {
            "to": "broadcast", "kind": "coordination",
            "body": "I own the peer-token /admin validation lane.",
        }),
        action("await_swarm_event", {"timeout": 1}),
        action("swarm_finish_bootstrap", {"summary": "Own /admin token validation"}),
        action("swarm_query_evidence", {"evidence_id": evidence_id}),
        action("dispatch_pentester", {
            "attack_idea": "use peer token for admin access", "target": "/admin",
            "context": content,
        }),
        action("await_subagents", {"which": "all"}),
        action("dispatch_validator", {**FINDING, "source_subagent": "peer-chain"}),
        action("await_subagents", {"which": "all"}),
        action("finalize", {"summary": "handoff led to one independently confirmed finding"}),
    ])
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="find a chained bug", backend="echo", execution_mode="c",
        swarm_size=2, use_container=False, yolo=True, max_turns=16,
    )
    orch = Orchestrator(cfg)
    scripts = {"worker-1": peer1, "worker-2": peer2}
    orch._swarm_backend_factory = lambda identity: scripts[identity.worker_id]
    orch._swarm_summarizer_backend_factory = lambda: ScriptedBackend(["combined swarm response"])
    orch._subagent_backend_factory = lambda rec: ScriptedBackend([pentester_final()])
    orch.validator_backend = ScriptedBackend([validator_final()])
    orch.router = AgentRouter(
        store=orch.findings_store, backend=orch.validator_backend,
        tools=orch.registry.catalog(), project_context=None,
        validator_max_turns=orch.cfg.validator_max_turns,
    )
    runtime = SwarmRuntime(orch, None)
    try:
        summary = await runtime.run_cycle(cfg.goal)
        events = runtime.store.events()
    finally:
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()

    assert summary == "combined swarm response"
    assert runtime.workers[0].rate_limiter is not runtime.workers[1].rate_limiter
    assert runtime.workers[0].identity.actor_id != runtime.workers[1].identity.actor_id
    assert runtime.workers[1].subagents.attempts[0].subagent_id.startswith(
        runtime.workers[1].identity.actor_id + "__"
    )
    confirmed = orch.findings_store.all_records("confirmed")
    assert len(confirmed) == 1 and confirmed[0].finding.cwe == "CWE-287"
    assert (tmp_path / ".pentest" / "report.md").exists()
    ui_events = []
    while not orch.ui_queue.empty():
        ui_events.append(orch.ui_queue.get_nowait())
    assert sum(isinstance(event, SessionSummary) for event in ui_events) == 1
    session_summary = next(event for event in ui_events if isinstance(event, SessionSummary))
    assert session_summary.confirmed == [{
        "id": confirmed[0].id,
        "title": "Admin access with leaked token",
        "cvss_score": 7.7,
        "file_path": str(tmp_path / ".pentest" / "findings" / "confirmed" / f"{confirmed[0].id}.md"),
    }]
    assert sum(
        isinstance(event, AgentTextDelta) and "combined swarm response" in event.text
        for event in ui_events
    ) == 1
    phases = [event.phase for event in ui_events if isinstance(event, SwarmPhase)]
    assert phases == ["bootstrap", "consolidating"]

    peer2_history = "\n".join(text for turn in peer2.histories for text in turn)
    assert evidence_id in peer2_history
    assert content in peer2_history
    kinds = [event["kind"] for event in events]
    assert "evidence_published" in kinds
    assert "message_accepted" in kinds
    assert "candidate_reserved" in kinds
    assert "candidate_verdict" in kinds
    assert "validation_started" in kinds
    assert "cycle_completed" in kinds
    audit = orch.audit.path.read_text(encoding="utf-8")
    assert '"kind":"swarm_bootstrap_completed"' in audit


def test_swarm_prompt_requires_coordination_bootstrap_before_testing():
    backend = ScriptedBackend([final("unused")])
    loop = build_swarm_orchestrator_loop(
        backend, [], project_context=None, max_turns=4, session=None,
    )
    prompt = loop.system_prompt
    assert "Mandatory initial bootstrap" in prompt
    assert "swarm_finish_bootstrap" in prompt
    assert "coordination-only bootstrap" in prompt
    assert "call target-facing tools" in prompt
    assert "Do not launch a child and negotiate ownership afterward" in prompt
    assert "Do not repeat the bootstrap" in prompt
    assert "broadcast a generic roll call" in prompt


async def test_initial_bootstrap_blocks_dispatch_until_completed(tmp_path):
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="test bootstrap gate", backend="echo", execution_mode="c",
        swarm_size=2, use_container=False, yolo=True, max_turns=4,
    )
    orch = Orchestrator(cfg)
    backends = {
        "worker-1": ScriptedBackend([
            action("dispatch_pentester", {"attack_idea": "too early", "target": "/x"}),
            final("blocked as expected"), final("still blocked"), final("turn cap"),
        ]),
        "worker-2": ScriptedBackend([
            final("no work"), final("still no work"), final("still blocked"), final("turn cap"),
        ]),
    }
    orch._swarm_backend_factory = lambda identity: backends[identity.worker_id]
    orch._swarm_summarizer_backend_factory = lambda: ScriptedBackend(["summary"])
    runtime = SwarmRuntime(orch, None)
    try:
        await runtime.run_cycle(cfg.goal)
        audit = orch.audit.path.read_text(encoding="utf-8")
        worker_history = "\n".join(
            content for turn in backends["worker-1"].histories for content in turn
        )
        cycle_id = runtime.store._db.execute(
            "SELECT cycle_id FROM response_cycles ORDER BY created_at DESC LIMIT 1"
        ).fetchone()["cycle_id"]
        contributions = runtime.store.cycle(cycle_id)["contributions"]
    finally:
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()

    assert '"kind":"dispatch_pentester"' not in audit
    assert "initial swarm bootstrap incomplete" in worker_history
    assert {item["status"] for item in contributions} == {"unavailable"}
    assert '"kind":"swarm_bootstrap_completed"' not in audit


async def test_followup_unwraps_json_and_does_not_repeat_engagement_report(tmp_path):
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="initial assessment", backend="echo", execution_mode="c",
        swarm_size=2, use_container=False, yolo=True, max_turns=4,
    )
    orch = Orchestrator(cfg)
    scripts = iter([
        ScriptedBackend([final("initial one")]), ScriptedBackend([final("initial two")]),
        ScriptedBackend([final("followup one")]), ScriptedBackend([final("followup two")]),
    ])
    orch._swarm_backend_factory = lambda _identity: next(scripts)
    summaries = iter([
        "initial response",
        json.dumps({"type": "final", "content": "clean follow-up answer"}),
    ])
    orch._swarm_summarizer_backend_factory = lambda: ScriptedBackend([next(summaries)])
    runtime = SwarmRuntime(orch, None)
    try:
        assert await runtime.run_cycle(cfg.goal) == "initial response"
        first_events = []
        while not orch.ui_queue.empty():
            first_events.append(orch.ui_queue.get_nowait())
        assert await runtime.run_cycle("How many High findings?") == "clean follow-up answer"
        second_events = []
        while not orch.ui_queue.empty():
            second_events.append(orch.ui_queue.get_nowait())
        audit = orch.audit.path.read_text(encoding="utf-8")
    finally:
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()

    assert sum(isinstance(event, SessionSummary) for event in first_events) == 1
    assert not any(isinstance(event, SessionSummary) for event in second_events)
    deltas = "".join(
        event.text for event in second_events if isinstance(event, AgentTextDelta)
    )
    assert "clean follow-up answer" in deltas
    assert '"type": "final"' not in deltas
    assert '"kind":"swarm_followup_response"' in audit


async def test_transient_top_level_worker_error_is_retried(tmp_path, monkeypatch):
    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("zero.swarm.worker.asyncio.sleep", no_delay)
    cfg = OrchestratorConfig(
        cwd=tmp_path, goal="finish despite a transient failure", backend="echo",
        execution_mode="c", swarm_size=2, use_container=False, yolo=True, max_turns=4,
    )
    orch = Orchestrator(cfg)
    backends = {
        "worker-1": TransientFailureBackend([final("recovered contribution")]),
        "worker-2": ScriptedBackend([final("steady contribution")]),
    }
    orch._swarm_backend_factory = lambda identity: backends[identity.worker_id]
    orch._swarm_summarizer_backend_factory = lambda: ScriptedBackend(["combined response"])
    runtime = SwarmRuntime(orch, None)
    # Exercise turn-error recovery independently from the initial ownership protocol.
    runtime._cycle_count = 1
    try:
        assert await runtime.run_cycle(cfg.goal) == "combined response"
        cycle = runtime.store._db.execute(
            "SELECT cycle_id FROM response_cycles ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        contributions = runtime.store.cycle(cycle["cycle_id"])["contributions"]
        audit = orch.audit.path.read_text(encoding="utf-8")
    finally:
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()

    assert {item["status"] for item in contributions} == {"complete"}
    assert "recovered contribution" in {item["body"] for item in contributions}
    assert '"kind":"swarm_turn_error"' in audit
    assert '"kind":"swarm_worker_error"' not in audit


def test_cli_rejects_swarm_for_non_orchestrated_modes_and_ralph():
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--execution-mode", "a", "--swarm-size", "2"])
    assert result.exit_code == 2
    assert "supported only" in result.output
    result = runner.invoke(main, ["run", "--swarm-size", "2", "--ralph"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_cli_swarm_size_has_no_arbitrary_upper_bound():
    runner = CliRunner()
    # Mode validation happens after Click has accepted the numeric size.
    result = runner.invoke(main, ["run", "--swarm-size", "33", "--execution-mode", "a"])
    assert result.exit_code == 2
    assert "supported only" in result.output
    assert "not in the range" not in result.output

    result = runner.invoke(main, ["run", "--swarm-size", "0"])
    assert result.exit_code == 2
    assert "not in the range" in result.output


def test_size_one_remains_the_default_legacy_path(tmp_path):
    cfg = OrchestratorConfig(cwd=tmp_path, backend="echo", use_container=False)
    orch = Orchestrator(cfg)
    assert cfg.swarm_size == 1
    assert orch._swarm_runtime is None
    orch.audit.close()
    orch.thinking.close()


async def test_respond_now_marks_active_peers_unavailable_and_still_summarizes(tmp_path):
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c", swarm_size=2,
        use_container=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    backends = {"worker-1": BlockingBackend(), "worker-2": BlockingBackend()}
    orch._swarm_backend_factory = lambda identity: backends[identity.worker_id]
    orch._swarm_summarizer_backend_factory = lambda: ScriptedBackend(["partial response"])
    runtime = SwarmRuntime(orch, None)
    task = asyncio.create_task(runtime.run_cycle("goal"))
    try:
        await asyncio.gather(*(backend.started.wait() for backend in backends.values()))
        assert runtime.respond_now()
        assert await task == "partial response"
        cycles = runtime.store._db.execute(
            "SELECT cycle_id FROM response_cycles ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        contributions = runtime.store.cycle(cycles["cycle_id"])["contributions"]
        assert {item["status"] for item in contributions} == {"unavailable"}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()


async def test_worker_renews_owned_work_while_long_running_children_are_active(tmp_path):
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c", swarm_size=2,
        use_container=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    runtime = SwarmRuntime(orch, None)
    worker = SwarmWorker(runtime, runtime.identities[0], ScriptedBackend([]))
    work_id, _ = runtime.store.create_work(
        "worker-1", kind="recon", title="long task", target="/slow",
        hypothesis="a child needs longer than the initial lease",
    )
    claim = runtime.store.claim_work("worker-1", work_id, lease_seconds=0.6)
    assert claim is not None
    worker._remember_claim(claim, 0.6)
    renewal = asyncio.create_task(worker._renew_work_leases())
    try:
        await asyncio.sleep(0.35)
        current = next(
            item for item in runtime.store.list_work() if item["work_id"] == work_id
        )
        assert current["lease_expires_at"] > claim.lease_expires_at
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        await runtime.close()
        orch.audit.close()
        orch.thinking.close()


async def test_stopped_worker_releases_validator_candidate_for_retry(tmp_path):
    cfg = OrchestratorConfig(
        cwd=tmp_path, backend="echo", execution_mode="c", swarm_size=2,
        use_container=False, yolo=True,
    )
    orch = Orchestrator(cfg)
    runtime = SwarmRuntime(orch, None)
    worker = SwarmWorker(runtime, runtime.identities[0], ScriptedBackend([]))
    candidate_id, acquired = runtime.store.reserve_candidate(
        "candidate-hash", {"title": "recover me"}, "worker-1"
    )
    assert acquired
    runtime.store.start_validation(candidate_id, "validator-cancelled")
    worker._validator_candidates["validator-cancelled"] = candidate_id

    worker._abandon_inflight_validations()
    retry_id, retry_acquired = runtime.store.reserve_candidate(
        "candidate-hash", {"title": "recover me"}, "worker-2"
    )

    assert retry_id == candidate_id and retry_acquired
    await runtime.close()
    orch.audit.close()
    orch.thinking.close()
