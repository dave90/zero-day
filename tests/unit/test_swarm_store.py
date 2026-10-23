import sqlite3

from zero.swarm.store import SwarmStore


def _store(tmp_path, *, clock=lambda: 100.0):
    store = SwarmStore(tmp_path / "state.sqlite3", clock=clock)
    store.register_worker("worker-1", "actor-1", "zero-1")
    store.register_worker("worker-2", "actor-2", "zero-2")
    store.register_worker("worker-3", "actor-3", "zero-3")
    return store


def test_work_claim_is_exclusive_and_expired_lease_is_recoverable(tmp_path):
    now = [100.0]
    store = _store(tmp_path, clock=lambda: now[0])
    work_id, created = store.create_work(
        "worker-1", kind="auth", title="test IDOR", target="/orders/1",
        hypothesis="another user can read the order",
    )
    duplicate_id, duplicate_created = store.create_work(
        "worker-2", kind=" AUTH ", title="same", target=" /orders/1 ",
        hypothesis=" Another user can read the order ",
    )
    assert created is True
    assert (duplicate_id, duplicate_created) == (work_id, False)

    first = store.claim_work("worker-1", work_id, lease_seconds=10)
    assert first is not None
    assert store.claim_work("worker-2", work_id) is None
    assert store.update_work("worker-2", work_id, first.lease_token, action="complete") is False

    now[0] = 111.0
    second = store.claim_work("worker-2", work_id)
    assert second is not None and second.lease_token != first.lease_token
    assert store.update_work("worker-2", work_id, second.lease_token, action="complete")


def test_dependency_unblocks_and_evidence_is_immutable_and_visible(tmp_path):
    store = _store(tmp_path)
    first, _ = store.create_work(
        "worker-1", kind="recon", title="get token", target="/login", hypothesis="token leaks"
    )
    second, _ = store.create_work(
        "worker-2", kind="exploit", title="use token", target="/admin",
        hypothesis="leaked token authorizes admin", dependency_ids=[first],
    )
    assert store.list_work(state="blocked")[0]["work_id"] == second
    claim = store.claim_work("worker-1", first)
    assert claim and store.update_work("worker-1", first, claim.lease_token, action="complete")
    assert {w["work_id"] for w in store.list_work(state="ready")} == {second}

    ev1 = store.publish_evidence(
        "worker-1", kind="token", target="/login", summary="recovered token",
        content="token=abc", work_id=first,
    )
    ev2 = store.publish_evidence(
        "worker-1", kind="token", target="/login", summary="duplicate publication",
        content="token=abc", work_id=first,
    )
    assert ev1.evidence_id == ev2.evidence_id
    assert store.query_evidence("worker-2", evidence_id=ev1.evidence_id)[0]["content"] == "token=abc"


def test_group_message_keeps_recipient_snapshot_and_delivery_states(tmp_path):
    store = _store(tmp_path)
    group_id = store.create_group("worker-1", "chain", "token chain", ["worker-2"])
    message_id = store.send_message(
        "worker-1", to="group", group_id=group_id, body="use evidence ev_1",
        evidence_ids=["ev_1"],
    )
    assert store.inbox("worker-2")[0]["message_id"] == message_id
    assert store.inbox("worker-3") == []
    store.acknowledge("worker-2", [message_id])
    assert store.inbox("worker-2") == []

    row = store._db.execute(
        "SELECT delivered_at,presented_at,acknowledged_at FROM message_recipients "
        "WHERE message_id=? AND worker_id='worker-2'", (message_id,),
    ).fetchone()
    assert all(row)
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_response_cycle_freezes_expected_workers(tmp_path):
    store = _store(tmp_path)
    cycle_id = store.start_cycle("find bugs", ["worker-1", "worker-2"])
    store.contribute(cycle_id, "worker-1", "found an artifact")
    cycle = store.cycle(cycle_id)
    assert cycle["expected_workers"] == ["worker-1", "worker-2"]
    assert [c["worker_id"] for c in cycle["contributions"]] == ["worker-1"]
    store.complete_cycle(cycle_id, "combined")
    assert store.cycle(cycle_id)["state"] == "completed"


def test_candidate_reservation_deduplicates_sources_and_verdict_is_immutable(tmp_path):
    store = _store(tmp_path)
    first, acquired = store.reserve_candidate("hash", {"title": "x"}, "worker-1", ["ev1"])
    store.start_validation(first, "validator-1")
    second, acquired_again = store.reserve_candidate(
        "hash", {"title": "x"}, "worker-2", ["ev2"]
    )
    assert first == second
    assert acquired is True and acquired_again is False

    assert store.abandon_validation(first, "validator-1", reason="cancelled")
    third, retry_acquired = store.reserve_candidate(
        "hash", {"title": "x"}, "worker-2", ["ev2"]
    )
    assert third == first and retry_acquired is True
    store.start_validation(first, "validator-2")
    assert store.commit_candidate_verdict(first, "confirmed", "validator-2")
    assert not store.commit_candidate_verdict(first, "rejected", "validator-late")
    assert store.candidates()[0]["verdict"] == "confirmed"


def test_reopening_store_recovers_interrupted_validator_reservation(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = _store(tmp_path)
    candidate_id, acquired = store.reserve_candidate(
        "recoverable", {"title": "x"}, "worker-1"
    )
    assert acquired
    store.start_validation(candidate_id, "validator-crashed")
    store.close()

    recovered = SwarmStore(path)
    recovered.register_worker("worker-1", "actor-1", "zero-1")
    retry_id, retry_acquired = recovered.reserve_candidate(
        "recoverable", {"title": "x"}, "worker-1"
    )

    assert retry_id == candidate_id
    assert retry_acquired is True
    attempt = recovered._db.execute(
        "SELECT state FROM validation_attempts WHERE validator_id='validator-crashed'"
    ).fetchone()
    assert attempt["state"] == "interrupted"


def test_retiring_stale_workers_preserves_history_and_releases_their_work(tmp_path):
    store = _store(tmp_path)
    work_id, _ = store.create_work(
        "worker-3", kind="recon", title="old lane", target="/old",
        hypothesis="the retired peer owns this",
    )
    claim = store.claim_work("worker-3", work_id)
    assert claim is not None

    store.retire_workers_except(["worker-1", "worker-2"])
    message_id = store.send_message("worker-1", body="active peers only")

    assert {worker["worker_id"] for worker in store.workers()} == {"worker-1", "worker-2"}
    retired = {
        worker["worker_id"]: worker for worker in store.workers(include_retired=True)
    }
    assert retired["worker-3"]["state"] == "retired"
    work = next(item for item in store.list_work() if item["work_id"] == work_id)
    assert work["state"] == "ready" and work["owner_worker_id"] is None
    recipients = {
        row[0] for row in store._db.execute(
            "SELECT worker_id FROM message_recipients WHERE message_id=?", (message_id,)
        )
    }
    assert recipients == {"worker-2"}
