import asyncio
from pathlib import Path

import pytest

from zero_web.config import RunConfig
from zero_web.storage import Store

API = "/api/v1"


async def create(client, name="First", **kwargs):
    r = await client.post(
        API + "/engagements",
        json={
            "name": name,
            "target": "https://app.example",
            "context": "# Isolated context\n" + name,
            **kwargs,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def eventually(client, sid, predicate, timeout=12):
    async with asyncio.timeout(timeout):
        while True:
            response = await client.get(API + f"/sessions/{sid}/snapshot")
            session = response.json()
            if predicate(session["snapshot"]):
                return session
            if session["snapshot"]["lifecycle"] in {"failed", "interrupted"}:
                raise AssertionError(session)
            await asyncio.sleep(0.05)


async def start(client, eid, mode="a", **extra):
    cfg = RunConfig().model_dump()
    cfg.update(container=False, execution_mode=mode, goal="Inspect the offline fixture", **extra)
    r = await client.post(
        API + f"/engagements/{eid}/sessions", json={"config": cfg, "idempotency_key": eid}
    )
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


async def resolve(client, sid, interaction, decision="yes"):
    kind = "approvals" if interaction["kind"] == "approval_prompt" else "questions"
    body = {"decision": decision} if kind == "approvals" else {"answer": "Orders"}
    return await client.post(
        API + f"/sessions/{sid}/{kind}/{interaction['id']}",
        json={**body, "idempotency_key": interaction["id"]},
    )


@pytest.mark.parametrize("kind", ["approvals", "questions", "plans"])
async def test_ack_retires_interaction_before_delayed_worker_event(client, monkeypatch, kind):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    a = await create(client)
    store = client._transport.app.state.store
    manager = client._transport.app.state.manager
    session = store.create_session(a["id"], RunConfig().model_dump())
    sid, rid = session["id"], "waiting-for-operator"
    event_kind = {
        "approvals": "approval_prompt",
        "questions": "ask_user",
        "plans": "plan_checkpoint",
    }[kind]
    store.append(sid, {"kind": event_kind, "request_id": rid, "free_form": True})
    # The worker accepts the reply, but its event pump has not published
    # InteractionResolved yet. This ordering is common on Windows.
    command = AsyncMock(return_value={})
    monkeypatch.setattr(manager, "worker", lambda _: SimpleNamespace(command=command))
    url = API + f"/sessions/{sid}/{kind}/{rid}"
    body = {"answer": "Orders"} if kind == "questions" else {"decision": "yes"}
    if kind == "plans":
        body = {"decision": "approve", "revision": "fixture-revision"}
    response = await client.post(url, json={**body, "idempotency_key": "first"})
    assert response.status_code == 200, response.text
    assert rid not in store.session(sid)["snapshot"]["interactions"]
    assert (await client.post(url, json={**body, "idempotency_key": "first"})).status_code == 200
    assert (await client.post(url, json={**body, "idempotency_key": "second"})).status_code == 409
    command.assert_awaited_once()


async def test_local_boundary_context_conflict_and_containment(client):
    assert (
        await client.post(API + "/engagements", json={}, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert (
        await client.post(API + "/engagements", json={}, headers={"X-Zero-CSRF": ""})
    ).status_code == 403
    a = await create(client)
    path = API + f"/engagements/{a['id']}"
    assert (
        await client.put(path + "/context", json={"revision": 0, "content": "stale"})
    ).status_code == 409
    assert (await client.put(path + "/context", json={"revision": 1, "content": "saved"})).json()[
        "revision"
    ] == 2
    for name in ("../escape", "/tmp/escape", "a/../../escape", ".pentest/secrets", "C:\\escape"):
        assert (
            await client.post(path + "/files", json={"name": name, "content": "x"})
        ).status_code == 422
    assert (
        await client.post(
            path + "/files", json={"name": "source/api.txt", "content": "<script>evil()</script>"}
        )
    ).status_code == 200
    assert (
        await client.post(path + "/files", json={"name": "source/api.txt", "content": "overwrite"})
    ).status_code == 409
    assert (await client.get(path + "/context")).json()["content"] == "saved"


async def test_symlink_import_and_artifact_read_rejected(client):
    a = await create(client)
    store = client._transport.app.state.store
    work = store.work(a["id"])
    outside = work.parent / "secret.txt"
    outside.write_text("secret")
    (work / "zero_files" / "link").symlink_to(outside)
    path = API + f"/engagements/{a['id']}"
    assert (
        await client.post(path + "/files", json={"name": "link", "content": "override"})
    ).status_code == 422
    files = (await client.get(path + "/artifacts")).json()
    assert not any("link" in f["path"] for f in files)
    assert outside.read_text() == "secret"


@pytest.mark.parametrize("mode", ["a", "c"])
async def test_real_worker_approval_question_validation_history(client, mode):
    a = await create(client)
    sid = await start(client, a["id"], mode)
    session = await eventually(client, sid, lambda s: bool(s["interactions"]))
    interaction = next(iter(session["snapshot"]["interactions"].values()))
    assert interaction["kind"] == "approval_prompt"
    assert interaction["execution_location"] == "host"
    assert (await resolve(client, sid, interaction)).status_code == 200
    # Idempotent replay is a transport acknowledgement, never a second grant.
    assert (await resolve(client, sid, interaction)).status_code == 200
    stale = await client.post(
        API + f"/sessions/{sid}/approvals/{interaction['id']}",
        json={"decision": "yes", "idempotency_key": "different"},
    )
    assert stale.status_code == 409
    session = await eventually(
        client, sid, lambda s: any(p["kind"] == "ask_user" for p in s["interactions"].values())
    )
    question = next(
        p for p in session["snapshot"]["interactions"].values() if p["kind"] == "ask_user"
    )
    assert question["actor_id"] in {"agent", "orchestrator"}
    assert (await resolve(client, sid, question)).status_code == 200
    session = await eventually(
        client, sid, lambda s: bool(s["findings"]) and s["lifecycle"] == "idle"
    )
    files = (await client.get(API + f"/engagements/{a['id']}/findings")).json()
    assert any(f["verdict"] == "confirmed" and f["validation"]["rationale"] for f in files)
    events = client._transport.app.state.store.events(sid)
    assert len({e["seq"] for e in events}) == len(events)
    assert all(e["session_id"] == sid for e in events)
    assert (
        await client.post(API + f"/sessions/{sid}/close", json={"idempotency_key": "close"})
    ).status_code == 200
    closed = (await client.get(API + f"/sessions/{sid}/snapshot")).json()
    assert closed["snapshot"]["lifecycle"] == "closed"
    assert closed["history_path"]
    assert not closed["snapshot"]["interactions"]
    historical = (
        await client.get(API + f"/engagements/{a['id']}/artifacts?session_id={sid}")
    ).json()
    memory = next(
        f
        for f in historical
        if f["path"] == f".pentest/memory/{'agent' if mode == 'a' else 'orchestrator'}.md"
    )
    text = (
        await client.get(API + f"/engagements/{a['id']}/artifacts/{memory['id']}?session_id={sid}")
    ).json()
    assert "Isolated" in text["content"]
    assert text["sealed"]


async def test_concurrent_engagement_workers_and_cross_request_rejection(client):
    a, b = await create(client, "Alpha"), await create(client, "Beta")
    s1, s2 = await asyncio.gather(start(client, a["id"]), start(client, b["id"]))
    one, two = await asyncio.gather(
        eventually(client, s1, lambda s: bool(s["interactions"])),
        eventually(client, s2, lambda s: bool(s["interactions"])),
    )
    r1 = next(iter(one["snapshot"]["interactions"].values()))
    assert (await resolve(client, s2, r1)).status_code == 409
    workers = client._transport.app.state.manager.workers
    assert workers[s1].process.pid != workers[s2].process.pid
    assert (await resolve(client, s1, r1, decision="no")).status_code == 200
    assert (
        next(iter(two["snapshot"]["interactions"]))
        in (await client.get(API + f"/sessions/{s2}/snapshot")).json()["snapshot"]["interactions"]
    )
    workers[s1].process.kill()
    async with asyncio.timeout(10):
        while (await client.get(API + f"/sessions/{s1}/snapshot")).json()["snapshot"][
            "lifecycle"
        ] != "interrupted":
            await asyncio.sleep(0.05)
    assert workers[s2].process.returncode is None
    assert not (await client.get(API + f"/sessions/{s1}/snapshot")).json()["snapshot"][
        "interactions"
    ]
    assert (
        await client.get(API + f"/engagements/{a['id']}/artifacts?session_id={s2}")
    ).status_code == 422


def test_restart_marks_unrecovered_sessions_interrupted(tmp_path: Path):
    from zero_web.sessions import Manager

    s = Store(tmp_path)
    a = s.create("Attack", "https://example.test", "# Scope")
    session = s.create_session(a["id"], RunConfig().model_dump())
    Manager(s)
    assert s.session(session["id"])["snapshot"]["lifecycle"] == "interrupted"
    s.db.close()


async def test_ralph_revision_checkpoint_pass_and_resume(client):
    a = await create(client)
    sid = await start(client, a["id"], "c", ralph=True, ralph_iterations=1)
    session = await eventually(
        client,
        sid,
        lambda s: any(p["kind"] == "plan_checkpoint" for p in s["interactions"].values()),
    )
    plan = next(
        p for p in session["snapshot"]["interactions"].values() if p["kind"] == "plan_checkpoint"
    )
    assert plan["plan"]["tasks"]
    url = API + f"/sessions/{sid}/plans/{plan['id']}"
    r = await client.post(
        url,
        json={
            "decision": "revise",
            "revision": plan["revision"],
            "feedback": "Focus on authorization evidence",
            "idempotency_key": "revise",
        },
    )
    assert r.status_code == 200, r.text
    revised = await eventually(
        client,
        sid,
        lambda s: any(
            p["kind"] == "plan_checkpoint" and p["revision"] != plan["revision"]
            for p in s["interactions"].values()
        ),
    )
    fresh = next(
        p for p in revised["snapshot"]["interactions"].values() if p["kind"] == "plan_checkpoint"
    )
    stale = await client.post(
        url,
        json={
            "decision": "approve",
            "revision": plan["revision"],
            "idempotency_key": "old-approval",
        },
    )
    assert stale.status_code == 409
    r = await client.post(
        API + f"/sessions/{sid}/plans/{fresh['id']}",
        json={"decision": "approve", "revision": fresh["revision"], "idempotency_key": "approve"},
    )
    assert r.status_code == 200, r.text
    session = await eventually(client, sid, lambda s: s["lifecycle"] == "closed", timeout=15)
    artifacts = (
        await client.get(API + f"/engagements/{a['id']}/artifacts?session_id={sid}")
    ).json()
    assert any(".ralph_logs/.pentest_1/" in a["path"] for a in artifacts)
    prd = next(a for a in artifacts if a["path"] == ".ralph/prd.json")
    content = (
        await client.get(API + f"/engagements/{a['id']}/artifacts/{prd['id']}?session_id={sid}")
    ).json()["content"]
    assert "completed" in content
    cfg = RunConfig().model_dump()
    cfg.update(
        container=False, ralph=True, ralph_resume=True, ralph_iterations=1, goal="Continue review"
    )
    result = await client.post(
        API + f"/engagements/{a['id']}/sessions", json={"config": cfg, "idempotency_key": "resume"}
    )
    assert result.status_code == 201, result.text
    resumed = await eventually(
        client, result.json()["session_id"], lambda s: s["lifecycle"] == "closed"
    )
    assert not resumed["snapshot"]["interactions"]


async def test_swarm_ownership_messages_validation_and_consolidation(client):
    a = await create(client)
    sid = await start(client, a["id"], "c", swarm_size=2, max_turns=12)
    session = await eventually(client, sid, lambda s: s["lifecycle"] == "idle", timeout=15)
    ledger = (await client.get(API + f"/engagements/{a['id']}/swarm")).json()
    assert len(ledger["workers"]) == 2
    assert len(ledger["work_items"]) >= 2
    assert ledger["messages"]
    files = (await client.get(API + f"/engagements/{a['id']}/findings")).json()
    assert any(f["verdict"] == "confirmed" for f in files)
    assert any(a.get("subkind") == "peer" for a in session["snapshot"]["actors"].values())


async def test_binary_import_is_lossless_and_reset_keeps_history(client):
    import base64

    a = await create(client)
    url = API + f"/engagements/{a['id']}"
    original = b"\x00\xff\xfePDF fixture\n"
    result = await client.post(
        url + "/files",
        json={"name": "binary.pdf", "data_base64": base64.b64encode(original).decode()},
    )
    assert result.status_code == 200, result.text
    item = next(a for a in result.json() if a["path"] == "zero_files/binary.pdf")
    assert (await client.get(url + f"/artifacts/{item['id']}?download=1")).content == original
    sid = await start(client, a["id"])
    assert (await client.post(url + "/reset", json={"scope": ".pentest"})).status_code == 409
    await client.post(API + f"/sessions/{sid}/close", json={"idempotency_key": "close"})
    assert (await client.post(url + "/reset", json={"scope": ".pentest"})).status_code == 200
    store = client._transport.app.state.store
    assert not (store.work(a["id"]) / ".pentest").exists()
    assert (store.work(a["id"]) / "pentest.md").exists()
    assert (store.work(a["id"]) / "zero_files/binary.pdf").read_bytes() == original
    assert store.session(sid)["history_path"]


async def test_recorded_replay_is_separate_from_live_and_import_is_readonly(client):
    import json

    a = await create(client)
    url = API + f"/engagements/{a['id']}"
    assert (await client.post(url + "/recordings", json={"content": "not-json"})).status_code == 422
    recording = (
        json.dumps(
            {
                "kind": "agent_step",
                "raw": json.dumps({"type": "final", "answer": "Recorded fixture answer"}),
            }
        )
        + "\n"
    )
    imported = await client.post(url + "/recordings", json={"content": recording})
    assert imported.status_code == 201, imported.text
    assert not (await client.get(url)).json()["session_id"]
    result = await client.post(
        url + "/replay",
        json={
            "artifact_id": imported.json()["id"],
            "config": {"container": False},
            "idempotency_key": "replay",
        },
    )
    assert result.status_code == 201, result.text
    sid = result.json()["session_id"]
    state = await eventually(client, sid, lambda s: s["lifecycle"] == "idle")
    assert state["kind"] == "replay"
    assert state["snapshot"]["effective"]["backend"] == "replay"
    assert any("Recorded fixture answer" in m["text"] for m in state["snapshot"]["messages"])
    assert not state["snapshot"]["findings"]


async def test_replay_recording_path_is_portable_in_worker_protocol(client, monkeypatch):
    from pathlib import PureWindowsPath
    from unittest.mock import AsyncMock

    from starlette.responses import JSONResponse

    from zero_web import app

    a = await create(client)
    store = client._transport.app.state.store
    work = PureWindowsPath("C:/workspace/engagement/work")
    recording = ".recordings/fixture.jsonl"
    monkeypatch.setattr(store, "work", lambda _: work)
    monkeypatch.setattr(app, "artifact", lambda *args: ({"path": recording}, work / recording))
    begin = AsyncMock(return_value=JSONResponse({"session_id": "fixture"}, 201))
    monkeypatch.setattr(app, "begin_session", begin)
    response = await client.post(
        API + f"/engagements/{a['id']}/replay",
        json={"artifact_id": "fixture", "config": {"container": False}, "idempotency_key": "run"},
    )
    assert response.status_code == 201, response.text
    assert begin.call_args.kwargs["replay_path"] == recording


async def test_followup_retains_goal_artifacts_and_original_context(client):
    a = await create(client)
    sid = await start(client, a["id"])
    session = await eventually(client, sid, lambda s: bool(s["interactions"]))
    await resolve(client, sid, next(iter(session["snapshot"]["interactions"].values())))
    session = await eventually(
        client, sid, lambda s: any(p["kind"] == "ask_user" for p in s["interactions"].values())
    )
    await resolve(
        client,
        sid,
        next(p for p in session["snapshot"]["interactions"].values() if p["kind"] == "ask_user"),
    )
    first = await eventually(client, sid, lambda s: s["lifecycle"] == "idle")
    first_run = first["snapshot"]["run_id"]
    store = client._transport.app.state.store
    (store.work(a["id"]) / ".pentest/memory/agent.md").write_text("Second-stage memory")
    await client.put(
        API + f"/engagements/{a['id']}/context",
        json={"revision": 1, "content": "Next session context"},
    )
    await client.post(
        API + f"/sessions/{sid}/messages",
        json={"text": "Summarize the evidence", "idempotency_key": "follow-up"},
    )
    final = await eventually(
        client, sid, lambda s: s["lifecycle"] == "idle" and s["run_id"] != first_run
    )
    assert len(final["runs"]) == 2
    assert all(r["artifact_path"] for r in final["runs"])
    await client.post(API + f"/sessions/{sid}/close", json={"idempotency_key": "close"})
    artifacts = (
        await client.get(API + f"/engagements/{a['id']}/artifacts?session_id={sid}")
    ).json()
    old = next(f for f in artifacts if f["path"] == f"goals/{first_run}/.pentest/memory/agent.md")
    old_text = (
        await client.get(API + f"/engagements/{a['id']}/artifacts/{old['id']}?session_id={sid}")
    ).json()["content"]
    assert "Isolated actor" in old_text
    context = next(f for f in artifacts if f["path"] == "pentest.md")
    old_scope = (
        await client.get(API + f"/engagements/{a['id']}/artifacts/{context['id']}?session_id={sid}")
    ).json()["content"]
    assert old_scope == "# Isolated context\nFirst"
