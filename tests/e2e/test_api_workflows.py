"""Public lifecycle through real LiteLLM/HTTP, with deterministic local answers."""
import asyncio
import hashlib
import json
import re

import pytest

from zero.llm.api import ApiConfig
from zero.orchestrator.audit import read_audit
from zero.orchestrator.orchestrator import Orchestrator, OrchestratorConfig
from zero.ralph import state
from zero.ralph.prd import load_prd
from zero.ui.events import ApprovalPrompt, AskUser, SessionSummary, AgentTextDelta
from zero.orchestrator.approval import Decision


FINDING = {
    "title": "Fixture marker exposed", "summary": "benign synthetic fixture finding",
    "endpoint": "GET /fixture", "method": "GET", "cwe": "CWE-200",
    "severity_hint": "low", "evidence": "fixture-marker-alpha", "payload_shape": "marker",
}


def action(tool, args):
    return json.dumps({"type": "action", "tool": tool, "args": args})


def final(answer):
    return json.dumps({"type": "final", "answer": answer if isinstance(answer, str)
                       else json.dumps(answer)})


def cfg_for(server, path, **options):
    return OrchestratorConfig(
        cwd=path, backend="api", model="openai/zero-loopback", context_window=500_000,
        api=ApiConfig(base_url=server.base_url, key_env="ZERO_TEST_API_KEY",
                      max_output_tokens=512, max_concurrency=2),
        goal="Inspect only the supplied benign fixture files", use_container=False,
        max_turns=16, validator_max_turns=4, pentester_max_turns=4,
        yolo=True, llm_timeout_seconds=15, **options,
    )


async def run_public(cfg, monkeypatch, *, followup=False):
    import zero.ui.app

    async def ui(orch):
        final_seen = False
        summaries = 0
        followup_sent = False
        while True:
            event = await asyncio.wait_for(orch.ui_queue.get(), timeout=25)
            if isinstance(event, ApprovalPrompt):
                orch.submit_approval(event.call_id, Decision.YES)
            elif isinstance(event, AskUser):
                orch.submit_ask_user_answer(event.ask_id, "proceed")
            elif isinstance(event, SessionSummary):
                if followup and not followup_sent:
                    orch.user_queue.put_nowait("Summarize existing evidence without further testing")
                    followup_sent = True
                    continue
                if not cfg.ralph or event.report_path.endswith("ralph_report.md"):
                    return
            elif isinstance(event, AgentTextDelta):
                if "[final]" in event.text:
                    final_seen = True
                if "Combined API swarm response" in event.text:
                    summaries += 1
                    if followup and summaries == 2:
                        return
            # Mode a returns to its prompt after a final; its shutdown summary is
            # produced only when the UI exits. Let background validation finish.
            if cfg.execution_mode == "a" and final_seen and orch.findings_store.all_records("confirmed"):
                return

    monkeypatch.setattr(zero.ui.app, "run_ui", ui)
    orch = Orchestrator(cfg)
    (orch._files_root_abs / "fixture.txt").write_text("fixture-marker-alpha")
    await asyncio.wait_for(orch.run_interactive(), timeout=30)
    assert orch._api_runtime._closed
    assert orch._api_runtime._session.closed
    return orch


class WorkflowProvider:
    def __init__(self, *, single=False, ralph=False, swarm=False):
        self.single, self.ralph, self.swarm = single, ralph, swarm
        self.roles = []
        self.content = "fixture-marker-alpha"
        self.evidence_id = "ev_" + hashlib.sha256(self.content.encode()).hexdigest()[:16]

    def __call__(self, body):
        messages = body["messages"]
        system = messages[0]["content"]
        turn = sum(m["role"] == "assistant" for m in messages)
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        if system.startswith("You consolidate"):
            self.roles.append("summary")
            assert "response_format" not in body
            return "Combined API swarm response with independently validated evidence."
        if system.startswith("# Analyzer-Validator"):
            self.roles.append("reviewer")
            if user.startswith("JOB: judge task completion"):
                iteration = int(re.search(r"iteration (\d+)", user)[1])
                return final({"verdicts": [{"task_id": f"t{iteration}", "completed": True,
                                            "reason": "fixture evidence inspected"}]})
            return final({"approved": True, "feedback": "", "missing": []})
        if system.startswith("# Analyzer"):
            self.roles.append("analyzer")
            if "JOB: produce the TASKS" in user:
                return final({"goal": "inspect fixtures", "tasks": [
                    {"id": f"t{i}", "epic_id": "e1", "priority": i,
                     "title": f"inspect fixture hypothesis {i}",
                     "acceptanceCriteria": "read fixture and exhaust hypothesis"} for i in (1, 2)
                ]})
            return final({"goal": "inspect fixtures", "epics": [{"id": "e1", "title": "Fixtures"}]})
        if system.startswith("# Validator"):
            self.roles.append("validator")
            if turn == 0:
                return action("read_file", {"path": "fixture.txt"})
            assert "fixture-marker-alpha" in user
            return final({"verdict": "confirmed", "rationale": "independent fixture read",
                          "reproducer": "read fixture.txt", "cwe": "CWE-200",
                          "cvss40_score": 0.0, "cvss40_vector": ""})
        if system.startswith("# Pentest"):
            self.roles.append("pentester")
            if turn == 0:
                return action("read_file", {"path": "fixture.txt"})
            assert "fixture-marker-alpha" in user
            if self.single:
                return action("finding_submit", FINDING) if turn == 1 else final("done")
            return final({"status": "NO_FINDING" if self.ralph else "EXPLOITED",
                          "summary": "fixture-marker-alpha read and hypothesis exhausted",
                          "candidate_findings": [] if self.ralph else [FINDING], "discovered": []})
        self.roles.append("orchestrator")
        if self.swarm:
            return self._peer(user, turn)
        script = [
            action("dispatch_pentester", {"attack_idea": "read fixture", "target": "/fixture"}),
            action("await_subagents", {"which": "all"}),
        ]
        if not self.ralph:
            script += [action("dispatch_validator", {**FINDING, "source_subagent": "#1"}),
                       action("await_subagents", {"which": "all"})]
        script += [action("finalize", {"summary": "fixture hypothesis exhausted"})]
        return script[min(turn, len(script) - 1)]

    def _peer(self, user, turn):
        peer = re.search(r"YOUR ID: (worker-\d+)", user)[1]
        if "CYCLE MODE: FOLLOW-UP" in user:
            return action("swarm_finish_cycle", {"contribution": "Existing evidence reviewed"})
        script = [action("swarm_status", {}), action("swarm_create_work", {
            "kind": "recon", "title": f"Fixture coverage {peer}", "target": f"/{peer}",
            "hypothesis": f"Distinct fixture lane {peer}", "claim": True,
        }), action("swarm_send", {"to": "broadcast", "kind": "coordination",
                                  "body": f"I own /{peer}; avoid overlap."}),
            action("await_swarm_event", {"timeout": 1}),
            action("swarm_finish_bootstrap", {"summary": f"Own /{peer}"})]
        if peer == "worker-1":
            script += [action("swarm_publish_evidence", {"kind": "artifact", "target": "/fixture",
                "summary": "fixture marker handoff", "content": self.content, "visibility": "swarm"}),
                action("swarm_send", {"to": "worker-2", "kind": "handoff",
                    "body": "Inspect this fixture marker", "evidence_ids": [self.evidence_id]}),
                action("swarm_finish_cycle", {"contribution": "Shared fixture marker"})]
        else:
            script += [action("swarm_query_evidence", {"evidence_id": self.evidence_id}),
                action("dispatch_pentester", {"attack_idea": "use shared fixture marker",
                    "target": "/fixture", "context": self.content}),
                action("await_subagents", {"which": "all"}),
                action("dispatch_validator", {**FINDING, "source_subagent": "peer-chain"}),
                action("await_subagents", {"which": "all"}),
                action("finalize", {"summary": "Shared marker independently validated"})]
        return script[min(turn, len(script) - 1)]


@pytest.mark.parametrize("mode", ["a", "c"])
async def test_api_normal_modes_execute_tools_and_independent_validation(api_server, tmp_path,
                                                                       monkeypatch, mode):
    provider = WorkflowProvider(single=mode == "a")
    api_server.respond = provider
    orch = await run_public(cfg_for(api_server, tmp_path, execution_mode=mode), monkeypatch)
    assert len(orch.findings_store.all_records("confirmed")) == 1
    assert {"pentester", "validator"} <= set(provider.roles)
    if mode == "c":
        assert "orchestrator" in provider.roles
    if mode == "c":
        assert (tmp_path / ".pentest" / "report.md").exists()
    events = read_audit(orch.audit.path)
    assert not any(e["kind"] in {"turn_error", "subagent_turn_error", "validator_crashed"} for e in events)
    assert any(e["kind"] == "api_call" for e in events)


async def test_api_ralph_epics_two_passes_and_resume_use_fresh_actor_histories(api_server, tmp_path,
                                                                          monkeypatch):
    provider = WorkflowProvider(ralph=True)
    api_server.respond = provider
    common = dict(ralph=True, ralph_iterations=1, ralph_approve_prd=False,
                  ralph_discussion_rounds=1)
    await run_public(cfg_for(api_server, tmp_path, **common), monkeypatch)
    first_prd = load_prd(state.prd_path(tmp_path))
    assert first_prd.by_id("t1").status == "completed"
    assert first_prd.by_id("t2").status == "pending"
    split = len(api_server.requests)
    planner_count = provider.roles.count("analyzer")
    await run_public(cfg_for(api_server, tmp_path, ralph_resume=True, **common), monkeypatch)
    assert provider.roles.count("analyzer") == planner_count
    prd = load_prd(state.prd_path(tmp_path))
    assert all(t.status == "completed" for t in prd.tasks)
    for i in (1, 2):
        assert (tmp_path / ".ralph_logs" / f".pentest_{i}").is_dir()
    assert state.final_report_path(tmp_path).exists()
    first_resumed = api_server.requests[split]["body"]["messages"]
    assert not any(m["role"] == "assistant" for m in first_resumed)
    assert "reviewer" in provider.roles
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix != ".sqlite3":
            assert "fake-loopback-provider-key" not in path.read_text(errors="replace")


async def test_api_swarm_handoff_children_validator_and_plain_summary(api_server, tmp_path, monkeypatch):
    provider = WorkflowProvider(swarm=True)
    api_server.respond = provider
    orch = await run_public(cfg_for(api_server, tmp_path, swarm_size=2), monkeypatch)
    assert len(orch.findings_store.all_records("confirmed")) == 1
    assert {"orchestrator", "pentester", "validator", "summary"} <= set(provider.roles)
    events = read_audit(orch.audit.path)
    assert sum(e["kind"] == "swarm_bootstrap_completed" for e in events) == 2
    assert not any(e["kind"] in {"swarm_worker_error", "swarm_summarizer_error"} for e in events)
    assert sum(e["kind"] == "swarm_report" for e in events) == 1
    peers = [r["body"]["messages"] for r in api_server.requests
             if "YOUR ID: worker-2" in str(r["body"]["messages"])]
    assert any(provider.evidence_id in str(messages) and provider.content in str(messages)
               for messages in peers)


async def test_api_swarm_followup_uses_fresh_history_without_duplicate_report(api_server, tmp_path,
                                                                         monkeypatch):
    provider = WorkflowProvider(swarm=True)
    api_server.respond = provider
    orch = await run_public(cfg_for(api_server, tmp_path, swarm_size=2), monkeypatch, followup=True)
    events = read_audit(orch.audit.path)
    assert provider.roles.count("summary") == 2
    assert sum(e["kind"] == "swarm_report" for e in events) == 1
    assert sum(e["kind"] == "swarm_bootstrap_completed" for e in events) == 2
    followups = [r["body"]["messages"] for r in api_server.requests
                 if "CYCLE MODE: FOLLOW-UP" in str(r["body"]["messages"])]
    assert len(followups) == 2
    assert all(not any(m["role"] == "assistant" for m in history) for history in followups)
