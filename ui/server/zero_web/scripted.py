"""Offline fixtures exercising the shared runtime; unavailable in normal service mode."""

from zero.llm.echo import EchoBackend


def configure(orch):
    def action(tool, args):
        return {"type": "action", "tool": tool, "args": args, "thought": "Offline product test"}

    from pydantic import BaseModel
    from zero.tools.base import Tool, ToolResult, RiskLevel

    class FixtureArgs(BaseModel):
        pass

    class FixtureTool(Tool):
        name = "offline_fixture"
        description = "Offline fixture; no target or network access"
        risk_level = RiskLevel.ACTIVE
        Args = FixtureArgs

        async def run(self, args):
            return ToolResult(ok=True, output="Synthetic fixture inspected")

    orch.registry.register(FixtureTool())
    candidate = {
        "title": "Synthetic access control fixture",
        "summary": "Offline fixture only",
        "endpoint": "/fixture",
        "cwe": "CWE-639",
        "method": "GET",
        "evidence": "Synthetic test record returned across fixture users",
    }
    question = {
        "type": "ask_user",
        "question": "Which fixture should I inspect?",
        "options": ["Orders", "Accounts"],
        "free_form": True,
        "blocking": True,
    }
    validation = {
        "type": "final",
        "answer": __import__("json").dumps(
            {
                "verdict": "confirmed",
                "rationale": "Independent scripted validator fixture",
                "reproducer": "Read the isolated synthetic fixture",
                "cwe": "CWE-639",
                "cvss40_score": 5.3,
                "cvss40_vector": "",
            }
        ),
    }
    orch.validator_backend = EchoBackend([validation])
    if orch.cfg.execution_mode == "a":
        orch.backend = EchoBackend(
            [
                action("offline_fixture", {}),
                action("memory_write", {"content": "# Fixture memory\nIsolated actor"}),
                action("todo_write", {"items": [
                    {"step": "Inspect the synthetic fixture", "status": "done"},
                    {"step": "Review independent validation", "status": "in_progress"},
                    {"step": "Prepare the fixture report", "status": "pending"},
                ]}),
                question,
                action("finding_submit", candidate),
                {
                    "type": "final",
                    "answer": "Offline assessment complete. Independent validation recorded.",
                },
            ]
        )
    else:
        import json

        orch.backend = EchoBackend(
            [
                action("offline_fixture", {}),
                action("memory_write", {"content": "# Fixture memory\nIsolated orchestrator"}),
                question,
                action(
                    "dispatch_pentester",
                    {"target": "/fixture", "attack_idea": "Check fixture ownership"},
                ),
                action("await_subagents", {}),
                action("dispatch_validator", candidate),
                action("await_subagents", {}),
                action("finalize", {"summary": "Offline assessment complete."}),
            ]
        )

        def backend(rec):
            if rec.kind == "validator":
                return EchoBackend([validation])
            return EchoBackend(
                [
                    {
                        "type": "final",
                        "answer": json.dumps(
                            {
                                "status": "EXPLOITED",
                                "summary": "Synthetic fixture candidate",
                                "candidate_findings": [candidate],
                                "discovered": [],
                            }
                        ),
                    }
                ]
            )

        orch._subagent_backend_factory = backend

    if orch.cfg.ralph:
        import json
        from zero.llm.backend import LLMBackend, LLMResponse

        class RalphFixture(LLMBackend):
            def __init__(self):
                self.revision = 0
                self.pass_backend = EchoBackend(
                    [
                        action(
                            "dispatch_pentester",
                            {"target": "/fixture", "attack_idea": "Check fixture"},
                        ),
                        action("await_subagents", {"which": "all"}),
                        action("dispatch_validator", candidate),
                        action("await_subagents", {"which": "all"}),
                        action("finalize", {"summary": "Synthetic task reviewed."}),
                    ]
                )

            async def run(self, system_prompt, messages, tools, *, session=None):
                if system_prompt.startswith("# Analyzer-Validator"):
                    data = {
                        "approved": True,
                        "feedback": "Scripted independent review",
                        "missing": [],
                        "verdicts": [
                            {
                                "task_id": "t1",
                                "completed": True,
                                "reason": "Synthetic coverage evidence reviewed",
                            }
                        ],
                    }
                elif system_prompt.startswith("# Analyzer"):
                    self.revision += 1
                    data = {
                        "goal": "Offline planning fixture",
                        "epics": [
                            {
                                "id": "e1",
                                "title": "Fixture surface",
                                "priority": 1,
                                "acceptanceCriteria": "Inventory fixture boundary",
                            }
                        ],
                        "tasks": [
                            {
                                "id": "t1",
                                "title": f"Review fixture · draft {self.revision}",
                                "priority": 1,
                                "epic_id": "e1",
                                "acceptanceCriteria": "Independently validate fixture",
                            }
                        ],
                    }
                else:
                    return await self.pass_backend.run(system_prompt, messages, tools)
                return LLMResponse(text=json.dumps({"type": "final", "answer": json.dumps(data)}))

        orch.backend = RalphFixture()

    if orch.cfg.swarm_size > 1:

        def peer_backend(ident):
            return EchoBackend(
                [
                    action("swarm_status", {}),
                    action(
                        "swarm_create_work",
                        {
                            "kind": "recon",
                            "title": f"Lane {ident.worker_id}",
                            "target": f"/fixture/{ident.worker_id}",
                            "hypothesis": "Isolated fixture boundary",
                            "priority": 10,
                            "claim": True,
                        },
                    ),
                    action(
                        "swarm_send",
                        {
                            "to": "broadcast",
                            "kind": "coordination",
                            "body": f"I own the {ident.worker_id} fixture lane",
                        },
                    ),
                    action("await_swarm_event", {"timeout": 1}),
                    action(
                        "swarm_finish_bootstrap", {"summary": "Fixture lane ownership established"}
                    ),
                    action(
                        "swarm_publish_evidence",
                        {
                            "kind": "observation",
                            "target": "/fixture",
                            "summary": "Synthetic evidence",
                            "content": "Isolated fixture observation",
                            "visibility": "swarm",
                        },
                    ),
                    action("dispatch_validator", {**candidate, "endpoint": f"/fixture/{ident.worker_id}"}),
                    action("dispatch_pentester", {"target": f"/fixture/{ident.worker_id}", "attack_idea": "Check the peer's synthetic fixture"}),
                    action("await_subagents", {"which": "all"}),
                    action(
                        "swarm_finish_cycle",
                        {"contribution": "Synthetic fixture independently reviewed"},
                    ),
                ]
            )

        orch._swarm_backend_factory = peer_backend
        orch._swarm_summarizer_backend_factory = lambda: EchoBackend(
            [{"type": "final", "answer": "Offline Swarm contributions consolidated."}]
        )
