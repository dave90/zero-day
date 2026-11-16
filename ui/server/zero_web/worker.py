"""One engagement per host process. Stdout is exclusively versioned NDJSON IPC."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from zero.orchestrator.approval import Decision
from zero.orchestrator.orchestrator import Orchestrator
from zero.ui.events import InteractionResolved, PlanCheckpoint
from zero_web.config import core_config
from zero_web.stdio import CommandReader

PROTOCOL = sys.stdout
sys.stdout = sys.stderr


def send(data: dict):
    PROTOCOL.write(json.dumps({"version": 1, **data}, default=str) + "\n")
    PROTOCOL.flush()


async def main():
    reader = CommandReader(sys.stdin.fileno())
    first = json.loads(await reader.readline())
    if first.get("version") != 1 or first.get("action") != "init":
        raise ValueError("Expected versioned initialization")
    cfg = core_config(Path.cwd(), first["config"])
    replay_path = first.get("replay_path")
    if replay_path:
        from zero_web.artifacts import contained

        if not replay_path.startswith(".recordings/"):
            raise ValueError("Unregistered replay recording")
        cfg.backend = "replay"
        cfg.replay_audit_path = contained(Path.cwd(), Path.cwd() / replay_path)
    elif first.get("scripted"):
        # Test-only adapter selected by the service process, never by browser config.
        cfg.backend = "echo"
    orch = Orchestrator(cfg)
    orch.initial_run_id = first["run_id"]
    if first.get("scripted") and not replay_path:
        from zero_web.scripted import configure

        configure(orch)
    messages: dict[str, str] = {}
    checkpoint = {}
    current_run = first["run_id"]
    shutdown = asyncio.Event()

    def actor_snapshot():
        from zero.tools._memroot import safe_name

        actors = []

        def add(aid, role, loop, parent="", **extra):
            actors.append(
                {
                    "id": aid,
                    "subkind": role,
                    "parent_id": parent,
                    "turn": getattr(loop, "turn", 0),
                    "context_tokens": getattr(loop, "context_tokens", None),
                    "context_window": getattr(loop, "context_window", None),
                    "memory_path": f".pentest/memory/{safe_name(aid)}.md",
                    "todo_path": f".pentest/todo/{safe_name(aid)}.json",
                    **extra,
                }
            )

        root_id = "agent" if cfg.execution_mode == "a" else "orchestrator"
        add(
            root_id,
            "pentest" if cfg.execution_mode == "a" else "orchestrator",
            orch._active_loop,
            idea=cfg.goal or "",
            state="active",
        )
        managers = [(orch.subagents, root_id)]
        if orch._swarm_runtime is not None:
            for peer in orch._swarm_runtime.workers:
                ident = peer.identity
                add(
                    ident.actor_id,
                    "peer",
                    peer.loop,
                    root_id,
                    state="done" if peer._finished else "running",
                    name=ident.name,
                    peer_id=ident.worker_id,
                    generation=ident.generation,
                )
                managers.append((peer.subagents, ident.actor_id))
        for manager, parent in managers:
            if manager is not None:
                for actor in manager.snapshot():
                    add(
                        actor["id"],
                        actor["kind"],
                        None,
                        parent,
                        state=actor["status"],
                        idea=actor["idea"],
                        target=actor["target"],
                        context_tokens=actor["context_tokens"],
                        context_window=actor["context_window"],
                        turn=actor["turn"],
                    )
        return {"kind": "actor_snapshot", "actors": actors}

    async def emit_events():
        while True:
            event = await orch.ui_queue.get()
            payload = asdict(event)
            event_run = getattr(event, "_run_id", "") or current_run
            actor = payload.get("agent") or payload.get("label") or "orchestrator"
            mid = ""
            if payload["kind"] in {"agent_text_delta", "agent_text_end"}:
                mid = messages.setdefault(actor, uuid.uuid4().hex)
                if payload["kind"] == "agent_text_end":
                    messages.pop(actor, None)
            send({"type": "event", "payload": payload, "message_id": mid, "run_id": event_run})
            if payload["kind"] in {"status", "subagent_started", "subagent_progress", "turn_start"}:
                send({"type": "event", "payload": actor_snapshot()})
            if payload["kind"] == "session_end":
                shutdown.set()

    async def plan(prd):
        data = prd.model_dump(mode="json")
        revision = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        checkpoint[rid] = (revision, future)
        await orch._emit(PlanCheckpoint(request_id=rid, revision=revision, plan=data))
        try:
            return await future
        finally:
            checkpoint.pop(rid, None)
            await orch._emit(InteractionResolved(request_id=rid))

    orch.plan_checkpoint_handler = plan

    async def commands():
        nonlocal current_run
        while line := await reader.readline():
            req = {}
            try:
                req = json.loads(line)
                if req.get("version") != 1:
                    raise ValueError("Unsupported IPC version")
                action, body = req["action"], req.get("body", {})
                result = {}
                if action == "message":
                    if checkpoint:
                        raise ValueError("Resolve the plan checkpoint before sending guidance")
                    current_run = body["run_id"]
                    send(
                        {
                            "type": "event",
                            "payload": {"kind": "user_text", "text": body["text"]},
                            "message_id": body["message_id"],
                            "run_id": current_run,
                        }
                    )
                    orch.submit_user_text(body["text"], run_id=current_run)
                    result = {"outcome": "queued"}
                elif action == "cancel":
                    result = {"cancelled": orch.request_cancel_turn()}
                elif action == "close":
                    shutdown.set()
                elif action == "approval":
                    rid = body["request_id"]
                    fut = orch._approval_replies.get(rid)
                    if fut is None or fut.done():
                        raise ValueError("Approval expired or already resolved")
                    decision = Decision(body["decision"])
                    if decision == Decision.EDIT:
                        raise ValueError("Argument editing is not supported")
                    orch.submit_approval(rid, decision)
                elif action == "answer":
                    rid = body["request_id"]
                    fut = orch._ask_user_replies.get(rid)
                    if fut is None or fut.done():
                        raise ValueError("Question expired or already resolved")
                    orch.submit_ask_user_answer(rid, body["answer"])
                elif action == "plan":
                    revision, future = checkpoint.get(body["request_id"], (None, None))
                    if future is None or future.done() or revision != body["revision"]:
                        raise ValueError("Plan revision expired or already resolved")
                    value = {"approve": "go", "abort": "abort"}.get(body["decision"])
                    if body["decision"] == "revise":
                        value = body.get("feedback", "").strip()
                        if not value or value.lower() in {"go", "approve", "yes", "ok", "start"}:
                            raise ValueError("Provide revision feedback")
                    if value is None:
                        raise ValueError("Invalid plan decision")
                    future.set_result(value)
                elif action == "respond-now":
                    if orch._swarm_runtime is None:
                        raise ValueError("No active Swarm runtime")
                    result = {"accepted": orch._swarm_runtime.respond_now()}
                else:
                    raise ValueError("Unknown command")
                send({"type": "ack", "command_id": req["command_id"], "result": result})
            except Exception as exc:
                send({"type": "ack", "command_id": req.get("command_id", ""), "error": str(exc)})
        # Pipe EOF means the parent is gone; never leave a model/tool worker running.
        shutdown.set()

    command_task = asyncio.create_task(commands())

    async def frontend(_):
        pump = asyncio.create_task(emit_events())
        send({"type": "ack", "command_id": first["command_id"], "result": {"ready": True}})
        try:
            stop = asyncio.create_task(shutdown.wait())
            done, _ = await asyncio.wait(
                {stop, pump, command_task}, return_when=asyncio.FIRST_COMPLETED
            )
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
            for task in done:
                if task is not stop:
                    await task
        finally:
            command_task.cancel()
            await asyncio.gather(command_task, return_exceptions=True)
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)

    try:
        await orch.run_headless(frontend)
    finally:
        command_task.cancel()
        await asyncio.gather(command_task, return_exceptions=True)
        # Include final cleanup events before the process exits.
        while not orch.ui_queue.empty():
            send({"type": "event", "payload": asdict(orch.ui_queue.get_nowait())})


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except BaseException as exc:
        send(
            {
                "type": "event",
                "payload": {"kind": "runtime_error", "error": f"{type(exc).__name__}: {exc}"},
            }
        )
        raise
