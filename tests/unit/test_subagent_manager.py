"""SubagentManager (M8): semaphore concurrency cap, await semantics, tracking."""
import asyncio

from zero.orchestrator.subagents import (
    SUMMARY_MAX_CHARS,
    SubagentManager,
    SubagentResult,
)
from zero.ui.events import SubagentFinished, SubagentStarted


def _result(rec, status="NO_FINDING", **kw):
    return SubagentResult(
        subagent_id=rec.id, kind=rec.kind, target=rec.target, idea=rec.idea,
        status=status, summary=kw.get("summary", "done"),
        candidate_findings=kw.get("candidate_findings", []),
        discovered=kw.get("discovered", []),
        finding_ids=kw.get("finding_ids", []),
    )


async def test_semaphore_caps_concurrency():
    gate = asyncio.Event()
    active = 0
    max_active = 0

    async def runner(rec):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await gate.wait()
        active -= 1
        return _result(rec)

    events: list = []

    async def emit(e):
        events.append(e)

    mgr = SubagentManager(max_concurrent=2, runner=runner, emit=emit)
    for i in range(5):
        mgr.spawn("pentester", target=f"/t{i}", idea=f"idea{i}")

    await asyncio.sleep(0.05)  # let admitted runners reach the gate
    assert max_active <= 2, "semaphore did not cap concurrent execution"
    assert active <= 2

    gate.set()
    results = await mgr.await_("all", timeout=2)
    assert len(results) == 5
    assert max_active == 2, "the cap should actually be reached with 5 spawned / cap 2"
    assert len(mgr.attempts) == 5
    assert all(a.status == "NO_FINDING" for a in mgr.attempts)
    # one SubagentStarted + one SubagentFinished per subagent
    assert sum(isinstance(e, SubagentStarted) for e in events) == 5
    assert sum(isinstance(e, SubagentFinished) for e in events) == 5


async def test_await_any_returns_subset_then_all():
    gates: dict[str, asyncio.Event] = {}

    async def runner(rec):
        await gates[rec.id].wait()
        return _result(rec, status="EXPLOITED")

    async def emit(e):
        pass

    mgr = SubagentManager(max_concurrent=10, runner=runner, emit=emit)
    ids = [mgr.spawn("pentester", target=f"/t{i}", idea="i") for i in range(3)]
    for i in ids:
        gates[i] = asyncio.Event()

    await asyncio.sleep(0)  # let runners start and block on their gate
    gates[ids[0]].set()
    got = await mgr.await_("any", timeout=2)
    assert len(got) == 1 and got[0].subagent_id == ids[0]

    p, v = mgr.running_counts()
    assert (p, v) == (2, 0)

    gates[ids[1]].set()
    gates[ids[2]].set()
    rest = await mgr.await_("all", timeout=2)
    assert {r.subagent_id for r in rest} == {ids[1], ids[2]}
    assert mgr.running_counts() == (0, 0)


async def test_runner_exception_becomes_error_result():
    async def runner(rec):
        raise RuntimeError("boom")

    async def emit(e):
        pass

    mgr = SubagentManager(max_concurrent=4, runner=runner, emit=emit)
    mgr.spawn("pentester", target="/x", idea="i")
    results = await mgr.await_("all", timeout=2)
    assert len(results) == 1
    assert results[0].status == "ERROR"
    assert "boom" in results[0].summary
    assert mgr.attempts[0].status == "ERROR"


async def test_await_all_empty_returns_immediately():
    async def runner(rec):  # pragma: no cover - never spawned
        return _result(rec)

    async def emit(e):
        pass

    mgr = SubagentManager(max_concurrent=4, runner=runner, emit=emit)
    assert await mgr.await_("all", timeout=1) == []


async def test_snapshot_and_format_shapes():
    gate = asyncio.Event()

    async def runner(rec):
        rec.loop = type("L", (), {"turn": 3})()
        await gate.wait()
        return _result(rec, status="EXPLOITED", discovered=["cookie=abc"])

    async def emit(e):
        pass

    mgr = SubagentManager(max_concurrent=4, runner=runner, emit=emit)
    mgr.spawn("pentester", target="/api/orders", idea="SQLi")
    await asyncio.sleep(0.02)
    snap = mgr.snapshot()
    assert len(snap) == 1 and snap[0]["target"] == "/api/orders"
    assert "running subagents (1)" in mgr.format_snapshot()
    gate.set()
    results = await mgr.await_("all", timeout=2)
    text = mgr.format_results(results)
    assert "EXPLOITED" in text and "cookie=abc" in text


async def test_format_results_truncates_summary_at_limit():
    """A long NO_FINDING summary (what-I-tested / why-it-failed) reaches the
    orchestrator up to SUMMARY_MAX_CHARS, then is clipped — so the brief's quoted
    budget and the actual truncation stay in sync."""
    async def runner(rec):
        return _result(rec, status="NO_FINDING", summary="x" * (SUMMARY_MAX_CHARS + 500))

    async def emit(e):
        pass

    mgr = SubagentManager(max_concurrent=2, runner=runner, emit=emit)
    mgr.spawn("pentester", target="/t", idea="i")
    results = await mgr.await_("all", timeout=2)
    text = mgr.format_results(results)
    # The new budget is well above the old 600-char cap...
    assert SUMMARY_MAX_CHARS > 600
    assert text.count("x") == SUMMARY_MAX_CHARS  # clipped at the limit, not before
