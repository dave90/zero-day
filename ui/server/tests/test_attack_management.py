"""Fresh templates and scoped deletion, using only the offline worker adapter."""

import asyncio
import shutil
import threading

from zero.orchestrator.ownership import WorkspaceOwnership
from zero_web.config import RunConfig

from test_service import API, create, eventually, start


def store_for(client):
    return client._transport.app.state.store


async def test_template_is_independent_setup_and_creates_fresh_attacks(client):
    source = await create(client, "Source")
    eid = source["id"]
    path = API + f"/engagements/{eid}"
    cfg = RunConfig().model_dump()
    cfg.update(goal="Original goal", model="future-model", ralph=True, ralph_resume=True)
    assert (await client.patch(path, json={"config": cfg, "draft": "unsent follow-up"})).status_code == 200
    work = store_for(client).work(eid)
    for name in (".pentest", ".ralph", ".ralph_logs", ".recordings"):
        (work / name).mkdir()
        (work / name / "old-evidence").write_text("not setup")
    (work / "zero_files" / "private.txt").write_text("upload")
    template = await client.post(API + "/templates", json={"source_id": eid, "name": "API preset"})
    assert template.status_code == 201, template.text
    template = template.json()
    assert template["context"] is None
    assert template["config"] == {**cfg, "ralph_resume": False}
    scoped = (await client.post(API + "/templates", json={
        "source_id": eid, "name": "With scope", "include_context": True,
    })).json()
    assert scoped["context"] == "# Isolated context\nSource"
    await client.patch(path, json={"config": {**cfg, "model": "changed"}})
    assert (await client.request("DELETE", path, json={"confirm_name": "Source"})).status_code == 200
    # A saved template survives the source's deletion and service DB reopen.
    from zero_web.storage import Store

    reopened = Store(store_for(client).root)
    assert reopened.template(template["id"])["config"]["model"] == "future-model"
    reopened.db.close()
    for preset in (template, scoped):
        r = await client.post(API + "/engagements", json={
            "name": "Fresh", "target": "https://new.example", "template_id": preset["id"],
        })
        assert r.status_code == 201, r.text
        fresh = r.json()
        assert fresh["session_id"] is None and fresh["archived"] is False
        assert fresh["config"] == template["config"]
        assert fresh["draft"] == "Original goal"
        fresh_work = store_for(client).work(fresh["id"])
        assert not (fresh_work / "zero_files" / "private.txt").exists()
        assert not any((fresh_work / name).exists() for name in (".pentest", ".ralph", ".recordings"))
        assert store_for(client).sessions(fresh["id"]) == []
        context = (fresh_work / "pentest.md").read_text()
        assert ("Isolated context" in context) == bool(preset["context"])
    assert len((await client.get(API + "/templates")).json()) == 2
    assert (await client.delete(API + f"/templates/{template['id']}")).status_code == 200
    assert len((await client.get(API + "/templates")).json()) == 1


async def test_delete_closes_only_selected_worker_and_removes_all_owned_records(client, tmp_path):
    a, b = await create(client, "Delete me"), await create(client, "Keep me")
    sa, sb = await start(client, a["id"]), await start(client, b["id"])
    await eventually(client, sa, lambda s: bool(s["interactions"]))
    await eventually(client, sb, lambda s: bool(s["interactions"]))
    store, manager = store_for(client), client._transport.app.state.manager
    worker, other = manager.workers[sa], manager.workers[sb]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("other application")
    root = store.work(a["id"]).parent
    (store.work(a["id"]) / "zero_files" / "symlink").symlink_to(outside, target_is_directory=True)
    path = API + f"/engagements/{a['id']}"
    assert (await client.request("DELETE", path, json={"confirm_name": "wrong", "close_session": True})).status_code == 409
    assert (await client.request("DELETE", path, json={"confirm_name": a["name"]})).status_code == 409
    assert worker.process.returncode is None and root.exists()
    r = await client.request("DELETE", path, json={"confirm_name": a["name"], "close_session": True})
    assert r.status_code == 200, r.text
    assert worker.process.returncode is not None and not root.exists()
    assert other.process.returncode is None
    assert (outside / "keep.txt").read_text() == "other application"
    assert (await client.get(path)).status_code == 404
    assert (await client.get(API + f"/sessions/{sa}/snapshot")).status_code == 404
    assert not store.db.execute("SELECT 1 FROM events WHERE session_id=?", (sa,)).fetchone()
    assert store.command(f"{a['id']}:start:{a['id']}") is None
    assert store.command(f"{b['id']}:start:{b['id']}") is not None
    assert len(store.sessions(b["id"])) == 1


async def test_delete_respects_external_ownership_and_root_symlinks(client, tmp_path):
    attack = await create(client)
    store = store_for(client)
    path = API + f"/engagements/{attack['id']}"
    with WorkspaceOwnership(store.work(attack["id"])):
        r = await client.request("DELETE", path, json={"confirm_name": attack["name"]})
    assert r.status_code == 409
    assert store.work(attack["id"]).exists()
    parent = store.work(attack["id"]).parent
    moved = tmp_path / "moved"
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    r = await client.request("DELETE", path, json={"confirm_name": attack["name"]})
    assert r.status_code == 422
    assert (moved / "work" / "pentest.md").exists()


async def test_delete_failure_keeps_index_for_retry(client, monkeypatch):
    attack = await create(client)
    path = API + f"/engagements/{attack['id']}"

    def denied(*args, **kwargs):
        raise PermissionError("fixture")

    with monkeypatch.context() as patch:
        patch.setattr("zero_web.app.shutil.rmtree", denied)
        r = await client.request("DELETE", path, json={"confirm_name": attack["name"]})
    assert r.status_code == 503
    assert (await client.get(path)).status_code == 200
    assert (await client.request("DELETE", path, json={"confirm_name": attack["name"]})).status_code == 200


async def test_invalid_template_and_creation_do_not_create_partial_attack(client):
    attack = await create(client)
    for extra in ({"include_context": "true"}, {"source_id": "missing"}):
        r = await client.post(API + "/templates", json={"source_id": attack["id"], "name": "Preset", **extra})
        assert r.status_code in {404, 422}
    for extra in ({"config": {"max_turns": -1}}, {"template_id": "missing"}):
        r = await client.post(API + "/engagements", json={"name": "Bad", "target": "https://new.example", **extra})
        assert r.status_code in {404, 422}
    assert len((await client.get(API + "/engagements")).json()) == 1
    assert (await client.get(API + "/templates")).json() == []


async def test_delayed_draft_cannot_resurrect_deleted_attack(client, monkeypatch):
    attack = await create(client)
    path = API + f"/engagements/{attack['id']}"
    started, release = threading.Event(), threading.Event()
    remove = shutil.rmtree

    def slow_remove(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return remove(*args, **kwargs)

    monkeypatch.setattr("zero_web.app.shutil.rmtree", slow_remove)
    deleting = asyncio.create_task(client.request("DELETE", path, json={"confirm_name": attack["name"]}))
    assert await asyncio.to_thread(started.wait, 2)
    editing = asyncio.create_task(client.patch(path, json={"draft": "late write"}))
    try:
        await asyncio.sleep(0.05)
        assert not editing.done()
    finally:
        release.set()
    assert (await deleting).status_code == 200
    assert (await editing).status_code == 404
    assert (await client.get(path)).status_code == 404
